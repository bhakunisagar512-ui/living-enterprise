"""Web UI server (FastAPI). Local use only: it listens on 127.0.0.1 and is not meant to be exposed.

It starts a run in a background thread, streams the run's live events to the browser
(Server-Sent Events), and pauses the run whenever a human decision is needed.

Security:
  * Binds to 127.0.0.1 only, and rejects requests whose Host header is not local
    (blocks DNS-rebinding attacks from web pages you visit).
  * State-changing requests must come from this page (Origin check), which blocks
    cross-site requests from other tabs.
  * Every input is validated (known request names, bounded budgets, allowed choices,
    trace names by strict pattern, note length).
  * Strict browser headers (CSP, no framing, no sniffing, no referrer, no caching of API data).
  * All text is shown in the page with textContent, never as HTML.
"""
import asyncio
import itertools
import json
import re
import threading
import time
import webbrowser
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import config
from .agents import quiet_crewai
from .workflow import run_request

STATIC = config.BASE / "static"
HOST = "127.0.0.1"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "testserver"}      # 'testserver' = the test client

app = FastAPI(title="The Living Enterprise", docs_url=None, redoc_url=None, openapi_url=None)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"),
}


@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in LOCAL_HOSTS:
        return JSONResponse({"detail": "This server only accepts local requests"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and re.sub(r"^https?://", "", origin).rsplit(":", 1)[0].lower() not in LOCAL_HOSTS:
            return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ================================================================
# One run at a time: its events, and the decision it may be waiting for
# ================================================================
class RunState:
    def __init__(self, run_id: int):
        self.id = run_id
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.running = True
        self.pending: Optional[dict] = None     # the decision the run is waiting for
        self.answer: Optional[dict] = None
        self.answered = threading.Event()

    def push(self, event: dict) -> None:
        with self.lock:
            event["seq"] = len(self.events)
            self.events.append(event)


STATE: Optional[RunState] = None
START_LOCK = threading.Lock()
RUN_IDS = itertools.count(1)
DECISION_IDS = itertools.count(1)


class WebHuman:
    """Same information and choices as the terminal, asked through the web page."""

    def __init__(self, state: RunState):
        self.state = state

    def _ask(self, run, kind: str, payload: dict) -> dict:
        state = self.state
        state.answered.clear()
        state.answer = None
        state.pending = {"id": next(DECISION_IDS), "kind": kind, **payload}
        run.emit("decision", pending=state.pending)
        started = time.time()
        state.answered.wait()                    # the run pauses here until you click
        run.human_seconds += time.time() - started
        answer, state.pending = state.answer, None
        run.emit("decision_made", kind=kind, choice=answer["choice"])
        return answer

    def escalation(self, run, reason, stage, detail, options):
        ans = self._ask(run, "escalation", {
            "reason": reason, "stage": stage, "detail": detail,
            "options": [{"key": k, "label": v} for k, v in options.items()],
            "warnings": list(run.warnings)})
        return ans["choice"], ans.get("text", "")

    def final_review(self, run, review: dict):
        ans = self._ask(run, "final_review", {"review": review})
        return ans["choice"], ans.get("text", "")


# ================================================================
# API
# ================================================================
class StartRun(BaseModel):
    request: str = Field(max_length=32)
    chaos: Literal["off", "partial", "all"] = "off"
    budget: Optional[float] = Field(default=None, gt=0, le=500)
    time_limit: Optional[float] = Field(default=None, gt=0, le=3600)
    agent_timeout: Optional[float] = Field(default=None, gt=0, le=600)


class Decision(BaseModel):
    decision_id: int
    choice: str = Field(max_length=32)
    text: str = Field(default="", max_length=config.MAX_HUMAN_NOTE_CHARS)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/requests")
def list_requests():
    return [{"key": k, "text": v} for k, v in config.REQUESTS.items()]


@app.post("/api/runs")
def start_run(body: StartRun):
    global STATE
    if body.request not in config.REQUESTS:
        raise HTTPException(400, "Unknown request")
    with START_LOCK:                             # two quick clicks cannot start two runs
        if STATE is not None and STATE.running:
            raise HTTPException(409, "A run is already in progress. Finish it first.")
        state = RunState(next(RUN_IDS))
        STATE = state

    def worker():
        try:
            run_request(body.request, body.chaos, body.budget, body.time_limit, body.agent_timeout,
                        human=WebHuman(state), on_event=state.push)
        except Exception as err:                 # run_request already traps errors; last resort
            state.push({"type": "done", "outcome": f"crashed: {type(err).__name__}", "stats": {}, "trace": None})
        finally:
            state.running = False

    threading.Thread(target=worker, daemon=True).start()
    return {"run_id": state.id}


@app.get("/api/runs/current")
def current_run():
    if STATE is None:
        return {"run_id": None}
    return {"run_id": STATE.id, "running": STATE.running, "events": len(STATE.events), "pending": STATE.pending}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: int, request: Request, since: int = 0):
    """Server-Sent Events. Replays everything from `since`, then streams new events live."""
    state = STATE
    if state is None or state.id != run_id:
        raise HTTPException(404, "No such run")
    last = request.headers.get("last-event-id")
    start = int(last) + 1 if last and last.isdigit() else max(0, since)

    async def generator():
        i = start
        while True:
            if await request.is_disconnected():
                break
            with state.lock:
                new = state.events[i:]
            for event in new:
                yield f"id: {event['seq']}\ndata: {json.dumps(event, default=str)}\n\n"
                i = event["seq"] + 1
                if event["type"] == "done":
                    return
            if not new:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs/{run_id}/decision")
def decide(run_id: int, body: Decision):
    state = STATE
    if state is None or state.id != run_id or state.pending is None:
        raise HTTPException(409, "This run is not waiting for a decision")
    pending = state.pending
    if pending["id"] != body.decision_id:
        raise HTTPException(409, "That decision has already been answered")
    allowed = ({o["key"] for o in pending["options"]} if pending["kind"] == "escalation"
               else {"approve", "sendback", "disapprove"})
    if body.choice not in allowed:
        raise HTTPException(400, f"Choice must be one of {sorted(allowed)}")
    if body.choice == "sendback" and not body.text.strip():
        raise HTTPException(400, "Tell the agents what to change")
    state.answer = {"choice": body.choice, "text": body.text.strip()}
    state.answered.set()
    return {"ok": True}


TRACE_NAME = re.compile(r"^trace_[a-z]{1,20}_\d{8}_\d{6}\.json$")


def _trace_path(name: str) -> Optional[Path]:
    """Only plain trace names inside the runs folder; anything else is refused."""
    if not TRACE_NAME.fullmatch(name):
        return None
    runs_dir = config.RUNS_DIR.resolve()
    path = (runs_dir / name).resolve()
    return path if path.parent == runs_dir else None


@app.get("/api/traces")
def list_traces():
    runs = []
    if not config.RUNS_DIR.exists():
        return runs
    files = sorted(config.RUNS_DIR.glob("trace_*.json"), key=lambda p: p.stem[-15:], reverse=True)
    for path in files[:40]:
        if _trace_path(path.name) is None:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        runs.append({"name": path.name, "label": path.name.split("_")[1],
                     "when": path.stem[-15:], "outcome": data.get("outcome", ""),
                     "cost_rs": data.get("cost_rs"), "chaos": data.get("chaos_mode", "off")})
    return runs


@app.get("/api/traces/{name}")
def get_trace(name: str):
    path = _trace_path(name)
    if path is None:
        raise HTTPException(400, "Bad trace name")
    if not path.exists():
        raise HTTPException(404, "Trace not found")
    return json.loads(path.read_text(encoding="utf-8"))


# ================================================================
# Start
# ================================================================
def serve(open_browser: bool = True, port: int = 8000) -> None:
    import uvicorn
    quiet_crewai()
    url = f"http://{HOST}:{port}"
    print(f"\n  The Living Enterprise is running at {url}\n  (press Ctrl+C here to stop)\n")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=port, log_level="warning")
