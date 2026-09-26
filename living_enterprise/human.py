"""The human channel: how the system hands control to a person.

Any object with these two methods can be the human (terminal, web page, a test script):

    escalation(run, reason, stage, detail, options) -> (choice_key, note)
    final_review(run, review_summary)               -> ('approve' | 'sendback' | 'disapprove', text)

Time spent waiting for a person is recorded on the run and never counted against the time budget.
With no keyboard available, the terminal picks the safe option (abort / disapprove).
"""
import time
from typing import Protocol

from . import config


class HumanChannel(Protocol):
    def escalation(self, run, reason: str, stage: str, detail: str,
                   options: dict[str, str]) -> tuple[str, str]: ...

    def final_review(self, run, review: dict) -> tuple[str, str]: ...


def _timed_input(run, prompt: str) -> str:
    started = time.time()
    try:
        return input(prompt)
    finally:
        run.human_seconds += time.time() - started


def _ask(run, prompt: str, default: str = "") -> str:
    try:
        return _timed_input(run, prompt).strip()[:config.MAX_HUMAN_NOTE_CHARS]
    except EOFError:
        return default


def _ask_choice(run, options: dict[str, str]) -> str:
    keys = list(options)
    for i, k in enumerate(keys, 1):
        print(f"  [{i}] {options[k]}")
    safe = next((k for k in ("abort", "disapprove") if k in keys), keys[-1])
    while True:
        try:
            raw = _timed_input(run, "  Your decision: ").strip()
        except EOFError:
            return safe
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        print(f"  Please type a number from 1 to {len(keys)}.")


class ConsoleHuman:
    """Asks the person in the terminal."""

    def escalation(self, run, reason, stage, detail, options):
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
        choice = _ask_choice(run, options)
        note = _ask(run, "  Instructions for the agents: ") if choice == "retry" else ""
        print("=" * 64 + "\n")
        return choice, note

    def final_review(self, run, r):
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
            choice = _ask(run, "  Your decision (1/2/3): ", "3")
        if choice == "1":
            return "approve", ""
        if choice == "3":
            return "disapprove", _ask(run, "  Reason for disapproving: ", "no reason given")
        return "sendback", _ask(run, "  What should be changed? ", "")
