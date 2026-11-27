"""#869 Q2a offline CLI. No live connection or mutation commands."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from . import qualification_869_safety as safety
else:
    import qualification_869_safety as safety


COMMANDS = ("preflight", "plan", "run", "cleanup", "recover", "verify-clean")


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostics may echo arbitrary supplied arguments.
        self.exit(2, "Q869 INVALID_ARGUMENTS\n")


def main(argv=None):
    parser = SafeParser(description="Q869 offline planning only")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--approval-file", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    if args.command != "plan":
        print("Q869 LIVE_COMMAND_UNAVAILABLE", file=sys.stderr)
        return 3
    try:
        safety.require(args.approval_file is not None and args.manifest is not None,
                       "PLAN_INPUT_REQUIRED")
        approval = safety.validate_approval(safety.read_json(args.approval_file))
        safety.validate_configuration(os.environ, approval["target"], approval["routing"])
        safety.verify_source(approval, safety.observe_source(Path(__file__).resolve().parents[1]))
        now = datetime.now(timezone.utc)
        safety.authorize_window(approval, now)
        plan = safety.read_json(args.manifest)
        # A not-yet-bound plan can be rehearsed but never authorizes mutation.
        safety.validate_plan(approval, plan, bound=approval["manifest_digest"] is not None)
        if plan["probe"] is not None:
            safety.authorize_probe_window(approval, now)
        print(json.dumps(safety.run_record(approval, plan), sort_keys=True))
        return 0
    except Exception:
        # No arbitrary exception messages, file paths, connection strings or inputs.
        print("Q869 OFFLINE_PLAN_REJECTED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
