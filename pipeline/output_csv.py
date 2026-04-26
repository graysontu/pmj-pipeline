import csv
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent / "output"

HEADERS = [
    "job title", "job type", "company name", "job location", "description",
    "post length", "post state", "apply url", "apply email", "company url",
    "company logo", "office location", "location limits", "salary minimum",
    "salary maximum", "salary currency", "salary schedule", "highlighted",
    "sticky", "category name", "date posted",
]


def _format_date(date_str: str) -> str:
    try:
        return datetime.fromisoformat(date_str).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return date_str or ""


def generate_jobs_csv(jobs: list[dict], timestamp: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"jobs_{timestamp}.csv"

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(HEADERS)
        for job in jobs:
            salary_min = job.get("salary_min") or ""
            salary_max = job.get("salary_max") or ""
            salary_currency = job.get("salary_currency") or ("USD" if salary_min else "")
            salary_schedule = job.get("salary_schedule") or ("yearly" if salary_min else "")
            writer.writerow([
                job["title"],
                "fulltime",
                job["company"],
                "onsite",
                job["rewritten_description"],
                60,
                "published",
                job["apply_url"],
                "",
                job.get("company_url") or "",
                "",
                job.get("location", ""),
                "United States",
                salary_min,
                salary_max,
                salary_currency,
                salary_schedule,
                "false",
                "false",
                job["category"],
                _format_date(job.get("date_posted", "")),
            ])

    logger.info("CSV written to %s (%d jobs)", path, len(jobs))
    return path
