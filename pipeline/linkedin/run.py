"""One LinkedIn post, in two steps run by .github/workflows/linkedin-post.yml:

    prepare  choose the job, draw the image, write the caption -> pending.json
             (the workflow then pushes the image so it has a public URL)
    publish  schedule the post in Buffer, or in preview mode only record it

Everything this writes lives on the linkedin-posts branch (history.json, the
images, and a README that shows every post). Nothing touches main, so this can
never collide with the daily pipeline's commit of state.json and feed.xml.
"""

import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from lxml import etree

import pipeline.state as state_module
from pipeline import config
from pipeline.linkedin.buffer import BufferClient, BufferError
from pipeline.linkedin.caption import build_post_text, template_hook, write_hook
from pipeline.linkedin.card import LABELS, THEMES, render_card
from pipeline.linkedin.picker import SiteJob, confirm_page_location, cooling_companies, evaluate, rank

logger = logging.getLogger(__name__)

SITE_JOBS_URL = f"{config.SITE_BASE_URL}/jobs.xml"
POST_BRANCH = "linkedin-posts"
REPO = os.getenv("GITHUB_REPOSITORY", "graysontu/pmj-pipeline")
MIN_SITE_JOBS = 20
MAX_PAGE_CHECKS = 10
MIN_LEAD = timedelta(minutes=15)
# Runs this close to the post time are the day's last chance (the final trigger
# fires about 7-8 AM Eastern for a 10 AM post).
FINAL_NOTICE_LEAD = timedelta(hours=3)


def raw_url(relative_path: str) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{POST_BRANCH}/{relative_path}"


# --- inputs ------------------------------------------------------------------

def load_site_jobs(http: httpx.Client | None = None) -> dict[str, SiteJob]:
    """The jobs live on the site right now, keyed by the employer's apply URL.

    The site publishes jobs.xml with each page's URL next to the original apply
    link, which is exactly how a feed job is matched to its page on the site."""
    http = http or httpx.Client(timeout=60.0, follow_redirects=True,
                                headers={"User-Agent": "pmj-pipeline linkedin"})
    last_error = None
    for attempt in range(3):
        try:
            response = http.get(SITE_JOBS_URL)
            if response.status_code == 200:
                break
            last_error = f"HTTP {response.status_code}"
        except httpx.TransportError as exc:
            last_error = str(exc)
        time.sleep(5 * (attempt + 1))
    else:
        raise RuntimeError(f"Could not fetch {SITE_JOBS_URL}: {last_error}")

    root = etree.fromstring(response.content)
    jobs = {}
    for job in root.findall("job"):
        link = (job.findtext("application_link") or "").strip()
        if link:
            jobs[link] = SiteJob(
                url=(job.findtext("url") or "").strip(),
                sticky=(job.findtext("sticky") or "").strip().lower() == "true",
                published_at=_parse_time(job.findtext("published_at")),
            )
    if len(jobs) < MIN_SITE_JOBS:
        raise RuntimeError(f"{SITE_JOBS_URL} listed only {len(jobs)} jobs; refusing to pick from it.")
    return jobs


def _parse_time(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((text or "").strip())
    except ValueError:
        return None


def fetch_page_location(url: str, http: httpx.Client | None = None) -> tuple[str | None, str | None]:
    """(city, state) as the job's page on the site shows them, read from the
    page's JobPosting structured data. jobs.xml can't be used for this: it
    leaves location empty for every job imported from the feed."""
    http = http or httpx.Client(timeout=30.0, follow_redirects=True,
                                headers={"User-Agent": "pmj-pipeline linkedin"})
    response = http.get(url)
    if response.status_code != 200:
        return None, None
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', response.text, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                places = item.get("jobLocation") or {}
                place = places[0] if isinstance(places, list) and places else places
                address = (place or {}).get("address") or {}
                return address.get("addressLocality"), address.get("addressRegion")
    return None, None


def load_active_jobs(state_path: Path | None = None) -> list[dict]:
    """The jobs currently in the feed. state_path reads a snapshot instead of
    data/state.json, for local dry runs."""
    if state_path is None:
        return state_module.State().get_active_jobs()
    original = state_module.STATE_PATH
    state_module.STATE_PATH = Path(state_path)
    try:
        return state_module.State().get_active_jobs()
    finally:
        state_module.STATE_PATH = original


def load_history(post_dir: Path) -> list[dict]:
    path = post_dir / "history.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def next_post_time(now: datetime) -> datetime | None:
    """When a post prepared now should go live, or None if it's too late today.

    On a posting day: LINKEDIN_POST_TIME, or shortly after now if a delayed run
    starts after that - unless it starts after LINKEDIN_LATEST_POST_HOUR, when
    nothing is posted that day. On any other day (a trigger that fires the
    evening before in local time, or a manual run): the next posting day's
    LINKEDIN_POST_TIME."""
    zone = ZoneInfo(config.LINKEDIN_POST_TIMEZONE)
    hour, minute = (int(part) for part in config.LINKEDIN_POST_TIME.split(":"))
    local = now.astimezone(zone)
    if local.weekday() in config.LINKEDIN_POST_WEEKDAYS:
        slot = datetime.combine(local.date(), dt_time(hour, minute), tzinfo=zone)
        if now + MIN_LEAD <= slot:
            return slot.astimezone(timezone.utc)
        if local.hour >= config.LINKEDIN_LATEST_POST_HOUR:
            return None
        return (now + MIN_LEAD + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for offset in range(1, 8):
        day = local.date() + timedelta(days=offset)
        if day.weekday() in config.LINKEDIN_POST_WEEKDAYS:
            return datetime.combine(day, dt_time(hour, minute), tzinfo=zone).astimezone(timezone.utc)
    return None


# --- prepare -----------------------------------------------------------------

def prepare(
    post_dir: Path,
    *,
    now: datetime | None = None,
    state_path: Path | None = None,
    site_jobs: dict[str, SiteJob] | None = None,
    page_locator=None,
    use_ai: bool = True,
    email_dir: Path | None = None,
) -> int:
    now = now or datetime.now(tz=timezone.utc)
    mode = "live" if config.LINKEDIN_POSTING_ENABLED else "preview"
    post_dir.mkdir(parents=True, exist_ok=True)
    pending_path = post_dir / "pending.json"
    pending_path.unlink(missing_ok=True)

    # Preview runs keep their own rotation, so going live starts the company
    # cooldown and the colour sequence fresh.
    history = [entry for entry in load_history(post_dir) if entry.get("mode") == mode]
    post_time = next_post_time(now)
    if post_time is None:
        logger.info("Too late in the day to post; nothing to do.")
        _append_summary("## LinkedIn post\n\nToo late in the day to post; nothing to do.\n")
        return 0

    # Never two live posts on one day. The workflow fires several times before each
    # posting day (GitHub delays and drops scheduled runs), so the first run to
    # start schedules the post and the rest stop here.
    zone = ZoneInfo(config.LINKEDIN_POST_TIMEZONE)
    post_day = post_time.astimezone(zone).date()
    if mode == "live" and any(
        datetime.fromisoformat(entry["due_at"]).astimezone(zone).date() == post_day for entry in history
    ):
        logger.info("A live post is already scheduled for %s; nothing to do.", post_day)
        _append_summary(f"## LinkedIn post\n\nA post is already scheduled for {post_day}; nothing to do.\n")
        return 0

    posted_ids = {entry["source_id"] for entry in history}
    cooling = cooling_companies(history, now, config.LINKEDIN_COMPANY_COOLDOWN_DAYS)

    jobs = load_active_jobs(state_path)
    site_jobs = site_jobs if site_jobs is not None else load_site_jobs()
    page_locator = page_locator or fetch_page_location

    candidates, skipped = [], Counter()
    for job in jobs:
        candidate, reason = evaluate(
            job, site_jobs, post_time=post_time, max_days_ago=config.LINKEDIN_MAX_POSTED_DAYS_AGO,
            posted_ids=posted_ids, cooling_companies=cooling,
        )
        if candidate:
            candidates.append(candidate)
        else:
            skipped[reason] += 1

    selection = {"considered": len(jobs), "qualified": len(candidates), "skipped": dict(skipped.most_common())}
    logger.info("%d jobs considered, %d qualified. Skipped: %s", len(jobs), len(candidates), selection["skipped"])

    # The last check needs the live page, so it runs on the best few only.
    pick = None
    for candidate in rank(candidates, history)[:MAX_PAGE_CHECKS]:
        confirmed, reason = confirm_page_location(candidate, *page_locator(candidate.site_url))
        if confirmed:
            pick = confirmed
            break
        skipped[reason] += 1
        logger.info("Skipping %s: %s", candidate.site_url, reason)
    selection["skipped"] = dict(skipped.most_common())

    if pick is None:
        # Not a failure: with the pay floor some days simply have nothing worth
        # highlighting. Skip the day and say why, rather than post a weaker job.
        _append_summary(f"## LinkedIn post\n\nNo job qualified for {post_day}, so nothing was posted.\n\n"
                        f"{_selection_line(selection)}\n")
        logger.warning("No job qualified for a LinkedIn post on %s.", post_day)
        # An earlier trigger may still find nothing before the day's jobs import,
        # so only the last chance says "no post today", and only once.
        if email_dir is not None and now >= post_time - FINAL_NOTICE_LEAD:
            if _note_skipped_day(post_dir, post_day, mode, now, selection):
                _write_skip_email(email_dir, selection)
        return 0

    number = len(history)
    theme, label = THEMES[number % len(THEMES)], LABELS[number % len(LABELS)]
    image = f"images/{now:%Y-%m-%d}-{pick.source_id}.png"
    render_card(title=pick.title, company=pick.company, location=pick.location, pay=pick.pay,
                logo_path=pick.logo_path, theme=theme, label=label, out_path=post_dir / image)

    hook = write_hook(pick) if use_ai else None
    entry = {
        "source_id": pick.source_id,
        "mode": mode,
        "created_at": now.isoformat(),
        "due_at": post_time.isoformat(),
        "title": pick.title,
        "company": pick.company,
        "category": pick.category,
        "location": pick.location,
        "state": pick.state,
        "pay": pick.pay,
        "site_url": pick.site_url,
        "image": image,
        "theme": theme.name,
        "label": label,
        "opening_line": "claude" if hook else "template",
        "text": build_post_text(pick, hook or template_hook(pick)),
        "selection": selection,
    }
    _write_json(pending_path, entry)
    logger.info("Prepared %s post: %s at %s (%s)", mode, pick.title, pick.company, pick.source_id)
    return 0


# --- publish -----------------------------------------------------------------

def _wait_until_public(url: str, http: httpx.Client | None = None, timeout_s: int = 180) -> None:
    http = http or httpx.Client(timeout=20.0)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            response = http.head(url)
            if response.status_code == 200 and response.headers.get("content-type", "").startswith("image/"):
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"Image never became reachable at {url}")
        time.sleep(10)


def publish(
    post_dir: Path,
    *,
    now: datetime | None = None,
    buffer_client: BufferClient | None = None,
    wait_for_image: bool = True,
    email_dir: Path | None = None,
) -> int:
    now = now or datetime.now(tz=timezone.utc)
    pending_path = post_dir / "pending.json"
    if not pending_path.exists():
        logger.info("Nothing pending to publish.")
        return 0
    entry = json.loads(pending_path.read_text(encoding="utf-8"))
    image_url = raw_url(entry["image"])
    if wait_for_image:
        _wait_until_public(image_url)

    if entry["mode"] == "live":
        client = buffer_client or BufferClient(config.BUFFER_API_KEY)
        channel = client.linkedin_channel(config.BUFFER_CHANNEL_ID or None)
        due_at = max(datetime.fromisoformat(entry["due_at"]), now + MIN_LEAD)
        post = client.schedule_image_post(channel["id"], entry["text"], image_url, due_at)
        entry.update(buffer_post_id=post.get("id"), due_at=due_at.isoformat(),
                     buffer=f"Scheduled on {channel.get('name')}")
    elif config.BUFFER_API_KEY or buffer_client:
        try:
            client = buffer_client or BufferClient(config.BUFFER_API_KEY)
            channel = client.linkedin_channel(config.BUFFER_CHANNEL_ID or None)
            entry["buffer"] = f"Connected - live posts would go to {channel.get('name')}"
        except (BufferError, httpx.HTTPError) as exc:
            entry["buffer"] = f"Not ready: {exc}"
    else:
        entry["buffer"] = "No Buffer API key saved yet"

    entry["recorded_at"] = now.isoformat()
    history = load_history(post_dir)
    history.append(entry)
    _write_json(post_dir / "history.json", history)
    pending_path.unlink()
    write_readme(post_dir, history)

    block = _entry_markdown(entry, image_url)
    _append_summary("## LinkedIn post\n\n" + block)
    if email_dir is not None:
        _write_email(email_dir, entry)
    logger.info("Recorded %s post %s (%s).", entry["mode"], entry["source_id"], entry["buffer"])
    return 0


# --- reporting ---------------------------------------------------------------

def _selection_line(selection: dict) -> str:
    skipped = ", ".join(f"{reason} {count}" for reason, count in selection["skipped"].items())
    return (f"Why this job: {selection['qualified']} of {selection['considered']} jobs in the feed "
            f"qualified. Skipped: {skipped or 'none'}.")


def _local_time(iso: str) -> str:
    moment = datetime.fromisoformat(iso).astimezone(ZoneInfo(config.LINKEDIN_POST_TIMEZONE))
    return moment.strftime("%a %b %d, %Y at %I:%M %p %Z").replace(" 0", " ")


def _entry_markdown(entry: dict, image_src: str) -> str:
    status = "Scheduled" if entry["mode"] == "live" else "Preview only - would post"
    return (
        f"### {entry['title']} at {entry['company']}\n\n"
        f"{status} {_local_time(entry['due_at'])} · {entry['location']} · {entry['pay']} · "
        f"[job page]({entry['site_url']})  \n"
        f"Buffer: {entry.get('buffer', '-')} · colours: {entry['theme']} · "
        f"opening line: {entry['opening_line']}  \n"
        f"{_selection_line(entry['selection'])}\n\n"
        f'<img src="{image_src}" width="420" alt="{entry["title"]} card">\n\n'
        f"```text\n{entry['text']}\n```\n"
    )


def write_readme(post_dir: Path, history: list[dict]) -> None:
    state = ("**on** - posts are scheduled in Buffer" if config.LINKEDIN_POSTING_ENABLED
             else "**off (preview mode)** - nothing is sent to Buffer or LinkedIn")
    parts = [
        "# LinkedIn posts\n\n",
        "Written automatically by `.github/workflows/linkedin-post.yml` every Monday, "
        f"Wednesday and Friday. Posting is currently {state}. "
        "The switch is `LINKEDIN_POSTING_ENABLED` in that workflow.\n\n",
        "Newest first.\n\n",
    ]
    for entry in reversed(history[-60:]):
        parts.append("---\n\n" + _entry_markdown(entry, entry["image"]) + "\n")
    (post_dir / "README.md").write_text("".join(parts), encoding="utf-8")


def _append_summary(markdown: str) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as summary:
            summary.write(markdown + "\n")


def _write_email(email_dir: Path, entry: dict) -> None:
    email_dir.mkdir(parents=True, exist_ok=True)
    verb = "scheduled" if entry["mode"] == "live" else "preview"
    subject = f"LinkedIn post {verb}: {entry['title']} at {entry['company']}"
    when = _local_time(entry["due_at"])
    body = (
        f"{'Scheduled in Buffer for' if entry['mode'] == 'live' else 'Preview only (not posted). Would post'} {when}.\n\n"
        f"{entry['text']}\n\n"
        f"Buffer: {entry.get('buffer', '-')}\n"
        f"Image: {raw_url(entry['image'])}\n"
        f"All posts: https://github.com/{REPO}/tree/{POST_BRANCH}\n"
    )
    (email_dir / "subject.txt").write_text(subject, encoding="utf-8")
    (email_dir / "body.txt").write_text(body, encoding="utf-8")


def _note_skipped_day(post_dir: Path, day, mode: str, now: datetime, selection: dict) -> bool:
    """Record that `day` had no post. False if it was already recorded, so the
    "no post today" email goes out once however many runs find nothing."""
    path = post_dir / "skipped_days.json"
    days = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if any(entry["date"] == day.isoformat() and entry["mode"] == mode for entry in days):
        return False
    days.append({"date": day.isoformat(), "mode": mode, "noted_at": now.isoformat(), "selection": selection})
    _write_json(path, days)
    return True


def _write_skip_email(email_dir: Path, selection: dict) -> None:
    email_dir.mkdir(parents=True, exist_ok=True)
    (email_dir / "subject.txt").write_text("LinkedIn: no post today - no job met the bar", encoding="utf-8")
    (email_dir / "body.txt").write_text(
        "No job qualified for today's LinkedIn post, so nothing was posted. Nothing is "
        "broken; the next run tries again.\n\n"
        f"{_selection_line(selection)}\n\n"
        f"All posts: https://github.com/{REPO}/tree/{POST_BRANCH}\n",
        encoding="utf-8",
    )
