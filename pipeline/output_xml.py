import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lxml import etree

from pipeline.config import SITE_BASE_URL
from pipeline.geo import parse_location

logger = logging.getLogger(__name__)

FEED_PATH = Path(__file__).parent.parent / "output" / "feed.xml"


class FeedGuardTripped(Exception):
    """Raised instead of publishing a feed that looks like the product of a
    failed or incomplete run. The existing feed on disk is left untouched, so
    the last good feed stays live."""


def _iso_date(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def count_feed_jobs(path: Path = FEED_PATH) -> int | None:
    """Job count in a feed already on disk, or None if there is no readable feed
    to compare against (first run, or a corrupt file)."""
    if not path.exists():
        return None
    try:
        tree = etree.parse(str(path))
    except etree.XMLSyntaxError as exc:
        logger.warning("Existing feed at %s is not parseable (%s). Skipping guard.", path, exc)
        return None
    return len(tree.getroot().findall("job"))


def _check_guard(
    new_count: int,
    previous_count: int | None,
    max_removal_fraction: float,
    allow_mass_removal: bool,
    path: Path,
) -> None:
    if previous_count is None or previous_count == 0:
        return

    if new_count == 0:
        raise FeedGuardTripped(
            f"refusing to publish an empty feed: {previous_count} jobs are currently live. "
            f"The last good feed at {path} was left in place."
        )

    removed = previous_count - new_count
    if removed <= 0:
        return

    fraction = removed / previous_count
    if fraction > max_removal_fraction and not allow_mass_removal:
        raise FeedGuardTripped(
            f"refusing to remove {removed} of {previous_count} jobs ({fraction:.1%}) in one run; "
            f"the limit is {max_removal_fraction:.0%}. The last good feed at {path} was left in "
            "place. If this drop is expected, re-run with the mass-removal override."
        )
    if fraction > max_removal_fraction:
        logger.warning(
            "Mass-removal override in effect: removing %d of %d jobs (%.1f%%).",
            removed, previous_count, fraction * 100,
        )


def build_feed_tree(active_jobs: list[dict]) -> etree._ElementTree:
    now = datetime.now(tz=timezone.utc)

    root = etree.Element("source")
    etree.SubElement(root, "publisher").text = "PropertyManagementJobs.us"
    etree.SubElement(root, "publisherurl").text = SITE_BASE_URL
    etree.SubElement(root, "lastBuildDate").text = now.strftime("%Y-%m-%d")

    sorted_jobs = sorted(active_jobs, key=lambda j: j["published_at"], reverse=True)

    for job in sorted_jobs:
        published_at = datetime.fromisoformat(job["published_at"]).astimezone(timezone.utc)
        expiry = published_at + timedelta(days=60)
        city, state = parse_location(job.get("location", ""))

        try:
            date_posted_dt = datetime.fromisoformat(job["date_posted"]).astimezone(timezone.utc)
        except (ValueError, TypeError, KeyError):
            date_posted_dt = published_at

        job_el = etree.SubElement(root, "job")
        etree.SubElement(job_el, "referencenumber").text = etree.CDATA(job["source_id"])
        etree.SubElement(job_el, "title").text = etree.CDATA(job["title"])
        etree.SubElement(job_el, "company").text = etree.CDATA(job["company"])
        etree.SubElement(job_el, "city").text = etree.CDATA(city)
        etree.SubElement(job_el, "state").text = etree.CDATA(state)
        if city or state:
            etree.SubElement(job_el, "country").text = "US"
        etree.SubElement(job_el, "jobtype").text = job.get("job_type") or "fulltime"
        etree.SubElement(job_el, "category").text = etree.CDATA(job["category"])
        etree.SubElement(job_el, "description").text = etree.CDATA(job["rewritten_description"])
        etree.SubElement(job_el, "url").text = etree.CDATA(job["apply_url"])
        etree.SubElement(job_el, "date").text = _iso_date(date_posted_dt)
        etree.SubElement(job_el, "expiration_date").text = _iso_date(expiry)
        etree.SubElement(job_el, "remotetype").text = job.get("remote_type") or "onsite"
        etree.SubElement(job_el, "companyurl").text = etree.CDATA(job.get("company_url") or "")
        if job.get("company_logo_url"):
            etree.SubElement(job_el, "companylogo").text = etree.CDATA(job["company_logo_url"])
        if job.get("salary_min") is not None and job.get("salary_max") is not None:
            etree.SubElement(job_el, "salary_min").text = str(int(job["salary_min"]))
            etree.SubElement(job_el, "salary_max").text = str(int(job["salary_max"]))
            etree.SubElement(job_el, "salary_currency").text = job.get("salary_currency") or "USD"
            etree.SubElement(job_el, "salary_schedule").text = job.get("salary_schedule") or "yearly"

    return etree.ElementTree(root)


def generate_feed_xml(
    active_jobs: list[dict],
    path: Path = FEED_PATH,
    max_removal_fraction: float | None = None,
    allow_mass_removal: bool = False,
) -> None:
    """Write the feed. When max_removal_fraction is set, a run that would empty
    the feed or drop more than that share of it raises FeedGuardTripped and
    writes nothing, leaving the previous feed live.

    The guard is opt-in so that deliberately small feeds (scripts/make_test_feed.py)
    still work unguarded.
    """
    if max_removal_fraction is not None:
        _check_guard(
            len(active_jobs), count_feed_jobs(path), max_removal_fraction, allow_mass_removal, path
        )

    tree = build_feed_tree(active_jobs)

    # Write to a sibling temp file and rename, so a crash mid-write cannot leave
    # a truncated feed being served.
    path.parent.mkdir(exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tree.write(str(tmp_path), xml_declaration=True, encoding="UTF-8", pretty_print=True)
    os.replace(tmp_path, path)

    logger.info("Feed written to %s (%d jobs)", path, len(active_jobs))
