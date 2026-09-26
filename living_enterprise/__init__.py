"""The Living Enterprise: a multi-agent system that plans, delegates, recovers and keeps a human in charge.

Package layout
  config.py    settings, stopping conditions, prices, sample requests
  security.py  untrusted-content fencing, prompt-injection detection, secret redaction, output checks
  schemas.py   structured outputs (Plan, Review) with safe parsing
  tools.py     the tools agents can use (documents, SLA calculator, live currency converter)
  fx.py        live exchange-rate API with its recovery chain
  agents.py    the four AI agents (built on first use)
  costs.py     real cost from token usage; AI-provider error sorting
  human.py     the human channel (terminal by default)
  run.py       one run: trace, flags, budgets, the single place an AI is called
  workflow.py  plan -> execute -> validate -> human review -> release
  web.py       FastAPI server for the web UI
  cli.py       command line
"""
__version__ = "0.7.0"

from .config import REQUESTS  # noqa: E402
from .workflow import run_request  # noqa: E402

__all__ = ["REQUESTS", "run_request", "__version__"]
