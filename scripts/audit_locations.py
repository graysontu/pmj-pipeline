"""Before/after audit of job locations, plus a preview feed. Writes nothing live.

    python -m scripts.audit_locations                    # audit the published feed
    python -m scripts.audit_locations --all              # audit all of state, not just live
    python -m scripts.audit_locations --preview out.xml  # also write a preview feed

"Before" is read from output/feed.xml - what is actually published right now -
rather than from a previous version of the parser, so the report reflects reality
rather than a reconstruction.
"""

import argparse
import collections
import json
import re
import sys
from pathlib import Path

from lxml import etree

from pipeline.geo import resolve_location
from pipeline.output_xml import FEED_PATH, build_feed_tree
from pipeline.state import State

ROOT = Path(__file__).parent.parent


def _published_locations(feed_path: Path) -> dict[str, tuple[str, str]]:
    """source_id -> (city, state) as currently published."""
    if not feed_path.exists():
        return {}
    root = etree.parse(str(feed_path)).getroot()
    out = {}
    for job in root.findall("job"):
        ref = (job.findtext("referencenumber") or "").strip()
        out[ref] = ((job.findtext("city") or "").strip(), (job.findtext("state") or "").strip())
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit job location extraction")
    parser.add_argument("--feed", type=Path, default=FEED_PATH)
    parser.add_argument("--all", action="store_true",
                        help="Audit every job in state, not just the published feed.")
    parser.add_argument("--preview", type=Path, default=None,
                        help="Write a preview feed with corrected locations to this path.")
    parser.add_argument("--limit-examples", type=int, default=12)
    args = parser.parse_args()

    state = State()
    published = _published_locations(args.feed)
    jobs = state.get_active_jobs() if not args.all else state.get_published_jobs()
    if not args.all:
        jobs = [j for j in jobs if j["source_id"] in published]

    print(f"Auditing {len(jobs)} jobs ({'all state' if args.all else 'published feed'})\n")

    buckets: collections.Counter = collections.Counter()
    examples: dict[str, list] = collections.defaultdict(list)
    review: list = []

    for job in jobs:
        raw = job.get("location", "")
        before = published.get(job["source_id"], ("", ""))
        after_loc = resolve_location(raw)
        after = (after_loc.city, after_loc.state)

        if after_loc.needs_review:
            review.append((job, raw, before, after_loc))

        if before == after:
            bucket = "unchanged"
        elif not before[0] and after[0]:
            bucket = "city recovered"
        elif before[0] and not after[0]:
            bucket = "bad city removed (now blank, flagged)"
        elif before[0] != after[0] and before[1] == after[1]:
            bucket = "city corrected"
        else:
            bucket = "other change"

        buckets[bucket] += 1
        if len(examples[bucket]) < args.limit_examples:
            examples[bucket].append((job["company"], raw, before, after))

    print("=" * 78)
    print("BEFORE / AFTER")
    print("=" * 78)
    for bucket, n in buckets.most_common():
        print(f"  {bucket:42s} {n:4d}")

    for bucket in buckets:
        if bucket == "unchanged":
            continue
        print(f"\n--- {bucket} ---")
        for company, raw, before, after in examples[bucket]:
            b = f"{before[0]}, {before[1]}".strip(", ") or "(blank)"
            a = f"{after[0]}, {after[1]}".strip(", ") or "(blank)"
            print(f"  {company[:22]:24s} {raw[:40]!r:44s}")
            print(f"       {b!r}  ->  {a!r}")

    resolved = sum(1 for j in jobs if resolve_location(j.get("location", "")).resolved)
    print()
    print("=" * 78)
    print(f"  Resolved (city AND state): {resolved} of {len(jobs)} ({resolved/max(len(jobs),1):.1%})")
    print(f"  Flagged for review:        {len(review)}")
    print("=" * 78)

    if review:
        print("\nUNRESOLVED LOCATIONS - these publish with no city/state and need a human:")
        by_company = collections.Counter(j["company"] for j, _, _, _ in review)
        for company, n in by_company.most_common():
            print(f"  {n:3d}  {company}")
        print("\n  examples:")
        for job, raw, _, loc in review[:args.limit_examples]:
            print(f"    {job['company'][:20]:22s} {raw[:38]!r:42s} {loc.reason[:60]}")

    if args.preview:
        corrected = []
        for job in jobs:
            loc = resolve_location(job.get("location", ""))
            # Rewrite only the location string; every other field is untouched so
            # IDs, URLs, descriptions, salaries and dates carry through unchanged.
            copy = dict(job)
            copy["location"] = f"{loc.city}, {loc.state}" if loc.resolved else ""
            corrected.append(copy)
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        tree = build_feed_tree(corrected)
        tree.write(str(args.preview), xml_declaration=True, encoding="UTF-8", pretty_print=True)
        print(f"\nPreview feed written to {args.preview} ({len(corrected)} jobs). Nothing published.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
