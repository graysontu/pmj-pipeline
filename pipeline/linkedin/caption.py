"""The text of each LinkedIn post: a short opening line, the facts, the link to
the job on the site, and a few hashtags.

Only the opening line is written by Claude. Every fact (location, pay, company,
link) comes straight from the job record, so the model cannot misstate them.
If the model call fails or returns something unusable, a plain template line is
used instead - a post never fails because of its caption.
"""

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import anthropic
from bs4 import BeautifulSoup

from pipeline.config import ANTHROPIC_API_KEY
from pipeline.linkedin.picker import Candidate

logger = logging.getLogger(__name__)

HOOK_MODEL = "claude-opus-5"
HOOK_MAX_CHARS = 320  # the prompt asks for ~240; this is only the hard stop
HOOK_ATTEMPTS = 2

# Tags every link so the site's analytics can count visitors LinkedIn sends.
UTM_PARAMS = {"utm_source": "linkedin", "utm_medium": "social", "utm_campaign": "job_post"}

ROLE_HASHTAGS = {
    "Property Manager Jobs": "PropertyManager",
    "Assistant Property Manager Jobs": "AssistantPropertyManager",
    "Community Manager Jobs": "CommunityManager",
    "Regional Property Manager Jobs": "RegionalManager",
    "Leasing Consultant Jobs": "LeasingJobs",
    "HOA & Association Manager Jobs": "HOAManagement",
    "Commercial Property Manager Jobs": "CommercialRealEstate",
    "Asset Manager Jobs": "AssetManagement",
    "Real Estate Admin & Coordinator Jobs": "RealEstateJobs",
}

# Grayson's direction (2026-09-28): helpful, professional, somewhat personable.
# Not cute, salesy or cocky; no em dashes; no "it's not X, it's Y" framing.
# _clean_hook enforces the mechanical rules, so a line that breaks them is never posted.
SYSTEM_PROMPT = """\
You write the opening line of a LinkedIn post for PropertyManagementJobs.us, a job \
board for property management careers. Each post shares one job opening. Below your \
line, the post lists the job's location, pay and company, then a link to the full \
listing.

Write one or two sentences, around 200 to 240 characters, that tell someone in \
property management what this job is and who it would suit. Sound like a \
knowledgeable, friendly recruiter describing a role to a colleague: plain, specific \
and accurate. Use one or two concrete details the description states, such as the \
kind or size of property, the main day-to-day work, the schedule, or training the \
employer offers. Use only facts from the description, and don't repeat the pay or \
location. The company's name is listed below your line, so there's no need to open \
with it; vary how you begin.

Write complete, natural sentences. Keep the tone calm and matter-of-fact: no hype, \
slogans, dramatic fragments or bold claims about where the job will lead. Never use \
em dashes or en dashes; use commas or periods instead (ordinary hyphenated words \
like "two-site" or "65-home" are fine). Don't frame the job by what \
it isn't ("It's not X, it's Y", "not just X, but Y"). No exclamation points, \
hashtags, emojis, links or quotation marks.

Reply with the line only."""


def tracked_url(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update(UTM_PARAMS)
    return urlunsplit(parts._replace(query=urlencode(query)))


def _plain_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


# Dashes used as punctuation: em, en, figure and horizontal-bar dashes, and the
# spaced or doubled hyphens that stand in for them. Hyphenated words are fine.
_DASHES = re.compile(r"[‒–—―]|\s-+\s|--")
# "It's not X, it's Y" / "not just X, but Y" / "This isn't X. It's Y."
_NOT_X_BUT_Y = re.compile(
    r"\bnot (?:just|only|simply|merely)\b"
    r"|\b(?:it'?s|it is|this is|that'?s|that is)\s+not\b"
    r"|\b(?:isn'?t|is not|aren'?t|are not|wasn'?t)\b[^.!?]*[.;,:]\s*(?:it'?s|it is|this is|they'?re|it'?s about)\b",
    re.IGNORECASE,
)


def _clean_hook(text: str) -> str | None:
    """The line, tidied, or None if it breaks a rule and must not be posted."""
    hook = re.sub(r"\s+", " ", text or "").strip().strip("\"'“”")
    plain = hook.replace("’", "'")
    if not 20 <= len(hook) <= HOOK_MAX_CHARS:
        return None
    if re.search(r"https?://|www\.|#|\$|!", hook):
        return None
    if _DASHES.search(hook) or _NOT_X_BUT_Y.search(plain):
        return None
    return hook


def write_hook(candidate: Candidate, client: anthropic.Anthropic | None = None) -> str | None:
    """Claude's opening line, or None if it couldn't produce a usable one."""
    if client is None:
        if not ANTHROPIC_API_KEY:
            logger.info("No ANTHROPIC_API_KEY; using the template opening line.")
            return None
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0, max_retries=2)

    prompt = (
        f"Job title: {candidate.title}\n"
        f"Company: {candidate.company}\n"
        f"Category: {candidate.category}\n\n"
        f"Job description:\n{_plain_text(candidate.description_html)}"
    )
    # A line that breaks a rule gets one more try before the template is used.
    for attempt in range(HOOK_ATTEMPTS):
        try:
            response = client.beta.messages.create(
                model=HOOK_MODEL,
                max_tokens=4000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "low"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - any failure, API or SDK, falls back to the template
            logger.warning("Opening line not generated (%s: %s); using the template.", type(exc).__name__, exc)
            return None

        if response.stop_reason != "end_turn":
            logger.warning("Opening line stopped with %s; using the template.", response.stop_reason)
            return None
        text = "".join(block.text for block in response.content if block.type == "text")
        hook = _clean_hook(text)
        if hook is not None:
            return hook
        logger.warning("Opening line broke a rule (attempt %d): %r", attempt + 1, text[:300])
    logger.warning("No usable opening line; using the template.")
    return None


def template_hook(candidate: Candidate) -> str:
    city = candidate.location.rsplit(", ", 1)[0]
    return f"New opening: {candidate.title} at {candidate.company} in {city}."


def _city_hashtag(location: str) -> str:
    city = location.rsplit(", ", 1)[0]
    return "".join(part.capitalize() if part.islower() else part
                   for part in re.findall(r"[A-Za-z]+", city)) + "Jobs"


def build_post_text(candidate: Candidate, hook: str) -> str:
    hashtags = ["#PropertyManagement"]
    if candidate.category in ROLE_HASHTAGS:
        hashtags.append("#" + ROLE_HASHTAGS[candidate.category])
    hashtags += ["#NowHiring", "#" + _city_hashtag(candidate.location)]
    return (
        f"{hook}\n\n"
        f"\U0001F4CD {candidate.location}\n"
        f"\U0001F4B0 {candidate.pay}\n"
        f"\U0001F3E2 {candidate.company}\n\n"
        f"See the full job and apply \U0001F449 {tracked_url(candidate.site_url)}\n\n"
        + " ".join(hashtags)
    )
