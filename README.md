# FixItFlow — Flask app, deployed on Render

Same app as the Claude-artifact version, rebuilt as a normal Flask app
with a real backend. See below for what changed and why.

## Deploying on Render (recommended path)

1. **Put the code on GitHub first.** Make a free account at github.com,
   create a new repository, and use "Add file → Upload files" to drag
   in every file in this project (keep the `templates/` and `static/`
   folders intact).
2. **Sign up at render.com** (free) and connect your GitHub account.
3. Click **New → Web Service**, and pick the repo you just created.
4. Set these two fields:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
5. Under **Environment**, add two variables:
   - `ANTHROPIC_API_KEY` — from console.anthropic.com
   - `FLASK_SECRET_KEY` — any random string you make up
6. Click **Create Web Service**. Render builds and deploys automatically,
   and gives you a real HTTPS link like `fixitflow.onrender.com`.

Two things worth knowing about Render's free tier:
- The service **sleeps after inactivity** and takes 30-60 seconds to
  wake up on the next visit — open the link yourself a couple minutes
  before recording your demo video or before judges are likely to check it.
- The free tier's disk isn't guaranteed to persist forever across
  redeploys, so the SQLite database (impact counter, donations, etc.)
  could occasionally reset. Fine for a demo; if this matters for your
  final submission, Render's paid tier adds a persistent disk.

## Getting it on your iPhone home screen

Once deployed, open the Render URL in **Safari** on your iPhone (must
be Safari — Chrome on iOS can't install PWAs). Tap the Share button →
**Add to Home Screen**. Launch it from that icon, not from Safari, for
the full native-feeling, full-screen experience in your demo video.

## What changed from the Claude-artifact version

1. `window.storage` → real SQLite, via `/api/storage` (see `db.py`)
2. Direct browser calls to Claude → `/api/chat`, which holds your real
   Anthropic API key **server-side** so it's never exposed to visitors
3. Direct browser calls to iFixit / OpenStreetMap → proxied through
   `/api/ifixit-search` and `/api/geocode`, which also avoids any
   browser CORS issues those direct calls might have hit

## Setting up locally to test before deploying
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here
export FLASK_SECRET_KEY=any_random_string
python app.py
```
Open http://127.0.0.1:5000

## What's real vs. what needs your own setup
- The iFixit search and OpenStreetMap geocoding endpoints are free,
  public APIs — they'll work as soon as this is deployed somewhere
  with normal internet access (I could only verify the request/response
  *shape* while building this, since the sandbox I built it in blocks
  those specific domains — test both live once it's running).
- The Claude chat needs your own API key set as an environment variable,
  as above. Without it, the bot will return a clear error instead of crashing.
- The event dates/locations and coach roster are demo data — update
  `EVENTS` in `static/app.js` and the coach signup flow as needed
  closer to your actual event dates.

## Database
`fixitflow.db` (SQLite) is created automatically on first run, in the
same folder as `app.py`. Persists across normal usage; see the Render
note above about redeploys. Delete the file any time to reset all data.
