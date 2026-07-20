import logging
from datetime import datetime, timedelta, timezone

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.config import JOB_MAX_AGE_DAYS
from pipeline.models import RawJob
from pipeline.sources.utils import html_to_text, infer_remote_type, normalize_job_type, normalize_location

logger = logging.getLogger(__name__)

# The v3 jobs endpoint only answers POST (GET returns 404 for every account).
# The list response has no description, so fresh jobs need a per-job v2 detail fetch.
WORKABLE_LIST_API = "https://apply.workable.com/api/v3/accounts/{slug}/jobs"
WORKABLE_DETAIL_API = "https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}"
TIMEOUT = 30.0
MAX_PAGES = 10


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _parse_job(job: dict, slug: str, company_name: str, company_url: str) -> RawJob:
    job_id = str(job["id"])
    shortcode = job.get("shortcode", "")
    title = job.get("title", "")
    loc = job.get("location", {}) or {}
    location_str = normalize_location(loc.get("city"), loc.get("region"))
    employment_type = job.get("type", "") or ""
    workplace = job.get("workplace", "") or ""

    parts = [job.get(key) or "" for key in ("description", "requirements", "benefits")]
    description_html = " ".join(p for p in parts if p)
    description_text = html_to_text(description_html)

    apply_url = f"https://apply.workable.com/{slug}/j/{shortcode}/"

    date_posted = _parse_iso(job.get("published", "")) or datetime.now(tz=timezone.utc)

    return RawJob(
        source_id=f"workable_{job_id}",
        source_name="workable",
        title=title,
        company=company_name,
        location=location_str,
        description_html=description_html,
        description_text=description_text,
        apply_url=apply_url,
        date_posted=date_posted,
        job_type=normalize_job_type(employment_type),
        remote_type=infer_remote_type(title, location_str, workplace),
        company_url=company_url,
    )


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _get_page(client: httpx.Client, slug: str, token: str | None) -> dict:
    url = WORKABLE_LIST_API.format(slug=slug)
    body: dict = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
    if token:
        body["token"] = token
    response = client.post(url, json=body)
    response.raise_for_status()
    return response.json()


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _get_detail(client: httpx.Client, slug: str, shortcode: str) -> dict:
    url = WORKABLE_DETAIL_API.format(slug=slug, shortcode=shortcode)
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def fetch_workable_jobs(slug: str, company_name: str) -> list[RawJob]:
    """Fetch recent jobs for a Workable-hosted company and return parsed RawJob objects."""
    logger.info("Fetching Workable jobs for %s (slug: %s)", company_name, slug)
    company_url = f"https://apply.workable.com/{slug}"

    listings: list[dict] = []
    token: str | None = None

    with httpx.Client(timeout=TIMEOUT) as client:
        for _ in range(MAX_PAGES):
            try:
                page = _get_page(client, slug, token)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.warning("Workable slug '%s' not found. Skipping.", slug)
                    return []
                logger.error("HTTP error fetching Workable slug '%s': %s", slug, exc)
                return []
            except httpx.TransportError as exc:
                logger.error("Network error fetching Workable slug '%s' after retries: %s", slug, exc)
                return []

            listings.extend(page.get("results", []))
            token = page.get("nextPage")
            if not token:
                break

        # Only detail-fetch jobs still inside the pipeline's age window (+1 day of slack);
        # main.py drops older jobs anyway, so this avoids one HTTP call per stale listing.
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=JOB_MAX_AGE_DAYS + 1)
        fresh = [
            j for j in listings
            if (_parse_iso(j.get("published", "")) or datetime.now(tz=timezone.utc)) >= cutoff
        ]

        jobs: list[RawJob] = []
        for listing in fresh:
            shortcode = listing.get("shortcode", "")
            try:
                detail = _get_detail(client, slug, shortcode)
                jobs.append(_parse_job(detail, slug, company_name, company_url))
            except Exception as exc:
                logger.warning("Failed to fetch Workable job %s from %s: %s", shortcode, slug, exc)

    logger.info(
        "Fetched %d recent jobs from %s (%d listed, %d within age window)",
        len(jobs), company_name, len(listings), len(fresh),
    )
    return jobs
