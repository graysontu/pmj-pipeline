"""Chooses the job for the next LinkedIn post, and records why every other job
wasn't chosen.

A job qualifies only if everything the post shows is present and trustworthy:
it is live on the site right now, has an explicit salary, a city and state the
site itself displays, a usable logo, and a title that reads cleanly on the
image. Maintenance roles are left out for now (see EXCLUDED_CATEGORIES).

Among qualifying jobs the choice keeps a balanced mix of role types, avoids
repeating a company within the cooldown, and spreads posts across states.
"""

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from pipeline import config
from pipeline.geo import _US_STATE_NAMES, _US_STATES, parse_location
from pipeline.linkedin.card import logo_problem, title_fits

LOGO_DIR = Path(__file__).parent.parent.parent / "output" / "logos"

# Maintenance roles are not posted to LinkedIn for now. Groundskeepers and porters
# are part of the maintenance team at a property, so they are excluded with it.
# Remove a category from this set to start posting it.
EXCLUDED_CATEGORIES = frozenset({
    "Maintenance Technician Jobs",
    "Maintenance Supervisor Jobs",
    "Groundskeeper & Porter Jobs",
})

# Backstop for a maintenance job the classifier filed under another category.
_MAINTENANCE_TITLE = re.compile(
    r"\b(maint\w*|technicians?|techs?|porters?|groundskeep\w*|grounds|landscap\w*|"
    r"horticultur\w*|housekeep\w*|janitor\w*|custodi\w*|hvac|painters?|make[- ]?ready|"
    r"engineers?|facilit\w*|handyman|electricians?|plumbers?|service manager)\b",
    re.IGNORECASE,
)

# A cleaned title must contain one of these to count as a job title at all.
_ROLE_WORDS = {
    "manager", "consultant", "agent", "specialist", "coordinator", "assistant",
    "associate", "director", "administrator", "admin", "supervisor", "concierge",
    "accountant", "analyst", "representative", "bookkeeper", "receptionist",
    "lead", "officer", "generalist", "clerk", "professional", "advisor",
    "executive", "president", "controller", "trainer", "ambassador", "liaison",
    "inspector", "floater", "auditor", "vp", "svp", "evp",
}
# Words that may follow a role word and still belong to the title.
_TITLE_TAIL = {"i", "ii", "iii", "iv", "trainee"}
_CONNECTORS = {"of", "for", "in", "to", "and", "&", "/"}

# Titles that aren't one job, or aren't in English.
_NOT_A_POSTING = re.compile(
    r"\b(job fair|hiring event|career fair|open house|general application|"
    r"talent (pool|community)|multiple (positions|openings)|evergreen|"
    r"feria|empleo|trabajos?|limpieza|mantenimiento|gerente|asistente)\b",
    re.IGNORECASE,
)

_KEEP_UPPER = {"HOA", "LIHTC", "HUD", "RV", "II", "III", "IV", "CAM", "APM", "HR",
               "IT", "AP", "AR", "DC", "US", "CMCA", "PM", "VP", "SVP", "EVP"}
_SMALL_WORDS = {"and", "of", "the", "for", "at", "in", "to", "a", "an", "&"}

MAX_TITLE_WORDS = 5
MAX_TITLE_CHARS = 42

# Explicit salaries outside these bounds are almost certainly extraction errors.
_PAY_BOUNDS = {"hourly": (10, 150), "yearly": (20_000, 400_000)}
_PAY_SUFFIX = {"hourly": "/hr", "yearly": "/yr"}


# --- titles ------------------------------------------------------------------

def _smart_case(title: str) -> str:
    words = []
    for index, word in enumerate(title.split()):
        if word.upper() in _KEEP_UPPER:
            words.append(word.upper())
        elif index and word.lower() in _SMALL_WORDS:
            words.append(word.lower())
        else:
            words.append("-".join(part.capitalize() for part in word.split("-")))
    return " ".join(words)


def _has_role_word(text: str) -> bool:
    return any(word in _ROLE_WORDS for word in re.findall(r"[a-z]+", text.lower()))


_ROLE_ALTERNATION = "|".join(sorted(_ROLE_WORDS, key=len, reverse=True))


def _split_after_role(title: str) -> str:
    """Put a separator where a property or place is glued onto the role:
    "Relations Manager-Cortland Fossil Creek", "Specialist at Antero Apartments"."""
    title = re.sub(rf"\b({_ROLE_ALTERNATION})-(?=(?-i:[A-Z0-9]))", r"\1 - ", title, flags=re.IGNORECASE)
    return re.sub(rf"\b({_ROLE_ALTERNATION}) at ", r"\1 - ", title, flags=re.IGNORECASE)


def _drop_trailing_descriptors(segment: str) -> str:
    """"Community Manager Manufactured Housing Community" -> "Community Manager".

    Cuts after the last role word unless the next word connects to it ("Manager
    of Leasing"), in which case the title is left whole and may be skipped as
    too long instead. The last one, because the earlier ones are usually part of
    the title: "Assistant Community Manager"."""
    words = segment.split()
    roles = [i for i, w in enumerate(words) if w.lower().strip(",.") in _ROLE_WORDS]
    if not roles:
        return segment
    end = roles[-1]
    while end + 1 < len(words) and words[end + 1].lower() in _TITLE_TAIL:
        end += 1
    if end + 1 < len(words) and words[end + 1].lower() not in _CONNECTORS:
        return " ".join(words[: end + 1])
    return segment


def clean_title(raw: str) -> tuple[str | None, str | None]:
    """The title as it should appear on the card, or (None, reason) when the job
    should be skipped instead.

    Employers pad titles with property names, requisition numbers, bonuses and
    shift notes ("Leasing Consultant - Lakeline at Bartram Park",
    "LIHTC Leasing Consultant (30024)"). Those are cut back to the role. A title
    that is still long, stuffed with keywords, not in English, or not a single
    job at all is skipped rather than shown."""
    title = unicodedata.normalize("NFKC", raw or "").replace(" ", " ")
    title = re.sub(r"\s+", " ", title).strip()
    if _NOT_A_POSTING.search(title):
        return None, "not a single English job posting"

    title = re.sub(r"\([^)]*\)?|\[[^\]]*\]?", " ", title)
    title = _split_after_role(title)
    segments = re.split(r"\s+[-–—|/]\s*|\s*[-–—|]\s+|:\s+|,\s+|\s+@\s+", title)
    segment = next((s for s in segments if _has_role_word(s)), None)
    if segment is None:
        return None, "no recognisable job title"

    segment = re.sub(r"\bfull(?:[- ]?time)?\s*(?:and|&|/|or)\s*part[- ]?time\b", " ", segment,
                     flags=re.IGNORECASE)
    segment = re.sub(r"\b(full[- ]?time|ft)\b", " ", segment, flags=re.IGNORECASE)
    segment = re.sub(r"\bpart[- ]?time\b", "Part-Time", segment, flags=re.IGNORECASE)
    segment = " ".join(w for w in segment.split() if len(re.findall(r"\d", w)) < 3)
    segment = re.sub(r"\s+", " ", segment).strip(" -,.#")
    if not segment:
        return None, "no recognisable job title"

    if re.search(r"[$!?*%]|\d", segment):
        return None, "promotional or coded title"
    if segment.isupper() or segment.islower():
        segment = _smart_case(segment)
    segment = _drop_trailing_descriptors(segment)

    words = [w for w in segment.split() if w != "&"]
    if len(words) > MAX_TITLE_WORDS or len(segment) > MAX_TITLE_CHARS:
        return None, "title too long"
    significant = [w.lower() for w in words if w.lower() not in _SMALL_WORDS]
    if len(significant) != len(set(significant)):
        return None, "repetitive title"
    if len(words) == 1 and words[0].lower() in _ROLE_WORDS - {"concierge", "bookkeeper", "receptionist", "accountant"}:
        return None, "title too vague"
    if not title_fits(segment):
        return None, "title too long for the image"
    return segment, None


# --- pay ---------------------------------------------------------------------

def _money(value: float, schedule: str) -> str:
    if schedule == "yearly":
        thousands = round(value / 1000, 1)
        text = f"{thousands:.1f}".rstrip("0").rstrip(".")
        return f"${text}K"
    if value == int(value):
        return f"${int(value)}"
    return f"${value:.2f}"


def format_pay(job: dict) -> tuple[str | None, str | None]:
    schedule = (job.get("salary_schedule") or "").lower()
    try:
        low = float(job.get("salary_min"))
        high = float(job.get("salary_max"))
    except (TypeError, ValueError):
        return None, "no salary"
    if schedule not in _PAY_BOUNDS:
        return None, f"unsupported salary schedule {schedule or 'missing'}"
    floor, ceiling = _PAY_BOUNDS[schedule]
    if not (floor <= low <= high <= ceiling):
        return None, "implausible salary"
    low_text, high_text = _money(low, schedule), _money(high, schedule)
    span = low_text if low_text == high_text else f"{low_text}–{high_text}"
    return span + _PAY_SUFFIX[schedule], None


def pay_at_or_below_floor(job: dict) -> bool:
    """Whether the bottom of the pay range is too low to highlight on LinkedIn:
    $21/hr or less, or $45,000/yr or less (config LINKEDIN_MIN_*_PAY_FLOOR).
    Call after format_pay has accepted the salary."""
    floors = {"hourly": config.LINKEDIN_MIN_HOURLY_PAY_FLOOR,
              "yearly": config.LINKEDIN_MIN_YEARLY_PAY_FLOOR}
    schedule = (job.get("salary_schedule") or "").lower()
    return float(job["salary_min"]) <= floors[schedule]


# --- location ----------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


_STATE_NAME_TO_CODE = {name.lower(): code for name, code in _US_STATE_NAMES.items()}


def feed_location(job: dict) -> tuple[str | None, str | None]:
    """"City, ST" from the job record, or a reason to skip. The same parse the
    feed uses, plus a guard against a state abbreviation read as a city
    ("Floating, MA, NH, RI." parses as city "Ma")."""
    city, state = parse_location(job.get("location", ""))
    if not city or state not in _US_STATES:
        return None, "no city and state"
    if len(_norm(city)) < 3 or city.upper() in _US_STATES or city.lower() in _STATE_NAME_TO_CODE:
        return None, "city looks wrong"
    return f"{city}, {state}", None


def confirm_page_location(
    candidate: "Candidate", locality: str | None, region: str | None
) -> tuple["Candidate | None", str | None]:
    """Check the card's location against what the job's page on the site shows.

    JobBoardly silently drops a city it cannot geocode, so the feed's city is
    only used when the page kept it. Returns the candidate with the page's own
    spelling of the city ("Mckinney" -> "McKinney")."""
    city, state = candidate.location.rsplit(", ", 1)
    if not locality:
        return None, "site page shows no city"
    if _norm(locality) != _norm(city):
        return None, "site page shows a different city"
    region_code = _STATE_NAME_TO_CODE.get((region or "").strip().lower(), (region or "").strip().upper())
    if region_code != state:
        return None, "site page shows a different state"
    return replace(candidate, location=f"{locality.strip()}, {state}"), None


# --- choosing ----------------------------------------------------------------

@dataclass(frozen=True)
class SiteJob:
    url: str
    sticky: bool
    published_at: datetime | None = None  # the date the site's "posted N days ago" counts from


def site_days_ago(elapsed: timedelta) -> int:
    """The N in the site's "posted N days ago" label for a job this old.

    The site rounds rather than counting whole days (Rails-style: 2 days 23 hours
    reads "3 days ago"), so "5 days ago" starts at 4.5 days. Measured against
    live job pages on 2026-09-28. Under 42 hours it reads "1 day" or hours."""
    minutes = elapsed.total_seconds() / 60
    if minutes < 2520:
        return 1 if minutes >= 1440 else 0
    return math.floor(minutes / 1440 + 0.5)


@dataclass(frozen=True)
class Candidate:
    source_id: str
    title: str
    company: str
    location: str
    pay: str
    category: str
    logo_path: Path
    site_url: str
    published_at: datetime
    description_html: str

    @property
    def state(self) -> str:
        return self.location.rsplit(", ", 1)[-1]


def evaluate(
    job: dict,
    site_jobs: dict[str, SiteJob],
    *,
    post_time: datetime,
    max_days_ago: int,
    posted_ids: set[str],
    cooling_companies: set[str],
    logo_dir: Path = LOGO_DIR,
) -> tuple[Candidate | None, str | None]:
    """(candidate, None) when the job may be posted, else (None, reason).

    post_time is when the LinkedIn post goes live: at that moment the job's page
    must still read "posted max_days_ago days ago" or newer."""
    site = site_jobs.get((job.get("apply_url") or "").strip())
    if site is None:
        return None, "not live on the site"
    if site.sticky:
        return None, "pinned employer post"
    if site.published_at is None:
        return None, "no posting date on the site"
    if site_days_ago(post_time - site.published_at) > max_days_ago:
        return None, f"posted more than {max_days_ago} days ago on the site"
    if job.get("category") in EXCLUDED_CATEGORIES or _MAINTENANCE_TITLE.search(job.get("title") or ""):
        return None, "maintenance role"
    if job.get("source_id") in posted_ids:
        return None, "already posted"
    if (job.get("company") or "").strip().lower() in cooling_companies:
        return None, "company posted recently"

    pay, reason = format_pay(job)
    if reason:
        return None, reason
    if pay_at_or_below_floor(job):
        return None, "pay at or below the LinkedIn floor"
    location, reason = feed_location(job)
    if reason:
        return None, reason
    logo_name = (job.get("company_logo_url") or "").rsplit("/", 1)[-1]
    if not logo_name:
        return None, "no logo"
    logo_path = logo_dir / logo_name
    reason = logo_problem(logo_path)
    if reason:
        return None, reason
    title, reason = clean_title(job.get("title") or "")
    if reason:
        return None, reason

    return Candidate(
        source_id=job["source_id"],
        title=title,
        company=job["company"].strip(),
        location=location,
        pay=pay,
        category=job.get("category") or "",
        logo_path=logo_path,
        site_url=site.url,
        published_at=site.published_at,
        description_html=job.get("rewritten_description") or "",
    ), None


def cooling_companies(history: list[dict], now: datetime, cooldown_days: int) -> set[str]:
    cutoff = now - timedelta(days=cooldown_days)
    return {
        entry["company"].strip().lower()
        for entry in history
        if datetime.fromisoformat(entry["created_at"]) >= cutoff
    }


def rank(candidates: list[Candidate], history: list[dict]) -> list[Candidate]:
    """Qualifying jobs, best first: the role type posted least in the last
    month (a balanced mix, no type favoured), then a state not used in the last
    few posts, then the newest job. Deterministic, so a re-run picks the same job."""
    recent = history[-12:]
    category_counts = Counter(entry["category"] for entry in recent)
    last_seen = {entry["category"]: i for i, entry in enumerate(recent)}
    recent_states = {entry.get("state") for entry in history[-3:]}

    def key(c: Candidate):
        return (
            category_counts.get(c.category, 0),
            last_seen.get(c.category, -1),
            c.state in recent_states,
            -c.published_at.timestamp(),
            c.source_id,
        )

    return sorted(candidates, key=key)
