"""Check every published job against its employer board. Reports by default.

    python -m scripts.check_closures                  # dry run, writes nothing
    python -m scripts.check_closures --apply          # persist removals + feed
    python -m scripts.check_closures --apply --allow-mass-removal --strikes 1

The third form is the one-time backlog cleanup: it removes jobs on a single
confirmed absence and bypasses the mass-removal guard. Use it only for a removal
list that has actually been reviewed.
"""

import argparse
import collections
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.census import run_census
from pipeline.closure import CLOSED, OPEN, UNKNOWN, apply_verdicts, build_source_index, reconcile
from pipeline.config import CLOSURE_STRIKES, MAX_FEED_REMOVAL_FRACTION, SOURCES
from pipeline.output_xml import FEED_PATH, FeedGuardTripped, count_feed_jobs, generate_feed_xml
import pipeline.state as state_module
from pipeline.state import ACTIVE_DAYS, State

ROOT = Path(__file__).parent.parent
AGE_BUCKETS = [(0, 7), (8, 14), (15, 21), (22, 30), (31, 45), (46, 60), (61, 10**6)]


def _bucket(age_days: int) -> str:
    for lo, hi in AGE_BUCKETS:
        if lo <= age_days <= hi:
            return f"{lo}-{hi}d" if hi < 10**6 else f"{lo}d+"
    return "unknown"


def _stratified_sample(removals: list, count: int) -> list:
    """Spread the sample across ATS platforms and age buckets rather than showing
    15 rows from whichever company happens to sort first."""
    groups: dict[tuple[str, str], list] = collections.defaultdict(list)
    for verdict in removals:
        ats = verdict.source_id.partition("_")[0]
        groups[(ats, _bucket(verdict.age_days))].append(verdict)

    for group in groups.values():
        group.sort(key=lambda v: v.age_days)

    ordered_keys = sorted(groups, key=lambda k: (-len(groups[k]), k))
    sample: list = []
    while len(sample) < count and any(groups[k] for k in ordered_keys):
        for key in ordered_keys:
            if groups[key]:
                sample.append(groups[key].pop(0))
                if len(sample) >= count:
                    break
    return sample


def _print_samples(removals: list, count: int) -> None:
    sample = _stratified_sample(removals, count)
    print(f"\n{'=' * 78}")
    print(f"{len(sample)} REPRESENTATIVE PROPOSED REMOVALS (of {len(removals)} total)")
    print(f"{'=' * 78}")
    for i, v in enumerate(sample, 1):
        ats = v.source_id.partition("_")[0]
        print(f"\n{i:2d}. {v.title}")
        print(f"    company    {v.company}")
        print(f"    platform   {ats}")
        print(f"    reference  {v.source_id}")
        print(f"    published  {v.published_at[:10]}  ({v.age_days} days in feed)")
        print(f"    apply url  {v.apply_url}")
        print(f"    evidence   {v.reason}")


def _print_breakdowns(report) -> None:
    by_ats: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    by_company: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    by_age: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for v in report.verdicts:
        ats = v.source_id.partition("_")[0]
        by_ats[ats][v.verdict] += 1
        by_company[v.company][v.verdict] += 1
        by_age[_bucket(v.age_days)][v.verdict] += 1

    print("\nBy employer platform:")
    for ats in sorted(by_ats):
        c = by_ats[ats]
        print(
            f"  {ats:16s} checked {sum(c.values()):4d} | open {c[OPEN]:4d} | "
            f"closed {c[CLOSED]:4d} | unknown {c[UNKNOWN]:3d}"
        )

    print("\nBy time in feed:")
    for lo, hi in AGE_BUCKETS:
        key = f"{lo}-{hi}d" if hi < 10**6 else f"{lo}d+"
        c = by_age.get(key)
        if not c:
            continue
        total = sum(c.values())
        print(
            f"  {key:8s} checked {total:4d} | open {c[OPEN]:4d} | closed {c[CLOSED]:4d} "
            f"({c[CLOSED] / total:5.1%}) | unknown {c[UNKNOWN]:3d}"
        )

    print("\nTop companies by confirmed-closed count:")
    ranked = sorted(by_company.items(), key=lambda kv: -kv[1][CLOSED])
    for company, c in ranked[:15]:
        if not c[CLOSED]:
            break
        print(f"  {c[CLOSED]:4d} closed / {c[OPEN]:4d} open   {company}")

    if report.unknown_jobs:
        print("\nUnknown (left in the feed, retried next run):")
        reasons = collections.Counter(v.reason for v in report.unknown_jobs)
        for reason, n in reasons.most_common():
            print(f"  {n:4d}  {reason}")

    if report.board_errors:
        print(f"\nBoards that could not be established ({len(report.board_errors)}):")
        for board, error in sorted(report.board_errors.items()):
            print(f"  {board}: {error}")


def _backup(backup_dir: Path, feed_path: Path, state_path: Path) -> list[Path]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written = []
    for src, name in ((feed_path, f"feed.{stamp}.xml"), (state_path, f"state.{stamp}.json")):
        if src.exists():
            dest = backup_dir / name
            shutil.copy2(src, dest)
            written.append(dest)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Employer job-availability check")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="Report only; write nothing. Default.")
    mode.add_argument("--apply", action="store_true",
                      help="Persist closures to state and rewrite the feed.")
    parser.add_argument("--strikes", type=int, default=CLOSURE_STRIKES,
                        help=f"Consecutive confirmed absences before removal (default {CLOSURE_STRIKES}).")
    parser.add_argument("--allow-mass-removal", action="store_true",
                        help="Bypass the feed mass-removal guard for this run only.")
    parser.add_argument("--samples", type=int, default=15,
                        help="How many representative proposed removals to print (default 15).")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "backups",
                        help="Where --apply copies the current feed and state first.")
    parser.add_argument("--json", type=Path, default=None,
                        help="Also write the full verdict list to this JSON path.")
    parser.add_argument("--state", type=Path, default=None,
                        help="Read a state.json snapshot from here instead of data/state.json.")
    parser.add_argument("--feed", type=Path, default=None,
                        help="Compare against (and with --apply, write) this feed instead of output/feed.xml.")
    args = parser.parse_args()

    # Pointing at snapshots lets a dry run reconcile a copy of production state
    # without touching the working tree.
    if args.state:
        state_module.STATE_PATH = args.state
    state_path = state_module.STATE_PATH
    feed_path = args.feed or FEED_PATH

    state = State()
    published = state.get_published_jobs()
    live_feed_count = count_feed_jobs(feed_path)

    print(f"Published jobs inside the {ACTIVE_DAYS}-day window: {len(published)}")
    print(f"Jobs in the live feed on disk: {live_feed_count}")
    print(f"Already marked closed in state: {state.closed_count()}")
    print(f"Strikes required for removal: {args.strikes}")
    print("\nCensusing employer boards...")

    censuses = run_census(SOURCES)
    report = reconcile(published, censuses, build_source_index(SOURCES), strikes_required=args.strikes)

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for line in report.summary_lines():
        print(f"  {line}")

    # Reconcile the arithmetic explicitly, so a count that does not add up is
    # visible rather than inferred.
    accounted = len(report.open_jobs) + len(report.closed_jobs) + len(report.unknown_jobs)
    print(
        f"\n  Arithmetic: {len(report.open_jobs)} open + {len(report.closed_jobs)} closed "
        f"+ {len(report.unknown_jobs)} unknown = {accounted} of {report.checked} checked"
    )
    if accounted != report.checked:
        print("  WARNING: verdicts do not account for every checked job.")

    under_threshold = len(report.closed_jobs) - len(report.removals) - len(report.already_closed_jobs)
    in_feed_now = live_feed_count if live_feed_count is not None else report.checked
    print(
        f"  Feed: {in_feed_now} now - {len(report.removals)} newly removed "
        f"= {in_feed_now - len(report.removals)} after this run"
    )
    print(
        f"              composed of {len(report.open_jobs)} confirmed open, "
        f"{len(report.unknown_jobs)} unknown (retained), "
        f"{under_threshold} closed but under the {args.strikes}-strike threshold"
    )

    _print_breakdowns(report)

    if report.removals:
        _print_samples(report.removals, args.samples)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([v.__dict__ for v in report.verdicts], f, indent=2)
        print(f"\nFull verdict list written to {args.json}")

    if not args.apply:
        print(f"\n{'=' * 78}")
        print("DRY RUN - nothing was written. No state change, no feed change.")
        print(f"Re-run with --apply to remove the {len(report.removals)} jobs above.")
        print("=" * 78)
        return 0

    backups = _backup(args.backup_dir, feed_path, state_path)
    for path in backups:
        print(f"\nBacked up {path}")

    removed, reinstated = apply_verdicts(state, report)
    active_jobs = state.get_active_jobs()

    try:
        generate_feed_xml(
            active_jobs,
            path=feed_path,
            max_removal_fraction=MAX_FEED_REMOVAL_FRACTION,
            allow_mass_removal=args.allow_mass_removal,
        )
    except FeedGuardTripped as exc:
        # State was only mutated in memory, so exiting here rolls the whole
        # operation back and leaves the live feed untouched.
        print(f"\nFEED GUARD TRIPPED - nothing written: {exc}", file=sys.stderr)
        return 1

    state.save()
    print(f"\nRemoved {removed} confirmed-closed jobs. Reinstated {reinstated}.")
    print(f"Feed now contains {len(active_jobs)} jobs (was {live_feed_count}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
