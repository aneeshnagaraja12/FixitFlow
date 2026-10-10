# Gunicorn reads this file automatically when it starts (Render runs
# "gunicorn app:app" from this folder). The live repair-event search
# can take 20-60 seconds while it searches the web, and gunicorn's
# default 30-second limit would kill it partway, so allow 120 seconds.
timeout = 120

