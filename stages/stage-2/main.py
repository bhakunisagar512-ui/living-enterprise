"""
The Living Enterprise - Stage 2
Multi-agent system for enterprise requests (Escape Velocity 1.0, P-03).

  Planner   -> writes a plan: which agent does which step, and why
  Retriever -> reads company documents, runs the SLA credit calculator
  Executor  -> writes the final reply using only retrieved facts
  Validator -> checks the reply rule by rule and can reject it
  Human     -> decides whenever the system raises an escalation flag, and always
               gives the final approve / send back / disapprove decision

Run:  python main.py renewal | dispute | question | unknown
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from crewai import Agent, Crew, LLM, Process, Task
from crewai.tools import tool
from pydantic import BaseModel, Field, ValidationError

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
RUNS_DIR = BASE / "runs"

# ---------- Stopping conditions ----------
MAX_AGENT_CALLS = 14      # hard limit on LLM agent calls per run
MAX_PLAN_STEPS = 5        # the plan may not be longer than this
MAX_DRAFTS = 2            # Validator may reject this many drafts before a human decides

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
    llm=fast, tools=[list_documents, read_document, sla_credit_calculator],
    allow_delegation=False, verbose=False, max_iter=8,
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
  'SLA credit calculator' (exact SLA misses and credit owed). Use it for every fact the reply needs.
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


class Check(BaseModel):
    rule: str
    result: Literal["PASS", "FAIL"]
    note: str = ""


class Review(BaseModel):
    verdict: Literal["APPROVED", "REJECTED"]
    checks: list[Check] = []
    fixes: list[str] = []
    needs_human: list[str] = []


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
        self.trace: list[dict] = []
        self.outcome = "in progress"

    # ---- trace ----
    def log(self, actor: str, action: str, detail: str = "", why: str = "", **extra):
        self.trace.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "actor": actor, "action": action, "why": why,
            "detail": detail[:1500], **extra,
        })

    # ---- Level 1 flag: system recovers, human is told later ----
    def warn(self, message: str):
        self.warnings.append(message)
        self.log("system", "WARNING", message)
        print(f"  [!] Warning: {message}")

    # ---- Level 2 flag: system stops, human decides ----
    def escalate(self, reason: str, stage: str, detail: str, options: dict[str, str]):
        self.log("system", "ESCALATED", detail, why=reason, stage=stage)
        print("\n" + "=" * 64)
        print("  ESCALATED TO HUMAN")
        print(f"  Reason : {reason}")
        print(f"  Stage  : {stage}")
        print("  Detail :")
        for line in detail.strip().splitlines():
            print(f"    {line}")
        if self.warnings:
            print(f"  Earlier warnings: {len(self.warnings)}")
        print("-" * 64)
        choice = ask_choice(options)
        note = ask_text("  Instructions for the agents: ") if choice == "retry" else ""
        print("=" * 64 + "\n")
        self.log("human", f"DECISION: {choice}", note, why=reason, stage=stage)
        if choice == "abort":
            self.outcome = f"aborted by human at {stage}"
            raise StopRun(self.outcome)
        return choice, note

    # ---- the only place an LLM agent is called ----
    def call_agent(self, name: str, instruction: str, expected: str, why: str = "") -> str:
        if self.calls >= MAX_AGENT_CALLS:
            self.outcome = f"stopped: reached the limit of {MAX_AGENT_CALLS} agent calls"
            self.log("system", "STOP", self.outcome)
            raise StopRun(self.outcome)
        self.calls += 1
        agent = AGENTS[name]
        print(f"  -> {agent.role} working... (call {self.calls}/{MAX_AGENT_CALLS})")
        task = Task(description=instruction, expected_output=expected, agent=agent)
        started = time.time()
        out = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False).kickoff()
        text = out.raw.strip()
        used = getattr(getattr(out, "token_usage", None), "total_tokens", 0) or 0
        self.tokens += used
        self.log(agent.role, "completed step", text, why=why, input=instruction[:600],
                 seconds=round(time.time() - started, 1), tokens=used)
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
        RUNS_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = RUNS_DIR / f"trace_{self.label}_{stamp}.json"
        path.write_text(json.dumps({
            "request": self.request, "outcome": self.outcome,
            "agent_calls": self.calls, "total_tokens": self.tokens,
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
    run.log("Planner", "PLAN", plan.model_dump_json(indent=2))
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
- The LAST line of your answer MUST be exactly one of:
    MISSING: none
    MISSING: <what the request needs that is in none of the documents>
  Example: if the request is about a vendor and there is no contract for that vendor,
  write "MISSING: contract for <vendor>".""",
                "Bullet list of facts with sources, then a final line starting 'MISSING:'.", why=step.why)
            missing = missing_line(text)
            if missing is None:
                run.warn("Retriever did not report its MISSING line; asked it to add one")
                text = run.call_agent("retriever",
                    f"Here is your previous answer:\n{text}\n\nRepeat it and end with a final line "
                    f"'MISSING: none' or 'MISSING: <what is missing>'. Request: {run.request}",
                    "Same answer, ending with a MISSING: line.", why="Repair missing status line")
                missing = missing_line(text) or "unknown (retriever did not say)"
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
            if plan.reply_type == "vendor_email" else "a short internal answer")
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
- NEVER use square brackets or placeholders like [date] or [name].
- If a date or detail is not in the facts, simply leave it out and write around it
  (e.g. "your recent email", "at your earliest convenience"). Never invent one.
- Always produce the complete message. Never refuse or say it cannot be written.
- Never tell the vendor about gaps in our internal records or documents.
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
Also FAIL the draft for: any number, name or claim not supported by the facts; wrong maths;
placeholders like [date]; citing the wrong rule.
Approvals, sign-offs or confirmations a person must give (e.g. Finance Director approval under
Rule 2) are NOT errors in the draft and are NOT a reason to reject it. Put them in "needs_human"
instead. Only reject for problems the writer can fix in the text. Leave "needs_human" empty if none.

Reply with ONLY this JSON:
{{"verdict": "APPROVED or REJECTED", "checks": [{{"rule": "...", "result": "PASS or FAIL", "note": "..."}}], "fixes": ["..."], "needs_human": ["..."]}}"""
    review = run.call_json("validator", instruction, Review, f"validation of draft {attempt}",
                           why="Every output is checked before a human sees it")
    fails = [c for c in review.checks if c.result == "FAIL"]
    print(f"  Verdict: {review.verdict}  ({len(review.checks) - len(fails)} pass, {len(fails)} fail)")
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
        fails = [c for c in review.checks if c.result == "FAIL"]
        print("\n" + "#" * 64)
        print("  FINAL REVIEW - HUMAN DECISION REQUIRED")
        print("#" * 64)
        print(f"\n  REQUEST\n    {run.request}")
        print(f"\n  PLAN ({len(plan.steps)} steps): {plan.summary}")
        for s in plan.steps:
            print(f"    {s.id}. [{s.agent}] {s.task}")
        print(f"\n  VALIDATOR: {review.verdict}  "
              f"({len(review.checks) - len(fails)} checks passed, {len(fails)} failed)")
        for c in review.checks:
            print(f"    {'PASS' if c.result == 'PASS' else 'FAIL'}  {c.rule}" + (f" - {c.note}" if c.note else ""))
        if review.needs_human:
            print("\n  APPROVALS ONLY A PERSON CAN GIVE (confirm these before approving):")
            for n in review.needs_human:
                print(f"    [ ] {n}")
        print(f"\n  WARNINGS ({len(run.warnings)})")
        for w in run.warnings or ["none"]:
            print(f"    - {w}")
        print(f"\n  COST SO FAR: {run.calls} agent calls, {run.tokens} tokens, "
              f"{run.agent_seconds()}s of system time")
        message, note = split_output(draft)
        if note:
            print("\n  INTERNAL NOTE (for you only - never sent)")
            for line in note.splitlines():
                print(f"    {line}")
        print("\n  " + "-" * 60 + "\n  OUTPUT TO BE SENT\n  " + "-" * 60)
        for line in message.splitlines():
            print(f"  {line}")
        print("  " + "-" * 60)
        print("\n  [1] APPROVE  - release this output")
        print("  [2] SEND BACK - ask the agents to change something")
        print("  [3] DISAPPROVE - reject and close this request")
        choice = ""
        while choice not in ("1", "2", "3"):
            choice = ask("  Your decision (1/2/3): ", "3")

        if choice == "1":
            confirmed = "; ".join(review.needs_human) or "none required"
            run.log("human", "FINAL DECISION: approved", message,
                    why="Human reviewed plan, checks, warnings and output",
                    approvals_confirmed=confirmed, internal_note=note)
            return message
        if choice == "3":
            reason = ask("  Reason for disapproving: ", "no reason given")
            run.log("human", "FINAL DECISION: disapproved", reason, why="Human rejected the output")
            run.outcome = f"disapproved by human: {reason}"
            raise StopRun(run.outcome)

        note = ask("  What should be changed? ", "")
        if note:
            run.human_notes.append(note)
        run.log("human", "FINAL DECISION: sent back", note, why="Human asked for changes")
        print("\n  Sending back to the Executor with your instructions")
        draft = write_draft(run, plan, plan.steps[-1].task, "Human reviewer asked for changes",
                            fixes=f"- Instruction from the human reviewer: {note}", previous=draft)
        draft, review = review_loop(run, plan, draft)


def release(run: Run, text: str) -> Path:
    """The irreversible action. In this prototype, 'sending' = saving to the outbox folder."""
    outbox = BASE / "outbox"
    outbox.mkdir(exist_ok=True)
    path = outbox / f"{run.label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    path.write_text(text, encoding="utf-8")
    run.log("system", "RELEASED", str(path), why="Human approved")
    return path


# ================================================================
# Main
# ================================================================
def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "renewal"
    if label not in REQUESTS:
        print(f"Unknown request '{label}'. Choose one of: {', '.join(REQUESTS)}")
        return
    run = Run(REQUESTS[label], label)
    print("=" * 64)
    print(f"REQUEST ({label}): {run.request}")
    print("=" * 64)
    run.log("intake", "REQUEST RECEIVED", run.request)
    final = ""
    try:
        plan = make_plan(run)
        draft = execute_plan(run, plan)
        draft, review = review_loop(run, plan, draft)
        final = final_review(run, plan, draft, review)
        sent_to = release(run, final)
        run.outcome = f"approved by human and released to {sent_to.relative_to(BASE)}"
    except StopRun as stop:
        print(f"\nRUN STOPPED: {stop}")
    except Exception as err:          # never crash without leaving a trace
        run.outcome = f"crashed: {type(err).__name__}: {err}"
        run.log("system", "ERROR", run.outcome)
        print(f"\nRUN FAILED: {run.outcome}")

    print("\n" + "-" * 64)
    print(f"Outcome     : {run.outcome}")
    print(f"Agent calls : {run.calls}   Tokens: {run.tokens}   "
          f"System time: {run.agent_seconds()}s   Human time: {round(HUMAN_WAIT[0])}s")
    for w in run.warnings:
        print(f"Warning     : {w}")
    print(f"Trace saved : {run.save()}")


if __name__ == "__main__":
    main()
