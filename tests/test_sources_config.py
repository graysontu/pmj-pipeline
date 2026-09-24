"""sources.yaml is edited by hand whenever a company is added, and a bad entry
fails quietly: the fetch loop skips an unknown source type, and closure detection
finds a job's board through a company_name -> board index. A duplicated
company_name would point one company's jobs at another company's census, where
they would be absent and eventually removed from the feed while still open."""

from collections import Counter
from pathlib import Path

from pipeline.census import CENSUS_FNS
from pipeline.config import SOURCES
from pipeline.main import DISPATCHERS

LOGO_DIR = Path(__file__).parent.parent / "output" / "logos"
LOGO_URL_PREFIX = "https://graysontu.github.io/pmj-pipeline/logos/"


def _entries():
    for source_type, entries in SOURCES.items():
        for entry in entries or []:
            yield source_type, entry


def test_every_source_type_can_be_fetched_and_censused():
    for source_type in SOURCES:
        assert source_type in DISPATCHERS, f"no fetcher for '{source_type}'"
        assert source_type in CENSUS_FNS, f"no census function for '{source_type}'"


def test_every_entry_has_a_slug_and_company_name():
    for source_type, entry in _entries():
        assert str(entry.get("slug", "")).strip(), f"{source_type} entry missing slug: {entry}"
        assert str(entry.get("company_name", "")).strip(), f"{source_type} entry missing company_name: {entry}"


def test_company_names_are_unique():
    counts = Counter(entry["company_name"] for _, entry in _entries())
    duplicates = [name for name, n in counts.items() if n > 1]
    assert not duplicates, f"company_name used more than once: {duplicates}"


def test_boards_are_listed_once():
    # Slugs are case-insensitive on every supported ATS.
    counts = Counter((source_type, entry["slug"].lower()) for source_type, entry in _entries())
    duplicates = [key for key, n in counts.items() if n > 1]
    assert not duplicates, f"board listed more than once: {duplicates}"


def test_logo_urls_point_at_published_files():
    for source_type, entry in _entries():
        url = entry.get("logo_url")
        if not url:
            continue
        assert url.startswith(LOGO_URL_PREFIX), f"{entry['company_name']}: unexpected logo host {url}"
        filename = url[len(LOGO_URL_PREFIX):]
        assert (LOGO_DIR / filename).is_file(), f"{entry['company_name']}: missing output/logos/{filename}"
