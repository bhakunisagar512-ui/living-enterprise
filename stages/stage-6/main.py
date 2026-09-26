"""
The Living Enterprise - Stage 5
Multi-agent system for enterprise requests (Escape Velocity 1.0, P-03).

  Planner   -> writes a plan: which agent does which step, and why
  Retriever -> reads company documents, runs the SLA credit calculator,
               calls a LIVE exchange-rate API (with retry, backup API and cached fallback)
  Executor  -> writes the final reply using only retrieved facts
  Validator -> checks the reply rule by rule and can reject it
  Human     -> decides whenever the system raises an escalation flag, and always
               gives the final approve / send back / disapprove decision

Web UI:  python app.py   (then open http://127.0.0.1:8000)
Run:  python main.py renewal | dispute | question | unknown | compare
      add --chaos          to make every exchange-rate API fail (tests recovery)
      add --chaos-partial  to make only the primary API fail
      add --budget 5       to set the cost budget in rupees (default 40)
      add --time-limit 60  to set the system-time budget in seconds (default 300)
      add --agent-timeout 5  to set the per-call AI timeout in seconds (default 120)
"""
import contextvars
import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from crewai import Agent, Crew, LLM, Process, Task
from crewai.tools import tool
from pydantic import BaseModel, Field, ValidationError, field_validator


def quiet_crewai():
    """Turns off CrewAI's cloud-tracing prompt ('Would you like to view your execution traces?').
    It can appear mid-run and swallow a keypress meant for our own human-review screens.
    We keep our own trace in runs/, so we record 'declined' using CrewAI's own settings."""
    try:
        from crewai.events.listeners.tracing import utils as tracing
        tracing.mark_first_execution_done(user_consented=False)
        tracing.set_suppress_tracing_messages(True)
    except Exception:
        pass   # a different CrewAI version: the prompt may appear, but the run still works

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
RUNS_DIR = BASE / "runs"

# ---------- Stopping conditions ----------
MAX_AGENT_CALLS = 14      # hard limit on LLM agent calls per run
MAX_PLAN_STEPS = 5        # the plan may not be longer than this
MAX_DRAFTS = 2            # Validator may reject this many drafts before a human decides
BUDGET_RS = 40.0          # cost budget per run, in rupees (--budget)
TIME_LIMIT_S = 300        # system-time budget per run, in seconds; human time not counted (--time-limit)
AGENT_TIMEOUT_S = 120     # a single AI call slower than this is abandoned and retried once (--agent-timeout)
WARN_AT = 0.8             # warn when 80% of a budget is used

# Claude prices in USD per million tokens (platform.claude.com pricing page, September 2026):
#               input  output  cache read
PRICES = {"claude-sonnet-5":           (2.0, 10.0, 0.20),
          "claude-haiku-4-5-20251001": (1.0,  5.0, 0.10)}
COST_FX_FALLBACK = 95.0   # USD->INR used for cost only when no exchange rate has been saved yet

# ---------- Who "we" are (given to every agent as trusted context) ----------
COMPANY_CONTEXT = ("We are Nimbus Retail Ltd (the Client in contract.txt). Replies are signed "
                   "'Nimbus Retail Procurement'. All amounts are in Indian rupees (Rs).")

# ---------- Sample requests ----------
REQUESTS = {
    "renewal": (
        "Email from Acme Cloud Services: 'Your contract ends on 31 October 2026. "
        "We would like to renew for another 12 months with a 12% price increase "
        "due to rising infrastructure costs. Please confirm.'"
    ),
    "dispute": (
        "Note from Accounts Payable: 'We have received two Acme Cloud Services invoices "
        "for September 2026 hosting. Please check whether this is a duplicate and, if so, "
        "draft a message to Acme disputing it.'"
    ),
    "question": (
        "Question from the CFO: 'In how many months this contract year did Acme miss its "
        "uptime SLA, and how much service credit are we owed?'"
    ),
    "compare": (
        "Question from the CFO: 'Acme wants a 12% increase. Using the competitor quotes we "
        "received, is Acme still competitive in rupees at its current price, at 7% and at 12%? "
        "Give me a short recommendation.'"
    ),
    "unknown": (
        "Email from Globex Logistics: 'We would like to renew our warehousing contract "
        "with a 5% increase from 1 December 2026. Please confirm.'"
    ),
}

# ---------- Models ----------
smart = LLM(model="anthropic/claude-sonnet-5", max_tokens=4096)
fast = LLM(model="anthropic/claude-haiku-4-5-20251001", max_tokens=4096)


# ================================================================
# Tools
# ================================================================
@tool("List company documents")
def list_documents() -> str:
    """Lists all company documents with the title line of each, so you know what each file contains."""
    lines = []
    for p in sorted(DATA_DIR.glob("*.txt")):
        text = p.read_text(encoding="utf-8").strip()
        title = text.splitlines()[0].strip() if text else "(empty file)"
        lines.append(f"{p.name}  -  {title}")
    return "\n".join(lines) or "(no documents found)"


@tool("Read company document")
def read_document(filename: str) -> str:
    """Reads one company document by its file name, for example 'contract.txt'."""
    path = DATA_DIR / Path(filename).name
    if not path.exists():
        return f"'{filename}' not found. Use 'List company documents' to see what exists."
    return path.read_text(encoding="utf-8")


def indian_rupees(amount: float) -> str:
    """Formats 2568000 as 'Rs 25,68,000'."""
    whole = str(int(round(amount)))
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join(re.findall(r"\d{1,2}", head[::-1]))[::-1]
        whole = f"{head},{tail}"
    return f"Rs {whole}"


@tool("SLA credit calculator")
def sla_credit_calculator(monthly_uptimes: str, sla_target_percent: float,
                          monthly_fee: float, credit_percent: float) -> str:
    """Works out which months missed the uptime SLA and the exact service credit owed.
    monthly_uptimes: copy the uptime line(s) from the document, e.g. 'Nov 99.95% | Dec 99.92% | May 99.42%'.
    sla_target_percent: e.g. 99.9. monthly_fee: fee per month in rupees, e.g. 200000.
    credit_percent: credit per missed month, e.g. 5."""
    pairs = re.findall(r"([A-Za-z]{3,9})\s*[:=]?\s*(\d{2,3}(?:\.\d+)?)\s*%?", monthly_uptimes)
    if not pairs:
        return "ERROR: no 'Month value%' pairs found. Copy the uptime line exactly as written."
    missed = [(m, float(v)) for m, v in pairs if float(v) < float(sla_target_percent)]
    met = [m for m, v in pairs if float(v) >= float(sla_target_percent)]
    credit_each = float(monthly_fee) * float(credit_percent) / 100
    total = credit_each * len(missed)
    missed_txt = ", ".join(f"{m} ({v}%)" for m, v in missed) or "none"
    return (
        f"Months checked: {len(pairs)}. SLA target: {sla_target_percent}%.\n"
        f"Months BELOW target (credit owed): {missed_txt}.\n"
        f"Months at or above target (no credit): {', '.join(met) or 'none'}.\n"
        f"Credit per missed month: {indian_rupees(credit_each)}. "
        f"TOTAL CREDIT OWED: {indian_rupees(total)}."
    )


# ================================================================
# Live currency converter: a REAL external API with a recovery chain
#   1. Primary API (Frankfurter)      - up to 2 tries, 4s timeout each
#   2. Backup API (open.er-api.com)   - up to 2 tries, 4s timeout each
#   3. Last known good rate (cache)   - used but marked UNVERIFIED (warning)
#   4. Nothing available              - returns an error -> Retriever reports MISSING -> human decides
# Every response is checked: HTTP status, JSON shape, number type, and a sanity range.
# ================================================================
CURRENT_RUN = None                      # set in main(); lets tools write to the trace
CHAOS = "off"                           # "off" | "all" (every API fails) | "partial" (primary fails)
FX_CACHE = BASE / "cache" / "fx_last_good.json"
FX_TIMEOUT = 4                          # seconds per API call
FX_LOCK = threading.Lock()              # CrewAI may run tool calls in parallel: fetch each rate once
FX_TRIES = 2                            # tries per API
SANE_RANGE = {("USD", "INR"): (50, 150), ("EUR", "INR"): (55, 170), ("GBP", "INR"): (65, 200)}

FX_APIS = [
    {"name": "Frankfurter (primary)",
     "url": "https://api.frankfurter.dev/v1/latest?base={src}&symbols={dst}"},
    {"name": "open.er-api.com (backup)",
     "url": "https://open.er-api.com/v6/latest/{src}"},
]


class FxError(Exception):
    """One failed attempt: kind is timeout | http | network | malformed | implausible."""
    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind


def fx_event(action: str, detail: str, warning: bool = False):
    """Writes API activity to the run trace (and warnings to the Final Review)."""
    print(f"     [api] {action}: {detail}")
    if CURRENT_RUN is not None:
        if action == "Unavailable":
            CURRENT_RUN.fx_unavailable = True
        if warning:
            CURRENT_RUN.warn(f"{action}: {detail}")
        else:
            CURRENT_RUN.log("tool:currency", action, detail)
        CURRENT_RUN.api_calls += action.startswith(("OK", "FAILED"))


def chaos_response(api_index: int, attempt: int):
    """Simulated failures for demos. Returns None when no failure is injected."""
    if CHAOS == "all" or (CHAOS == "partial" and api_index == 0):
        failures = [("http", "HTTP 500 Internal Server Error"),
                    ("timeout", f"no answer within {FX_TIMEOUT}s"),
                    ("malformed", "response was not valid JSON: '<html>Bad Gateway</html>'"),
                    ("implausible", "rate 0.012 is outside the sane range 50-150")]
        return failures[(api_index * FX_TRIES + attempt - 1) % len(failures)]
    return None


def fetch_json(url: str) -> dict:
    """The real network call. Raises FxError for every kind of failure."""
    import socket
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "LivingEnterprise/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=FX_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise FxError("http", f"HTTP {e.code} {e.reason}")
    except (socket.timeout, TimeoutError):
        raise FxError("timeout", f"no answer within {FX_TIMEOUT}s")
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise FxError("timeout", f"no answer within {FX_TIMEOUT}s")
        raise FxError("network", str(e.reason)[:120])
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise FxError("malformed", f"response was not valid JSON: {body[:60]!r}")


def parse_rate(data: dict, src: str, dst: str) -> tuple[float, str]:
    """Checks the response shape and returns (rate, date). Handles both APIs' formats."""
    if not isinstance(data, dict):
        raise FxError("malformed", "response is not a JSON object")
    if data.get("result") == "error":
        raise FxError("http", f"API reported an error: {data.get('error-type', 'unknown')}")
    rates = data.get("rates")
    if not isinstance(rates, dict) or dst not in rates:
        raise FxError("malformed", f"no '{dst}' rate in response (keys: {list(data)[:6]})")
    rate = rates[dst]
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise FxError("malformed", f"rate is not a number: {rate!r}")
    low, high = SANE_RANGE.get((src, dst), (1e-9, 1e9))
    if not low <= float(rate) <= high:
        raise FxError("implausible", f"rate {rate} is outside the sane range {low}-{high}")
    date = str(data.get("date") or data.get("time_last_update_utc") or "unknown date")
    return float(rate), date


def load_cache() -> dict:
    try:
        return json.loads(FX_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(pair: str, rate: float, date: str, source: str):
    cache = load_cache()
    cache[pair] = {"rate": rate, "date": date, "source": source,
                   "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    FX_CACHE.parent.mkdir(parents=True, exist_ok=True)
    FX_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def get_rate(src: str, dst: str) -> dict:
    """Runs the recovery chain. Returns {'rate','date','source','verified'} or raises FxError."""
    pair = f"{src}_{dst}"
    with FX_LOCK:                       # a parallel call waits here, then reuses the rate
        return _get_rate_locked(src, dst, pair)


def _get_rate_locked(src: str, dst: str, pair: str) -> dict:
    if CURRENT_RUN is not None and pair in CURRENT_RUN.fx_rates:
        fx = CURRENT_RUN.fx_rates[pair]
        CURRENT_RUN.log("tool:currency", "REUSED", f"{pair} rate {fx['rate']} already fetched in this run")
        return fx
    fx = _fetch_rate_chain(src, dst, pair)
    if CURRENT_RUN is not None:
        CURRENT_RUN.fx_rates[pair] = fx
    return fx


def _fetch_rate_chain(src: str, dst: str, pair: str) -> dict:
    for i, api in enumerate(FX_APIS):
        url = api["url"].format(src=src, dst=dst)
        for attempt in range(1, FX_TRIES + 1):
            started = time.time()
            try:
                injected = chaos_response(i, attempt)
                if injected:
                    if injected[0] == "timeout":
                        time.sleep(1)       # a short pause so the demo feels like a timeout
                    raise FxError(*injected)
                rate, date = parse_rate(fetch_json(url), src, dst)
                ms = round((time.time() - started) * 1000)
                fx_event("OK", f"{api['name']} try {attempt}: 1 {src} = {rate} {dst} (rates dated {date}, {ms} ms)")
                save_cache(pair, rate, date, api["name"])
                if i > 0 or attempt > 1:
                    fx_event("Recovered", f"live rate obtained from {api['name']} on try {attempt}", warning=True)
                return {"rate": rate, "date": date, "source": api["name"], "verified": True}
            except FxError as err:
                fx_event("FAILED", f"{api['name']} try {attempt}: {err}")
                if attempt < FX_TRIES:
                    time.sleep(0.5 * attempt)       # short back-off before retrying
    cached = load_cache().get(pair)
    if cached:
        fx_event("Fallback", f"all live APIs failed; using last known {src}->{dst} rate {cached['rate']} "
                 f"from {cached['fetched_at']} (UNVERIFIED)", warning=True)
        return {"rate": cached["rate"], "date": cached["date"],
                "source": f"cached copy of {cached['source']} from {cached['fetched_at']}", "verified": False}
    fx_event("Unavailable", f"all live APIs failed and no cached {src}->{dst} rate exists", warning=True)
    raise FxError("unavailable", "no live or cached exchange rate")


@tool("Currency converter (live)")
def convert_currency(amount: float, from_currency: str, to_currency: str = "INR") -> str:
    """Converts an amount between currencies using a LIVE exchange-rate API.
    Only use it when a document shows an amount in a foreign currency (e.g. USD).
    amount: e.g. 2200. from_currency: 3-letter code, e.g. 'USD'. to_currency: e.g. 'INR'."""
    src, dst = from_currency.strip().upper(), to_currency.strip().upper()
    if not (re.fullmatch(r"[A-Z]{3}", src) and re.fullmatch(r"[A-Z]{3}", dst)):
        return "ERROR: currencies must be 3-letter codes such as USD or INR."
    if src == dst:
        return f"{amount} {src} = {amount} {dst} (same currency, no conversion needed)."
    try:
        fx = get_rate(src, dst)
    except FxError:
        return (f"ERROR: exchange rate {src}->{dst} is unavailable (live APIs failed, no cached rate). "
                f"Report this under MISSING.")
    converted = float(amount) * fx["rate"]
    shown = indian_rupees(converted) if dst == "INR" else f"{converted:,.2f} {dst}"
    status = ("LIVE rate" if fx["verified"] else
              "UNVERIFIED rate (live APIs were down) - any figure using it must say it is indicative")
    return (f"{float(amount):,.2f} {src} = {shown} at 1 {src} = {fx['rate']} {dst}.\n"
            f"Source: {fx['source']}, rates dated {fx['date']}. Status: {status}.")


# ================================================================
# Agents
# ================================================================
planner = Agent(
    role="Planner",
    goal="Turn an enterprise request into a short plan that only uses agents and tools that exist.",
    backstory="A senior operations lead. You delegate precisely and never plan work nobody can do.",
    llm=smart, allow_delegation=False, verbose=False,
)
retriever = Agent(
    role="Retriever",
    goal="Find the facts a step needs in company documents, and say where each fact came from.",
    backstory="A careful analyst. You never state a fact without its source, and you use the "
              "calculator for any SLA or credit maths instead of doing it in your head.",
    llm=fast, tools=[list_documents, read_document, sla_credit_calculator, convert_currency],
    allow_delegation=False, verbose=False, max_iter=10,
)
executor = Agent(
    role="Executor",
    goal="Write the final reply using only the facts retrieved for this request.",
    backstory="A procurement specialist who writes firm, accurate, professional messages. "
              "You never invent numbers, names or claims.",
    llm=smart, allow_delegation=False, verbose=False,   # writing quality matters: stronger model
)
validator = Agent(
    role="Validator",
    goal="Check the reply against the retrieved facts and rules, and reject it if anything is wrong.",
    backstory="A strict compliance officer. Any number, name or claim not supported by the facts "
              "is a failure. You know which decisions only a human can make.",
    llm=smart, allow_delegation=False, verbose=False,
)

AGENTS = {"planner": planner, "retriever": retriever, "executor": executor, "validator": validator}

CATALOG = """Agents you can assign steps to:
- retriever: reads company documents. Tools: 'List company documents', 'Read company document',
  'SLA credit calculator' (exact SLA misses and credit owed),
  'Currency converter (live)' (calls a live exchange-rate API; ONLY needed when a document
  has amounts in a foreign currency such as USD). Use the retriever for every fact the reply needs.
- executor: writes the final message or answer from facts gathered in earlier steps. No tools.
There is NO agent that can email people, consult stakeholders or browse the web.
Approvals are NOT a reason to hold back: the agents prepare the full recommended response
(for example a counter-offer and any money owed to us), and a human reviews and approves it
at the end. Plan to gather ALL facts the full response needs (contract, policy, performance, invoices)."""


# ================================================================
# Structured outputs
# ================================================================
class Step(BaseModel):
    id: int
    agent: Literal["retriever", "executor"]
    task: str
    why: str


class Plan(BaseModel):
    summary: str
    reply_type: Literal["vendor_email", "internal_answer"]
    steps: list[Step] = Field(min_length=1)
    escalate: Optional[str] = None


def _as_text(item) -> str:
    """Agents sometimes return {"fix": "..."} instead of "..."; keep the words either way."""
    if isinstance(item, dict):
        return "; ".join(str(v) for v in item.values())
    return str(item)


class Check(BaseModel):
    rule: str
    result: Literal["PASS", "FAIL", "N/A"]
    note: str = ""

    @field_validator("rule", "note", mode="before")
    @classmethod
    def _text(cls, v):
        return "" if v is None else _as_text(v)

    @field_validator("result", mode="before")
    @classmethod
    def _normalise(cls, v):
        """Accept reasonable variations. Anything doubtful counts as FAIL (the safe side)."""
        word = str(v).strip().upper().replace("_", " ")
        if word in ("PASS", "PASSED", "OK", "YES", "TRUE", "COMPLIANT"):
            return "PASS"
        if word in ("N/A", "NA", "NOT APPLICABLE", "SKIP", "SKIPPED"):
            return "N/A"
        return "FAIL"     # FAIL, PARTIAL, WARNING, PARTIAL FAIL, unknown words ...


class Review(BaseModel):
    verdict: Literal["APPROVED", "REJECTED"]
    checks: list[Check] = []
    fixes: list[str] = []
    needs_human: list[str] = []

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, v):
        word = str(v).strip().upper()
        return "APPROVED" if word in ("APPROVED", "APPROVE", "PASS", "PASSED") else "REJECTED"

    @field_validator("fixes", "needs_human", mode="before")
    @classmethod
    def _texts(cls, v):
        if v is None:
            return []
        return [_as_text(x) for x in (v if isinstance(v, list) else [v])]


class StopRun(Exception):
    """Raised to end a run cleanly (human abort or stopping condition)."""


# ================================================================
# The run: shared state + trace + escalation flags
# ================================================================
HUMAN_WAIT = [0.0]   # seconds spent waiting for a human, kept apart from agent time


def timed_input(prompt: str) -> str:
    started = time.time()
    try:
        return input(prompt)
    finally:
        HUMAN_WAIT[0] += time.time() - started


def ask_choice(options: dict[str, str]) -> str:
    """Shows numbered options and returns the key the human picked.
    With no keyboard input available, it picks the safe option (abort / disapprove)."""
    keys = list(options)
    for i, k in enumerate(keys, 1):
        print(f"  [{i}] {options[k]}")
    safe = next((k for k in ("abort", "disapprove") if k in keys), keys[-1])
    while True:
        try:
            raw = timed_input("  Your decision: ").strip()
        except EOFError:
            return safe
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        print(f"  Please type a number from 1 to {len(keys)}.")


def ask_text(prompt: str) -> str:
    try:
        return timed_input(prompt).strip()
    except EOFError:
        return ""


# ================================================================
# The human channel. Terminal by default; the web app swaps in its own (see app.py).
# Both give the human the same information and the same choices.
# ================================================================
class ConsoleHuman:
    def escalation(self, run, reason: str, stage: str, detail: str,
                   options: dict[str, str]) -> tuple[str, str]:
        print("\n" + "=" * 64)
        print("  ESCALATED TO HUMAN")
        print(f"  Reason : {reason}")
        print(f"  Stage  : {stage}")
        print("  Detail :")
        for line in detail.strip().splitlines():
            print(f"    {line}")
        if run.warnings:
            print(f"  Earlier warnings: {len(run.warnings)}")
        print("-" * 64)
        choice = ask_choice(options)
        note = ask_text("  Instructions for the agents: ") if choice == "retry" else ""
        print("=" * 64 + "\n")
        return choice, note

    def final_review(self, run, r: dict) -> tuple[str, str]:
        """Returns ('approve' | 'sendback' | 'disapprove', text)."""
        st = r["stats"]
        print("\n" + "#" * 64)
        print("  FINAL REVIEW - HUMAN DECISION REQUIRED")
        print("#" * 64)
        print(f"\n  REQUEST\n    {r['request']}")
        print(f"\n  PLAN ({len(r['plan']['steps'])} steps): {r['plan']['summary']}")
        for s in r["plan"]["steps"]:
            print(f"    {s['id']}. [{s['agent']}] {s['task']}")
        fails = [c for c in r["checks"] if c["result"] == "FAIL"]
        print(f"\n  VALIDATOR: {r['verdict']}  "
              f"({len(r['checks']) - len(fails)} checks passed, {len(fails)} failed)")
        for c in r["checks"]:
            print(f"    {c['result']}  {c['rule']}" + (f" - {c['note']}" if c["note"] else ""))
        if r["needs_human"]:
            print("\n  APPROVALS ONLY A PERSON CAN GIVE (confirm these before approving):")
            for n in r["needs_human"]:
                print(f"    [ ] {n}")
        print(f"\n  WARNINGS ({len(r['warnings'])})")
        for w in r["warnings"] or ["none"]:
            print(f"    - {w}")
        print(f"\n  COST SO FAR: Rs {st['cost_rs']:.2f} of Rs {st['budget_rs']:.0f} budget "
              f"(${st['cost_usd']:.3f}) | {st['calls']} agent calls, {st['api_calls']} API calls, "
              f"{st['tokens']} tokens | {st['seconds']}s of {st['time_limit']}s")
        if r["internal_note"]:
            print("\n  INTERNAL NOTE (for you only - never sent)")
            for line in r["internal_note"].splitlines():
                print(f"    {line}")
        print("\n  " + "-" * 60 + "\n  OUTPUT TO BE SENT\n  " + "-" * 60)
        for line in r["message"].splitlines():
            print(f"  {line}")
        print("  " + "-" * 60)
        print("\n  [1] APPROVE  - release this output")
        print("  [2] SEND BACK - ask the agents to change something")
        print("  [3] DISAPPROVE - reject and close this request")
        choice = ""
        while choice not in ("1", "2", "3"):
            choice = ask("  Your decision (1/2/3): ", "3")
        if choice == "1":
            return "approve", ""
        if choice == "3":
            return "disapprove", ask("  Reason for disapproving: ", "no reason given")
        return "sendback", ask("  What should be changed? ", "")


HUMAN = ConsoleHuman()
EMIT = None          # the web app sets this to receive live events; None = terminal only


class AgentTimeout(Exception):
    """An AI call took longer than AGENT_TIMEOUT_S."""


def classify_ai_error(err: Exception) -> tuple[str, str]:
    """Sorts an AI provider error into (kind, plain-English reason).
    kind 'retry' = temporary (worth waiting and trying again); 'stop' = retrying cannot help."""
    text = str(err).lower()
    status = getattr(err, "status_code", None) or getattr(getattr(err, "response", None), "status_code", None)
    if "usage limit" in text or "credit balance" in text or "billing" in text:
        return "stop", "Anthropic account usage/spend limit reached - raise it in the Claude Console (Settings > Limits / Billing)"
    if status in (401, 403) or "authentication" in text or "api key" in text or "x-api-key" in text:
        return "stop", "Anthropic API key missing or invalid - check the ANTHROPIC_API_KEY setting"
    if status == 429 or "rate limit" in text or "rate_limit" in text:
        return "retry", "rate limited by the AI provider"
    if status in (500, 502, 503, 504, 529) or "overloaded" in text or "internal server error" in text:
        return "retry", "AI provider temporarily unavailable"
    if isinstance(err, (ConnectionError, TimeoutError)) or "connection" in text:
        return "retry", "network problem reaching the AI provider"
    return "stop", f"unexpected AI error: {type(err).__name__}: {str(err)[:160]}"


def kickoff_with_timeout(crew, timeout: float):
    """Runs one agent call in a background thread and stops waiting after `timeout` seconds.
    (CrewAI's own max_execution_time still waits for a stuck call to finish.)"""
    result: dict = {}
    ctx = contextvars.copy_context()

    def target():
        try:
            result["out"] = ctx.run(crew.kickoff)
        except BaseException as err:          # hand any error back to the main thread
            result["err"] = err

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AgentTimeout()
    if "err" in result:
        raise result["err"]
    return result["out"]


USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "cached_prompt_tokens",
                "cache_creation_tokens", "total_tokens")


def llm_usage(llm) -> dict:
    """The model object's running token totals (all calls made with it so far)."""
    try:
        summary = llm.get_token_usage_summary()
        return {f: getattr(summary, f, 0) or 0 for f in USAGE_FIELDS}
    except Exception:
        return {f: 0 for f in USAGE_FIELDS}


def usage_delta(before: dict, after: dict):
    from types import SimpleNamespace
    return SimpleNamespace(**{f: max(0, after[f] - before[f]) for f in USAGE_FIELDS})


def call_cost_usd(model: str, usage) -> float:
    """Real cost of one call from its token usage. Unknown models are priced like Sonnet (safe side)."""
    price_in, price_out, price_cache = PRICES.get(model.split("/")[-1], PRICES["claude-sonnet-5"])
    tok = lambda field: getattr(usage, field, 0) or 0
    return (tok("prompt_tokens") * price_in
            + tok("cached_prompt_tokens") * price_cache
            + tok("cache_creation_tokens") * price_in * 1.25
            + tok("completion_tokens") * price_out) / 1_000_000


def cost_fx() -> float:
    """USD->INR for reporting cost: the last saved live rate if there is one, else a fixed fallback."""
    return load_cache().get("USD_INR", {}).get("rate", COST_FX_FALLBACK)


def fmt_budget(value: float, unit: str) -> str:
    return f"Rs {value:.2f}" if unit == "Rs" else f"{int(value)}s"


class Run:
    def __init__(self, request: str, label: str):
        self.request = request
        self.label = label
        self.started = time.time()
        self.calls = 0
        self.tokens = 0
        self.results: dict[int, str] = {}   # shared memory: step id -> output
        self.warnings: list[str] = []
        self.human_notes: list[str] = []    # human instructions, shared with every agent
        self.drafts = 0                      # drafts validated so far in this run
        self.api_calls = 0                   # external API attempts (success + failure)
        self.fx_unavailable = False          # set by the currency tool when no rate could be found
        self.fx_rates: dict[str, dict] = {}  # rates fetched in this run (reused, so all figures match)
        self.cost_usd = 0.0                  # measured from real token usage, per call
        self.budget_rs = BUDGET_RS
        self.time_limit = TIME_LIMIT_S
        self.warned: set[str] = set()        # which 80% warnings were already given
        self.trace: list[dict] = []
        self.outcome = "in progress"

    # ---- live events (for the web page) ----
    def stats(self) -> dict:
        return {"cost_rs": round(self.cost_rs(), 2), "cost_usd": round(self.cost_usd, 4),
                "budget_rs": self.budget_rs, "seconds": self.agent_seconds(),
                "time_limit": self.time_limit, "calls": self.calls, "max_calls": MAX_AGENT_CALLS,
                "api_calls": self.api_calls, "tokens": self.tokens, "warnings": len(self.warnings)}

    def emit(self, event_type: str, **data):
        if EMIT is not None:
            try:
                EMIT({"type": event_type, "time": datetime.now().strftime("%H:%M:%S"), "stats": self.stats(), **data})
            except Exception:
                pass    # the page must never be able to break a run

    # ---- trace ----
    def log(self, actor: str, action: str, detail: str = "", why: str = "", **extra):
        entry = {"time": datetime.now().strftime("%H:%M:%S"),
                 "actor": actor, "action": action, "why": why,
                 "detail": detail[:1500], **extra}
        self.trace.append(entry)
        self.emit("log", entry=entry)

    # ---- Level 1 flag: system recovers, human is told later ----
    def warn(self, message: str):
        if message in self.warnings:
            return
        self.warnings.append(message)
        self.log("system", "WARNING", message)
        print(f"  [!] Warning: {message}")

    # ---- Level 2 flag: system stops, human decides ----
    def escalate(self, reason: str, stage: str, detail: str, options: dict[str, str]):
        self.log("system", "ESCALATED", detail, why=reason, stage=stage)
        choice, note = HUMAN.escalation(self, reason, stage, detail, options)
        self.log("human", f"DECISION: {choice}", note, why=reason, stage=stage)
        if choice == "abort":
            self.outcome = f"aborted by human at {stage}"
            raise StopRun(self.outcome)
        return choice, note

    # ---- budgets: cost (rupees) and system time ----
    def cost_rs(self) -> float:
        return self.cost_usd * cost_fx()

    def check_budgets(self):
        """Runs before every AI call. 80% -> warning. 100% -> the human decides."""
        spent, secs = self.cost_rs(), self.agent_seconds()
        for key, used, limit, unit in (("cost", spent, self.budget_rs, "Rs"),
                                       ("time", secs, self.time_limit, "s")):
            if used >= WARN_AT * limit and key not in self.warned and used < limit:
                self.warned.add(key)
                self.warn(f"{int(WARN_AT * 100)}% of the {key} budget used "
                          f"({fmt_budget(used, unit)} of {fmt_budget(limit, unit)})")
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

    # ---- the only place an LLM agent is called ----
    def call_agent(self, name: str, instruction: str, expected: str, why: str = "") -> str:
        if self.calls >= MAX_AGENT_CALLS:
            self.outcome = f"stopped: reached the limit of {MAX_AGENT_CALLS} agent calls"
            self.log("system", "STOP", self.outcome)
            raise StopRun(self.outcome)
        self.check_budgets()
        agent = AGENTS[name]
        for attempt in (1, 2):
            self.calls += 1
            self.emit("agent", role=agent.role, call=self.calls, attempt=attempt)
            print(f"  -> {agent.role} working... (call {self.calls}/{MAX_AGENT_CALLS} | "
                  f"Rs {self.cost_rs():.2f} of Rs {self.budget_rs:.0f} | "
                  f"{self.agent_seconds()}s of {self.time_limit}s)")
            task = Task(description=instruction, expected_output=expected, agent=agent)
            usage_before = llm_usage(agent.llm)
            crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
            started = time.time()
            try:
                out = kickoff_with_timeout(crew, AGENT_TIMEOUT_S)
                break
            except AgentTimeout:
                # SLOW step: abandon the call, retry once, then a human decides
                self.log(agent.role, "TIMED OUT", f"no answer within {AGENT_TIMEOUT_S}s", why=why)
                if attempt == 1:
                    self.warn(f"{agent.role} did not answer within {AGENT_TIMEOUT_S}s; "
                              f"abandoned the call and retried (its tokens may not be counted)")
                    continue
                self.escalate(f"{agent.role} timed out twice", f"agent call {self.calls}",
                              f"Two calls to the {agent.role} took longer than {AGENT_TIMEOUT_S}s each.",
                              {"abort": "Stop the run here"})
            except Exception as err:
                # FAILED step at the AI provider: retry what is temporary, stop cleanly on the rest
                kind, reason = classify_ai_error(err)
                self.log(agent.role, "AI PROVIDER ERROR", str(err)[:500], why=reason, kind=kind)
                if kind == "retry" and attempt == 1:
                    self.warn(f"{agent.role}: {reason}; waited 5s and retried")
                    time.sleep(5)
                    continue
                self.outcome = f"stopped: {reason}"
                print(f"\n  [x] {agent.role} could not be reached: {reason}")
                raise StopRun(self.outcome)
        text = out.raw.strip()
        # CrewAI reports usage as the model's running total since the program started (shared by
        # every agent on that model), so this call's usage = total after - total before.
        usage = usage_delta(usage_before, llm_usage(agent.llm))
        if not usage.total_tokens:          # model gave no running total: fall back to the crew's figure
            usage = getattr(out, "token_usage", None) or usage
        used = getattr(usage, "total_tokens", 0) or 0
        call_usd = call_cost_usd(agent.llm.model, usage)
        self.tokens += used
        self.cost_usd += call_usd
        self.log(agent.role, "completed step", text, why=why, input=instruction[:600],
                 seconds=round(time.time() - started, 1), tokens=used,
                 cost_rs=round(call_usd * cost_fx(), 2))
        return text

    # ---- ask for JSON, repair once if malformed ----
    def call_json(self, name: str, instruction: str, model, stage: str, why: str = ""):
        prompt = instruction
        for attempt in (1, 2):
            text = self.call_agent(name, prompt, "Only a single JSON object, no other text.", why)
            try:
                return model.model_validate(json.loads(text[text.index("{"): text.rindex("}") + 1]))
            except (ValueError, ValidationError) as err:
                problem = str(err).splitlines()[0][:300]
                if attempt == 1:
                    self.warn(f"{AGENTS[name].role} returned malformed output; asked it to try again ({problem})")
                    prompt = (f"{instruction}\n\nYour previous answer could not be used: {problem}\n"
                              f"Previous answer:\n{text[:1500]}\n\nReply again with ONLY the corrected JSON object.")
        self.escalate("An agent returned malformed output twice", stage,
                      f"{AGENTS[name].role} output could not be parsed:\n{text[:800]}",
                      {"abort": "Abort the request"})

    def agent_seconds(self) -> int:
        return round(time.time() - self.started - HUMAN_WAIT[0])

    def facts(self) -> str:
        return "\n\n".join(f"[Step {i}]\n{t}" for i, t in self.results.items()) or "(none)"

    def save(self):
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = RUNS_DIR / f"trace_{self.label}_{stamp}.json"
        path.write_text(json.dumps({
            "request": self.request, "outcome": self.outcome,
            "agent_calls": self.calls, "api_calls": self.api_calls, "chaos_mode": CHAOS,
            "total_tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 4), "cost_rs": round(self.cost_rs(), 2),
            "budget_rs": self.budget_rs, "time_limit_s": self.time_limit,
            "seconds_system": self.agent_seconds(),
            "seconds_waiting_for_human": round(HUMAN_WAIT[0]),
            "warnings": self.warnings, "trace": self.trace,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        return path


# ================================================================
# Phases
# ================================================================
def make_plan(run: Run) -> Plan:
    print("\n[1] PLANNING")
    instruction = f"""An enterprise request has arrived:

{run.request}

{CATALOG}

Write a plan of 1 to {MAX_PLAN_STEPS} steps. Rules:
- Only use the agents and tools listed above. Do not plan work nobody can do.
- Use as few steps as the request really needs. Skip tools that are not needed.
- Put ALL lookups in ONE retriever step unless a later lookup truly depends on an earlier one.
  A typical plan is 2 steps: one retriever step, then the executor.
- The LAST step must be the executor writing the final output.
- For each step, 'why' explains in one sentence why this agent and step are needed.
- reply_type is "vendor_email" if the output goes to a vendor, else "internal_answer".
- If the request cannot be handled with these agents and tools, set "escalate" to the reason.

Reply with ONLY this JSON:
{{"summary": "...", "reply_type": "vendor_email", "steps": [{{"id": 1, "agent": "retriever", "task": "...", "why": "..."}}], "escalate": null}}"""
    plan = run.call_json("planner", instruction, Plan, "planning", why="Every request starts with a plan")

    if plan.escalate:
        run.escalate("Planner says the request cannot be handled", "planning", plan.escalate,
                     {"continue": "Continue with the plan anyway", "abort": "Abort the request"})
    if len(plan.steps) > MAX_PLAN_STEPS:
        run.warn(f"Plan had {len(plan.steps)} steps; trimmed to the limit of {MAX_PLAN_STEPS}")
        plan.steps = plan.steps[:MAX_PLAN_STEPS - 1] + [plan.steps[-1]]
    if plan.steps[-1].agent != "executor":
        run.warn("Plan did not end with the executor; added a final writing step")
        plan.steps.append(Step(id=len(plan.steps) + 1, agent="executor",
                               task="Write the final output for the request.",
                               why="Every request needs a final written output"))

    print(f"  Plan: {plan.summary}")
    for s in plan.steps:
        print(f"    {s.id}. [{s.agent}] {s.task}\n       why: {s.why}")
    run.log("Planner", "PLAN", plan.model_dump_json(indent=2), plan=plan.model_dump())  # full plan, untruncated
    run.emit("plan", summary=plan.summary, reply_type=plan.reply_type,
             steps=[s.model_dump() for s in plan.steps])
    return plan


def execute_plan(run: Run, plan: Plan) -> str:
    print("\n[2] EXECUTING THE PLAN")
    draft = ""
    for step in plan.steps:
        if step.agent == "retriever":
            text = run.call_agent("retriever", f"""Request: {run.request}

Your step: {step.task}

Facts gathered so far:
{run.facts()}

Rules:
- First list the documents (the list shows what each file contains), then read the ones you need.
  Information may be in a file whose name does not match exactly - check the titles and contents.
- For every fact give the source file and clause/rule number.
- For any SLA or service-credit maths, use the 'SLA credit calculator' tool.
- If a document shows amounts in a foreign currency (e.g. USD), convert each one with the
  'Currency converter (live)' tool and copy its full result, including the rate, date and Status.
  If the Status says UNVERIFIED, say so clearly. Do not call it when everything is already in Rs.
- Be concise: at most 25 bullets, one line each, only facts this request needs.
  Copy calculator and converter results in one line each. No headings, no commentary.
- The LAST line of your answer MUST be exactly one of:
    MISSING: none
    MISSING: <what the request needs that is in none of the documents>
  Use MISSING only when the request CANNOT be answered without it, for example there is no
  contract at all for the vendor in the request ("MISSING: contract for <vendor>").
  Details that simply are not in the documents (a notice period, incident logs, a clause that
  does not exist) are NOT missing: list them as "Not in documents: ..." and end with MISSING: none.""",
                "Bullet list of facts with sources, then a final line starting 'MISSING:'.", why=step.why)
            missing = missing_line(text)
            if missing is None:
                run.warn("Retriever did not report its MISSING line; asked it to add one")
                text = run.call_agent("retriever",
                    f"Here is your previous answer:\n{text}\n\nRepeat it and end with a final line "
                    f"'MISSING: none' or 'MISSING: <what is missing>'. Request: {run.request}",
                    "Same answer, ending with a MISSING: line.", why="Repair missing status line")
                missing = missing_line(text) or "unknown (retriever did not say)"
            if run.fx_unavailable and missing.lower() in NONE_WORDS:
                missing = "live exchange rate (every API failed and no cached rate exists)"
                run.warn("Currency tool failed but the Retriever did not report it; flagged by the system")
            if missing.lower() not in NONE_WORDS:
                run.escalate("A required document is missing", f"step {step.id} (retriever)",
                             f"Missing: {missing}",
                             {"continue": "Continue with a holding reply (no confirmation, no commitments)",
                              "abort": "Abort the request"})
                run.human_notes.append(
                    f"Required information is missing ({missing}). Write only a polite holding reply: "
                    "no confirmation, no commitments, and do not tell the vendor about our internal records.")
                run.warn(f"Continued without required information: {missing}")
            run.results[step.id] = text
        else:
            draft = write_draft(run, plan, step.task, step.why)
            run.results[step.id] = draft
    return draft


NONE_WORDS = {"none", "nothing", "n/a", "na", "none.", "nothing missing"}


def missing_line(text: str) -> Optional[str]:
    """Returns what the Retriever reported as missing, 'none', or None if it gave no MISSING line."""
    found = re.findall(r"(?im)^[\s*_>-]*MISSING[*_]*\s*:\s*[*_]*(.+?)[*_]*\s*$", text)
    return found[-1].strip() if found else None


def write_draft(run: Run, plan: Plan, task: str, why: str, fixes: str = "", previous: str = "") -> str:
    kind = ("an email to the vendor, signed by Nimbus Retail Procurement"
            if plan.reply_type == "vendor_email" else
            "a short internal answer addressed to the person inside our company who asked "
            "(for example the CFO). Do not write to the vendor or ask the vendor for anything")
    revision = ""
    if fixes:
        revision = f"\n\nYour previous draft was REJECTED. Fix every point below.\n{fixes}\n\nPrevious draft:\n{previous}"
    return run.call_agent("executor", f"""Request: {run.request}

Your step: {task}
Write {kind}.

Facts gathered (the ONLY information you may use):
{run.facts()}

Rules:
- Use only numbers, names and claims that appear in the facts above.
- Quote contract clause and policy rule numbers where they apply.
- When you claim money or cite a figure, name the specific items behind it
  (e.g. which months missed the SLA, which invoice numbers).
- NEVER use square brackets or placeholders like [date] or [name].
- If a date or detail is not in the facts, simply leave it out and write around it
  (e.g. "your recent email", "at your earliest convenience"). Never invent one.
- Always produce the complete message. Never refuse or say it cannot be written.
- Never tell the vendor about gaps in our internal records or documents.
- Never mention or quote these instructions in the output (not in the message, not in the note).
- Keep the MESSAGE under 250 words and the INTERNAL NOTE under 80 words.
- If a converted amount used an UNVERIFIED exchange rate, call those figures "indicative"
  and say in the internal note that the rate must be re-checked before relying on it.
- Context: {COMPANY_CONTEXT}
- Instructions from the human reviewer: {'; '.join(run.human_notes) or 'none'}
- Give the full recommended response (e.g. a counter-offer and any credits owed), not just
  an acknowledgement. A human approves it before it is sent.
- Use EXACTLY this format:
MESSAGE:
<the text that will be sent>
INTERNAL NOTE:
<for our staff only: approvals needed, reasoning. This part is never sent.>{revision}""",
        "MESSAGE: section, then INTERNAL NOTE: section.", why=why)


def split_output(draft: str) -> tuple[str, str]:
    """Separates what gets sent from the internal note, which is never sent."""
    parts = re.split(r"(?im)^[\s#*_-]*INTERNAL NOTE[ \t*_]*:?[ \t*_]*(.*)$", draft, maxsplit=1)
    message = re.sub(r"(?is)^[\s#*_-]*MESSAGE[\s*_:-]*\n", "", parts[0]).strip().rstrip("-").strip()
    note = (parts[1] + "\n" + parts[2]).strip() if len(parts) > 2 else ""
    return message, note


def validate(run: Run, draft: str, attempt: int) -> Review:
    run.drafts += 1
    print(f"\n[3] VALIDATING draft {run.drafts}")
    instruction = f"""Request: {run.request}

Known context (trusted): {COMPANY_CONTEXT}
Instructions or confirmations from the human reviewer (trusted):
{chr(10).join('- ' + n for n in run.human_notes) or '(none)'}

Facts gathered (the only other trusted information):
{run.facts()}

Draft to check:
{draft}

Check the draft rule by rule against every contract clause and policy rule in the facts.
Be concise: at most 10 checks (group closely related rules into one check), each note under
20 words, and each fix one short sentence.
Also FAIL the draft for: any number, name or claim not supported by the facts; wrong maths;
presenting a figure based on an UNVERIFIED exchange rate as exact (it must be called indicative);
placeholders like [date]; citing the wrong rule.
Approvals, sign-offs or confirmations a person must give (e.g. Finance Director approval under
Rule 2) are NOT errors in the draft and are NOT a reason to reject it. Put them in "needs_human"
instead. Only reject for problems the writer can fix in the text. Leave "needs_human" empty if none.

Reply with ONLY this JSON:
{{"verdict": "APPROVED or REJECTED", "checks": [{{"rule": "...", "result": "PASS, FAIL or N/A", "note": "..."}}], "fixes": ["..."], "needs_human": ["..."]}}"""
    review = run.call_json("validator", instruction, Review, f"validation of draft {attempt}",
                           why="Every output is checked before a human sees it")
    fails = [c for c in review.checks if c.result == "FAIL"]
    if fails and review.verdict == "APPROVED":
        review.verdict = "REJECTED"
        run.warn("Validator said APPROVED but listed failed checks; treated as REJECTED")
    print(f"  Verdict: {review.verdict}  ({len(review.checks) - len(fails)} pass, {len(fails)} fail)")
    run.emit("verdict", draft=run.drafts, verdict=review.verdict,
             passed=len(review.checks) - len(fails), failed=len(fails),
             fails=[c.model_dump() for c in fails])
    for c in fails:
        print(f"    FAIL {c.rule}: {c.note}")
    return review


def review_loop(run: Run, plan: Plan, draft: str) -> tuple[str, Review]:
    last_exec = plan.steps[-1]
    attempt = 1
    while True:
        review = validate(run, draft, attempt)
        if review.verdict == "APPROVED":
            if attempt > 1:
                run.warn(f"Draft was rewritten {attempt - 1} time(s) before approval")
            return draft, review
        fixes = "\n".join(f"- {f}" for f in review.fixes) or "- See the failed checks."
        if attempt >= MAX_DRAFTS:
            choice, note = run.escalate(
                f"Validator rejected {attempt} drafts", f"validation (attempt {attempt} of {MAX_DRAFTS})",
                f"Remaining problems:\n{fixes}",
                {"retry": "Retry once more with my instructions",
                 "accept": "Accept the draft anyway (I take responsibility)",
                 "abort": "Abort the request"})
            if choice == "accept":
                run.warn("Human accepted a draft the Validator rejected")
                return draft, review
            if note:
                run.human_notes.append(note)
            fixes += f"\n- Instruction from the human reviewer: {note}"
        print(f"\n  Sending draft {attempt} back to the Executor with {len(review.fixes)} fixes")
        draft = write_draft(run, plan, last_exec.task, "Validator rejected the previous draft",
                            fixes=fixes, previous=draft)
        attempt += 1


def ask(prompt: str, default: str = "") -> str:
    try:
        return timed_input(prompt).strip()
    except EOFError:
        return default


def final_review(run: Run, plan: Plan, draft: str, review: Review) -> str:
    """Nothing leaves the system until a human has seen everything and approved it."""
    while True:
        message, note = split_output(draft)
        summary = {"request": run.request,
                   "plan": {"summary": plan.summary, "steps": [s.model_dump() for s in plan.steps]},
                   "verdict": review.verdict, "checks": [c.model_dump() for c in review.checks],
                   "needs_human": review.needs_human, "warnings": list(run.warnings),
                   "stats": run.stats(), "internal_note": note, "message": message}
        decision, text = HUMAN.final_review(run, summary)

        if decision == "approve":
            confirmed = "; ".join(review.needs_human) or "none required"
            run.log("human", "FINAL DECISION: approved", message,
                    why="Human reviewed plan, checks, warnings and output",
                    approvals_confirmed=confirmed, internal_note=note)
            return message
        if decision == "disapprove":
            reason = text or "no reason given"
            run.log("human", "FINAL DECISION: disapproved", reason, why="Human rejected the output")
            run.outcome = f"disapproved by human: {reason}"
            raise StopRun(run.outcome)

        if text:
            run.human_notes.append(text)
        run.log("human", "FINAL DECISION: sent back", text, why="Human asked for changes")
        print("\n  Sending back to the Executor with your instructions")
        draft = write_draft(run, plan, plan.steps[-1].task, "Human reviewer asked for changes",
                            fixes=f"- Instruction from the human reviewer: {text}", previous=draft)
        draft, review = review_loop(run, plan, draft)


def release(run: Run, text: str) -> Path:
    """The irreversible action. In this prototype, 'sending' = saving to the outbox folder."""
    outbox = BASE / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{run.label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    path.write_text(text, encoding="utf-8")
    run.log("system", "RELEASED", str(path), why="Human approved")
    return path


# ================================================================
# Main
# ================================================================
def parse_args(argv: list[str]):
    """Returns (request names, on/off flags, numeric options). Accepts '--budget 5' and '--budget=5'."""
    numeric = ("--budget", "--time-limit", "--agent-timeout")
    args, flags, values = [], [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        name, _, inline = a.partition("=")
        if name in numeric:
            raw = inline or (argv[i + 1] if i + 1 < len(argv) else "")
            i += 0 if inline else 1
            try:
                values[name] = float(raw)
                assert values[name] > 0
            except (ValueError, AssertionError):
                raise ValueError(f"{name} needs a positive number, e.g. {name} 10")
        elif a.startswith("--"):
            flags.append(a)
        else:
            args.append(a)
        i += 1
    return args, flags, values


def run_request(label: str, chaos: str = "off", budget: Optional[float] = None,
                time_limit: Optional[float] = None, agent_timeout: Optional[float] = None) -> Run:
    """Runs one request end to end. Used by the terminal (main) and by the web app."""
    global CURRENT_RUN, CHAOS, AGENT_TIMEOUT_S
    CHAOS = chaos
    AGENT_TIMEOUT_S = agent_timeout or AGENT_TIMEOUT_S
    HUMAN_WAIT[0] = 0.0
    run = Run(REQUESTS[label], label)
    run.budget_rs = budget or BUDGET_RS
    run.time_limit = int(time_limit or TIME_LIMIT_S)
    CURRENT_RUN = run
    print("=" * 64)
    print(f"REQUEST ({label}): {run.request}")
    if CHAOS != "off":
        print(f"CHAOS MODE: {'every exchange-rate API will fail' if CHAOS == 'all' else 'the primary API will fail'}")
    print(f"BUDGETS: Rs {run.budget_rs:.0f} cost | {run.time_limit}s system time | "
          f"{MAX_AGENT_CALLS} agent calls | {AGENT_TIMEOUT_S:.0f}s per AI call")
    print("=" * 64)
    run.emit("start", label=label, request=run.request, chaos=CHAOS, agent_timeout=AGENT_TIMEOUT_S)
    run.log("system", "BUDGETS", f"cost Rs {run.budget_rs}, time {run.time_limit}s, "
            f"{MAX_AGENT_CALLS} agent calls, {AGENT_TIMEOUT_S}s per call")
    if CHAOS != "off":
        run.log("system", "CHAOS MODE", CHAOS)
    run.log("intake", "REQUEST RECEIVED", run.request)
    released = None
    try:
        plan = make_plan(run)
        draft = execute_plan(run, plan)
        draft, review = review_loop(run, plan, draft)
        final = final_review(run, plan, draft, review)
        sent_to = release(run, final)
        released = final
        run.outcome = f"approved by human and released to {sent_to.relative_to(BASE)}"
    except StopRun as stop:
        print(f"\nRUN STOPPED: {stop}")
    except Exception as err:          # never crash without leaving a trace
        run.outcome = f"crashed: {type(err).__name__}: {err}"
        run.log("system", "ERROR", run.outcome)
        print(f"\nRUN FAILED: {run.outcome}")

    print("\n" + "-" * 64)
    print(f"Outcome     : {run.outcome}")
    print(f"Cost        : Rs {run.cost_rs():.2f} (${run.cost_usd:.3f}) of Rs {run.budget_rs:.0f} budget")
    print(f"Agent calls : {run.calls}   API calls: {run.api_calls}   Tokens: {run.tokens}   "
          f"System time: {run.agent_seconds()}s of {run.time_limit}s   Human time: {round(HUMAN_WAIT[0])}s")
    for w in run.warnings:
        print(f"Warning     : {w}")
    path = run.save()
    print(f"Trace saved : {path}")
    run.emit("done", outcome=run.outcome, trace=path.name, released=released,
             human_seconds=round(HUMAN_WAIT[0]), warnings_list=list(run.warnings))
    return run


def main():
    quiet_crewai()
    try:
        args, flags, values = parse_args(sys.argv[1:])
    except ValueError as err:
        print(err)
        return
    label = args[0] if args else "renewal"
    if label not in REQUESTS:
        print(f"Unknown request '{label}'. Choose one of: {', '.join(REQUESTS)}")
        return
    chaos = "all" if "--chaos" in flags else "partial" if "--chaos-partial" in flags else "off"
    run_request(label, chaos, values.get("--budget"), values.get("--time-limit"),
                values.get("--agent-timeout"))


if __name__ == "__main__":
    main()
