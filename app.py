"""
app.py
------
FixItFlow backend.

Replaces two things the Claude-artifact version relied on that only
work inside Claude.ai:
  1. window.storage           -> /api/storage  (real SQLite, see db.py)
  2. direct fetch to iFixit / -> /api/ifixit-search, /api/geocode
     Nominatim from the browser  (proxied server-side, avoids any CORS
                                   issues and lets us set a proper
                                   User-Agent for Nominatim's usage policy)

The FixIt Bot chat is rule-based (keyword matching against real iFixit
guide search results + real Open Repair Alliance stats), not a live
LLM call -- this avoids any API billing risk entirely, which mattered
more than a fancier chat for a CAC submission on a tight budget.

Run locally:      python app.py

Optional environment variable:
  FLASK_SECRET_KEY    -- any random string; used to sign session cookies
                          that identify "this browser" for personal
                          (non-shared) storage. A default is provided
                          for local dev, but set your own in production.
"""

import os
import uuid

import requests
from flask import Flask, render_template, request, jsonify, session

from db import init_db, kv_get, kv_set

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

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
# iFixit search proxy (public endpoint, but proxying avoids any CORS
# uncertainty and keeps all outbound calls in one place)
# ---------------------------------------------------------------------
@app.route("/api/ifixit-search", methods=["GET"])
def ifixit_search():
    query = request.args.get("q", "")
    if not query:
        return jsonify({"results": []})
    try:
        resp = requests.get(
            f"https://www.ifixit.com/api/2.0/search/{query}",
            params={"filter": "guide", "limit": 3},
            timeout=8,
        )
        if not resp.ok:
            return jsonify({"results": []})
        data = resp.json()
        results = [
            {"title": r.get("display_title") or r.get("title", ""), "url": r.get("url", "")}
            for r in data.get("results", [])
            if r.get("url")
        ]
        return jsonify({"results": results})
    except requests.RequestException:
        return jsonify({"results": []})


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
