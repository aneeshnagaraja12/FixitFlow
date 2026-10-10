"""
app.py
------
FixItFlow backend.

Replaces three things the Claude-artifact version relied on that only
work inside Claude.ai:
  1. window.storage           -> /api/storage  (real SQLite, see db.py)
  2. direct fetch to Claude   -> /api/chat      (server holds the API key)
  3. direct fetch to iFixit / -> /api/ifixit-search, /api/geocode
     Nominatim from the browser  (proxied server-side, avoids any CORS
                                   issues and lets us set a proper
                                   User-Agent for Nominatim's usage policy)

The FixIt Bot chat calls the real Claude API, grounded with real live
iFixit guide search results and real Open Repair Alliance success-rate
stats so its answers stay factual rather than just improvised.

Run locally:      python app.py

Required environment variable:
  ANTHROPIC_API_KEY   -- from console.anthropic.com. Without it, the
                          chat endpoint returns a clear error instead
                          of crashing.
Optional environment variables:
  YOUTUBE_API_KEY     -- free key from Google Cloud (YouTube Data API v3).
                          With it, the Coach links to actual repair videos;
                          without it, it links to a YouTube search for
                          the same repair instead.
  FLASK_SECRET_KEY    -- any random string; used to sign session cookies
                          that identify "this browser" for personal
                          (non-shared) storage. A default is provided
                          for local dev, but set your own in production.
"""

import html
import os
import uuid
from urllib.parse import quote

import requests
from flask import Flask, render_template, request, jsonify, session

from db import init_db, kv_get, kv_set

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

init_db()


def get_user_scope():
    """Personal-storage scope key = a random id stuck in this browser's
    signed session cookie. Not real authentication -- just enough to
    keep one visitor's RSVPs/bookings separate from another's."""
    if "user_id" not in session:
        session["user_id"] = uuid.uuid4().hex
    return session["user_id"]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    # Served from root (not /static/sw.js) so its cache scope covers the
    # whole app, not just the static folder.
    return app.send_static_file("sw.js"), 200, {"Content-Type": "application/javascript"}


# ---------------------------------------------------------------------
# Storage API (mirrors window.storage.get/set: shared vs personal scope)
# ---------------------------------------------------------------------
@app.route("/api/storage", methods=["GET"])
def storage_get():
    key = request.args.get("key", "")
    shared = request.args.get("shared", "false").lower() == "true"
    scope = "shared" if shared else get_user_scope()
    value = kv_get(scope, key)
    if value is None:
        return jsonify({"value": None}), 404
    return jsonify({"key": key, "value": value, "shared": shared})


@app.route("/api/storage", methods=["POST"])
def storage_set():
    data = request.get_json(force=True)
    key = data.get("key", "")
    value = data.get("value", "")
    shared = bool(data.get("shared", False))
    if not key:
        return jsonify({"error": "key is required"}), 400
    scope = "shared" if shared else get_user_scope()
    kv_set(scope, key, value)
    return jsonify({"key": key, "value": value, "shared": shared})


# ---------------------------------------------------------------------
# Claude API proxy (keeps the API key server-side, off the browser)
# ---------------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def chat():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY is not set on the server"}), 500

    body = request.get_json(force=True)
    messages = body.get("messages", [])
    system = body.get("system", "")

    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "system": system,
                "messages": messages,
            },
            timeout=30,
        )
        return jsonify(resp.json()), resp.status_code
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------
# iFixit search proxy (public endpoint, but proxying avoids any CORS
# uncertainty and keeps all outbound calls in one place)
# ---------------------------------------------------------------------
IFIXIT_HEADERS = {"User-Agent": "FixItFlow/1.0 (student repair app)"}


def _ifixit_guide_search(query):
    """One search against iFixit's guide index. Returns [] on any error."""
    try:
        resp = requests.get(
            "https://www.ifixit.com/api/2.0/search/" + quote(query, safe=""),
            params={"filter": "guide", "limit": 3},
            headers=IFIXIT_HEADERS,
            timeout=8,
        )
        if not resp.ok:
            return []
        results = []
        for r in resp.json().get("results", []):
            url = r.get("url", "")
            if not url:
                continue
            if url.startswith("/"):
                url = "https://www.ifixit.com" + url
            results.append({
                "title": r.get("display_title") or r.get("title", ""),
                "url": url,
                # guideid lets the app fetch this guide's real step-by-step
                # photos via /api/ifixit-guide/<id> below.
                "guideid": r.get("guideid"),
            })
        return results
    except (requests.RequestException, ValueError, AttributeError):
        return []


@app.route("/api/ifixit-search", methods=["GET"])
def ifixit_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"results": []})
    # A very specific search ("hamilton beach toaster heating element
    # replacement") can find nothing, so if it does, drop words from the
    # end and try again ("hamilton beach toaster heating", then
    # "hamilton beach toaster"...). The item name comes first in the
    # query, so it's the last thing to be dropped.
    words = query.split()
    for n in range(len(words), 0, -1):
        results = _ifixit_guide_search(" ".join(words[:n]))
        if results:
            return jsonify({"results": results, "matched_query": " ".join(words[:n])})
        if len(words) - n >= 3:
            break  # don't hammer iFixit with too many retries
    return jsonify({"results": []})


# ---------------------------------------------------------------------
# YouTube video search -- finds actual repair VIDEOS (not just a search
# page) using the official YouTube Data API. Needs a free
# YOUTUBE_API_KEY from Google Cloud. Without a key this returns no
# results, and the app falls back to a YouTube search link for the
# same repair, so the person always gets a YouTube link either way.
#
# The free quota is 10,000 units a day and each search costs 100, so
# about 100 searches a day. Results are cached so asking about the same
# repair twice doesn't use up quota twice.
# ---------------------------------------------------------------------
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
_youtube_cache = {}


@app.route("/api/youtube-search", methods=["GET"])
def youtube_search():
    query = request.args.get("q", "").strip()
    if not query or not YOUTUBE_API_KEY:
        return jsonify({"results": [], "configured": bool(YOUTUBE_API_KEY)})
    cache_key = query.lower()
    if cache_key in _youtube_cache:
        return jsonify({"results": _youtube_cache[cache_key], "configured": True})
    try:
        resp = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "type": "video",
                "maxResults": 2,
                "q": query,
                "safeSearch": "strict",
                "relevanceLanguage": "en",
                "key": YOUTUBE_API_KEY,
            },
            timeout=8,
        )
        if not resp.ok:
            return jsonify({"results": [], "configured": True})
        results = []
        for item in resp.json().get("items", []):
            video_id = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not video_id:
                continue
            results.append({
                # YouTube returns titles HTML-escaped ("Don&#39;t"), so unescape
                "title": html.unescape(snippet.get("title", "")),
                "channel": html.unescape(snippet.get("channelTitle", "")),
                "url": f"https://www.youtube.com/watch?v={video_id}",
            })
        if len(_youtube_cache) > 500:
            _youtube_cache.clear()
        _youtube_cache[cache_key] = results
        return jsonify({"results": results, "configured": True})
    except (requests.RequestException, ValueError, AttributeError):
        return jsonify({"results": [], "configured": True})


# ---------------------------------------------------------------------
# iFixit guide steps -- real step-by-step repair photos and text from
# an actual iFixit guide, so the app shows genuine photos of the real
# device instead of AI-invented diagrams. iFixit content is licensed
# CC BY-NC-SA, which allows non-commercial use with attribution (the
# app credits iFixit and links back to the original guide).
# ---------------------------------------------------------------------
MAX_GUIDE_STEPS = 8


def _pick_step_image(media):
    """Pick a mid-size photo from an iFixit step's media block, trying
    sizes from most to least preferred. Returns None if the step has no
    photo (some steps are text-only)."""
    if not isinstance(media, dict) or media.get("type") != "image":
        return None
    images = media.get("data") or []
    if not images or not isinstance(images[0], dict):
        return None
    img = images[0]
    for size in ("standard", "medium", "440x330", "large", "original"):
        if img.get(size):
            return img[size]
    return None


@app.route("/api/ifixit-guide/<int:guide_id>", methods=["GET"])
def ifixit_guide(guide_id):
    try:
        resp = requests.get(f"https://www.ifixit.com/api/2.0/guides/{guide_id}", headers=IFIXIT_HEADERS, timeout=8)
        if not resp.ok:
            return jsonify({"error": "Guide not found"}), 404
        data = resp.json()
        # Collect every usable step (skipping any empty ones), then send
        # back only the first MAX_GUIDE_STEPS -- slicing first and
        # filtering after would come up short whenever an early step
        # happens to be empty.
        usable = []
        for step in data.get("steps") or []:
            if not isinstance(step, dict):
                continue
            lines = step.get("lines") or []
            text = " ".join(
                (line.get("text_raw") or "").strip() for line in lines if isinstance(line, dict)
            ).strip()
            image = _pick_step_image(step.get("media"))
            if text or image:
                usable.append({"text": text, "image": image})
        return jsonify({
            "title": data.get("title", ""),
            "url": data.get("url", ""),
            "steps": usable[:MAX_GUIDE_STEPS],
            "total_steps": len(usable),
        })
    except (requests.RequestException, ValueError):
        return jsonify({"error": "Couldn't load guide"}), 502


# ---------------------------------------------------------------------
# Geocoding proxy (OpenStreetMap Nominatim -- free, but their usage
# policy asks for a real User-Agent identifying the app, which we can
# set here but not from a browser fetch)
# ---------------------------------------------------------------------
@app.route("/api/geocode", methods=["GET"])
def geocode():
    address = request.args.get("address", "")
    if not address:
        return jsonify({"error": "address is required"}), 400
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "limit": 1, "q": address},
            headers={"User-Agent": "FixItFlow-CAC-Student-Project/1.0"},
            timeout=8,
        )
        results = resp.json()
        if not results:
            return jsonify({"error": "No matching address found"}), 404
        return jsonify({"lat": float(results[0]["lat"]), "lng": float(results[0]["lon"])})
    except (requests.RequestException, ValueError, KeyError):
        return jsonify({"error": "Geocoding failed"}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
