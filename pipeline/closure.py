"""Reconcile the jobs we publish against what employer boards still list.

Verdicts are deliberately three-valued. "open" and "closed" both require a
complete, successful census of that job's board; everything else is "unknown",
which never removes anything. A job only leaves the feed after CLOSURE_STRIKES
consecutive confirmed-absent observations, so one bad census cannot delist
inventory even if it somehow returned a complete-looking empty board.

Retention rule this module preserves: a job stays in the feed until it is
confirmed closed or ages out via ACTIVE_DAYS. Not being rediscovered in a given
day's *discovery* batch means nothing here - discovery is filtered by age window
and per-company caps, the census is not.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

OPEN = "open"
CLOSED = "closed"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class JobVerdict:
    source_id: str
    company: str
    title: str
    apply_url: str
    published_at: str
    age_days: int
    verdict: str
    reason: str
    strikes: int = 0
    will_remove: bool = False
    # True when this job was already confirmed closed on an earlier run and is
    # therefore already out of the feed. Without this, every daily report would
    # keep re-proposing the whole historical backlog as new removals.
    already_closed: bool = False


@dataclass
class ClosureReport:
    verdicts: list[JobVerdict] = field(default_factory=list)
    boards_ok: int = 0
    boards_failed: int = 0
    board_errors: dict[str, str] = field(default_factory=dict)

    @property
    def checked(self) -> int:
        return len(self.verdicts)

    @property
    def open_jobs(self) -> list[JobVerdict]:
        return [v for v in self.verdicts if v.verdict == OPEN]

    @property
    def closed_jobs(self) -> list[JobVerdict]:
        return [v for v in self.verdicts if v.verdict == CLOSED]

    @property
    def unknown_jobs(self) -> list[JobVerdict]:
        return [v for v in self.verdicts if v.verdict == UNKNOWN]

    @property
    def already_closed_jobs(self) -> list[JobVerdict]:
        return [v for v in self.verdicts if v.already_closed]

    @property
    def removals(self) -> list[JobVerdict]:
        """Jobs this run would newly take out of the feed."""
        return [v for v in self.verdicts if v.will_remove]

    def summary_lines(self) -> list[str]:
        total = max(self.checked, 1)
        lines = [
            f"Boards:   {self.boards_ok} established, {self.boards_failed} unavailable",
            f"Checked:  {self.checked} published jobs",
            f"Open:     {len(self.open_jobs)} ({len(self.open_jobs) / total:.1%})",
            f"Closed:   {len(self.closed_jobs)} ({len(self.closed_jobs) / total:.1%}), "
            f"of which {len(self.already_closed_jobs)} already out of the feed",
            f"Unknown:  {len(self.unknown_jobs)} ({len(self.unknown_jobs) / total:.1%}) - retried, never removed",
            f"Removals: {len(self.removals)} newly proposed",
        ]
        return lines


def build_source_index(sources: dict[str, list[dict]]) -> dict[str, tuple[str, str]]:
    """company_name -> (source_type, slug), the mapping used to find a job's board."""
    index: dict[str, tuple[str, str]] = {}
    for source_type, entries in sources.items():
        for entry in entries or []:
            index[entry["company_name"]] = (source_type, entry["slug"])
    return index


def _age_days(published_at: str, now: datetime) -> int:
    try:
        return (now - datetime.fromisoformat(published_at).astimezone(timezone.utc)).days
    except (ValueError, TypeError):
        return -1


def reconcile(
    published_jobs: list[dict],
    censuses: dict[tuple[str, str], object],
    source_index: dict[str, tuple[str, str]],
    strikes_required: int = 2,
    now: datetime | None = None,
) -> ClosureReport:
    """Classify every published job as open, closed, or unknown.

    `published_jobs` are state records (each carries source_id, company,
    published_at and the existing closure bookkeeping). Nothing is mutated here;
    the caller decides whether to persist the verdicts.
    """
    now = now or datetime.now(tz=timezone.utc)
    report = ClosureReport()

    for census in censuses.values():
        if census.ok:
            report.boards_ok += 1
        else:
            report.boards_failed += 1
            report.board_errors[f"{census.source_type}/{census.slug}"] = census.error or "unknown"

    for job in published_jobs:
        source_id = job.get("source_id", "")
        company = job.get("company", "")
        published_at = job.get("published_at", "")
        base = {
            "source_id": source_id,
            "company": company,
            "title": job.get("title", ""),
            # Stored verbatim. Requisition-identifying query parameters are never
            # stripped or rebuilt, because the census matches on ID, not on URL.
            "apply_url": job.get("apply_url", ""),
            "published_at": published_at,
            "age_days": _age_days(published_at, now),
        }
        prior_strikes = int(job.get("closure_misses") or 0)
        already_closed = bool(job.get("closed_at"))

        ats, _, native_id = source_id.partition("_")
        if not native_id:
            report.verdicts.append(JobVerdict(
                **base, verdict=UNKNOWN,
                reason=f"source_id '{source_id}' has no requisition part to match",
            ))
            continue

        entry = source_index.get(company)
        if entry is None:
            report.verdicts.append(JobVerdict(
                **base, verdict=UNKNOWN,
                reason=f"'{company}' is no longer in sources.yaml, so its board cannot be checked",
            ))
            continue

        source_type, slug = entry
        if source_type != ats:
            report.verdicts.append(JobVerdict(
                **base, verdict=UNKNOWN,
                reason=(
                    f"job came from {ats} but '{company}' is now configured as {source_type}; "
                    "the original board is no longer polled"
                ),
            ))
            continue

        census = censuses.get((source_type, slug))
        if census is None:
            report.verdicts.append(JobVerdict(
                **base, verdict=UNKNOWN,
                reason=f"no census was attempted for {source_type}/{slug}",
            ))
            continue

        if not census.ok:
            report.verdicts.append(JobVerdict(
                **base, verdict=UNKNOWN, strikes=prior_strikes,
                reason=f"board {source_type}/{slug} unavailable: {census.error}",
            ))
            continue

        if native_id in census.open_ids:
            report.verdicts.append(JobVerdict(
                **base, verdict=OPEN,
                reason=f"requisition {native_id} still listed on {source_type}/{slug}",
            ))
            continue

        strikes = prior_strikes + 1
        report.verdicts.append(JobVerdict(
            **base, verdict=CLOSED, strikes=strikes, already_closed=already_closed,
            # A job already out of the feed is not a new removal, so it stops
            # being counted as one on every subsequent run.
            will_remove=strikes >= strikes_required and not already_closed,
            reason=(
                f"requisition {native_id} absent from a complete {source_type}/{slug} listing "
                f"of {len(census.open_ids)} open postings (consecutive misses: {strikes}"
                f"/{strikes_required})"
            ),
        ))

    return report


def apply_verdicts(state, report: ClosureReport, now: datetime | None = None) -> tuple[int, int]:
    """Persist verdicts to state. Returns (removed, reinstated).

    Only closure bookkeeping is touched. published_at, source_id, apply_url and
    every content field are left exactly as they were - a job that reopens keeps
    its original publication date rather than being republished as new.
    """
    now = now or datetime.now(tz=timezone.utc)
    removed = reinstated = 0

    for verdict in report.verdicts:
        if verdict.verdict == OPEN:
            if state.clear_closure(verdict.source_id, now):
                reinstated += 1
        elif verdict.verdict == CLOSED:
            if verdict.already_closed:
                # Still absent, still out of the feed. Rewriting its bookkeeping
                # every day would churn state.json (and the daily commit) for no
                # gain, so leave it alone.
                continue
            state.record_absent(verdict.source_id, verdict.strikes, verdict.reason, now)
            if verdict.will_remove:
                state.mark_closed(verdict.source_id, verdict.reason, now)
                removed += 1
        # UNKNOWN deliberately writes nothing: the strike counter must not
        # advance on an observation that proved nothing, and must not reset
        # either, so an intermittently flaky board still converges.

    return removed, reinstated
