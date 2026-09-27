"""LinkedIn posts. What matters most is that a post never shows something wrong
(a mangled title, a place the job page doesn't show, a maintenance role while
those are excluded), never repeats a company inside the cooldown, and never
sends anything to Buffer in preview mode."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from PIL import Image

from pipeline import config
from pipeline.linkedin import run
from pipeline.linkedin.buffer import BufferClient, BufferError, _literal
from pipeline.linkedin.caption import _clean_hook, build_post_text, tracked_url, write_hook
from pipeline.linkedin.card import THEMES, logo_problem, render_card, title_fits
from pipeline.linkedin.picker import (
    Candidate,
    SiteJob,
    clean_title,
    confirm_page_location,
    cooling_companies,
    evaluate,
    feed_location,
    format_pay,
    rank,
)

LOGOS = Path(__file__).parent.parent / "output" / "logos"
NOW = datetime(2026, 9, 28, 12, 17, tzinfo=timezone.utc)


# --- titles ------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Property Manager", "Property Manager"),
    ("Leasing Consultant - Lakeline at Bartram Park", "Leasing Consultant"),
    ("LIHTC Leasing Consultant (30024)", "LIHTC Leasing Consultant"),
    ("Assistant Community Manager (Sunset Creek, Sunset Pines)", "Assistant Community Manager"),
    ("Leasing Specialist - Full - Time", "Leasing Specialist"),
    ("Front Desk Associate- Union House", "Front Desk Associate"),
    ("Community Relations Manager-Cortland Fossil Creek", "Community Relations Manager"),
    ("Customer Experience Specialist at Antero Apartments", "Customer Experience Specialist"),
    ("Community Manager-3700M", "Community Manager"),
    ("Leasing Agent-in-Training", "Leasing Agent-in-Training"),
    ("Sunrise Management - District Manager - San Diego County Region", "District Manager"),
    # Descriptors piled on after the role are cut back to the role itself.
    ("Assistant Community Manager Manufactured Housing Community and RV Park", "Assistant Community Manager"),
    ("Part Time Leasing Consultant", "Part-Time Leasing Consultant"),
    ("Full-Time Concierge (Mon-Fri 4pm-12am)", "Concierge"),
    ("Leasing Consultant $500 Sign On Bonus", "Leasing Consultant"),
    ("LEASING CONSULTANT", "Leasing Consultant"),
    ("HOA COMMUNITY MANAGER", "HOA Community Manager"),
    ("Market Property Manager III", "Market Property Manager III"),
    ("Manager of Resident Retention", "Manager of Resident Retention"),
])
def test_titles_are_cut_back_to_the_role(raw, expected):
    assert clean_title(raw) == (expected, None)


@pytest.mark.parametrize("raw", [
    "Feria De Empleo - Trabajos de Limpieza (Martes, 29 de Septiembre)",
    "Hiring Event 7/15/26 - Park Avenue Apartments",
    "COE Career Fair 5/21/2026",
    "Parkview Terrace",                                     # a building, not a job
    "Senior Community Manager of Permanent Supportive Housing (Donner Lofts)",
    "Providence at McDowell Assistant Community Manager",   # property name first
    "Manager, Centralized Resident Account Services",       # reduces to just "Manager"
    "$1,000 Bonus!! Leasing Pro",
])
def test_strange_or_overlong_titles_are_skipped(raw):
    title, reason = clean_title(raw)
    assert title is None and reason


def test_every_accepted_title_fits_the_image():
    for raw in ("Community Administrative Coordinator", "Affordable Assistant Property Manager II"):
        title, _ = clean_title(raw)
        assert title and title_fits(title)
    assert not title_fits("Assistant Community Manager Manufactured Housing Community and RV Park")


# --- pay and location --------------------------------------------------------

@pytest.mark.parametrize("job,expected", [
    ({"salary_min": "80000", "salary_max": "85000", "salary_schedule": "yearly"}, "$80K–$85K/yr"),
    ({"salary_min": "45760", "salary_max": "52000", "salary_schedule": "yearly"}, "$45.8K–$52K/yr"),
    ({"salary_min": "22", "salary_max": "25", "salary_schedule": "hourly"}, "$22–$25/hr"),
    ({"salary_min": "18.5", "salary_max": "18.5", "salary_schedule": "hourly"}, "$18.50/hr"),
])
def test_pay_formats(job, expected):
    assert format_pay(job) == (expected, None)


@pytest.mark.parametrize("job", [
    {"salary_min": None, "salary_max": None, "salary_schedule": None},
    {"salary_min": "24000", "salary_max": "38400", "salary_schedule": "hourly"},  # the old hallucination
    {"salary_min": "30", "salary_max": "20", "salary_schedule": "hourly"},
    {"salary_min": "3000", "salary_max": "4000", "salary_schedule": "monthly"},
])
def test_missing_or_implausible_pay_is_skipped(job):
    pay, reason = format_pay(job)
    assert pay is None and reason


def test_state_abbreviation_read_as_a_city_is_rejected():
    assert feed_location({"location": "Floating, MA, NH, RI."}) == (None, "city looks wrong")
    assert feed_location({"location": "Davis, California"}) == ("Davis, CA", None)
    assert feed_location({"location": "Redfield Ridge"}) == (None, "no city and state")


def _candidate(**overrides) -> Candidate:
    values = dict(
        source_id="greenhouse_1", title="Property Manager", company="PeakMade Real Estate",
        location="Davis, CA", pay="$80K–$85K/yr", category="Property Manager Jobs",
        logo_path=LOGOS / "peakmade-real-estate.png",
        site_url="https://propertymanagementjobs.us/jobs/property-manager-c960a0b0",
        published_at=NOW - timedelta(days=1), description_html="<p>Run a community.</p>",
    )
    values.update(overrides)
    return Candidate(**values)


def test_page_location_must_match_and_supplies_its_spelling():
    confirmed, reason = confirm_page_location(_candidate(location="Mckinney, TX"), "McKinney", "Texas")
    assert reason is None and confirmed.location == "McKinney, TX"
    assert confirm_page_location(_candidate(), None, "California") == (None, "site page shows no city")
    assert confirm_page_location(_candidate(), "Sacramento", "California")[1] == "site page shows a different city"
    assert confirm_page_location(_candidate(), "Davis", "Oregon")[1] == "site page shows a different state"


# --- evaluating and ranking --------------------------------------------------

APPLY = "https://apply.workable.com/peak-made/j/84C40F6B99/"


def _job(**overrides) -> dict:
    job = {
        "source_id": "workable_84C40F6B99", "title": "Property Manager", "company": "PeakMade Real Estate",
        "location": "Davis, California", "category": "Property Manager Jobs", "apply_url": APPLY,
        "published_at": (NOW - timedelta(days=2)).isoformat(),
        "salary_min": "80000", "salary_max": "85000", "salary_schedule": "yearly",
        "company_logo_url": "https://graysontu.github.io/pmj-pipeline/logos/peakmade-real-estate.png",
        "rewritten_description": "<p>Run a student housing community.</p>",
    }
    job.update(overrides)
    return job


SITE = {APPLY: SiteJob(url="https://propertymanagementjobs.us/jobs/property-manager-c960a0b0", sticky=False)}


def _evaluate(job, **overrides):
    kwargs = dict(now=NOW, max_age_days=7, posted_ids=set(), cooling_companies=set())
    kwargs.update(overrides)
    return evaluate(job, SITE, **kwargs)


def test_a_complete_job_qualifies_and_links_to_the_site():
    candidate, reason = _evaluate(_job())
    assert reason is None
    assert candidate.site_url == SITE[APPLY].url
    assert candidate.location == "Davis, CA" and candidate.pay == "$80K–$85K/yr"


@pytest.mark.parametrize("overrides,reason", [
    ({"apply_url": "https://elsewhere.example/job"}, "not live on the site"),
    ({"published_at": (NOW - timedelta(days=9)).isoformat()}, "older than the posting window"),
    ({"category": "Maintenance Technician Jobs"}, "maintenance role"),
    ({"category": "Groundskeeper & Porter Jobs", "title": "Porter"}, "maintenance role"),
    # misfiled by the classifier, caught by the title
    ({"category": "Community Manager Jobs", "title": "Maintenance Supervisor"}, "maintenance role"),
    ({"salary_min": None, "salary_max": None, "salary_schedule": None}, "no salary"),
    ({"location": "Canvas"}, "no city and state"),
    ({"company_logo_url": ""}, "no logo"),
    ({"company_logo_url": "https://graysontu.github.io/pmj-pipeline/logos/fairstead.svg"},
     "unsupported logo format .svg"),
    ({"title": "Job Fair - Property Managers - Bozeman, MT"}, "not a single English job posting"),
])
def test_jobs_missing_something_are_skipped_with_a_reason(overrides, reason):
    assert _evaluate(_job(**overrides)) == (None, reason)


def test_pinned_employer_posts_are_never_picked():
    site = {APPLY: SiteJob(url=SITE[APPLY].url, sticky=True)}
    assert evaluate(_job(), site, now=NOW, max_age_days=7, posted_ids=set(),
                    cooling_companies=set()) == (None, "pinned employer post")


def test_posted_jobs_and_cooling_companies_are_skipped():
    assert _evaluate(_job(), posted_ids={"workable_84C40F6B99"}) == (None, "already posted")
    assert _evaluate(_job(), cooling_companies={"peakmade real estate"}) == (None, "company posted recently")


def test_company_cooldown_is_fourteen_days_by_default():
    history = [
        {"company": "Bozzuto", "created_at": (NOW - timedelta(days=13)).isoformat()},
        {"company": "Cortland", "created_at": (NOW - timedelta(days=15)).isoformat()},
    ]
    assert cooling_companies(history, NOW, config.LINKEDIN_COMPANY_COOLDOWN_DAYS) == {"bozzuto"}


def test_ranking_keeps_a_balanced_mix_of_roles():
    history = [{"category": "Leasing Consultant Jobs", "state": "TX"},
               {"category": "Property Manager Jobs", "state": "CA"}]
    leasing = _candidate(source_id="a", category="Leasing Consultant Jobs", published_at=NOW)
    community = _candidate(source_id="b", category="Community Manager Jobs",
                           published_at=NOW - timedelta(days=3))
    manager = _candidate(source_id="c", category="Property Manager Jobs")
    assert [c.source_id for c in rank([leasing, manager, community], history)] == ["b", "a", "c"]


def test_ranking_prefers_a_state_not_just_posted():
    history = [{"category": "Leasing Consultant Jobs", "state": "CA"}]
    same_state = _candidate(source_id="a", location="Davis, CA", published_at=NOW)
    other_state = _candidate(source_id="b", location="Austin, TX", published_at=NOW - timedelta(days=2))
    assert rank([same_state, other_state], history)[0].source_id == "b"


# --- image -------------------------------------------------------------------

@pytest.mark.parametrize("theme", THEMES, ids=lambda t: t.name)
def test_every_theme_renders_a_square_card(tmp_path, theme):
    out = render_card(title="Community Administrative Coordinator", company="Action Property Management",
                      location="San Francisco, CA", pay="$29–$30/hr",
                      logo_path=LOGOS / "action-property-management.png", theme=theme,
                      label="NEW JOB", out_path=tmp_path / "card.png")
    assert Image.open(out).size == (1200, 1200)


def test_broken_and_blank_logos_are_rejected(tmp_path):
    # Reside Living's file is a white wordmark flattened onto white; only the dot survives.
    assert logo_problem(LOGOS / "reside-living.png")
    blank = tmp_path / "blank.png"
    Image.new("RGB", (400, 200), "white").save(blank)
    assert logo_problem(blank) == "logo is blank"
    assert logo_problem(LOGOS / "peakmade-real-estate.png") is None


# --- caption -----------------------------------------------------------------

def test_links_carry_linkedin_tracking():
    url = tracked_url("https://propertymanagementjobs.us/jobs/x")
    assert url == ("https://propertymanagementjobs.us/jobs/x?utm_source=linkedin"
                   "&utm_medium=social&utm_campaign=job_post")


@pytest.mark.parametrize("text", [
    "Too short.",
    "Apply now at https://example.com for this role today, it is great.",
    "Great leasing role with a growing team #hiring and more to come.",
    "Earn $25 an hour running tours at a beautiful new community downtown.",
    "x" * 300,
])
def test_unusable_opening_lines_are_rejected(text):
    assert _clean_hook(text) is None


def test_post_text_has_the_facts_link_and_hashtags():
    text = build_post_text(_candidate(), "Run a student housing community end to end.")
    assert text.startswith("Run a student housing community end to end.")
    assert "\U0001F4CD Davis, CA" in text and "\U0001F4B0 $80K–$85K/yr" in text
    assert "property-manager-c960a0b0?utm_source=linkedin" in text
    assert text.endswith("#PropertyManagement #PropertyManager #NowHiring #DavisJobs")


def _fake_client(stop_reason="end_turn", text="Run a 200-unit community with a team that promotes from within."):
    response = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])
    client = MagicMock()
    client.beta.messages.create.return_value = response
    return client


def test_opening_line_comes_from_claude_when_usable():
    client = _fake_client()
    assert write_hook(_candidate(), client).startswith("Run a 200-unit community")
    kwargs = client.beta.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5" and "temperature" not in kwargs


@pytest.mark.parametrize("client", [
    _fake_client(stop_reason="refusal"),
    _fake_client(text="#hiring"),
])
def test_unusable_model_output_falls_back(client):
    assert write_hook(_candidate(), client) is None


def test_any_model_error_falls_back_rather_than_failing_the_post():
    client = MagicMock()
    client.beta.messages.create.side_effect = TypeError("unexpected keyword argument")
    assert write_hook(_candidate(), client) is None


# --- Buffer ------------------------------------------------------------------

def _buffer(handler) -> BufferClient:
    return BufferClient("key", http=httpx.Client(transport=httpx.MockTransport(handler)))


def _channels_handler(channels, captured=None):
    def handler(request):
        query = json.loads(request.content)["query"]
        if captured is not None:
            captured.append(query)
        if "GetOrganizations" in query:
            return httpx.Response(200, json={"data": {"account": {"organizations": [{"id": "org1", "name": "PMJ"}]}}})
        if "GetChannels" in query:
            return httpx.Response(200, json={"data": {"channels": channels}})
        return httpx.Response(200, json={"data": {"createPost": {"post": {"id": "p1", "dueAt": "2026-09-28T14:00:00.000Z"}}}})
    return handler


def test_the_single_linkedin_channel_is_found():
    channels = [{"id": "c1", "name": "PMJ", "service": "linkedin"}, {"id": "c2", "name": "X", "service": "twitter"}]
    assert _buffer(_channels_handler(channels)).linkedin_channel()["id"] == "c1"


def test_buffer_refuses_to_guess_between_linkedin_channels():
    channels = [{"id": "c1", "name": "Page", "service": "linkedin"}, {"id": "c2", "name": "Grayson", "service": "linkedin"}]
    with pytest.raises(BufferError, match="Several LinkedIn channels"):
        _buffer(_channels_handler(channels)).linkedin_channel()
    assert _buffer(_channels_handler(channels)).linkedin_channel("c2")["name"] == "Grayson"


def test_no_linkedin_channel_is_an_error():
    with pytest.raises(BufferError, match="No LinkedIn channel"):
        _buffer(_channels_handler([{"id": "c2", "name": "X", "service": "twitter"}])).linkedin_channel()


def test_post_text_survives_quoting_into_the_mutation():
    text = 'Say "hi" \\ now\n\n\U0001F4CD Davis, CA'
    captured = []
    post = _buffer(_channels_handler([], captured)).schedule_image_post(
        "c1", text, "https://raw.githubusercontent.com/x/y/z.png", datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc))
    assert post["id"] == "p1"
    mutation = captured[-1]
    assert _literal(text) in mutation and "\U0001F4CD" in mutation
    assert 'dueAt: "2026-09-28T14:00:00.000Z"' in mutation and "mode: customScheduled" in mutation


@pytest.mark.parametrize("response,match", [
    (httpx.Response(200, json={"data": {"createPost": {"message": "Channel not found"}}}), "Channel not found"),
    (httpx.Response(200, json={"errors": [{"message": "bad key", "extensions": {"code": "UNAUTHORIZED"}}]}), "UNAUTHORIZED"),
    (httpx.Response(401, text="nope"), "rejected the API key"),
])
def test_buffer_failures_raise(response, match):
    with pytest.raises(BufferError, match=match):
        _buffer(lambda request: response).schedule_image_post("c1", "t", "https://i/x.png", NOW)


# --- the two workflow steps ----------------------------------------------------

@pytest.fixture
def state_file(tmp_path):
    jobs = {
        "workable_84C40F6B99": _job(published_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat()),
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(jobs), encoding="utf-8")
    return path


def _davis(_url):
    return "Davis", "California"


def test_preview_run_records_the_post_without_touching_buffer(tmp_path, state_file, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", False)
    monkeypatch.setattr(config, "BUFFER_API_KEY", "")
    post_dir = tmp_path / "posts"

    assert run.prepare(post_dir, state_path=state_file, site_jobs=SITE, page_locator=_davis, use_ai=False) == 0
    pending = json.loads((post_dir / "pending.json").read_text(encoding="utf-8"))
    assert pending["mode"] == "preview" and (post_dir / pending["image"]).exists()

    assert run.publish(post_dir, wait_for_image=False) == 0
    history = json.loads((post_dir / "history.json").read_text(encoding="utf-8"))
    assert [e["source_id"] for e in history] == ["workable_84C40F6B99"]
    assert history[0]["buffer"] == "No Buffer API key saved yet"
    assert not (post_dir / "pending.json").exists()
    assert "Property Manager at PeakMade Real Estate" in (post_dir / "README.md").read_text(encoding="utf-8")

    # The same job is not picked twice, and nothing else qualifies.
    assert run.prepare(post_dir, state_path=state_file, site_jobs=SITE, page_locator=_davis, use_ai=False) == 1


def test_preview_with_a_key_only_checks_the_connection(tmp_path, state_file, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", False)
    post_dir = tmp_path / "posts"
    run.prepare(post_dir, state_path=state_file, site_jobs=SITE, page_locator=_davis, use_ai=False)
    buffer = MagicMock()
    buffer.linkedin_channel.return_value = {"id": "c1", "name": "Property Management Jobs"}
    assert run.publish(post_dir, buffer_client=buffer, wait_for_image=False) == 0
    buffer.schedule_image_post.assert_not_called()
    history = json.loads((post_dir / "history.json").read_text(encoding="utf-8"))
    assert history[0]["buffer"] == "Connected - live posts would go to Property Management Jobs"


def test_live_run_schedules_in_buffer(tmp_path, state_file, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", True)
    post_dir = tmp_path / "posts"
    assert run.prepare(post_dir, state_path=state_file, site_jobs=SITE, page_locator=_davis, use_ai=False) == 0

    buffer = MagicMock()
    buffer.linkedin_channel.return_value = {"id": "c1", "name": "Property Management Jobs"}
    buffer.schedule_image_post.return_value = {"id": "post-9"}
    assert run.publish(post_dir, buffer_client=buffer, wait_for_image=False) == 0

    channel_id, text, image_url, _ = buffer.schedule_image_post.call_args.args
    assert channel_id == "c1" and "utm_source=linkedin" in text
    assert image_url.startswith("https://raw.githubusercontent.com/") and image_url.endswith(".png")
    history = json.loads((post_dir / "history.json").read_text(encoding="utf-8"))
    assert history[0]["mode"] == "live" and history[0]["buffer_post_id"] == "post-9"


def test_a_failed_buffer_post_is_not_recorded(tmp_path, state_file, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", True)
    post_dir = tmp_path / "posts"
    run.prepare(post_dir, state_path=state_file, site_jobs=SITE, page_locator=_davis, use_ai=False)
    buffer = MagicMock()
    buffer.linkedin_channel.side_effect = BufferError("No LinkedIn channel is connected in Buffer.")
    with pytest.raises(BufferError):
        run.publish(post_dir, buffer_client=buffer, wait_for_image=False)
    assert not (post_dir / "history.json").exists()


def test_preview_history_does_not_count_against_live_posts(tmp_path, state_file, monkeypatch):
    post_dir = tmp_path / "posts"
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", False)
    monkeypatch.setattr(config, "BUFFER_API_KEY", "")
    run.prepare(post_dir, state_path=state_file, site_jobs=SITE, page_locator=_davis, use_ai=False)
    run.publish(post_dir, wait_for_image=False)

    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", True)
    assert run.prepare(post_dir, state_path=state_file, site_jobs=SITE, page_locator=_davis, use_ai=False) == 0


def test_a_city_the_page_does_not_show_is_never_posted(tmp_path, state_file, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", False)
    assert run.prepare(tmp_path, state_path=state_file, site_jobs=SITE,
                       page_locator=lambda url: (None, "California"), use_ai=False) == 1


def test_post_time_is_ten_eastern_or_soon_after_a_late_start():
    early = datetime(2026, 9, 28, 12, 17, tzinfo=timezone.utc)   # 8:17 EDT
    assert run.next_post_time(early) == datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 12, 7, 12, 17, tzinfo=timezone.utc)  # 7:17 EST
    assert run.next_post_time(winter) == datetime(2026, 12, 7, 15, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 28, 15, 30, tzinfo=timezone.utc)    # cron ran 11:30 EDT
    assert late + timedelta(minutes=15) <= run.next_post_time(late) <= late + timedelta(minutes=17)
