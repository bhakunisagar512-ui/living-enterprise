from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from living_enterprise.costs import call_cost_usd, classify_ai_error, usage_delta
from living_enterprise.schemas import Check, Plan, Review


class TestSchemas:
    def test_plan_cannot_use_unknown_agents(self):
        with pytest.raises(ValidationError):
            Plan.model_validate({"summary": "s", "reply_type": "vendor_email",
                                 "steps": [{"id": 1, "agent": "emailer", "task": "t", "why": "w"}]})

    def test_plan_needs_at_least_one_step(self):
        with pytest.raises(ValidationError):
            Plan.model_validate({"summary": "s", "reply_type": "vendor_email", "steps": []})

    @pytest.mark.parametrize("word,expected", [("PASS", "PASS"), ("ok", "PASS"), ("N/A", "N/A"),
                                               ("skipped", "N/A"), ("PARTIAL", "FAIL"),
                                               ("warning", "FAIL"), ("banana", "FAIL")])
    def test_doubtful_check_results_count_as_fail(self, word, expected):
        assert Check(rule="r", result=word).result == expected

    def test_unknown_verdict_counts_as_rejected(self):
        assert Review(verdict="maybe").verdict == "REJECTED"
        assert Review(verdict="approve").verdict == "APPROVED"

    def test_fixes_given_as_objects_are_kept_as_text(self):
        r = Review(verdict="REJECTED", fixes=[{"fix": "use 7%"}], needs_human="FD approval")
        assert r.fixes == ["use 7%"] and r.needs_human == ["FD approval"]


class TestCosts:
    def test_usage_delta_never_negative(self):
        before = dict.fromkeys(("prompt_tokens", "completion_tokens", "cached_prompt_tokens",
                                "cache_creation_tokens", "total_tokens"), 100)
        after = dict(before, prompt_tokens=1100, total_tokens=50)
        d = usage_delta(before, after)
        assert d.prompt_tokens == 1000 and d.total_tokens == 0

    def test_sonnet_and_haiku_prices(self):
        usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=100_000,
                                cached_prompt_tokens=0, cache_creation_tokens=0)
        assert call_cost_usd("anthropic/claude-sonnet-5", usage) == pytest.approx(3.0)
        assert call_cost_usd("anthropic/claude-haiku-4-5-20251001", usage) == pytest.approx(1.5)

    def test_unknown_model_priced_on_the_safe_side(self):
        usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=0)
        assert call_cost_usd("mystery-model", usage) == pytest.approx(2.0)


class TestAiErrors:
    @pytest.mark.parametrize("message,kind", [
        ("You have reached your specified API usage limits", "stop"),
        ("authentication_error: invalid x-api-key", "stop"),
        ("rate_limit_error", "retry"),
        ("Overloaded", "retry"),
        ("Connection reset", "retry"),
        ("something odd", "stop"),
    ])
    def test_classification(self, message, kind):
        assert classify_ai_error(Exception(message))[0] == kind

    def test_reason_never_contains_a_key(self):
        _, reason = classify_ai_error(Exception("weird failure with sk-ant-abcdefghijklmnop"))
        assert "sk-ant" not in reason
