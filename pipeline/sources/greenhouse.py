import logging
import re
from datetime import datetime, timezone

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.geo import mentions_us_state, resolve_location
from pipeline.models import RawJob
from pipeline.sources.utils import html_to_text, infer_remote_type, unescape_html

logger = logging.getLogger(__name__)

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
TIMEOUT = 30.0

# Words that say nothing about *which* property a posting belongs to. An office
# only stands in for a posting's location when the two names share a word outside
# this set (and outside the employer's own name).
_GENERIC_WORDS = frozenset({
    "the", "and", "for", "apartment", "apartments", "apts", "home", "homes",
    "community", "communities", "property", "properties", "management", "residential",
    "living", "corporate", "corp", "office", "offices", "headquarters", "group", "llc",
    "inc", "company", "remote", "hybrid", "senior", "place", "park", "village", "plaza",
    "court", "square", "center", "centre", "north", "south", "east", "west", "unknown",
})


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) >= 3}


def _office_location(job: dict, location: str, company_name: str) -> str | None:
    """An office address for a posting whose location field is only a property name.

    Avanath puts a bare property name ("Northpointe") in the location field, which
    resolve_location rightly refuses. Greenhouse also lets the employer tag each
    requisition with an office, and offices carry an address ("Northpointe" ->
    "5441 N. Paramount Blvd, Long Beach, CA 90805"). The office is used only when
    its name shares a distinctive word with the location - the employer's own tag
    for the same property. Postings filed under a corporate office keep no location
    rather than inheriting headquarters: Avanath's "San Diego" posting is tagged to
    its Irvine office, and Fairstead's remote roles to New York. A location that
    names a state, or says remote, is left to the parser.
    """
    if resolve_location(location).state or mentions_us_state(location):
        return None
    if "remote" in location.lower():
        return None
    ignore = _GENERIC_WORDS | _words(company_name)
    wanted = _words(location) - ignore
    if not wanted:
        return None

    matches: list[tuple[str, str]] = []
    for office in job.get("offices") or []:
        address = (office.get("location") or "").strip()
        if not address or not wanted & (_words(office.get("name") or "") - ignore):
            continue
        state = resolve_location(address).state
        if state:
            matches.append((address, state))

    # Offices for the same property in two different states would be a guess.
    if not matches or len({state for _, state in matches}) > 1:
        return None
    return matches[0][0]


def _parse_job(job: dict, company_name: str, company_url: str | None) -> RawJob:
    job_id = str(job["id"])
    title = job.get("title", "")
    listed_location = (job.get("location", {}) or {}).get("name", "").strip() or "Unknown"
    location = _office_location(job, listed_location, company_name) or listed_location
    description_html = unescape_html(job.get("content", "") or "")
    description_text = html_to_text(description_html)
    apply_url = job.get("absolute_url", "")

    metadata = job.get("metadata", []) or []
    metadata_values = " ".join(str(m.get("value", "")) for m in metadata)

    first_published = job.get("first_published") or job.get("updated_at", "")
    try:
        date_posted = datetime.fromisoformat(first_published.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        date_posted = datetime.now(tz=timezone.utc)

    return RawJob(
        source_id=f"greenhouse_{job_id}",
        source_name="greenhouse",
        title=title,
        company=company_name,
        location=location,
        description_html=description_html,
        description_text=description_text,
        apply_url=apply_url,
        date_posted=date_posted,
        remote_type=infer_remote_type(title, listed_location, metadata_values),
        company_url=company_url,
    )


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _get_jobs_json(slug: str) -> dict:
    url = GREENHOUSE_API.format(slug=slug)
    with httpx.Client(timeout=TIMEOUT) as client:
        response = client.get(url, params={"content": "true"})
        response.raise_for_status()
        return response.json()


def fetch_greenhouse_jobs(slug: str, company_name: str) -> list[RawJob]:
    """Fetch all jobs for a Greenhouse-hosted company and return parsed RawJob objects."""
    logger.info("Fetching Greenhouse jobs for %s (slug: %s)", company_name, slug)

    try:
        data = _get_jobs_json(slug)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            logger.warning("Greenhouse slug '%s' not found. Skipping.", slug)
            return []
        logger.error("HTTP error fetching slug '%s': %s", slug, exc)
        return []
    except httpx.TransportError as exc:
        logger.error("Network error fetching slug '%s' after retries: %s", slug, exc)
        return []

    jobs_raw = data.get("jobs", [])
    company_url = f"https://boards.greenhouse.io/{slug}"

    jobs: list[RawJob] = []
    for job in jobs_raw:
        try:
            jobs.append(_parse_job(job, company_name, company_url))
        except Exception as exc:
            logger.warning("Failed to parse job %s from %s: %s", job.get("id"), slug, exc)

    logger.info("Fetched %d jobs from %s", len(jobs), company_name)
    return jobs
