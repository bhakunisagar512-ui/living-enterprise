"""Shared test fixtures. No test calls a real AI or a real network API, and none needs an API key."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key-000000")   # never used for a real call

from living_enterprise import config, fx  # noqa: E402

# ---------- canned agent outputs ----------
PLAN = ('{"summary": "Check contract, policy and SLA; write counter-offer", "reply_type": "vendor_email", '
        '"steps": [{"id": 1, "agent": "retriever", "task": "Gather facts", "why": "Facts first"}, '
        '{"id": 2, "agent": "executor", "task": "Write the reply", "why": "Final output"}], "escalate": null}')
FACTS = ("- Clause 8.2: increases capped at 7% (contract.txt)\n"
         "- SLA missed in May and July; TOTAL CREDIT OWED: Rs 20,000 (calculator)\n"
         "MISSING: none")
GOOD = ("MESSAGE:\nWe cannot accept 12%. Under Clause 8.2 we propose 7%, and claim Rs 20,000 in "
        "service credits for May and July.\nNimbus Retail Procurement\n"
        "INTERNAL NOTE:\nFinance Director approval needed.")
BAD = ("MESSAGE:\nWe propose 7% and claim Rs 40,000.\nINTERNAL NOTE:\nnone")
APPROVE = ('{"verdict": "APPROVED", "checks": [{"rule": "Clause 8.2", "result": "PASS", "note": "7%"}], '
           '"fixes": [], "needs_human": ["Finance Director approval"]}')
REJECT = ('{"verdict": "REJECTED", "checks": [{"rule": "SLA credit", "result": "FAIL", "note": "wrong total"}], '
          '"fixes": ["Credit is Rs 20,000"], "needs_human": []}')


class _Usage:
    prompt_tokens, completion_tokens, cached_prompt_tokens, cache_creation_tokens = 4000, 600, 0, 0
    total_tokens = 4600


class _Out:
    def __init__(self, raw):
        self.raw = raw
        self.token_usage = _Usage()


class FakeCrew:
    """Replaces CrewAI's kickoff. Each role answers from its own queue; the last answer repeats.
    An answer can be an Exception (raised) or a callable(prompt) -> str."""

    def __init__(self):
        self.script = {"Planner": [PLAN], "Retriever": [FACTS], "Executor": [GOOD], "Validator": [APPROVE]}
        self.prompts: dict[str, list[str]] = {}

    def answer(self, role: str, prompt: str):
        self.prompts.setdefault(role, []).append(prompt)
        queue = self.script[role]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return _Out(item(prompt) if callable(item) else item)


class ScriptedHuman:
    """Stands in for the person. Records what it was shown."""

    def __init__(self, final=("approve", ""), escalation=("abort", "")):
        self.final = list(final) if isinstance(final, list) else [final]
        self.escalation_answer = escalation
        self.escalations: list[dict] = []
        self.reviews: list[dict] = []

    def escalation(self, run, reason, stage, detail, options):
        self.escalations.append({"reason": reason, "stage": stage, "detail": detail, "options": options})
        return self.escalation_answer

    def final_review(self, run, review):
        self.reviews.append(review)
        return self.final.pop(0) if len(self.final) > 1 else self.final[0]


@pytest.fixture(scope="session", autouse=True)
def warm_up_agents():
    """Builds the CrewAI agents once before any test. On some PCs this first build takes
    30+ seconds, which would otherwise eat into the timing of whichever test runs first."""
    from living_enterprise.agents import get_agents
    get_agents()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Traces, outbox and the FX cache go to a temporary folder; waits are instant."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(config, "FX_CACHE", tmp_path / "cache" / "fx.json")
    monkeypatch.setattr(config, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(fx.time, "sleep", lambda s: None)
    return tmp_path


@pytest.fixture
def crew(monkeypatch, sandbox):
    """Fake agents: patches Crew.kickoff so every agent call is answered from a script."""
    from crewai import Crew

    fake = FakeCrew()

    def kickoff(self, inputs=None, **kwargs):
        return fake.answer(self.agents[0].role, self.tasks[0].description)

    monkeypatch.setattr(Crew, "kickoff", kickoff)
    return fake


@pytest.fixture
def no_network(monkeypatch):
    """Any real HTTP call fails the test."""
    def blocked(url):
        raise AssertionError(f"test tried to reach the network: {url}")
    monkeypatch.setattr(fx, "fetch_json", blocked)
