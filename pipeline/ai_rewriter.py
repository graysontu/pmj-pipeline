import asyncio
import hashlib
import html as html_module
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
from bs4 import BeautifulSoup
from pydantic import BaseModel

from pipeline.ai_classifier import ClassifiedJob
from pipeline.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2500
TEMPERATURE = 0.85
MAX_CONCURRENT = 3

SALARY_MODEL = "claude-haiku-4-5"
SALARY_MAX_TOKENS = 80
SALARY_MAX_CONCURRENT = 2
SALARY_REQUEST_INTERVAL = 3.0

SALARY_SYSTEM = """\
You extract explicit salary ranges from US property management job descriptions. Return ONLY valid JSON, no other text.

Look for explicit compensation in the description:
- If the description mentions a specific dollar amount per hour (e.g. "$22/hr", "$27 to $29 per hour"), extract it and use "hourly".
- If it mentions an annual salary or salary range (e.g. "$68,000 to $75,000", "72,600 annually"), extract it and use "yearly".
- If NO explicit salary or pay rate is mentioned anywhere in the description, return null for all fields.

Return exactly one of:
- {"salary_min": integer, "salary_max": integer, "salary_currency": "USD", "salary_schedule": "hourly" or "yearly"}
- {"salary_min": null, "salary_max": null, "salary_currency": null, "salary_schedule": null}\
"""
REWRITE_CACHE_PATH = Path(__file__).parent.parent / "data" / "rewrite_cache.json"
QUALITY_SAMPLES_PATH = Path(__file__).parent.parent / "output" / "quality_samples.html"

ALLOWED_TAGS = {"p", "h2", "h3", "ul", "li", "strong", "em"}

BANNED_PHRASES = [
    "delve into",
    "navigate the complexities",
    "in today's",
    "moreover",
    "furthermore",
    "it's important to note",
    "robust",
    "seamless",
    "game-changer",
    "game changer",
    "pivotal",
    "tapestry",
    "ever-evolving",
    "fast-paced environment",
    "passionate about",
    "apply today",
    "don't miss this opportunity",
    "join us on this journey",
    "leverage",
    "unlock",
]

PERSONAS = {
    "veteran_insider": {
        "voice": "You write like a 15-year property management veteran who has seen it all. Conversational but knowing. You drop industry shorthand naturally without explaining it. You're occasionally a little dry. You don't oversell roles.",
        "tone_notes": "First person plural ('we've seen', 'in our industry') is fine occasionally. Acknowledge real challenges of the role.",
    },
    "pragmatic_recruiter": {
        "voice": "You write like a no-nonsense recruiter who respects the reader's time. Direct. Concrete. You cut filler words. You focus on what the job actually entails day-to-day.",
        "tone_notes": "Short paragraphs. Active voice. No hype. State things plainly.",
    },
    "career_strategist": {
        "voice": "You write like a career coach who specializes in property management. You frame the role in terms of where it leads and what it builds. You're thoughtful about how skills compound.",
        "tone_notes": "Reference adjacent roles and growth paths. Don't be preachy. Treat the reader as smart.",
    },
    "local_market_analyst": {
        "voice": "You write like someone who knows specific rental markets cold. You mention local market dynamics where they're genuinely relevant.",
        "tone_notes": "Only invoke local knowledge if you actually have something accurate to say about that market. If unsure about specifics, drop the local angle entirely. Never fabricate.",
    },
    "operations_nerd": {
        "voice": "You write like an operator who lives and breathes property KPIs. NOI, occupancy, delinquency, T-12, traffic-to-lease ratios. You make the operational reality of the role tangible.",
        "tone_notes": "Use numbers and metrics where natural. Don't lecture. Make ops sound interesting, not jargony.",
    },
    "people_first_hr": {
        "voice": "You write like an HR partner who genuinely cares about team fit. You highlight how this role interacts with residents, vendors, owners, and other team members.",
        "tone_notes": "Don't be saccharine. Real workplaces have real dynamics. Acknowledge them.",
    },
    "skills_coach": {
        "voice": "You write like someone who teaches the property management craft. You frame the role around what skills you'll use, develop, and demonstrate.",
        "tone_notes": "Distinguish between skills the role REQUIRES vs skills the role BUILDS. Both matter.",
    },
    "realist": {
        "voice": "You write like someone who refuses to whitewash jobs. You acknowledge real challenges (after-hours emergencies, difficult residents, paperwork loads) without being negative or dramatic.",
        "tone_notes": "Honesty as a feature. The reader trusts you because you're not pretending the job is glamorous.",
    },
}

STRUCTURES = [
    {
        "id": "context_first",
        "guide": "Open with one or two sentences of role context (what this kind of role usually involves at this kind of company), then move into the actual responsibilities, then end with growth or culture angle. Use 2-3 H2 headings.",
    },
    {
        "id": "scenario_open",
        "guide": "Open with a brief, specific scenario or moment from the day-to-day reality of this role (one or two sentences), then transition into formal responsibilities and requirements. Use 2 H2 headings.",
    },
    {
        "id": "what_you_do",
        "guide": "Lead with a tight 'What you'll actually do' section that's specific to the role, then 'What you bring', then a closing context paragraph. 2-3 H2 headings.",
    },
    {
        "id": "industry_lens",
        "guide": "Open with a sentence or two of industry context that this role sits inside (e.g. why this role exists, what it solves), then specifics. 2 H2 headings.",
    },
    {
        "id": "skills_framed",
        "guide": "Frame the description around skills. Open with what skills this role uses heavily, then describe responsibilities through that lens. End with what's distinctive about doing this role at this company. 2-3 H2 headings.",
    },
    {
        "id": "minimal_clean",
        "guide": "Minimal structure. Mostly prose paragraphs with at most one H2 and one short list of qualifications. Trust the prose to carry it.",
    },
]

STATIC_SYSTEM_PROMPT = """\
You are rewriting a job posting for a niche property management job board called PropertyManagementJobs.us. Your task is to produce a unique, valuable, human-sounding description that adds real insight beyond the original posting.

== HARD WRITING RULES ==

NEVER use these characters or phrases (these are AI tells):
- Em dashes (—) or en dashes (–). Use periods, commas, colons, or parentheses instead.
- Phrases: "delve into", "navigate the complexities", "in today's [anything] world", "moreover", "furthermore", "it's important to note", "robust", "leverage" as a verb, "seamless", "unlock", "elevate", "game-changer", "pivotal", "tapestry", "landscape" as a metaphor, "ever-evolving", "fast-paced environment" (cliche), "passionate about" (overused).
- Rhetorical openers: "Are you...?", "Looking for...?", "Do you have what it takes?"
- Stock CTAs: "Apply today!", "Don't miss this opportunity!", "Join us on this journey!"

ALWAYS:
- Use contractions naturally (you're, don't, we're, it's)
- Vary sentence length aggressively. Mix 3-5 word sentences with 20-30 word sentences in the same paragraph.
- Use active voice as the default
- Use property management vocabulary correctly when relevant: turn, make-ready, punch list, lease-up, T-12, NOI, delinquency, MTM, concessions, capex, RUBS, T-3, T-12, occupancy, traffic, Class A/B/C, garden-style, mid-rise, high-rise, fee management, third-party management, in-house. Only use terms that genuinely fit this specific role; do not stuff them.
- Reflect the actual role and requirements from the source posting (paraphrased, not copied)

NEVER:
- Copy more than 8 consecutive words from the original posting verbatim
- Invent specific facts about the company, property, salary, benefits, or perks not present in the original
- Use the same opening pattern across jobs
- Bullet-list everything. Use prose for context; lists only for genuinely list-shaped content (e.g., qualifications)
- Always title sections "Role Overview" or "What You'll Do". Use varied, specific section headings (or skip headings if the structure says minimal).

== VALUE-ADD REQUIREMENT ==

Include ONE OR TWO additions beyond what the original posting says. Pick from this menu only if it genuinely fits this specific role and company. Do NOT force one in.

- A day-in-the-life angle that's specific and concrete to this role
- Career trajectory: where this role typically leads in property management, briefly
- What separates strong candidates from average ones in this specific role
- Local market color (only if the location is one you can speak to accurately, otherwise skip)
- How this role connects to the broader operations of a property
- Skills from this role that transfer to other PM roles
- Honest acknowledgment of common challenges (e.g., after-hours, difficult residents) when warranted
- Industry context: how recent trends affect this role (smart home tech, AI leasing, RUBS billing, fee compression, regulatory scrutiny, build-to-rent, single-family rental at scale)

The value-add should feel like an insight, not a paragraph filler. One paragraph or one short list. Sometimes a single well-placed sentence is the right value-add.

== OUTPUT FORMAT ==

Return ONLY valid HTML using exclusively these tags: <p>, <h2>, <h3>, <ul>, <li>, <strong>, <em>. No other tags. No markdown. No code fences. No preamble.

Length: 350 to 650 words. Vary the length across jobs; do not always hit the same word count.

== OUTPUT QUALITY CHECK (mental) ==

Before finalizing, verify:
1. No em dashes anywhere
2. No banned phrases
3. Opening doesn't follow a pattern you've used before
4. The value-add is specific and actually useful, not generic
5. The voice matches the assigned persona
6. The structure matches the assigned variant
7. Industry vocab used correctly (or not at all)\
"""

PERSONA_STRUCTURE_TEMPLATE = """\
== WRITING VOICE FOR THIS JOB ==
{persona_voice}
{persona_tone_notes}

== STRUCTURE FOR THIS JOB ==
{structure_guide}\
"""


class RewrittenJob(ClassifiedJob):
    rewritten_description: str
    persona_used: str
    structure_used: str
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: str = "USD"
    salary_schedule: str = "yearly"


def _load_cache() -> dict[str, dict]:
    if REWRITE_CACHE_PATH.exists():
        try:
            with open(REWRITE_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load rewrite cache: %s. Starting fresh.", exc)
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    REWRITE_CACHE_PATH.parent.mkdir(exist_ok=True)
    with open(REWRITE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _pick_persona_and_structure(source_id: str) -> tuple[str, dict, dict]:
    h = hashlib.sha256(source_id.encode()).hexdigest()
    persona_seed = int(h[:16], 16)
    structure_seed = int(h[16:32], 16)
    persona_keys = list(PERSONAS.keys())
    persona_name = persona_keys[persona_seed % len(persona_keys)]
    structure = STRUCTURES[structure_seed % len(STRUCTURES)]
    return persona_name, PERSONAS[persona_name], structure


def clean_output(raw: str) -> tuple[str, int]:
    # Strip markdown code fences
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) >= 3 else parts[0]
        if raw.startswith("html"):
            raw = raw[4:]
        raw = raw.strip()

    # Replace em dashes
    raw = raw.replace("\u2014", ", ")

    # Replace en dashes: between digits use hyphen, otherwise comma-space
    raw = re.sub(r"(\d)\s*\u2013\s*(\d)", r"\1-\2", raw)
    raw = raw.replace("\u2013", ", ")

    # Strip preamble before first block-level tag
    p_pos = raw.find("<p>")
    h2_pos = raw.find("<h2>")
    candidates = [x for x in [p_pos, h2_pos] if x != -1]
    if candidates:
        first = min(candidates)
        if first > 0:
            raw = raw[first:]

    # Normalize headings and lists to allowed tags, then unwrap everything else
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all("h1"):
        tag.name = "h2"
    for tag in soup.find_all(["h4", "h5", "h6"]):
        tag.name = "h3"
    for tag in soup.find_all("ol"):
        tag.name = "ul"
    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
    cleaned = str(soup)

    # Check for banned phrases and count warnings
    lower = cleaned.lower()
    warnings = 0
    for phrase in BANNED_PHRASES:
        if phrase in lower:
            logger.warning("Banned phrase detected in rewrite: '%s'", phrase)
            warnings += 1

    return cleaned, warnings


def _build_system_prompt(persona: dict, structure: dict) -> list[dict]:
    variable = PERSONA_STRUCTURE_TEMPLATE.format(
        persona_voice=persona["voice"],
        persona_tone_notes=persona["tone_notes"],
        structure_guide=structure["guide"],
    )
    return [
        {"type": "text", "text": STATIC_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": variable},
    ]


def _word_count(html: str) -> int:
    return len(BeautifulSoup(html, "html.parser").get_text().split())


async def _call_api(
    client: anthropic.AsyncAnthropic,
    system_prompt: list[dict],
    user_prompt: str,
    semaphore: asyncio.Semaphore,
) -> str:
    async with semaphore:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    return response.content[0].text.strip()


async def _rewrite_one(
    client: anthropic.AsyncAnthropic,
    job: ClassifiedJob,
    cache: dict[str, dict],
    semaphore: asyncio.Semaphore,
    counter: list[int],
    total: int,
) -> tuple[RewrittenJob, int]:
    persona_name, persona, structure = _pick_persona_and_structure(job.source_id)

    if job.source_id in cache:
        cached = cache[job.source_id]
        counter[0] += 1
        if counter[0] % 25 == 0 or counter[0] == total:
            logger.info("Rewriting %d/%d...", counter[0], total)
        return RewrittenJob(
            **job.model_dump(),
            rewritten_description=cached["html"],
            persona_used=cached.get("persona", persona_name),
            structure_used=cached.get("structure", structure["id"]),
        ), 0

    system_prompt = _build_system_prompt(persona, structure)
    description_source = job.description_text or BeautifulSoup(job.description_html, "lxml").get_text(" ", strip=True)
    user_prompt = (
        f"== JOB TO REWRITE ==\n\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Category: {job.category}\n\n"
        f"Original description (use as factual source, paraphrase, do not copy):\n\n"
        f"{description_source[:6000]}"
    )

    write_to_cache = True
    ban_count = 0
    try:
        raw = await _call_api(client, system_prompt, user_prompt, semaphore)
        cleaned, ban_count = clean_output(raw)
    except anthropic.RateLimitError:
        logger.warning("Rate limit on rewrite for %s. Waiting 60s.", job.source_id)
        await asyncio.sleep(60)
        try:
            raw = await _call_api(client, system_prompt, user_prompt, semaphore)
            cleaned, ban_count = clean_output(raw)
        except Exception as exc:
            logger.error("Rewrite retry failed for %s: %s. Using truncated original.", job.source_id, exc)
            cleaned = f"<p>{html_module.escape(description_source[:500])}</p>"
            write_to_cache = False
    except anthropic.APIError as exc:
        logger.error("API error rewriting %s: %s. Using truncated original.", job.source_id, exc)
        cleaned = f"<p>{html_module.escape(description_source[:500])}</p>"
        write_to_cache = False

    if write_to_cache:
        cache[job.source_id] = {
            "html": cleaned,
            "persona": persona_name,
            "structure": structure["id"],
        }

    counter[0] += 1
    if counter[0] % 10 == 0 or counter[0] == total:
        logger.info("Rewriting %d/%d...", counter[0], total)

    return RewrittenJob(
        **job.model_dump(),
        rewritten_description=cleaned,
        persona_used=persona_name,
        structure_used=structure["id"],
    ), ban_count


async def _batch_rewrite_async(jobs: list[ClassifiedJob]) -> tuple[list[RewrittenJob], int]:
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not set. Check your .env file.")

    cache = _load_cache()
    cached_count = sum(1 for j in jobs if j.source_id in cache)
    logger.info(
        "Rewriting %d jobs. %d already cached, %d need API calls.",
        len(jobs), cached_count, len(jobs) - cached_count,
    )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    counter: list[int] = [0]
    total = len(jobs)

    tasks = [_rewrite_one(client, job, cache, semaphore, counter, total) for job in jobs]
    results = await asyncio.gather(*tasks)

    _save_cache(cache)

    rewritten = [r[0] for r in results]
    total_warnings = sum(r[1] for r in results)
    return rewritten, total_warnings


def batch_rewrite(jobs: list[ClassifiedJob]) -> tuple[list[RewrittenJob], int]:
    """Rewrite descriptions for a list of classified PM jobs. Returns (jobs, ban_warning_count)."""
    if not jobs:
        return [], 0
    return asyncio.run(_batch_rewrite_async(jobs))


async def _extract_salary_one(
    client: anthropic.AsyncAnthropic,
    job: RewrittenJob,
    cache: dict[str, dict],
    semaphore: asyncio.Semaphore,
) -> RewrittenJob:
    entry = cache.get(job.source_id, {})
    if entry.get("salary_min") is not None:
        return job.model_copy(update={
            "salary_min": entry["salary_min"],
            "salary_max": entry["salary_max"],
            "salary_currency": entry.get("salary_currency", "USD"),
            "salary_schedule": entry.get("salary_schedule", "yearly"),
        })

    user_prompt = (
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Category: {job.category}\n\n"
        f"Description:\n{job.description_text[:12000]}"
    )

    try:
        async with semaphore:
            response = await client.messages.create(
                model=SALARY_MODEL,
                max_tokens=SALARY_MAX_TOKENS,
                system=SALARY_SYSTEM,
                messages=[{"role": "user", "content": user_prompt}],
            )
            await asyncio.sleep(SALARY_REQUEST_INTERVAL)
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        if parsed.get("salary_min") is None or parsed.get("salary_max") is None:
            salary_data = {"salary_min": None, "salary_max": None, "salary_currency": None, "salary_schedule": None}
        else:
            salary_data = {
                "salary_min": int(parsed["salary_min"]),
                "salary_max": int(parsed["salary_max"]),
                "salary_currency": parsed.get("salary_currency", "USD"),
                "salary_schedule": parsed.get("salary_schedule", "yearly"),
            }
    except Exception as exc:
        logger.warning("Salary extraction failed for %s: %s. Using None.", job.source_id, exc)
        salary_data = {"salary_min": None, "salary_max": None, "salary_currency": "USD", "salary_schedule": "yearly"}

    if job.source_id in cache and salary_data["salary_min"] is not None:
        cache[job.source_id].update(salary_data)

    return job.model_copy(update=salary_data)


async def _batch_extract_salary_async(jobs: list[RewrittenJob]) -> list[RewrittenJob]:
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not set.")

    cache = _load_cache()
    needs_extraction = sum(1 for j in jobs if cache.get(j.source_id, {}).get("salary_min") is None)
    logger.info(
        "Extracting salary for %d jobs. %d already cached, %d need API calls.",
        len(jobs), len(jobs) - needs_extraction, needs_extraction,
    )

    semaphore = asyncio.Semaphore(SALARY_MAX_CONCURRENT)
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    tasks = [_extract_salary_one(client, job, cache, semaphore) for job in jobs]
    results = await asyncio.gather(*tasks)

    _save_cache(cache)
    return list(results)


def batch_extract_salary(jobs: list[RewrittenJob]) -> list[RewrittenJob]:
    """Extract or estimate salary data for rewritten jobs. Caches results alongside rewrite data."""
    if not jobs:
        return []
    return asyncio.run(_batch_extract_salary_async(jobs))


def generate_quality_samples(jobs: list[RewrittenJob]) -> None:
    """Write output/quality_samples.html with 5 random rewrites for spot-checking."""
    if not jobs:
        logger.warning("No rewritten jobs to sample.")
        return

    samples = random.sample(jobs, min(5, len(jobs)))
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    cards = []
    for job in samples:
        original_escaped = html_module.escape(job.description_text[:1500])
        if len(job.description_text) > 1500:
            original_escaped += "\n[truncated...]"
        cards.append(f"""
    <div class="card">
      <div class="card-header">
        <div class="card-title">{html_module.escape(job.title)}</div>
        <div class="card-meta">
          {html_module.escape(job.company)} &nbsp;|&nbsp; {html_module.escape(job.location)}
          &nbsp;|&nbsp; <strong>{html_module.escape(job.category)}</strong>
        </div>
        <div class="card-meta">
          Persona: <em>{job.persona_used}</em> &nbsp;|&nbsp;
          Structure: <em>{job.structure_used}</em> &nbsp;|&nbsp;
          Words: {_word_count(job.rewritten_description)}
        </div>
      </div>
      <div class="columns">
        <div class="col">
          <div class="col-label">Original</div>
          <pre class="original">{original_escaped}</pre>
        </div>
        <div class="col">
          <div class="col-label">Rewritten</div>
          <div class="rewritten">{job.rewritten_description}</div>
        </div>
      </div>
    </div>""")

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PMJ Pipeline Quality Samples</title>
<style>
  body {{ font-family: Georgia, serif; background: #f0f0f0; margin: 0; padding: 24px; color: #222; }}
  h1 {{ font-size: 1.4em; margin-bottom: 4px; }}
  .run-meta {{ color: #666; font-size: 0.9em; margin-bottom: 32px; }}
  .card {{ background: #fff; border-radius: 6px; box-shadow: 0 1px 4px rgba(0,0,0,.12); margin-bottom: 40px; overflow: hidden; }}
  .card-header {{ padding: 16px 20px; border-bottom: 2px solid #222; }}
  .card-title {{ font-size: 1.2em; font-weight: bold; margin-bottom: 4px; }}
  .card-meta {{ font-size: 0.85em; color: #555; margin-top: 2px; }}
  .columns {{ display: grid; grid-template-columns: 1fr 1fr; }}
  .col {{ padding: 20px; }}
  .col:first-child {{ border-right: 1px solid #e0e0e0; }}
  .col-label {{ font-size: 0.75em; text-transform: uppercase; letter-spacing: .06em; color: #888; margin-bottom: 12px; font-family: monospace; }}
  .original {{ font-family: monospace; font-size: 0.82em; white-space: pre-wrap; color: #444; line-height: 1.55; margin: 0; }}
  .rewritten {{ font-size: 0.95em; line-height: 1.75; }}
  .rewritten h2 {{ font-size: 1em; margin: 18px 0 6px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
  .rewritten h3 {{ font-size: 0.95em; margin: 14px 0 4px; color: #333; }}
  .rewritten p {{ margin: 0 0 12px; }}
  .rewritten ul {{ margin: 0 0 12px; padding-left: 20px; }}
  .rewritten li {{ margin-bottom: 4px; }}
  @media (max-width: 900px) {{ .columns {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<h1>PMJ Pipeline Quality Samples</h1>
<div class="run-meta">Generated {timestamp} &nbsp;|&nbsp; {len(samples)} of {len(jobs)} jobs sampled randomly</div>
{"".join(cards)}
</body>
</html>"""

    QUALITY_SAMPLES_PATH.parent.mkdir(exist_ok=True)
    with open(QUALITY_SAMPLES_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)

    logger.info("Quality samples written to %s", QUALITY_SAMPLES_PATH)
