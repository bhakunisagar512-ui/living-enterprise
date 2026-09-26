"""Live exchange rates: a REAL external API with a recovery chain.

  1. Primary API (Frankfurter)      - up to 2 tries, 4 s timeout each
  2. Backup API (open.er-api.com)   - up to 2 tries, 4 s timeout each
  3. Last known good rate (cache)   - used but marked UNVERIFIED (warning)
  4. Nothing available              - FxError -> the Retriever reports MISSING -> a human decides

Every response is checked: HTTP status, size, JSON shape, number type and a sanity range.
Only a number and a sanitised date leave this module, so an API cannot inject text into a prompt.
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from . import config
from .context import active_run
from .security import safe_short_text

FX_LOCK = threading.Lock()      # CrewAI may run tool calls in parallel: fetch each rate once


class FxError(Exception):
    """One failed attempt. kind: timeout | http | network | malformed | implausible | unavailable."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind


def _event(action: str, detail: str, warning: bool = False) -> None:
    """Writes API activity to the active run's trace (warnings also reach the Final Review)."""
    print(f"     [api] {action}: {detail}")
    run = active_run()
    if run is None:
        return
    if action == "Unavailable":
        run.fx_unavailable = True
    if warning:
        run.warn(f"{action}: {detail}")
    else:
        run.log("tool:currency", action, detail)
    run.api_calls += action.startswith(("OK", "FAILED"))


def chaos_response(chaos: str, api_index: int, attempt: int):
    """Simulated failures for demos and tests. Returns (kind, detail) or None."""
    if chaos == "all" or (chaos == "partial" and api_index == 0):
        failures = [("http", "HTTP 500 Internal Server Error"),
                    ("timeout", f"no answer within {config.FX_TIMEOUT_S}s"),
                    ("malformed", "response was not valid JSON: '<html>Bad Gateway</html>'"),
                    ("implausible", "rate 0.012 is outside the sane range 50-150")]
        return failures[(api_index * config.FX_TRIES + attempt - 1) % len(failures)]
    return None


def fetch_json(url: str) -> dict:
    """The real network call (HTTPS only). Raises FxError for every kind of failure."""
    if not url.startswith("https://"):
        raise FxError("network", "only HTTPS endpoints are allowed")
    req = urllib.request.Request(url, headers={"User-Agent": "LivingEnterprise/1.0",
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=config.FX_TIMEOUT_S) as resp:
            raw = resp.read(config.FX_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise FxError("http", f"HTTP {e.code} {e.reason}") from None
    except (socket.timeout, TimeoutError):
        raise FxError("timeout", f"no answer within {config.FX_TIMEOUT_S}s") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise FxError("timeout", f"no answer within {config.FX_TIMEOUT_S}s") from None
        raise FxError("network", str(e.reason)[:120]) from None
    if len(raw) > config.FX_MAX_BYTES:
        raise FxError("malformed", f"response larger than {config.FX_MAX_BYTES} bytes")
    body = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise FxError("malformed", f"response was not valid JSON: {safe_short_text(body, 60)!r}") from None


def parse_rate(data, src: str, dst: str) -> tuple[float, str]:
    """Checks the response shape and returns (rate, date). Handles both providers' formats."""
    if not isinstance(data, dict):
        raise FxError("malformed", "response is not a JSON object")
    if data.get("result") == "error":
        raise FxError("http", f"API reported an error: {safe_short_text(data.get('error-type', 'unknown'))}")
    rates = data.get("rates")
    if not isinstance(rates, dict) or dst not in rates:
        keys = [safe_short_text(k, 20) for k in list(data)[:6]]
        raise FxError("malformed", f"no '{dst}' rate in response (keys: {keys})")
    rate = rates[dst]
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise FxError("malformed", f"rate is not a number: {safe_short_text(rate, 20)!r}")
    low, high = config.FX_SANE_RANGE.get((src, dst), (1e-9, 1e9))
    if not low <= float(rate) <= high:
        raise FxError("implausible", f"rate {rate} is outside the sane range {low}-{high}")
    date = safe_short_text(data.get("date") or data.get("time_last_update_utc") or "unknown date")
    return float(rate), date


def load_cache() -> dict:
    try:
        data = json.loads(config.FX_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(pair: str, rate: float, date: str, source: str) -> None:
    cache = load_cache()
    cache[pair] = {"rate": rate, "date": date, "source": source,
                   "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    config.FX_CACHE.parent.mkdir(parents=True, exist_ok=True)
    config.FX_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def get_rate(src: str, dst: str) -> dict:
    """Returns {'rate','date','source','verified'} or raises FxError.
    A rate fetched earlier in the same run is reused, so every figure in a reply matches."""
    pair = f"{src}_{dst}"
    with FX_LOCK:                       # a parallel call waits here, then reuses the rate
        run = active_run()
        if run is not None and pair in run.fx_rates:
            fx = run.fx_rates[pair]
            run.log("tool:currency", "REUSED", f"{pair} rate {fx['rate']} already fetched in this run")
            return fx
        fx = _fetch_rate_chain(src, dst, pair, getattr(run, "chaos", "off"))
        if run is not None:
            run.fx_rates[pair] = fx
        return fx


def _fetch_rate_chain(src: str, dst: str, pair: str, chaos: str) -> dict:
    for i, api in enumerate(config.FX_APIS):
        url = api["url"].format(src=src, dst=dst)
        for attempt in range(1, config.FX_TRIES + 1):
            started = time.time()
            try:
                injected = chaos_response(chaos, i, attempt)
                if injected:
                    if injected[0] == "timeout":
                        time.sleep(1)       # a short pause so the demo feels like a timeout
                    raise FxError(*injected)
                rate, date = parse_rate(fetch_json(url), src, dst)
                ms = round((time.time() - started) * 1000)
                _event("OK", f"{api['name']} try {attempt}: 1 {src} = {rate} {dst} (rates dated {date}, {ms} ms)")
                save_cache(pair, rate, date, api["name"])
                if i > 0 or attempt > 1:
                    _event("Recovered", f"live rate obtained from {api['name']} on try {attempt}", warning=True)
                return {"rate": rate, "date": date, "source": api["name"], "verified": True}
            except FxError as err:
                _event("FAILED", f"{api['name']} try {attempt}: {err}")
                if attempt < config.FX_TRIES:
                    time.sleep(0.5 * attempt)       # short back-off before retrying
    cached = load_cache().get(pair)
    if isinstance(cached, dict) and isinstance(cached.get("rate"), (int, float)):
        _event("Fallback", f"all live APIs failed; using last known {src}->{dst} rate {cached['rate']} "
               f"from {cached.get('fetched_at', '?')} (UNVERIFIED)", warning=True)
        return {"rate": float(cached["rate"]), "date": safe_short_text(cached.get("date", "?")),
                "source": f"cached copy of {safe_short_text(cached.get('source', '?'))} "
                          f"from {safe_short_text(cached.get('fetched_at', '?'))}",
                "verified": False}
    _event("Unavailable", f"all live APIs failed and no cached {src}->{dst} rate exists", warning=True)
    raise FxError("unavailable", "no live or cached exchange rate")
