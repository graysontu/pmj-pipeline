"""python -m pipeline.linkedin prepare|publish - see pipeline/linkedin/run.py."""

import argparse
import logging
import sys
from pathlib import Path

from pipeline.linkedin.run import prepare, publish


def main() -> int:
    parser = argparse.ArgumentParser(description="Pick, draw and schedule one LinkedIn post.")
    parser.add_argument("step", choices=("prepare", "publish"))
    parser.add_argument("--dir", type=Path, default=Path("linkedin-posts"),
                        help="checkout of the linkedin-posts branch (default: ./linkedin-posts)")
    parser.add_argument("--state", type=Path, help="prepare: read this state.json instead of data/state.json")
    parser.add_argument("--no-ai", action="store_true", help="prepare: use the template opening line")
    parser.add_argument("--no-wait", action="store_true",
                        help="publish: don't wait for the image to be reachable (local dry runs)")
    parser.add_argument("--email-dir", type=Path, help="write the email's subject.txt and body.txt here")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.step == "prepare":
        return prepare(args.dir, state_path=args.state, use_ai=not args.no_ai, email_dir=args.email_dir)
    return publish(args.dir, wait_for_image=not args.no_wait, email_dir=args.email_dir)


if __name__ == "__main__":
    sys.exit(main())
