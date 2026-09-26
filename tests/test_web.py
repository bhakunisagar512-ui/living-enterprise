import time

import pytest
from conftest import ScriptedHuman  # noqa: F401
from fastapi.testclient import TestClient

from living_enterprise import web


@pytest.fixture
def client(crew):
    web.STATE = None
    yield TestClient(web.app)
    state = web.STATE                     # never leave a run thread behind
    if state is not None and state.running:
        state.answer = {"choice": "disapprove", "text": "test ended"}
        state.answered.set()
        _wait_for(lambda: not state.running)


def test_security_headers(client):
    r = client.get("/api/requests")
    assert r.status_code == 200
    for h in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options"):
        assert h in r.headers
    assert r.headers["Cache-Control"] == "no-store"


def test_non_local_host_is_refused(client):
    assert client.get("/api/requests", headers={"host": "evil.example"}).status_code == 403


def test_cross_site_post_is_refused(client):
    r = client.post("/api/runs", json={"request": "renewal"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize("body", [
    {"request": "nope"}, {"request": "renewal", "chaos": "boom"},
    {"request": "renewal", "budget": -1}, {"request": "renewal", "budget": 100000},
    {"request": "renewal", "time_limit": 0},
])
def test_bad_run_inputs_are_rejected(client, body):
    assert client.post("/api/runs", json=body).status_code in (400, 422)


@pytest.mark.parametrize("name", ["../main.py", "trace_x.json", "..%2F..%2Fapp.py", "trace_a_1_2.json"])
def test_trace_names_are_validated(client, name):
    assert client.get(f"/api/traces/{name}").status_code in (400, 404)


def _wait_for(pred, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_full_run_through_the_api(client):
    run_id = client.post("/api/runs", json={"request": "renewal"}).json()["run_id"]
    assert client.post("/api/runs", json={"request": "renewal"}).status_code == 409      # one at a time
    assert _wait_for(lambda: web.STATE.pending is not None)
    pending = web.STATE.pending
    assert pending["kind"] == "final_review"
    bad = client.post(f"/api/runs/{run_id}/decision", json={"decision_id": pending["id"], "choice": "hack"})
    assert bad.status_code == 400
    ok = client.post(f"/api/runs/{run_id}/decision", json={"decision_id": pending["id"], "choice": "approve"})
    assert ok.status_code == 200
    assert _wait_for(lambda: not web.STATE.running)
    done = [e for e in web.STATE.events if e["type"] == "done"][0]
    assert done["outcome"].startswith("approved")
    traces = client.get("/api/traces").json()
    assert traces and client.get(f"/api/traces/{traces[0]['name']}").status_code == 200


def test_decision_text_length_is_limited(client):
    r = client.post("/api/runs/1/decision", json={"decision_id": 1, "choice": "sendback", "text": "x" * 5000})
    assert r.status_code == 422
