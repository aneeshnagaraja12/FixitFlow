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
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

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
# Live "repair events near me" search. Works like a Google search for
# "repair events near <zip>": Claude runs real web searches (the Claude
# API's built-in web search tool), reads the results, and returns the
# upcoming events it found as structured data. Then this code
# double-checks everything before the app shows it:
#   - the event must mention repair/fixing (Repair Cafe, Fixit Clinic,
#     bike repair night, mending circle...)
#   - the date must be today or later
#   - the source link must be a page that actually came back from the
#     web search, so an event can't be made up out of thin air
#
# Cost: web search is $10 per 1,000 searches plus normal tokens -- about
# 10-15 cents per lookup here. Results are cached for 12 hours per
# location, and there's a daily cap, so the API credit can't be drained.
# ---------------------------------------------------------------------
EVENT_SEARCH_DAILY_LIMIT = 40
EVENT_CACHE_HOURS = 12
REPAIR_WORDS = re.compile(r"repair|fix|mend|tinker|restart|darn", re.I)
PACIFIC = ZoneInfo("America/Los_Angeles")

EVENT_SEARCH_SYSTEM = """You find real, upcoming, in-person community repair events for the FixItFlow app.

Use web search to look for events near the location you're given: Repair Cafes, Fixit Clinics, repair fairs, fix-it workshops, community bike repair nights, sewing/mending circles, electronics repair meetups. Check library, city, makerspace, and organizer event calendars. Do a few different searches (e.g. "repair cafe near <place>", "fixit clinic <city>", "<city> library repair event").

Rules:
- Only include events you actually saw on a page in your search results, with the date written on that page. Never guess or invent an event, date, time, or address.
- If a page gives a regular schedule (e.g. "every 2nd Saturday, 10am-1pm"), you may list the next upcoming date and put the schedule in "recurring".
- Only events dated from today up to about 3 months out, within roughly 25 miles (40 km) of the location. The location can be anywhere in the world: a US zip code, a postal code, or a city in any country. Search in the local language too if that helps (e.g. "Repair Café" is also used in the Netherlands, Germany, France, Japan...).
- Write times as they're given locally, in 12-hour form like "2:00 PM".
- source_url must be the exact URL of the page where you found the event.

Reply with ONLY a JSON array (no other text, no markdown), each item:
{"name": "...", "date": "YYYY-MM-DD", "start_time": "11:00 AM" or "", "end_time": "3:00 PM" or "", "venue": "...", "address": "street, city", "description": "one short sentence", "recurring": "" or "schedule text", "source_url": "https://..."}
If you find nothing, reply with []"""


def _host(url):
    try:
        h = urlparse(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except ValueError:
        return ""


def _extract_json_array(text):
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, list) else []
    except ValueError:
        return []


def _clean_events(raw_events, seen_hosts, today):
    latest = today + timedelta(days=120)
    cleaned, seen = [], set()
    for e in raw_events:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()[:140]
        desc = str(e.get("description", "")).strip()[:240]
        venue = str(e.get("venue", "")).strip()[:140]
        url = str(e.get("source_url", "")).strip()
        if not name or not url.startswith(("http://", "https://")):
            continue
        # must be about repair
        if not REPAIR_WORDS.search(" ".join([name, desc, venue])):
            continue
        # must come from a page the search really returned
        host = _host(url)
        if not host or not any(host == h or host.endswith("." + h) or h.endswith("." + host) for h in seen_hosts):
            continue
        try:
            day = datetime.strptime(str(e.get("date", "")), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < today or day > latest:
            continue
        key = (name.lower(), day.isoformat())
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({
            "name": name,
            "date": day.isoformat(),
            "start_time": str(e.get("start_time", "")).strip()[:12],
            "end_time": str(e.get("end_time", "")).strip()[:12],
            "venue": venue,
            "address": str(e.get("address", "")).strip()[:180],
            "description": desc,
            "recurring": str(e.get("recurring", "")).strip()[:100],
            "source_url": url,
        })
    cleaned.sort(key=lambda ev: ev["date"])
    return cleaned[:15]


@app.route("/api/find-events", methods=["GET"])
def find_events():
    location = re.sub(r"\s+", " ", request.args.get("location", "")).strip()[:80]
    if len(location) < 3:
        return jsonify({"error": "Enter a zip code or city."}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY is not set on the server"}), 500

    now = datetime.now(PACIFIC)
    today = now.date()
    cache_key = "events:" + location.lower()
    cached = kv_get("cache", cache_key)
    if cached:
        try:
            c = json.loads(cached)
            if time.time() - c["at"] < EVENT_CACHE_HOURS * 3600:
                events = [ev for ev in c["events"] if ev["date"] >= today.isoformat()]
                return jsonify({"events": events, "cached": True})
        except (ValueError, KeyError, TypeError):
            pass

    count_key = "event-searches:" + today.isoformat()
    used = int(kv_get("cache", count_key) or 0)
    if used >= EVENT_SEARCH_DAILY_LIMIT:
        return jsonify({"error": "Daily web-search limit reached. Try again tomorrow, or use the events list below."}), 429
    kv_set("cache", count_key, str(used + 1))

    messages = [{
        "role": "user",
        "content": f"Today is {now.strftime('%A, %B %d, %Y')}. Find upcoming community repair events near: {location}",
    }]
    seen_hosts, final_text = set(), ""
    try:
        for _ in range(3):  # a long search can pause; continue it up to 3 times
            resp = requests.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 4000,
                    "system": EVENT_SEARCH_SYSTEM,
                    "messages": messages,
                    "tools": [{
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": 4,
                        # No user_location on purpose: the place the person
                        # typed (any zip/postal code or city, any country)
                        # is in the message, so results aren't pulled
                        # toward the US.
                    }],
                },
                timeout=100,
            )
            data = resp.json()
            if not resp.ok:
                msg = (data.get("error") or {}).get("message", "Search failed")
                return jsonify({"error": msg}), 502
            content = data.get("content", [])
            for block in content:
                if block.get("type") == "web_search_tool_result" and isinstance(block.get("content"), list):
                    for r in block["content"]:
                        if r.get("url"):
                            seen_hosts.add(_host(r["url"]))
                if block.get("type") == "text":
                    for cit in block.get("citations") or []:
                        if cit.get("url"):
                            seen_hosts.add(_host(cit["url"]))
            final_text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            if data.get("stop_reason") != "pause_turn":
                break
            messages.append({"role": "assistant", "content": content})
    except (requests.RequestException, ValueError) as e:
        return jsonify({"error": "Search failed: " + str(e)}), 502

    seen_hosts.discard("")
    events = _clean_events(_extract_json_array(final_text), seen_hosts, today)
    kv_set("cache", cache_key, json.dumps({"at": time.time(), "events": events}))
    return jsonify({"events": events, "cached": False})


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
