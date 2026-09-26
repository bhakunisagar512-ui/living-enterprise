"""Measuring real cost from token usage, and sorting AI-provider errors."""
from types import SimpleNamespace

from . import config
from .security import redact

USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "cached_prompt_tokens",
                "cache_creation_tokens", "total_tokens")


def llm_usage(llm) -> dict:
    """The model object's running token totals (all calls made with it so far)."""
    try:
        summary = llm.get_token_usage_summary()
        return {f: getattr(summary, f, 0) or 0 for f in USAGE_FIELDS}
    except Exception:
        return {f: 0 for f in USAGE_FIELDS}


def usage_delta(before: dict, after: dict) -> SimpleNamespace:
    """CrewAI reports running totals, so one call's usage is 'after minus before'."""
    return SimpleNamespace(**{f: max(0, after[f] - before[f]) for f in USAGE_FIELDS})


def call_cost_usd(model: str, usage) -> float:
    """Real cost of one call. Unknown models are priced like Sonnet (the safe side)."""
    price_in, price_out, price_cache = config.PRICES.get(model.split("/")[-1],
                                                         config.PRICES["claude-sonnet-5"])
    tok = lambda field: getattr(usage, field, 0) or 0   # noqa: E731
    return (tok("prompt_tokens") * price_in
            + tok("cached_prompt_tokens") * price_cache
            + tok("cache_creation_tokens") * price_in * 1.25
            + tok("completion_tokens") * price_out) / 1_000_000


def cost_fx() -> float:
    """USD->INR for reporting cost: the last saved live rate if there is one, else a fixed value."""
    from .fx import load_cache
    return load_cache().get("USD_INR", {}).get("rate", config.COST_FX_FALLBACK)


def classify_ai_error(err: Exception) -> tuple[str, str]:
    """Sorts an AI-provider error into (kind, plain-English reason).
    'retry' = temporary, worth one more try; 'stop' = retrying cannot help.
    The reason never contains the raw error text of a key problem, and is always redacted."""
    text = str(err).lower()
    status = getattr(err, "status_code", None) or getattr(getattr(err, "response", None), "status_code", None)
    if "usage limit" in text or "credit balance" in text or "billing" in text:
        return "stop", ("Anthropic account usage/spend limit reached - raise it in the Claude Console "
                        "(Settings > Limits / Billing)")
    if status in (401, 403) or "authentication" in text or "api key" in text or "x-api-key" in text:
        return "stop", "Anthropic API key missing or invalid - check the ANTHROPIC_API_KEY setting"
    if status == 429 or "rate limit" in text or "rate_limit" in text:
        return "retry", "rate limited by the AI provider"
    if status in (500, 502, 503, 504, 529) or "overloaded" in text or "internal server error" in text:
        return "retry", "AI provider temporarily unavailable"
    if isinstance(err, (ConnectionError, TimeoutError)) or "connection" in text:
        return "retry", "network problem reaching the AI provider"
    return "stop", redact(f"unexpected AI error: {type(err).__name__}: {str(err)[:160]}")
