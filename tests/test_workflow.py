"""End-to-end runs with fake agents: every recovery path, no API key, no cost."""
import json

from conftest import APPROVE, BAD, GOOD, REJECT, ScriptedHuman

from living_enterprise import config
from living_enterprise.workflow import run_request, split_output


def test_happy_path_is_approved_and_released(crew):
    human = ScriptedHuman()
    run = run_request("renewal", human=human)
    assert run.outcome.startswith("approved")
    sent = list(config.OUTBOX_DIR.glob("*.txt"))
    assert len(sent) == 1 and "INTERNAL NOTE" not in sent[0].read_text()      # the note is never sent
    assert human.reviews[0]["needs_human"] == ["Finance Director approval"]


def test_trace_is_saved_and_contains_no_key(crew, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-for-this-test-123456")
    crew.script["Executor"] = [GOOD + "\nkey sk-ant-secret-for-this-test-123456"]
    run_request("renewal", human=ScriptedHuman())
    (trace,) = config.RUNS_DIR.glob("trace_renewal_*.json")
    text = trace.read_text()
    assert "sk-ant-secret" not in text
    assert json.loads(text)["agent_calls"] == 4


def test_validator_rejects_then_executor_fixes(crew):
    crew.script["Executor"] = [BAD, GOOD]
    crew.script["Validator"] = [REJECT, APPROVE]
    run = run_request("renewal", human=ScriptedHuman())
    assert run.outcome.startswith("approved") and run.drafts == 2
    assert "Credit is Rs 20,000" in crew.prompts["Executor"][1]      # the fix was passed back


def test_two_rejections_escalate_to_the_human(crew):
    crew.script["Validator"] = [REJECT]
    human = ScriptedHuman(escalation=("abort", ""))
    run = run_request("renewal", human=human)
    assert run.outcome.startswith("aborted") and "rejected 2 drafts" in human.escalations[0]["reason"]


def test_missing_document_escalates_before_drafting(crew):
    crew.script["Retriever"] = ["- no contract found\nMISSING: contract for Globex Logistics"]
    human = ScriptedHuman(escalation=("abort", ""))
    run = run_request("unknown", human=human)
    assert run.outcome.startswith("aborted") and "Executor" not in crew.prompts


def test_malformed_json_is_repaired_once(crew):
    crew.script["Validator"] = ["not json at all", APPROVE]
    run = run_request("renewal", human=ScriptedHuman())
    assert run.outcome.startswith("approved")
    assert any("malformed" in w for w in run.warnings)


def test_malformed_twice_escalates(crew):
    crew.script["Planner"] = ["nonsense"]
    run = run_request("renewal", human=ScriptedHuman())
    assert run.outcome.startswith("aborted")


def test_approved_with_a_failed_check_is_overruled(crew):
    lying = '{"verdict": "APPROVED", "checks": [{"rule": "r", "result": "FAIL"}], "fixes": [], "needs_human": []}'
    crew.script["Validator"] = [lying, APPROVE]
    run = run_request("renewal", human=ScriptedHuman())
    assert any("treated as REJECTED" in w for w in run.warnings) and run.drafts == 2


def test_human_send_back_and_disapprove(crew):
    human = ScriptedHuman(final=[("sendback", "Be more polite"), ("disapprove", "not needed")])
    run = run_request("renewal", human=human)
    assert run.outcome == "disapproved by human: not needed"
    assert "Be more polite" in crew.prompts["Executor"][1]


def test_unknown_human_answer_is_treated_as_disapprove(crew):
    run = run_request("renewal", human=ScriptedHuman(final=("banana", "")))
    assert run.outcome.startswith("disapproved")


def test_temporary_ai_error_is_retried(crew):
    crew.script["Planner"] = [Exception("Overloaded"), crew.script["Planner"][0]]
    run = run_request("renewal", human=ScriptedHuman())
    assert run.outcome.startswith("approved") and any("retried" in w for w in run.warnings)


def test_permanent_ai_error_stops_cleanly(crew):
    crew.script["Planner"] = [Exception("You have reached your specified API usage limits")]
    run = run_request("renewal", human=ScriptedHuman())
    assert run.outcome.startswith("stopped") and "usage" in run.outcome


def test_cost_budget_escalates(crew):
    human = ScriptedHuman(escalation=("abort", ""))
    run = run_request("renewal", budget=0.01, human=human)
    assert human.escalations[0]["reason"] == "Cost budget reached" and run.outcome.startswith("aborted")


def test_agent_call_limit(crew, monkeypatch):
    monkeypatch.setattr(config, "MAX_AGENT_CALLS", 2)
    run = run_request("renewal", human=ScriptedHuman())
    assert "limit of 2 agent calls" in run.outcome


class TestPromptInjection:
    def test_attack_in_request_is_flagged_and_given_to_the_validator(self, crew):
        human = ScriptedHuman()
        run = run_request("injection", human=human)
        assert run.security_flags
        assert "SECURITY" in crew.prompts["Validator"][0]
        assert human.reviews[0]["security_flags"]          # the human sees it too
        assert any("prompt injection" in w for w in human.reviews[0]["warnings"])

    def test_request_is_fenced_as_data_in_every_prompt(self, crew):
        run_request("injection", human=ScriptedHuman())
        for role in ("Planner", "Retriever", "Executor", "Validator"):
            assert "<<<UNTRUSTED incoming request" in crew.prompts[role][0], role

    def test_normal_request_raises_no_flag(self, crew):
        run = run_request("renewal", human=ScriptedHuman())
        assert run.security_flags == [] and "SECURITY" not in crew.prompts["Validator"][0]

    def test_unknown_link_in_draft_is_flagged_for_the_human(self, crew):
        crew.script["Executor"] = [GOOD.replace("Nimbus", "Pay at https://evil.example/pay Nimbus")]
        human = ScriptedHuman()
        run_request("renewal", human=human)
        assert any("link" in w for w in human.reviews[0]["warnings"])


def test_split_output_keeps_the_note_private():
    msg, note = split_output("MESSAGE:\nHello\n**INTERNAL NOTE:** secret")
    assert msg == "Hello" and note == "secret"
