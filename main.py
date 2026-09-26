"""
The Living Enterprise - command line.

    python main.py renewal | dispute | question | compare | unknown | injection
        --chaos            every exchange-rate API fails (tests recovery)
        --chaos-partial    only the primary API fails
        --budget 5         cost budget in rupees (default 40)
        --time-limit 60    system-time budget in seconds (default 300)
        --agent-timeout 5  seconds before one AI call is abandoned (default 120)

Web UI: python app.py   (then open http://127.0.0.1:8000)
The code lives in the living_enterprise/ package.
"""
import sys

from living_enterprise.cli import main

if __name__ == "__main__":
    sys.exit(main())
