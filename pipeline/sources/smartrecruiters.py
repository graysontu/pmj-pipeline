import logging
from datetime import datetime, timezone

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.models import RawJob
from pipeline.sources.utils import html_to_text, infer_remote_type, normalize_job_type, normalize_location

logger = logging.getLogger(__name__)

SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
TIMEOUT = 30.0

_DESCRIPTION_SECTIONS = (
    "companyDescription",
    "jobDescription",
    "qualifications",
    "additionalInformation",
)


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
    employment_type_id = type_of_employment.get("typeId", "") or ""

    sections = ((job.get("jobAd") or {}).get("sections") or {})
    description_html, description_text = _combine_sections(sections)

    apply_url = job.get("ref", "") or job.get("applyUrl", "")

    released_date = job.get("releasedDate", "")
    try:
        date_posted = datetime.fromisoformat(released_date.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        date_posted = datetime.now(tz=timezone.utc)

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
        job_type=normalize_job_type(employment_type_id),
        remote_type=infer_remote_type(title, location_str, employment_type_id),
        company_url=company_url,
    )


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _get_jobs_json(slug: str) -> dict:
    url = SMARTRECRUITERS_API.format(slug=slug)
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def fetch_smartrecruiters_jobs(slug: str, company_name: str) -> list[RawJob]:
    """Fetch all jobs for a SmartRecruiters-hosted company and return parsed RawJob objects."""
    logger.info("Fetching SmartRecruiters jobs for %s (slug: %s)", company_name, slug)
    company_url = f"https://careers.smartrecruiters.com/{slug}"

    try:
        data = _get_jobs_json(slug)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            logger.warning("SmartRecruiters slug '%s' not found. Skipping.", slug)
            return []
        logger.error("HTTP error fetching SmartRecruiters slug '%s': %s", slug, exc)
        return []
    except httpx.TransportError as exc:
        logger.error("Network error fetching SmartRecruiters slug '%s' after retries: %s", slug, exc)
        return []

    jobs_raw = data.get("content", [])
    jobs: list[RawJob] = []
    for job in jobs_raw:
        try:
            jobs.append(_parse_job(job, company_name, company_url))
        except Exception as exc:
            logger.warning(
                "Failed to parse SmartRecruiters job %s from %s: %s", job.get("id"), slug, exc
            )

    logger.info("Fetched %d jobs from %s", len(jobs), company_name)
    return jobs
