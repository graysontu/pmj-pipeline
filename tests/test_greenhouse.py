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
