"""
Reliability test: runs the same request several times and measures how often it completes
and how often the answer is actually correct (P-03 advanced direction: "task completion rate
measured across repeated runs of the same input").

    python reliability.py              5 runs of 'renewal'
    python reliability.py renewal 3    3 runs

TEST ONLY: a scripted reviewer stands in for the human. It APPROVES at Final Review and ABORTS
any escalation (an escalation counts as "not completed"). The real app never does this.
Nothing is emailed; approved outputs go to the outbox folder as usual.
"""
import json
import re
import sys
import time
from datetime import datetime

import main as core

def accepts_12(text: str) -> bool:
    """True only if the text AGREES to 12% ("we accept the 12% increase"),
    not when it refuses it ("we cannot accept 12%", "unable to agree to 12%")."""
    for m in re.finditer(r"(?i)\b(accept\w*|agree\w*\s+to|confirm\w*)\b[^.]{0,40}?\b12\s?%", text):
        before = text[max(0, m.start() - 30):m.start()].lower()
        if not re.search(r"\b(not|cannot|can't|unable|decline\w*|won't|will not|reject\w*|no)\b", before):
            return True
    return False


# What a correct answer must (and must not) contain, per request
EXPECTED = {
    "renewal": {
        "must": {"7% counter-offer": r"\b7\s?%",
                 "Rs 20,000 SLA credit": r"20,000",
                 "May and July named": r"(?s)(?=.*\bMay\b)(?=.*\bJul)"},
        "must_not": {"wrong credit Rs 40,000": r"40,000",
                     "accepts 12%": accepts_12},
    },
    "question": {
        "must": {"2 months": r"(?i)\b(2|two)\b[^.]{0,30}\bmonths?\b",
                 "Rs 20,000": r"20,000"},
        "must_not": {"wrong credit Rs 40,000": r"40,000"},
    },
    "dispute": {
        "must": {"duplicate invoice INV-2609-07": r"INV-2609-07"},
        "must_not": {},
    },
}


class TestReviewer:
    """Stands in for the human during the test only."""
    def __init__(self):
        self.escalations = []

    def escalation(self, run, reason, stage, detail, options):
        self.escalations.append(f"{reason}: {detail.strip()[:80]}")
        return "abort", ""

    def final_review(self, run, review):
        return "approve", ""


def check(label: str, text: str) -> dict:
    spec = EXPECTED.get(label, {"must": {}, "must_not": {}})
    hit = lambda rule: rule(text or "") if callable(rule) else bool(re.search(rule, text or ""))
    missing = [name for name, rule in spec["must"].items() if not hit(rule)]
    wrong = [name for name, rule in spec["must_not"].items() if hit(rule)]
    return {"correct": bool(text) and not missing and not wrong, "missing": missing, "wrong": wrong}


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "renewal"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    if label not in core.REQUESTS:
        print(f"Unknown request '{label}'. Choose one of: {', '.join(core.REQUESTS)}")
        return
    core.quiet_crewai()
    results = []
    print(f"\nRELIABILITY TEST: {n} runs of '{label}'\n")
    for i in range(1, n + 1):
        reviewer = TestReviewer()
        core.HUMAN = reviewer
        released = {}
        core.EMIT = lambda ev: released.update(text=ev.get("released")) if ev["type"] == "done" else None
        print(f"\n########## RUN {i} of {n} ##########")
        started = time.time()
        run = core.run_request(label)
        completed = run.outcome.startswith("approved")
        verdict = check(label, released.get("text") or "") if completed else \
            {"correct": False, "missing": [], "wrong": []}
        results.append({
            "run": i, "completed": completed, "correct": verdict["correct"],
            "missing": verdict["missing"], "wrong": verdict["wrong"],
            "outcome": run.outcome, "escalations": reviewer.escalations,
            "drafts": run.drafts, "agent_calls": run.calls, "api_calls": run.api_calls,
            "tokens": run.tokens, "cost_rs": round(run.cost_rs(), 2),
            "seconds": round(time.time() - started), "warnings": len(run.warnings),
        })
        core.EMIT = None

    done = [r for r in results if r["completed"]]
    right = [r for r in results if r["correct"]]
    avg = lambda key, rows: round(sum(r[key] for r in rows) / len(rows), 1) if rows else 0
    summary = {
        "request": label, "runs": n, "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "completion_rate": f"{len(done)}/{n}", "correct_rate": f"{len(right)}/{n}",
        "first_draft_approved": f"{sum(1 for r in done if r['drafts'] == 1)}/{len(done) or 0}",
        "avg_cost_rs": avg("cost_rs", results), "total_cost_rs": round(sum(r["cost_rs"] for r in results), 2),
        "avg_seconds": avg("seconds", results), "avg_agent_calls": avg("agent_calls", results),
        "avg_tokens": avg("tokens", results), "results": results,
    }

    print("\n" + "=" * 72)
    print(f"RELIABILITY REPORT: '{label}' x {n}")
    print("=" * 72)
    print(f"{'Run':<5}{'Completed':<11}{'Correct':<9}{'Drafts':<8}{'Calls':<7}{'Cost':<11}{'Time':<7}Notes")
    for r in results:
        notes = "; ".join(r["missing"] + r["wrong"] + r["escalations"]) or ("" if r["completed"] else r["outcome"][:40])
        print(f"{r['run']:<5}{'yes' if r['completed'] else 'NO':<11}{'yes' if r['correct'] else 'NO':<9}"
              f"{r['drafts']:<8}{r['agent_calls']:<7}{'Rs %.2f' % r['cost_rs']:<11}{str(r['seconds']) + 's':<7}{notes}")
    print("-" * 72)
    print(f"Completion rate      : {summary['completion_rate']}")
    print(f"Correct answers      : {summary['correct_rate']}   (checked: {', '.join(EXPECTED.get(label, {}).get('must', {})) or 'none'})")
    print(f"Approved on 1st draft: {summary['first_draft_approved']}")
    print(f"Average per run      : Rs {summary['avg_cost_rs']}  |  {summary['avg_seconds']}s  |  "
          f"{summary['avg_agent_calls']} agent calls  |  {summary['avg_tokens']:.0f} tokens")
    print(f"Total cost           : Rs {summary['total_cost_rs']}")
    core.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = core.RUNS_DIR / f"reliability_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Report saved         : {path}")


if __name__ == "__main__":
    main()
