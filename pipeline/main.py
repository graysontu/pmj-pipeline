import argparse
import json
import logging
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from pipeline.ai_classifier import ClassifiedJob, batch_classify, summarize
from pipeline.ai_rewriter import RewrittenJob, batch_extract_salary, batch_rewrite, generate_quality_samples
from pipeline.config import JOB_MAX_AGE_DAYS, SITE_BASE_URL, SOURCES
from pipeline.indexing_api import notify_google
from pipeline.models import RawJob
from pipeline.output_csv import generate_jobs_csv
from pipeline.output_xml import generate_feed_xml
from pipeline.state import State
from pipeline.sources.ashby import fetch_ashby_jobs
from pipeline.sources.greenhouse import fetch_greenhouse_jobs
from pipeline.sources.lever import fetch_lever_jobs
from pipeline.sources.recruitee import fetch_recruitee_jobs
from pipeline.sources.smartrecruiters import fetch_smartrecruiters_jobs
from pipeline.sources.workable import fetch_workable_jobs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
NOTICE_PATH = Path(__file__).parent.parent / "NOTICE.txt"

FetchFn = Callable[[str, str], list[RawJob]]

DISPATCHERS: dict[str, FetchFn] = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
    "workable": fetch_workable_jobs,
    "recruitee": fetch_recruitee_jobs,
    "smartrecruiters": fetch_smartrecruiters_jobs,
}


def _save_json(path: Path, data: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    return text.strip("-")


def _job_url(job: RewrittenJob) -> str:
    return f"{SITE_BASE_URL}/jobs/{_slugify(job.title)}-at-{_slugify(job.company)}"


def _job_snapshot(job: RewrittenJob) -> dict:
    return {
        "source_id": job.source_id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "category": job.category,
        "apply_url": job.apply_url,
        "rewritten_description": job.rewritten_description,
        "date_posted": job.date_posted.isoformat(),
        "job_type": job.job_type or "fulltime",
        "remote_type": job.remote_type or "onsite",
        "company_url": job.company_url or "",
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "salary_currency": job.salary_currency,
        "salary_schedule": job.salary_schedule,
    }


def _avg_word_count(jobs: list[RewrittenJob]) -> int:
    from bs4 import BeautifulSoup
    if not jobs:
        return 0
    total = sum(
        len(BeautifulSoup(j.rewritten_description, "html.parser").get_text().split())
        for j in jobs
    )
    return total // len(jobs)


def _write_notice(reasons: list[str]) -> None:
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(NOTICE_PATH, "w", encoding="utf-8") as f:
        f.write(f"Pipeline notice - {timestamp}\n\n")
        for reason in reasons:
            f.write(f"- {reason}\n")
        f.write("\nCheck the Actions log for details.\n")
    logger.warning("NOTICE.txt written: %s", reasons)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PMJ Pipeline")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Rewrite only the first N kept jobs (for prompt testing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip rewrite API calls; use placeholder text. Useful for testing source pulls.",
    )
    parser.add_argument(
        "--skip-indexing",
        action="store_true",
        help="Skip Google Indexing API notifications. Use when JobBoardly handles indexing.",
    )
    return parser.parse_args()


def run() -> None:
    args = _parse_args()
    state = State()
    all_jobs: list[RawJob] = []
    fetch_counts: dict[str, int] = defaultdict(int)

    for source_type, sources in SOURCES.items():
        fetch_fn = DISPATCHERS.get(source_type)
        if fetch_fn is None:
            logger.error("No dispatcher for source type '%s'. Skipping.", source_type)
            continue

        for source in sources:
            slug = source["slug"]
            company_name = source["company_name"]
            try:
                jobs = fetch_fn(slug, company_name)
                all_jobs.extend(jobs)
                fetch_counts[source_type] += len(jobs)
            except Exception as exc:
                logger.error(
                    "Unexpected error fetching %s/%s: %s. Skipping.", source_type, slug, exc
                )

    print(f"\nFetched {len(all_jobs)} total jobs")
    for source_type, count in sorted(fetch_counts.items()):
        print(f"  {source_type}: {count} jobs")

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=JOB_MAX_AGE_DAYS)
    fresh_jobs = [j for j in all_jobs if j.date_posted.astimezone(timezone.utc) >= cutoff]
    stale_count = len(all_jobs) - len(fresh_jobs)
    if stale_count:
        logger.info(
            "Dropped %d jobs older than %d days. %d remain.",
            stale_count, JOB_MAX_AGE_DAYS, len(fresh_jobs),
        )
    all_jobs = fresh_jobs

    if not all_jobs:
        logger.info("No jobs fetched. Nothing to classify.")
        return

    DATA_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    raw_path = DATA_DIR / f"raw_jobs_{timestamp}.json"
    _save_json(raw_path, [job.model_dump(mode="json") for job in all_jobs])
    logger.info("Saved %d raw jobs to %s", len(all_jobs), raw_path)

    try:
        classified: list[ClassifiedJob] = batch_classify(all_jobs)
    except ValueError as exc:
        logger.error("Classification skipped: %s", exc)
        return
    except Exception as exc:
        logger.error("Unexpected error during classification: %s", exc)
        return

    kept = [j for j in classified if j.is_pm_job]
    summarize(classified)

    classified_path = DATA_DIR / f"classified_jobs_{timestamp}.json"
    _save_json(classified_path, [job.model_dump(mode="json") for job in kept])
    logger.info("Saved %d classified jobs to %s", len(kept), classified_path)

    if not kept:
        logger.info("No PM jobs to rewrite.")
        return

    jobs_to_rewrite = kept[:args.limit] if args.limit else kept
    if args.limit:
        logger.info("--limit %d: rewriting %d of %d kept jobs.", args.limit, len(jobs_to_rewrite), len(kept))

    if args.dry_run:
        logger.info("--dry-run: skipping rewrite API calls.")
        rewritten_jobs = [
            RewrittenJob(
                **job.model_dump(),
                rewritten_description="<p>Dry run placeholder.</p>",
                persona_used="none",
                structure_used="none",
            )
            for job in jobs_to_rewrite
        ]
        ban_warnings = 0
        rewrite_elapsed = 0.0
    else:
        try:
            rewrite_start = time.perf_counter()
            rewritten_jobs, ban_warnings = batch_rewrite(jobs_to_rewrite)
            rewrite_elapsed = time.perf_counter() - rewrite_start
        except ValueError as exc:
            logger.error("Rewrite skipped: %s", exc)
            return
        except Exception as exc:
            logger.error("Unexpected error during rewrite: %s", exc)
            return

    avg_words = _avg_word_count(rewritten_jobs)
    print(
        f"\nRewrote {len(rewritten_jobs)} jobs in {rewrite_elapsed:.0f}s. "
        f"Average length: {avg_words} words. "
        f"Banned phrase warnings: {ban_warnings}."
    )

    rewritten_path = DATA_DIR / f"rewritten_jobs_{timestamp}.json"
    _save_json(rewritten_path, [job.model_dump(mode="json") for job in rewritten_jobs])
    logger.info("Saved %d rewritten jobs to %s", len(rewritten_jobs), rewritten_path)

    if not args.dry_run:
        try:
            rewritten_jobs = batch_extract_salary(rewritten_jobs)
        except Exception as exc:
            logger.error("Salary extraction failed: %s. Continuing without salary data.", exc)

    generate_quality_samples(rewritten_jobs)

    now = datetime.now(tz=timezone.utc)
    new_jobs = [j for j in rewritten_jobs if not state.is_processed(j.source_id)]
    for job in new_jobs:
        state.mark_processed(job.source_id, now, _job_snapshot(job))
    state.save()

    active_jobs = state.get_active_jobs()
    generate_feed_xml(active_jobs)

    if new_jobs:
        generate_jobs_csv([_job_snapshot(j) for j in new_jobs], timestamp)
    else:
        logger.info("No new jobs this run. CSV skipped.")

    if not args.skip_indexing:
        for job in new_jobs:
            notify_google(_job_url(job))

    print(
        f"\nState: {len(new_jobs)} new jobs this run. "
        f"Active feed: {len(active_jobs)} jobs."
    )

    # Health checks - write NOTICE.txt if anything looks wrong, clear it if not
    notices = []
    if len(new_jobs) == 0 and len(kept) > 0:
        notices.append(
            f"Zero new jobs this run despite {len(kept)} PM jobs fetched. "
            "All may already be in state, or deduplication may have an issue."
        )
    rejection_rate = (len(classified) - len(kept)) / len(classified) if classified else 0
    if rejection_rate > 0.50:
        notices.append(
            f"High rejection rate: {rejection_rate:.0%} of classified jobs were rejected "
            f"({len(classified) - len(kept)} of {len(classified)}). "
            "Check classifier behavior or source feed quality."
        )
    if ban_warnings > 5:
        notices.append(
            f"{ban_warnings} banned phrase warnings in rewritten descriptions. "
            "Review quality_samples.html and consider prompt tuning."
        )

    if notices:
        _write_notice(notices)
    elif NOTICE_PATH.exists():
        NOTICE_PATH.unlink()
        logger.info("Previous NOTICE.txt cleared - run looks healthy.")


if __name__ == "__main__":
    run()
