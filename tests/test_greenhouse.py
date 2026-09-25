from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pipeline.sources.greenhouse import fetch_greenhouse_jobs


MOCK_RESPONSE = {
    "jobs": [
        {
            "id": 12345,
            "title": "Property Manager",
            "location": {"name": "Dallas, TX"},
            "content": "<p>Manage residential properties.</p>",
            "absolute_url": "https://boards.greenhouse.io/greystar/jobs/12345",
            "updated_at": "2026-04-01T12:00:00Z",
            "metadata": [],
        },
        {
            "id": 67890,
            "title": "Remote Leasing Consultant",
            "location": {"name": "Remote"},
            "content": "<ul><li>Handle leasing inquiries.</li></ul>",
            "absolute_url": "https://boards.greenhouse.io/greystar/jobs/67890",
            "updated_at": "2026-04-10T08:30:00Z",
            "metadata": [],
        },
    ]
}


def _make_mock_response(status_code: int = 200, json_data: dict = None):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data or MOCK_RESPONSE
    mock.raise_for_status = MagicMock()
    return mock


@patch("pipeline.sources.greenhouse._get_jobs_json")
def test_fetch_returns_raw_jobs(mock_get):
    mock_get.return_value = MOCK_RESPONSE

    jobs = fetch_greenhouse_jobs("greystar", "Greystar")

    assert len(jobs) == 2
    assert jobs[0].source_id == "greenhouse_12345"
    assert jobs[0].source_name == "greenhouse"
    assert jobs[0].title == "Property Manager"
    assert jobs[0].company == "Greystar"
    assert jobs[0].location == "Dallas, TX"
    assert jobs[0].description_text == "Manage residential properties."
    assert jobs[0].apply_url == "https://boards.greenhouse.io/greystar/jobs/12345"
    assert isinstance(jobs[0].date_posted, datetime)


@patch("pipeline.sources.greenhouse._get_jobs_json")
def test_remote_type_inferred(mock_get):
    mock_get.return_value = MOCK_RESPONSE

    jobs = fetch_greenhouse_jobs("greystar", "Greystar")

    assert jobs[0].remote_type == "onsite"
    assert jobs[1].remote_type == "remote"


@patch("pipeline.sources.greenhouse._get_jobs_json")
def test_html_stripped_from_description_text(mock_get):
    mock_get.return_value = MOCK_RESPONSE

    jobs = fetch_greenhouse_jobs("greystar", "Greystar")

    assert "<" not in jobs[1].description_text
    assert "leasing inquiries" in jobs[1].description_text


@patch("pipeline.sources.greenhouse._get_jobs_json")
def test_missing_slug_returns_empty_list(mock_get):
    import httpx

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_get.side_effect = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=mock_response
    )

    jobs = fetch_greenhouse_jobs("nonexistent-slug", "Ghost Co")

    assert jobs == []


@patch("pipeline.sources.greenhouse._get_jobs_json")
def test_empty_jobs_list(mock_get):
    mock_get.return_value = {"jobs": []}

    jobs = fetch_greenhouse_jobs("greystar", "Greystar")

    assert jobs == []


def _job(location, offices, title="Leasing Specialist"):
    return {
        "id": 1,
        "title": title,
        "location": {"name": location},
        "offices": offices,
        "content": "<p>x</p>",
        "absolute_url": "https://boards.greenhouse.io/x/jobs/1",
        "first_published": "2026-09-24T12:00:00Z",
    }


def _parsed_location(location, offices, company="Avanath"):
    from pipeline.sources.greenhouse import _parse_job
    return _parse_job(_job(location, offices), company, None).location


def test_property_name_takes_the_matching_office_address():
    offices = [{"name": "Northpointe", "location": "5441 N. Paramount Blvd, Long Beach, CA 90805"}]
    assert _parsed_location("Northpointe", offices) == "5441 N. Paramount Blvd, Long Beach, CA 90805"


def test_office_name_only_needs_to_share_a_distinctive_word():
    offices = [{"name": "Acorn I and II (0188)", "location": "Oakland, California, United States"}]
    assert _parsed_location("Acorn", offices) == "Oakland, California, United States"


def test_a_headquarters_office_is_not_used_for_a_property_posting():
    """Avanath tags a "San Diego" posting to its Irvine corporate office."""
    offices = [{"name": "Corporate", "location": "Irvine, California, United States"}]
    assert _parsed_location("Seaport Village", offices) == "Seaport Village"


def test_the_employer_name_alone_does_not_count_as_a_match():
    """Fairstead's remote roles are tagged "Fairstead Communities", New York."""
    offices = [{"name": "Fairstead Communities", "location": "New York, New York, United States"}]
    listed = "Fairstead Operations Hub"
    assert _parsed_location(listed, offices, company="Fairstead") == listed


def test_remote_postings_do_not_inherit_an_office():
    offices = [{"name": "Remote Leasing", "location": "Charlotte, North Carolina, United States"}]
    assert _parsed_location("Remote Leasing", offices, company="Lincoln Property Company") == "Remote Leasing"


def test_a_location_that_names_a_state_is_left_to_the_parser():
    """"Concord, NC (Charlotte area)" is a real place the parser misses; an office
    in Charlotte would be the wrong city."""
    offices = [{"name": "Charlotte", "location": "Charlotte, North Carolina, United States"}]
    listed = "Concord, NC (Charlotte area)"
    assert _parsed_location(listed, offices, company="Weinstein Properties") == listed


def test_a_resolvable_location_ignores_offices():
    offices = [{"name": "Dallas", "location": "Houston, Texas, United States"}]
    assert _parsed_location("Dallas, TX", offices) == "Dallas, TX"


def test_matching_offices_in_different_states_are_not_guessed_between():
    offices = [
        {"name": "Canvas Austin", "location": "Austin, Texas, United States"},
        {"name": "Canvas Denver", "location": "Denver, Colorado, United States"},
    ]
    assert _parsed_location("Canvas", offices) == "Canvas"


def test_no_offices_leaves_the_location_alone():
    assert _parsed_location("Northpointe", []) == "Northpointe"
    assert _parsed_location("Northpointe", None) == "Northpointe"


def test_office_without_an_address_is_skipped():
    offices = [{"name": "Northpointe", "location": None}]
    assert _parsed_location("Northpointe", offices) == "Northpointe"


def test_remote_type_still_comes_from_the_listed_location():
    from pipeline.sources.greenhouse import _parse_job
    job = _job("Remote", [{"name": "Remote", "location": "Charlotte, North Carolina, United States"}])
    parsed = _parse_job(job, "Lincoln Property Company", None)
    assert parsed.location == "Remote"
    assert parsed.remote_type == "remote"
