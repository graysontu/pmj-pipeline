import logging
from datetime import datetime, timezone

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.models import RawJob
from pipeline.sources.utils import html_to_text, infer_remote_type, normalize_job_type

logger = logging.getLogger(__name__)

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
TIMEOUT = 30.0


def _parse_job(job: dict, company_name: str, company_url: str) -> RawJob:
    job_id = str(job["id"])
    title = job.get("title", "")
    location = job.get("location", "") or ""
    employment_type = job.get("employmentType", "") or ""

    description_html = job.get("descriptionHtml", "") or ""
    description_text = html_to_text(description_html)

    apply_url = job.get("jobUrl", "")

    published_at = job.get("publishedAt", "")
    try:
        date_posted = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        date_posted = datetime.now(tz=timezone.utc)

    return RawJob(
        source_id=f"ashby_{job_id}",
        source_name="ashby",
        title=title,
        company=company_name,
        location=location or "Unknown",
        description_html=description_html,
        description_text=description_text,
        apply_url=apply_url,
        date_posted=date_posted,
        job_type=normalize_job_type(employment_type),
        remote_type=infer_remote_type(title, location, employment_type),
        company_url=company_url,
    )


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _get_jobs_json(slug: str) -> dict:
    url = ASHBY_API.format(slug=slug)
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def fetch_ashby_jobs(slug: str, company_name: str) -> list[RawJob]:
    """Fetch all jobs for an Ashby-hosted company and return parsed RawJob objects."""
    logger.info("Fetching Ashby jobs for %s (slug: %s)", company_name, slug)
    company_url = f"https://jobs.ashbyhq.com/{slug}"

    try:
        data = _get_jobs_json(slug)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            logger.warning("Ashby slug '%s' not found. Skipping.", slug)
            return []
        logger.error("HTTP error fetching Ashby slug '%s': %s", slug, exc)
        return []
    except httpx.TransportError as exc:
        logger.error("Network error fetching Ashby slug '%s' after retries: %s", slug, exc)
        return []

    jobs_raw = data.get("jobs", [])
    jobs: list[RawJob] = []
    for job in jobs_raw:
        try:
            jobs.append(_parse_job(job, company_name, company_url))
        except Exception as exc:
            logger.warning("Failed to parse Ashby job %s from %s: %s", job.get("id"), slug, exc)

    logger.info("Fetched %d jobs from %s", len(jobs), company_name)
    return jobs
