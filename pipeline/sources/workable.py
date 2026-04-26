import logging
from datetime import datetime, timezone

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.models import RawJob
from pipeline.sources.utils import html_to_text, infer_remote_type, normalize_job_type, normalize_location

logger = logging.getLogger(__name__)

WORKABLE_API = "https://apply.workable.com/api/v3/accounts/{slug}/jobs"
TIMEOUT = 30.0


def _parse_job(job: dict, company_name: str, company_url: str) -> RawJob:
    job_id = str(job["id"])
    title = job.get("title", "")
    loc = job.get("location", {}) or {}
    location_str = normalize_location(loc.get("city"), loc.get("region"))
    employment_type = job.get("employment_type", "") or ""

    description_html = job.get("description", "") or ""
    description_text = html_to_text(description_html)

    apply_url = job.get("url", "")

    created_at = job.get("created_at", "")
    try:
        date_posted = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        date_posted = datetime.now(tz=timezone.utc)

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
        remote_type=infer_remote_type(title, location_str, employment_type),
        company_url=company_url,
    )


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _get_page(slug: str, next_page: str | None) -> dict:
    url = WORKABLE_API.format(slug=slug)
    params: dict[str, str] = {}
    if next_page:
        params["next_page"] = next_page
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()


def fetch_workable_jobs(slug: str, company_name: str) -> list[RawJob]:
    """Fetch all paginated jobs for a Workable-hosted company and return parsed RawJob objects."""
    logger.info("Fetching Workable jobs for %s (slug: %s)", company_name, slug)
    company_url = f"https://apply.workable.com/{slug}"

    jobs_raw: list[dict] = []
    next_page: str | None = None

    while True:
        try:
            page = _get_page(slug, next_page)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning("Workable slug '%s' not found. Skipping.", slug)
                return []
            logger.error("HTTP error fetching Workable slug '%s': %s", slug, exc)
            return []
        except httpx.TransportError as exc:
            logger.error("Network error fetching Workable slug '%s' after retries: %s", slug, exc)
            return []

        jobs_raw.extend(page.get("results", []))
        next_page = (page.get("paging") or {}).get("next")
        if not next_page:
            break

    jobs: list[RawJob] = []
    for job in jobs_raw:
        try:
            jobs.append(_parse_job(job, company_name, company_url))
        except Exception as exc:
            logger.warning("Failed to parse Workable job %s from %s: %s", job.get("id"), slug, exc)

    logger.info("Fetched %d jobs from %s", len(jobs), company_name)
    return jobs
