"""
The Living Enterprise - web UI.

    python app.py      then open http://127.0.0.1:8000

Local use only. The server code lives in living_enterprise/web.py.
"""
from living_enterprise.web import app, serve  # noqa: F401  (app is importable for tests/uvicorn)

if __name__ == "__main__":
    serve()
