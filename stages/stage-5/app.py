"""
The Living Enterprise - web UI server.

    python app.py            then open http://127.0.0.1:8000

The agents, tools, budgets and recovery logic all live in main.py and are unchanged.
This file only:
  - starts a run in the background,
  - streams its live events to the browser (Server-Sent Events),
  - pauses the run when a human decision is needed and resumes it when you click.
"""
import asyncio
import itertools
import json
import re
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import main as core

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="The Living Enterprise")


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

    def push(self, event: dict):
        with self.lock:
            event["seq"] = len(self.events)
            self.events.append(event)


STATE: Optional[RunState] = None
RUN_IDS = itertools.count(1)
DECISION_IDS = itertools.count(1)


class WebHuman:
    """Same choices as the terminal, but asked through the web page."""

    def _ask(self, run, kind: str, payload: dict) -> dict:
        state = STATE
        state.answered.clear()
        state.answer = None
        state.pending = {"id": next(DECISION_IDS), "kind": kind, **payload}
        run.emit("decision", pending=state.pending)
        started = time.time()
        state.answered.wait()                    # the run pauses here until you click
        core.HUMAN_WAIT[0] += time.time() - started
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


def push_event(event: dict):
    if STATE is not None:
        STATE.push(event)


# ================================================================
# API
# ================================================================
class StartRun(BaseModel):
    request: str
    chaos: str = "off"
    budget: Optional[float] = None
    time_limit: Optional[float] = None
    agent_timeout: Optional[float] = None


class Decision(BaseModel):
    decision_id: int
    choice: str
    text: str = ""


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/requests")
def list_requests():
    return [{"key": k, "text": v} for k, v in core.REQUESTS.items()]


@app.post("/api/runs")
def start_run(body: StartRun):
    global STATE
    if STATE is not None and STATE.running:
        raise HTTPException(409, "A run is already in progress. Finish it first.")
    if body.request not in core.REQUESTS:
        raise HTTPException(400, f"Unknown request '{body.request}'")
    if body.chaos not in ("off", "partial", "all"):
        raise HTTPException(400, "chaos must be off, partial or all")
    for name in ("budget", "time_limit", "agent_timeout"):
        value = getattr(body, name)
        if value is not None and value <= 0:
            raise HTTPException(400, f"{name} must be a positive number")

    state = RunState(next(RUN_IDS))
    STATE = state

    def worker():
        try:
            core.run_request(body.request, body.chaos, body.budget, body.time_limit, body.agent_timeout)
        except Exception as err:                 # run_request already traps errors; this is a last resort
            state.push({"type": "done", "outcome": f"crashed: {err}", "stats": {}, "trace": None})
        finally:
            state.running = False

    threading.Thread(target=worker, daemon=True).start()
    return {"run_id": state.id}


@app.get("/api/runs/current")
def current_run():
    if STATE is None:
        return {"run_id": None}
    return {"run_id": STATE.id, "running": STATE.running, "events": len(STATE.events),
            "pending": STATE.pending}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: int, request: Request, since: int = 0):
    """Server-Sent Events. Replays everything from `since`, then streams new events live."""
    state = STATE
    if state is None or state.id != run_id:
        raise HTTPException(404, "No such run")
    last = request.headers.get("last-event-id")
    start = int(last) + 1 if last and last.isdigit() else since

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


TRACE_NAME = re.compile(r"^trace_[a-z]+_\d{8}_\d{6}\.json$")


@app.get("/api/traces")
def list_traces():
    runs = []
    for path in sorted(core.RUNS_DIR.glob("trace_*.json"), key=lambda p: p.stem[-15:], reverse=True)[:40]:
        if not TRACE_NAME.match(path.name):
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
    if not TRACE_NAME.match(name):
        raise HTTPException(400, "Bad trace name")
    path = core.RUNS_DIR / name
    if not path.exists():
        raise HTTPException(404, "Trace not found")
    return json.loads(path.read_text(encoding="utf-8"))


# ================================================================
# Start
# ================================================================
def serve(open_browser: bool = True, port: int = 8000):
    core.quiet_crewai()
    core.HUMAN = WebHuman()
    core.EMIT = push_event
    url = f"http://127.0.0.1:{port}"
    print(f"\n  The Living Enterprise is running at {url}\n  (press Ctrl+C here to stop)\n")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    serve()
