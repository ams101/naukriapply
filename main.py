"""Naukri auto-apply.

Usage:
    python main.py login              # one-time: log in manually, saves auth.json
    python main.py run --dry-run      # score everything, log decisions, never apply
    python main.py run                # live: apply to strong matches
"""
from __future__ import annotations

import argparse
import os
import sys

from naukri_autoapply.config import load_config
from naukri_autoapply.runner import Runner, login


def main() -> int:
    parser = argparse.ArgumentParser(description="Naukri Product Manager auto-apply")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="open a browser, log in manually, save the session to auth.json")
    run_p = sub.add_parser("run", help="search, score and apply")
    run_p.add_argument("--dry-run", action="store_true",
                       help="do everything except clicking Apply; log would_* decisions")
    args = parser.parse_args()

    # Windows consoles default to cp1252; make sure ₹ and Indian company names print.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cfg = load_config(args.config)
    if args.command == "login":
        login(cfg)
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. In PowerShell:  $env:ANTHROPIC_API_KEY = \"sk-ant-...\"")
        return 1
    Runner(cfg, dry_run=args.dry_run).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
