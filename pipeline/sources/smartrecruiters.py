import logging
from datetime import datetime, timedelta, timezone

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.config import JOB_MAX_AGE_DAYS
from pipeline.models import RawJob
from pipeline.sources.utils import html_to_text, infer_remote_type, normalize_job_type, normalize_location

logger = logging.getLogger(__name__)

# The postings list has no jobAd (description) and its `ref` is an API URL,
# so fresh jobs need a per-posting detail fetch for content and the real apply page.
SMARTRECRUITERS_LIST_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
SMARTRECRUITERS_DETAIL_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
TIMEOUT = 30.0
PAGE_LIMIT = 100
MAX_PAGES = 10

_DESCRIPTION_SECTIONS = (
    "companyDescription",
    "jobDescription",
    "qualifications",
    "additionalInformation",
)


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _combine_sections(sections: dict) -> tuple[str, str]:
    parts = []
    for key in _DESCRIPTION_SECTIONS:
        text = (sections.get(key) or {}).get("text", "") or ""
        if text:
            parts.append(text)
    description_html = " ".join(parts)
    return description_html, html_to_text(description_html)


def _parse_job(job: dict, company_name: str, company_url: str) -> RawJob:
    job_id = str(job["id"])
    title = job.get("name", "")
    loc = job.get("location", {}) or {}
    location_str = normalize_location(loc.get("city"), loc.get("region"))

    type_of_employment = (job.get("typeOfEmployment") or {})
    employment_type = type_of_employment.get("label", "") or type_of_employment.get("typeId", "") or ""

    sections = ((job.get("jobAd") or {}).get("sections") or {})
    description_html, description_text = _combine_sections(sections)

    apply_url = job.get("applyUrl", "") or job.get("postingUrl", "")

    released_date = job.get("releasedDate", "")
    date_posted = _parse_iso(released_date) or datetime.now(tz=timezone.utc)

    return RawJob(
        source_id=f"smartrecruiters_{job_id}",
        source_name="smartrecruiters",
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
def _get_page(client: httpx.Client, slug: str, offset: int) -> dict:
    url = SMARTRECRUITERS_LIST_API.format(slug=slug)
    response = client.get(url, params={"limit": PAGE_LIMIT, "offset": offset})
    response.raise_for_status()
    return response.json()


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _get_detail(client: httpx.Client, slug: str, posting_id: str) -> dict:
    url = SMARTRECRUITERS_DETAIL_API.format(slug=slug, posting_id=posting_id)
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def fetch_smartrecruiters_jobs(slug: str, company_name: str) -> list[RawJob]:
    """Fetch recent jobs for a SmartRecruiters-hosted company and return parsed RawJob objects."""
    logger.info("Fetching SmartRecruiters jobs for %s (slug: %s)", company_name, slug)
    company_url = f"https://careers.smartrecruiters.com/{slug}"

    listings: list[dict] = []

    with httpx.Client(timeout=TIMEOUT) as client:
        offset = 0
        for _ in range(MAX_PAGES):
            try:
                data = _get_page(client, slug, offset)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.warning("SmartRecruiters slug '%s' not found. Skipping.", slug)
                    return []
                logger.error("HTTP error fetching SmartRecruiters slug '%s': %s", slug, exc)
                return []
            except httpx.TransportError as exc:
                logger.error(
                    "Network error fetching SmartRecruiters slug '%s' after retries: %s", slug, exc
                )
                return []

            content = data.get("content", [])
            listings.extend(content)
            offset += len(content)
            if not content or offset >= data.get("totalFound", 0):
                break

        # Only detail-fetch postings still inside the pipeline's age window (+1 day of slack);
        # main.py drops older jobs anyway, so this avoids one HTTP call per stale listing.
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=JOB_MAX_AGE_DAYS + 1)
        fresh = [
            j for j in listings
            if (_parse_iso(j.get("releasedDate", "")) or datetime.now(tz=timezone.utc)) >= cutoff
        ]

        jobs: list[RawJob] = []
        for listing in fresh:
            posting_id = str(listing.get("id", ""))
            try:
                detail = _get_detail(client, slug, posting_id)
                jobs.append(_parse_job(detail, company_name, company_url))
            except Exception as exc:
                logger.warning(
                    "Failed to fetch SmartRecruiters job %s from %s: %s", posting_id, slug, exc
                )

    logger.info(
        "Fetched %d recent jobs from %s (%d listed, %d within age window)",
        len(jobs), company_name, len(listings), len(fresh),
    )
    return jobs
