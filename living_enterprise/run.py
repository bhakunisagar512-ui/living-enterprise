"""One run: shared state, the audit trace, the flag system, budgets and the only place an AI is called."""
import contextvars
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from pydantic import ValidationError

from . import config
from .costs import call_cost_usd, classify_ai_error, cost_fx, llm_usage, usage_delta
from .human import ConsoleHuman, HumanChannel
from .security import redact, redact_obj


class StopRun(Exception):
    """Ends a run cleanly (human abort or a stopping condition)."""


class AgentTimeout(Exception):
    """An AI call took longer than the per-call timeout."""


def kickoff_with_timeout(crew, timeout: float):
    """Runs one agent call in a background thread and stops waiting after `timeout` seconds.
    (CrewAI's own max_execution_time still waits for a stuck call to finish.)"""
    result: dict = {}
    ctx = contextvars.copy_context()

    def target():
        try:
            result["out"] = ctx.run(crew.kickoff)
        except BaseException as err:          # hand any error back to the calling thread
            result["err"] = err

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AgentTimeout()
    if "err" in result:
        raise result["err"]
    return result["out"]


def _fmt(value: float, unit: str) -> str:
    return f"Rs {value:.2f}" if unit == "Rs" else f"{int(value)}s"


class Run:
    """Everything about one request while it is being handled."""

    def __init__(self, request: str, label: str, *, chaos: str = "off",
                 budget_rs: Optional[float] = None, time_limit: Optional[float] = None,
                 agent_timeout: Optional[float] = None, human: Optional[HumanChannel] = None,
                 on_event: Optional[Callable[[dict], None]] = None):
        self.request = request
        self.label = label
        self.chaos = chaos                                   # off | partial | all (FX failure drill)
        self.budget_rs = float(budget_rs or config.BUDGET_RS)
        self.time_limit = int(time_limit or config.TIME_LIMIT_S)
        self.agent_timeout = float(agent_timeout or config.AGENT_TIMEOUT_S)
        self.human = human or ConsoleHuman()
        self.on_event = on_event                             # live events for the web page
        self.started = time.time()
        self.human_seconds = 0.0                             # never counted against the time budget
        self.calls = 0
        self.tokens = 0
        self.cost_usd = 0.0                                  # measured from real token usage, per call
        self.api_calls = 0                                   # external API attempts
        self.results: dict[int, str] = {}                    # shared memory: step id -> output
        self.warnings: list[str] = []
        self.human_notes: list[str] = []                     # human instructions, shared with every agent
        self.security_flags: list[str] = []                  # suspected prompt-injection findings
        self.drafts = 0
        self.fx_unavailable = False
        self.fx_rates: dict[str, dict] = {}                  # rates fetched in this run (reused)
        self.warned: set[str] = set()
        self.trace: list[dict] = []
        self.outcome = "in progress"

    # ---------- live events ----------
    def stats(self) -> dict:
        return {"cost_rs": round(self.cost_rs(), 2), "cost_usd": round(self.cost_usd, 4),
                "budget_rs": self.budget_rs, "seconds": self.agent_seconds(),
                "time_limit": self.time_limit, "calls": self.calls, "max_calls": config.MAX_AGENT_CALLS,
                "api_calls": self.api_calls, "tokens": self.tokens, "warnings": len(self.warnings)}

    def emit(self, event_type: str, **data) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(redact_obj({"type": event_type, "time": datetime.now().strftime("%H:%M:%S"),
                                      "stats": self.stats(), **data}))
        except Exception:
            pass    # the page must never be able to break a run

    # ---------- trace ----------
    def log(self, actor: str, action: str, detail: str = "", why: str = "", **extra) -> None:
        entry = redact_obj({"time": datetime.now().strftime("%H:%M:%S"), "actor": actor,
                            "action": action, "why": why, "detail": str(detail)[:1500], **extra})
        self.trace.append(entry)
        self.emit("log", entry=entry)

    # ---------- flag level 1: carry on, but show it ----------
    def warn(self, message: str) -> None:
        message = redact(message)
        if message in self.warnings:
            return
        self.warnings.append(message)
        self.log("system", "WARNING", message)
        print(f"  [!] Warning: {message}")

    def flag_security(self, source: str, finding: str) -> None:
        """A suspected prompt-injection attempt: warned, and passed to the Validator."""
        item = f"{source}: {finding}"
        if item not in self.security_flags:
            self.security_flags.append(item)
            self.warn(f"Possible prompt injection in {item}")

    # ---------- flag level 2: stop, a human decides ----------
    def escalate(self, reason: str, stage: str, detail: str, options: dict[str, str]) -> tuple[str, str]:
        detail = redact(detail)
        self.log("system", "ESCALATED", detail, why=reason, stage=stage)
        choice, note = self.human.escalation(self, reason, stage, detail, options)
        if choice not in options:
            choice = "abort" if "abort" in options else list(options)[-1]
        note = (note or "")[:config.MAX_HUMAN_NOTE_CHARS]
        self.log("human", f"DECISION: {choice}", note, why=reason, stage=stage)
        if choice == "abort":
            self.outcome = f"aborted by human at {stage}"
            raise StopRun(self.outcome)
        return choice, note

    # ---------- budgets ----------
    def cost_rs(self) -> float:
        return self.cost_usd * cost_fx()

    def agent_seconds(self) -> int:
        return round(time.time() - self.started - self.human_seconds)

    def check_budgets(self) -> None:
        """Runs before every AI call. 80% -> warning. 100% -> the human decides."""
        spent, secs = self.cost_rs(), self.agent_seconds()
        for key, used, limit, unit in (("cost", spent, self.budget_rs, "Rs"),
                                       ("time", secs, self.time_limit, "s")):
            if used >= config.WARN_AT * limit and key not in self.warned and used < limit:
                self.warned.add(key)
                self.warn(f"{int(config.WARN_AT * 100)}% of the {key} budget used "
                          f"({_fmt(used, unit)} of {_fmt(limit, unit)})")
        if spent >= self.budget_rs:
            extra = max(10.0, round(self.budget_rs / 2))
            self.escalate("Cost budget reached", f"budget check before agent call {self.calls + 1}",
                          f"Spent Rs {spent:.2f} of the Rs {self.budget_rs:.2f} budget "
                          f"({self.tokens} tokens, {self.calls} agent calls).",
                          {"raise": f"Raise the budget by Rs {extra:.0f} and continue",
                           "abort": "Stop the run here"})
            self.budget_rs += extra
            self.warned.discard("cost")
            self.warn(f"Human raised the cost budget to Rs {self.budget_rs:.0f}")
        if secs >= self.time_limit:
            self.escalate("Time budget reached", f"budget check before agent call {self.calls + 1}",
                          f"{secs}s of system time used; the limit is {self.time_limit}s.",
                          {"raise": "Allow 2 more minutes and continue", "abort": "Stop the run here"})
            self.time_limit = secs + 120
            self.warned.discard("time")
            self.warn(f"Human extended the time budget to {self.time_limit}s")

    # ---------- the only place an AI agent is called ----------
    def call_agent(self, name: str, instruction: str, expected: str, why: str = "") -> str:
        from crewai import Crew, Process, Task

        from .agents import get_agents

        if self.calls >= config.MAX_AGENT_CALLS:
            self.outcome = f"stopped: reached the limit of {config.MAX_AGENT_CALLS} agent calls"
            self.log("system", "STOP", self.outcome)
            raise StopRun(self.outcome)
        self.check_budgets()
        agent = get_agents()[name]
        for attempt in (1, 2):
            self.calls += 1
            self.emit("agent", role=agent.role, call=self.calls, attempt=attempt)
            print(f"  -> {agent.role} working... (call {self.calls}/{config.MAX_AGENT_CALLS} | "
                  f"Rs {self.cost_rs():.2f} of Rs {self.budget_rs:.0f} | "
                  f"{self.agent_seconds()}s of {self.time_limit}s)")
            task = Task(description=instruction, expected_output=expected, agent=agent)
            usage_before = llm_usage(agent.llm)
            crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
            started = time.time()
            try:
                out = kickoff_with_timeout(crew, self.agent_timeout)
                break
            except AgentTimeout:
                self.log(agent.role, "TIMED OUT", f"no answer within {self.agent_timeout:.0f}s", why=why)
                if attempt == 1:
                    self.warn(f"{agent.role} did not answer within {self.agent_timeout:.0f}s; "
                              f"abandoned the call and retried (its tokens may not be counted)")
                    continue
                self.escalate(f"{agent.role} timed out twice", f"agent call {self.calls}",
                              f"Two calls to the {agent.role} took longer than {self.agent_timeout:.0f}s each.",
                              {"abort": "Stop the run here"})
            except Exception as err:
                kind, reason = classify_ai_error(err)
                self.log(agent.role, "AI PROVIDER ERROR", str(err)[:500], why=reason, kind=kind)
                if kind == "retry" and attempt == 1:
                    self.warn(f"{agent.role}: {reason}; waited {config.RETRY_WAIT_S}s and retried")
                    time.sleep(config.RETRY_WAIT_S)
                    continue
                self.outcome = f"stopped: {reason}"
                print(f"\n  [x] {agent.role} could not be reached: {reason}")
                raise StopRun(self.outcome) from None
        text = (out.raw or "").strip()
        usage = usage_delta(usage_before, llm_usage(agent.llm))
        if not usage.total_tokens:           # no running total: fall back to the crew's own figure
            usage = getattr(out, "token_usage", None) or usage
        used = getattr(usage, "total_tokens", 0) or 0
        call_usd = call_cost_usd(agent.llm.model, usage)
        self.tokens += used
        self.cost_usd += call_usd
        self.log(agent.role, "completed step", text, why=why, input=instruction[:600],
                 seconds=round(time.time() - started, 1), tokens=used,
                 cost_rs=round(call_usd * cost_fx(), 2))
        return text

    def call_json(self, name: str, instruction: str, model, stage: str, why: str = ""):
        """Asks for JSON matching `model`. A malformed answer is repaired once, then escalated."""
        prompt = instruction
        text = ""
        for attempt in (1, 2):
            text = self.call_agent(name, prompt, "Only a single JSON object, no other text.", why)
            try:
                return model.model_validate(json.loads(text[text.index("{"): text.rindex("}") + 1]))
            except (ValueError, ValidationError) as err:
                problem = str(err).splitlines()[0][:300]
                if attempt == 1:
                    self.warn(f"{name.title()} returned malformed output; asked it to try again ({problem})")
                    prompt = (f"{instruction}\n\nYour previous answer could not be used: {problem}\n"
                              f"Previous answer:\n{text[:1500]}\n\nReply again with ONLY the corrected JSON object.")
        self.escalate("An agent returned malformed output twice", stage,
                      f"{name.title()} output could not be parsed:\n{text[:800]}",
                      {"abort": "Abort the request"})

    # ---------- shared memory and the saved trace ----------
    def facts(self) -> str:
        return "\n\n".join(f"[Step {i}]\n{t}" for i, t in self.results.items()) or "(none)"

    def save(self) -> Path:
        config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = config.RUNS_DIR / f"trace_{self.label}_{stamp}.json"
        data = redact_obj({
            "request": self.request, "outcome": self.outcome,
            "agent_calls": self.calls, "api_calls": self.api_calls, "chaos_mode": self.chaos,
            "total_tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 4), "cost_rs": round(self.cost_rs(), 2),
            "budget_rs": self.budget_rs, "time_limit_s": self.time_limit,
            "seconds_system": self.agent_seconds(),
            "seconds_waiting_for_human": round(self.human_seconds),
            "warnings": self.warnings, "security_flags": self.security_flags, "trace": self.trace,
        })
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path
