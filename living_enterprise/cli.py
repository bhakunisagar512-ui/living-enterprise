"""Command line: python main.py <request> [--chaos | --chaos-partial] [--budget N] [--time-limit N] [--agent-timeout N]"""
import argparse
import sys

from . import config
from .agents import quiet_crewai
from .workflow import run_request


def build_parser() -> argparse.ArgumentParser:
    def positive(value: str) -> float:
        try:
            number = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be a number") from None
        if number <= 0:
            raise argparse.ArgumentTypeError("must be greater than 0")
        return number

    p = argparse.ArgumentParser(prog="main.py", description="The Living Enterprise: multi-agent request handling.")
    p.add_argument("request", nargs="?", default="renewal", choices=list(config.REQUESTS),
                   help="which sample request to run (default: renewal)")
    chaos = p.add_mutually_exclusive_group()
    chaos.add_argument("--chaos", action="store_true", help="make every exchange-rate API fail")
    chaos.add_argument("--chaos-partial", action="store_true", help="make only the primary API fail")
    p.add_argument("--budget", type=positive, help=f"cost budget in rupees (default {config.BUDGET_RS:.0f})")
    p.add_argument("--time-limit", type=positive, help=f"system-time budget in seconds (default {config.TIME_LIMIT_S})")
    p.add_argument("--agent-timeout", type=positive,
                   help=f"seconds before one AI call is abandoned (default {config.AGENT_TIMEOUT_S:.0f})")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    quiet_crewai()
    chaos = "all" if args.chaos else "partial" if args.chaos_partial else "off"
    run = run_request(args.request, chaos, args.budget, args.time_limit, args.agent_timeout)
    return 0 if run.outcome.startswith(("approved", "aborted", "disapproved")) else 1
