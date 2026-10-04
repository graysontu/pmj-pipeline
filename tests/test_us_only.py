"""No non-US job may be published. PeakMade's "Kingston, Ontario" was, once.

Two layers enforce it: each fetcher drops listings whose ATS-supplied country is
explicitly not the US, and main.py drops any job whose location text names a
foreign country or Canadian province (Greenhouse supplies nothing else).
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pipeline.geo import is_foreign_country, listed_outside_us, names_foreign_country


# --- location text ------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "Kingston, Ontario",            # PeakMade, published 2026 before this check existed
    "Toronto, ON",
    "Vancouver, British Columbia, Canada",
    "Remote (Canada)",
    "Canada - Toronto",
    "London, England",
    "London, U.K.",
    "Manila, Philippines",
    "Mexico City, Mexico",
    "Paris, France",
    # "WA" reads as Washington, but the country position says Australia.
    "Perth, WA, Australia",
])
def test_foreign_locations_are_caught(raw):
    assert names_foreign_country(raw)


@pytest.mark.parametrize("raw", [
    # US towns named after countries or provinces.
    "Lebanon, PA",
    "Mexico, MO",
    "Paris, TX",
    "Ontario, California",
    "Ontario, CA",
    # Georgia is a state here.
    "Georgia",
    "Atlanta, Georgia",
    "San Juan, Puerto Rico",
    "Chicago, Illinois, United States (Remote)",
    "REMOTE - Domestic US",
    # Says nothing about a country: left for the location parser to flag.
    "Remote",
    "Unknown",
    "Tinsley on the Park",
    "",
])
def test_us_and_unknown_locations_are_kept(raw):
    assert not names_foreign_country(raw)


def test_a_requisition_also_listed_in_the_us_is_kept():
    assert not names_foreign_country("Toronto, ON; Seattle, WA")
    assert not names_foreign_country("Seattle, WA; Toronto, ON")


# --- ATS country fields -------------------------------------------------------

@pytest.mark.parametrize("value", ["US", "us", "USA", "United States", "U.S.", "United States of America"])
def test_us_country_values(value):
    assert not is_foreign_country(value)


@pytest.mark.parametrize("value", ["GB", "CA", "Canada", "ES", "Mexico"])
def test_foreign_country_values(value):
    assert is_foreign_country(value)


def test_a_missing_country_is_not_evidence_either_way():
    assert not listed_outside_us()
    assert not listed_outside_us(None, "")
    assert listed_outside_us("GB")
    # Several places: kept when any one of them is in the US.
    assert not listed_outside_us("CA", "US")
    assert listed_outside_us("GB", "IE")


# --- fetchers -----------------------------------------------------------------

NOW = datetime.now(tz=timezone.utc).isoformat()


@patch("pipeline.sources.lever._get_jobs_json")
def test_lever_drops_non_us_postings(mock_get):
    from pipeline.sources.lever import fetch_lever_jobs
    base = {"text": "Leasing Consultant", "categories": {"location": "Somewhere"},
            "hostedUrl": "https://jobs.lever.co/x/1", "createdAt": 1_790_000_000_000}
    mock_get.return_value = [
        {**base, "id": "us", "country": "US"},
        {**base, "id": "ca", "country": "CA"},
        {**base, "id": "none"},
    ]
    ids = [j.source_id for j in fetch_lever_jobs("x", "X")]
    assert ids == ["lever_us", "lever_none"]


@patch("pipeline.sources.ashby._get_jobs_json")
def test_ashby_keeps_a_posting_with_any_us_location(mock_get):
    from pipeline.sources.ashby import fetch_ashby_jobs

    def job(job_id, *countries):
        places = [{"address": {"postalAddress": {"addressCountry": c}}} for c in countries]
        return {"id": job_id, "title": "Property Manager", "location": "x", "publishedAt": NOW,
                "jobUrl": "https://jobs.ashbyhq.com/x", **places[0],
                "secondaryLocations": places[1:]}

    mock_get.return_value = {"jobs": [job("ca", "Canada"), job("mixed", "Canada", "USA"), job("us", "United States")]}
    ids = [j.source_id for j in fetch_ashby_jobs("x", "X")]
    assert ids == ["ashby_mixed", "ashby_us"]


@patch("pipeline.sources.recruitee._get_jobs_json")
def test_recruitee_drops_non_us_offers(mock_get):
    from pipeline.sources.recruitee import fetch_recruitee_jobs
    base = {"title": "Leasing Agent", "city": "x", "created_at": NOW, "careers_url": "https://x"}
    mock_get.return_value = {"offers": [{**base, "id": 1, "country_code": "US"},
                                        {**base, "id": 2, "country_code": "NL"}]}
    assert [j.source_id for j in fetch_recruitee_jobs("x", "X")] == ["recruitee_1"]


def _workable_listing(shortcode, country):
    return {"id": shortcode, "shortcode": shortcode, "title": "Leasing Agent", "published": NOW,
            "location": {"city": "x", "region": "y", "countryCode": country}}


@patch("pipeline.sources.workable._get_detail")
@patch("pipeline.sources.workable._get_page")
def test_workable_skips_non_us_listings_before_fetching_them(mock_page, mock_detail):
    """Yugo lists UK and European jobs on the same account as its US ones."""
    from pipeline.sources.workable import fetch_workable_jobs
    mock_page.return_value = {"results": [_workable_listing("US1", "US"), _workable_listing("GB1", "GB"),
                                          _workable_listing("ES1", "ES")]}
    mock_detail.side_effect = lambda client, slug, shortcode: {
        **_workable_listing(shortcode, "US"), "description": "<p>x</p>"}

    jobs = fetch_workable_jobs("yugo", "Yugo")

    assert [j.source_id for j in jobs] == ["workable_US1"]
    assert [call.args[2] for call in mock_detail.call_args_list] == ["US1"]


@patch("pipeline.sources.smartrecruiters._get_detail")
@patch("pipeline.sources.smartrecruiters._get_page")
def test_smartrecruiters_skips_non_us_postings_before_fetching_them(mock_page, mock_detail):
    from pipeline.sources.smartrecruiters import fetch_smartrecruiters_jobs

    def posting(posting_id, country):
        return {"id": posting_id, "name": "Leasing Consultant", "releasedDate": NOW,
                "location": {"city": "x", "region": "y", "country": country}}

    mock_page.return_value = {"content": [posting("1", "us"), posting("2", "ca")], "totalFound": 2}
    mock_detail.side_effect = lambda client, slug, posting_id: posting(posting_id, "us")

    jobs = fetch_smartrecruiters_jobs("Morguard", "Morguard")

    assert [j.source_id for j in jobs] == ["smartrecruiters_1"]
    assert [call.args[2] for call in mock_detail.call_args_list] == ["1"]


# --- the main.py choke point ----------------------------------------------------

def test_run_drops_foreign_jobs_before_anything_else(monkeypatch):
    """A Greenhouse job carries only location text. It must not reach state or
    classification, whatever else happens in the run."""
    from pipeline import main
    from pipeline.models import RawJob

    def raw(source_id, location):
        return RawJob(source_id=source_id, source_name="greenhouse", title="Leasing Consultant",
                      company=source_id, location=location, description_html="", description_text="",
                      apply_url="https://x", date_posted=datetime.now(tz=timezone.utc), remote_type="onsite")

    fetched = [raw("greenhouse_1", "Kingston, Ontario"), raw("greenhouse_2", "Dallas, TX")]
    monkeypatch.setattr(main, "SOURCES", {"greenhouse": [{"slug": "x", "company_name": "X"}]})
    monkeypatch.setitem(main.DISPATCHERS, "greenhouse", lambda slug, name: fetched)
    monkeypatch.setattr("sys.argv", ["pipeline.main", "--dry-run", "--skip-indexing"])

    seen = {}
    state = MagicMock()
    state.is_processed.side_effect = lambda source_id: seen.setdefault("checked", []).append(source_id) or True
    monkeypatch.setattr(main, "State", lambda: state)
    monkeypatch.setattr(main, "_closure_and_publish", lambda state, args, note: 0)

    assert main.run() == 0
    assert seen["checked"] == ["greenhouse_2"]
