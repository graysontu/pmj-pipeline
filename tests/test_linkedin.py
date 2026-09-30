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


PAGE = "https://propertymanagementjobs.us/jobs/property-manager-c960a0b0"
CENTRAL = timezone(timedelta(hours=-5))
POST_TIME = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)   # Monday 10:00 AM Eastern


def _site(posted: datetime | None, sticky: bool = False) -> dict[str, SiteJob]:
    return {APPLY: SiteJob(url=PAGE, sticky=sticky, published_at=posted)}


# The site dates each job at midnight Central, as jobs.xml shows ("2026-09-25T00:00:00-05:00").
SITE = _site(datetime(2026, 9, 26, tzinfo=CENTRAL))
# For the workflow tests, which run as if on Monday 2026-09-28 at 8:17 AM Eastern.
RECENT_SITE = _site(NOW - timedelta(days=1))


def _evaluate(job, site=None, **overrides):
    kwargs = dict(post_time=POST_TIME, max_days_ago=4, posted_ids=set(), cooling_companies=set())
    kwargs.update(overrides)
    return evaluate(job, site or SITE, **kwargs)


def test_a_complete_job_qualifies_and_links_to_the_site():
    candidate, reason = _evaluate(_job())
    assert reason is None
    assert candidate.site_url == SITE[APPLY].url
    assert candidate.location == "Davis, CA" and candidate.pay == "$80K–$85K/yr"


@pytest.mark.parametrize("overrides,reason", [
    ({"apply_url": "https://elsewhere.example/job"}, "not live on the site"),
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


@pytest.mark.parametrize("low,high,schedule", [
    ("15", "17", "hourly"),        # the Rise Association Management Group concierge
    ("21", "21", "hourly"),        # exactly $21 is at the floor
    ("20", "30", "hourly"),        # judged on the bottom of the range, not the top
    ("45000", "45000", "yearly"),  # exactly $45,000 is at the floor
    ("40000", "60000", "yearly"),
])
def test_low_paying_jobs_are_not_highlighted(low, high, schedule):
    job = _job(salary_min=low, salary_max=high, salary_schedule=schedule)
    assert _evaluate(job) == (None, "pay at or below the LinkedIn floor")


@pytest.mark.parametrize("low,high,schedule", [
    ("21.50", "23", "hourly"),
    ("22", "24", "hourly"),
    ("45760", "52000", "yearly"),
])
def test_pay_just_above_the_floor_qualifies(low, high, schedule):
    candidate, reason = _evaluate(_job(salary_min=low, salary_max=high, salary_schedule=schedule))
    assert reason is None and candidate


@pytest.mark.parametrize("age_days,label", [
    # Measured on live job pages, 2026-09-28: the site rounds to the nearest day.
    (6.97, 7), (5.97, 6), (4.97, 5), (3.97, 4), (2.97, 3), (1.97, 2),
    (4.49, 4), (4.5, 5), (1.2, 1), (0.8, 0),
])
def test_days_ago_matches_the_site_label(age_days, label):
    from pipeline.linkedin.picker import site_days_ago
    assert site_days_ago(timedelta(days=age_days)) == label


@pytest.mark.parametrize("site_date,qualifies", [
    (datetime(2026, 9, 28, tzinfo=CENTRAL), True),    # "9 hours ago"
    (datetime(2026, 9, 24, tzinfo=CENTRAL), True),    # 4 days 9 hours: reads "4 days ago"
    (datetime(2026, 9, 23, tzinfo=CENTRAL), False),   # 5 days 9 hours: reads "5 days ago"
])
def test_jobs_older_than_four_days_on_the_site_are_not_posted(site_date, qualifies):
    candidate, reason = _evaluate(_job(), site=_site(site_date))
    if qualifies:
        assert reason is None and candidate
    else:
        assert reason == "posted more than 4 days ago on the site"


def test_a_job_without_a_site_date_is_not_posted():
    assert _evaluate(_job(), site=_site(None)) == (None, "no posting date on the site")


def test_pinned_employer_posts_are_never_picked():
    assert _evaluate(_job(), site=_site(datetime(2026, 9, 26, tzinfo=CENTRAL), sticky=True)) == (
        None, "pinned employer post")


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
    # Reside Living's old file was a white wordmark flattened onto white, so only
    # the blue "i" survived: a tall sliver of artwork on an empty canvas.
    sliver = tmp_path / "sliver.png"
    image = Image.new("RGB", (400, 200), "white")
    image.paste((0, 157, 220), (185, 60, 215, 140))
    image.save(sliver)
    assert logo_problem(sliver) == "logo artwork looks cut off (tall sliver)"
    blank = tmp_path / "blank.png"
    Image.new("RGB", (400, 200), "white").save(blank)
    assert logo_problem(blank) == "logo is blank"
    assert logo_problem(LOGOS / "peakmade-real-estate.png") is None
    assert logo_problem(LOGOS / "reside-living.png") is None


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
    "x" * 330,
    # Grayson's rules, 2026-09-28: never an em dash (or a stand-in for one) ...
    "630 units, a waitlist longer than the vacancies, and a full LIHTC certification cycle to own "
    "— the kind of compliance depth that leads to senior leasing and compliance coordinator roles.",
    "A leasing role at a busy lease-up – tours, move-ins and renewals every day.",
    "A leasing role at a busy lease-up - tours, move-ins and renewals every day.",
    "A leasing role at a busy lease-up -- tours, move-ins and renewals every day.",
    # ... and never "it's not X, it's Y" framing.
    "This isn't a traditional leasing role. It's a centralized hub working leads by phone and text.",
    "It’s not just leasing, it’s owning the whole resident experience at a 300-unit community.",
    "The job is not only tours but also renewals, delinquency and move-out statements for the team.",
    "A leasing role at a 300-unit community with tours, renewals and a supportive team!",
])
def test_unusable_opening_lines_are_rejected(text):
    assert _clean_hook(text) is None


@pytest.mark.parametrize("text", [
    # Lines the revised prompt produced for real jobs on 2026-09-28.
    "Pines at Castle Rock, a 630-unit affordable community, is hiring a leasing consultant to handle "
    "tours and move-in inspections alongside income certifications and waitlist management.",
    "A two-site Community Manager role covering 168 units, with three days a week at Cottonwood Creek "
    "and two at Elowyn Townhomes.",
    "This assistant role covers two sites side by side: a 65-home 55+ manufactured housing community "
    "and a 107-site RV park, with rent collection, leasing and daily property walks.",
])
def test_plain_professional_lines_with_hyphenated_words_pass(text):
    assert _clean_hook(text) == text


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


def _reply(text):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def test_a_line_that_breaks_a_rule_gets_one_retry():
    client = MagicMock()
    client.beta.messages.create.side_effect = [
        _reply("Own the whole leasing cycle — from first tour to signed lease."),
        _reply("A leasing role at a 300-unit community covering tours, applications and renewals."),
    ]
    assert write_hook(_candidate(), client) == (
        "A leasing role at a 300-unit community covering tours, applications and renewals.")
    assert client.beta.messages.create.call_count == 2


def test_two_rule_breaking_lines_fall_back_to_the_template():
    client = MagicMock()
    client.beta.messages.create.return_value = _reply("It's not just leasing, it's a career launchpad.")
    assert write_hook(_candidate(), client) is None
    assert client.beta.messages.create.call_count == 2


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

    assert run.prepare(post_dir, state_path=state_file, site_jobs=RECENT_SITE, page_locator=_davis, use_ai=False, now=NOW) == 0
    pending = json.loads((post_dir / "pending.json").read_text(encoding="utf-8"))
    assert pending["mode"] == "preview" and (post_dir / pending["image"]).exists()

    assert run.publish(post_dir, wait_for_image=False, now=NOW) == 0
    history = json.loads((post_dir / "history.json").read_text(encoding="utf-8"))
    assert [e["source_id"] for e in history] == ["workable_84C40F6B99"]
    assert history[0]["buffer"] == "No Buffer API key saved yet"
    assert not (post_dir / "pending.json").exists()
    assert "Property Manager at PeakMade Real Estate" in (post_dir / "README.md").read_text(encoding="utf-8")

    # The same job is not picked twice, and nothing else qualifies: the day is skipped.
    assert run.prepare(post_dir, state_path=state_file, site_jobs=RECENT_SITE, page_locator=_davis, use_ai=False, now=NOW) == 0
    assert not (post_dir / "pending.json").exists()


def test_preview_with_a_key_only_checks_the_connection(tmp_path, state_file, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", False)
    post_dir = tmp_path / "posts"
    run.prepare(post_dir, state_path=state_file, site_jobs=RECENT_SITE, page_locator=_davis, use_ai=False, now=NOW)
    buffer = MagicMock()
    buffer.linkedin_channel.return_value = {"id": "c1", "name": "Property Management Jobs"}
    assert run.publish(post_dir, buffer_client=buffer, wait_for_image=False, now=NOW) == 0
    buffer.schedule_image_post.assert_not_called()
    history = json.loads((post_dir / "history.json").read_text(encoding="utf-8"))
    assert history[0]["buffer"] == "Connected - live posts would go to Property Management Jobs"


def test_live_run_schedules_in_buffer(tmp_path, state_file, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", True)
    post_dir = tmp_path / "posts"
    assert run.prepare(post_dir, state_path=state_file, site_jobs=RECENT_SITE, page_locator=_davis, use_ai=False, now=NOW) == 0

    buffer = MagicMock()
    buffer.linkedin_channel.return_value = {"id": "c1", "name": "Property Management Jobs"}
    buffer.schedule_image_post.return_value = {"id": "post-9"}
    assert run.publish(post_dir, buffer_client=buffer, wait_for_image=False, now=NOW) == 0

    channel_id, text, image_url, _ = buffer.schedule_image_post.call_args.args
    assert channel_id == "c1" and "utm_source=linkedin" in text
    assert image_url.startswith("https://raw.githubusercontent.com/") and image_url.endswith(".png")
    history = json.loads((post_dir / "history.json").read_text(encoding="utf-8"))
    assert history[0]["mode"] == "live" and history[0]["buffer_post_id"] == "post-9"


def test_never_two_live_posts_on_the_same_day(tmp_path, monkeypatch):
    """A manual re-run after the scheduled run must not post a second job, even
    though another qualifying job is available."""
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", True)
    other_apply = "https://job-boards.greenhouse.io/commonplace/jobs/1"
    jobs = {
        "workable_84C40F6B99": _job(),
        "greenhouse_1": _job(source_id="greenhouse_1", company="CommonPlace", apply_url=other_apply,
                             company_logo_url="https://x/logos/commonplace.png", title="Leasing Consultant",
                             category="Leasing Consultant Jobs", location="Charlotte, NC"),
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(jobs), encoding="utf-8")
    posted = NOW - timedelta(days=1)
    site = {**_site(posted), other_apply: SiteJob(url="https://propertymanagementjobs.us/jobs/lc", sticky=False,
                                                  published_at=posted)}
    locate = {PAGE: ("Davis", "California"), "https://propertymanagementjobs.us/jobs/lc": ("Charlotte", "North Carolina")}
    post_dir = tmp_path / "posts"

    assert run.prepare(post_dir, state_path=state_path, site_jobs=site, page_locator=locate.get, use_ai=False, now=NOW) == 0
    buffer = MagicMock()
    buffer.linkedin_channel.return_value = {"id": "c1", "name": "Page"}
    buffer.schedule_image_post.return_value = {"id": "post-1"}
    run.publish(post_dir, buffer_client=buffer, wait_for_image=False, now=NOW)

    assert run.prepare(post_dir, state_path=state_path, site_jobs=site, page_locator=locate.get, use_ai=False, now=NOW) == 0
    assert not (post_dir / "pending.json").exists()
    assert len(json.loads((post_dir / "history.json").read_text(encoding="utf-8"))) == 1


def test_a_failed_buffer_post_is_not_recorded(tmp_path, state_file, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", True)
    post_dir = tmp_path / "posts"
    run.prepare(post_dir, state_path=state_file, site_jobs=RECENT_SITE, page_locator=_davis, use_ai=False, now=NOW)
    buffer = MagicMock()
    buffer.linkedin_channel.side_effect = BufferError("No LinkedIn channel is connected in Buffer.")
    with pytest.raises(BufferError):
        run.publish(post_dir, buffer_client=buffer, wait_for_image=False, now=NOW)
    assert not (post_dir / "history.json").exists()


def test_preview_history_does_not_count_against_live_posts(tmp_path, state_file, monkeypatch):
    post_dir = tmp_path / "posts"
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", False)
    monkeypatch.setattr(config, "BUFFER_API_KEY", "")
    run.prepare(post_dir, state_path=state_file, site_jobs=RECENT_SITE, page_locator=_davis, use_ai=False, now=NOW)
    run.publish(post_dir, wait_for_image=False, now=NOW)

    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", True)
    assert run.prepare(post_dir, state_path=state_file, site_jobs=RECENT_SITE, page_locator=_davis, use_ai=False, now=NOW) == 0
    assert json.loads((post_dir / "pending.json").read_text(encoding="utf-8"))["mode"] == "live"


def test_a_city_the_page_does_not_show_is_never_posted(tmp_path, state_file, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", False)
    assert run.prepare(tmp_path, state_path=state_file, site_jobs=RECENT_SITE,
                       page_locator=lambda url: (None, "California"), use_ai=False, now=NOW) == 0
    assert not (tmp_path / "pending.json").exists()


def test_a_day_with_no_qualifying_job_is_skipped_with_an_email(tmp_path, monkeypatch):
    """With the pay floor some days have nothing worth highlighting. That is a
    skip, not a failure: no post, exit 0, and an email saying why."""
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", True)
    low_pay = {"workable_84C40F6B99": _job(salary_min="15", salary_max="17", salary_schedule="hourly",
                                           published_at=datetime.now(timezone.utc).isoformat())}
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(low_pay), encoding="utf-8")
    post_dir, email_dir = tmp_path / "posts", tmp_path / "email"

    assert run.prepare(post_dir, state_path=state_path, site_jobs=RECENT_SITE, page_locator=_davis,
                       use_ai=False, now=NOW, email_dir=email_dir) == 0
    assert not (post_dir / "pending.json").exists()
    assert (email_dir / "subject.txt").read_text(encoding="utf-8") == "LinkedIn: no post today - no job met the bar"
    assert "pay at or below the LinkedIn floor 1" in (email_dir / "body.txt").read_text(encoding="utf-8")

    buffer = MagicMock()
    assert run.publish(post_dir, buffer_client=buffer, wait_for_image=False, now=NOW, email_dir=email_dir) == 0
    buffer.schedule_image_post.assert_not_called()
    assert (email_dir / "subject.txt").read_text(encoding="utf-8").startswith("LinkedIn: no post today")


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize("now,expected", [
    # The three triggers on a summer Monday: 12:17, 4:17 and 8:17 AM EDT all aim at 10 AM.
    (_utc(2026, 9, 28, 4, 17), _utc(2026, 9, 28, 14, 0)),
    (_utc(2026, 9, 28, 8, 17), _utc(2026, 9, 28, 14, 0)),
    (_utc(2026, 9, 28, 12, 17), _utc(2026, 9, 28, 14, 0)),
    # In winter the first trigger lands on Sunday 11:17 PM EST: still Monday 10 AM EST.
    (_utc(2026, 12, 7, 4, 17), _utc(2026, 12, 7, 15, 0)),
    (_utc(2026, 12, 7, 12, 17), _utc(2026, 12, 7, 15, 0)),
    # A manual run on a non-posting day aims at the next posting day.
    (_utc(2026, 9, 29, 20, 0), _utc(2026, 9, 30, 14, 0)),     # Tuesday -> Wednesday
    (_utc(2026, 10, 3, 15, 0), _utc(2026, 10, 5, 14, 0)),     # Saturday -> Monday
    # A run delayed past 10 AM on a posting day posts 15-16 minutes later...
    (_utc(2026, 9, 30, 17, 0), _utc(2026, 9, 30, 17, 16)),    # 1:00 PM EDT
    # ...but one that only starts in the evening posts nothing that day.
    (_utc(2026, 9, 30, 22, 30), None),                         # 6:30 PM EDT
])
def test_post_time(now, expected):
    assert run.next_post_time(now) == expected


def test_no_post_today_email_only_from_the_last_chance_and_only_once(tmp_path, monkeypatch):
    """Three triggers a day means three runs that might find nothing. Earlier ones
    stay quiet (the day's jobs may not have imported yet); the one near the post
    time sends the email; any later straggler doesn't send it again."""
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", True)
    low_pay = {"workable_84C40F6B99": _job(salary_min="15", salary_max="17", salary_schedule="hourly",
                                           published_at=datetime.now(timezone.utc).isoformat())}
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(low_pay), encoding="utf-8")
    post_dir = tmp_path / "posts"

    def attempt(now, name):
        email_dir = tmp_path / name
        assert run.prepare(post_dir, state_path=state_path, site_jobs=RECENT_SITE, page_locator=_davis,
                           use_ai=False, now=now, email_dir=email_dir) == 0
        return (email_dir / "subject.txt").exists()

    assert not attempt(_utc(2026, 9, 28, 4, 17), "first")    # 12:17 AM EDT
    assert not attempt(_utc(2026, 9, 28, 8, 17), "second")   # 4:17 AM EDT
    assert attempt(_utc(2026, 9, 28, 12, 17), "third")       # 8:17 AM EDT
    assert not attempt(_utc(2026, 9, 28, 13, 30), "late")    # a delayed straggler
    days = json.loads((post_dir / "skipped_days.json").read_text(encoding="utf-8"))
    assert [d["date"] for d in days] == ["2026-09-28"]


def test_a_run_that_starts_in_the_evening_does_nothing(tmp_path, state_file, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_POSTING_ENABLED", True)
    email_dir = tmp_path / "email"
    assert run.prepare(tmp_path / "posts", state_path=state_file, site_jobs=RECENT_SITE, page_locator=_davis,
                       use_ai=False, now=_utc(2026, 9, 30, 22, 30), email_dir=email_dir) == 0
    assert not (tmp_path / "posts" / "pending.json").exists() and not email_dir.exists()
