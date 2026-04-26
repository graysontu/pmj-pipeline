import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

from lxml import etree

from pipeline.config import SITE_BASE_URL
from pipeline.geo import parse_location

logger = logging.getLogger(__name__)

FEED_PATH = Path(__file__).parent.parent / "output" / "feed.xml"


def _rfc822(dt: datetime) -> str:
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def generate_feed_xml(active_jobs: list[dict]) -> None:
    now = datetime.now(tz=timezone.utc)

    root = etree.Element("source")
    etree.SubElement(root, "publisher").text = "PropertyManagementJobs.us"
    etree.SubElement(root, "publisherurl").text = SITE_BASE_URL
    etree.SubElement(root, "lastBuildDate").text = _rfc822(now)

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
        etree.SubElement(job_el, "country").text = "US"
        etree.SubElement(job_el, "jobtype").text = job.get("job_type") or "fulltime"
        etree.SubElement(job_el, "category").text = etree.CDATA(job["category"])
        etree.SubElement(job_el, "description").text = etree.CDATA(job["rewritten_description"])
        etree.SubElement(job_el, "url").text = etree.CDATA(job["apply_url"])
        etree.SubElement(job_el, "date").text = _rfc822(date_posted_dt)
        etree.SubElement(job_el, "expiration_date").text = _rfc822(expiry)
        etree.SubElement(job_el, "remotetype").text = job.get("remote_type") or "onsite"
        etree.SubElement(job_el, "companyurl").text = etree.CDATA(job.get("company_url") or "")

    FEED_PATH.parent.mkdir(exist_ok=True)
    tree = etree.ElementTree(root)
    tree.write(str(FEED_PATH), xml_declaration=True, encoding="UTF-8", pretty_print=True)
    logger.info("Feed written to %s (%d jobs)", FEED_PATH, len(sorted_jobs))
