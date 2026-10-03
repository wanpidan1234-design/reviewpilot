#!/usr/bin/env python3
"""ReviewPilot source-distribution command line entry point.

Help and version handling never imports the workflow engine.
"""
import argparse
import importlib
import sys

VERSION = "0.1.0"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="reviewpilot",
        description="ReviewPilot: explicit pull-request review workflows.",
        epilog="Running a workflow can call AI services and modify GitHub. Read SECURITY.md first.",
    )
    parser.add_argument("--version", action="version", version=f"ReviewPilot {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one PR synchronously using config.yml")
    run.add_argument("--repo", required=True, help="configured repository slug")
    run.add_argument("--pr", required=True, type=int, help="positive pull-request number")
    commands.add_parser("labeler", help="run the optional labeler once for configured repositories")
    commands.add_parser("observer", help="run the optional lifecycle observer once")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or later is required")
    if args.command == "run" and args.pr <= 0:
        parser.error("--pr must be a positive integer")
    module_name = {"run": "engine", "labeler": "labeler", "observer": "observer"}[args.command]
    # Import only after explicit invocation and validation of command arguments.
    module = importlib.import_module(module_name)
    old_argv = sys.argv
    sys.argv = [module.__file__]
    if args.command == "run":
        sys.argv += ["--worker", "--repo", args.repo, "--pr", str(args.pr)]
    try:
        return module.main()
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    raise SystemExit(main())
