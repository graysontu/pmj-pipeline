# PMJ Pipeline — Non-Obvious Findings & Gotchas

Reference this when troubleshooting. These are things that aren't obvious from reading the code.

---

## Start every session by syncing with GitHub

GitHub's `main` branch is the master copy. The pipeline commits `state.json` and
`feed.xml` to it every day, and work is done from both local and cloud sessions, so a
local checkout falls behind within a day. Before reading, changing or running anything:

1. `git status`. If there are uncommitted changes, tell Grayson what they are and ask
   before stashing, committing or discarding anything.
2. `git checkout main` and `git pull origin main`. If the pull reports a conflict,
   stop and explain it in plain words; never resolve it by discarding the remote side.
3. Make changes on a new branch, open a pull request and merge it into `main` (squash,
   matching the history), rather than pushing to `main` directly - the daily run
   pushes there too.

Never commit `data/state.json` or `output/feed.xml` from a local session: only the
runner writes them, and committing a stale copy reverts days of pipeline work. If a
local run changed them, `git checkout -- data/state.json output/feed.xml` before
committing. Files git does not track stay local and are not synced: `.env`, `.venv`,
`data/classification_cache.json`, `data/rewrite_cache.json`.

Grayson works in local sessions and is not a git user - do the syncing for him and
explain anything that needs his decision without git jargon.

---

## Greenhouse API

**Use `first_published`, not `created_at` or `updated_at`**
- `created_at` is missing from the Greenhouse boards API response entirely (returns null).
- `updated_at` changes every time a job is edited, so almost every active job appears "fresh" — this defeats `JOB_MAX_AGE_DAYS` filtering entirely.
- `first_published` is the correct field: the date the job was first made public.

**Location formats are wildly inconsistent across companies**
Greenhouse lets each company set location however they want. Known formats encountered:
- `"City, ST"` — standard
- `"City, State, United States"` — full state name + country
- `"Property Name, City, State, Country"` — Scion Group style (property name prefix)
- `"State - City"` — Cottonwood Residential (reversed, dash-separated)
- `"City ST"` — Hawthorne (no comma, state abbreviation appended)
- `"Property - Street Address - City, ST - ZIP"` — Hawthorne verbose
- `"Property Name Only"` — Berkshire Group (no geographic data at all)
- `"United States"` — literally just the country (no useful data)

- `"City A, ST; City B, ST"` — Asset Living, Griffis (several locations, one requisition)
- `"Property; Property; Property"` — CloudTen, Sunrise (a list of buildings)
- `"City, ST (Neighborhood)"` — Weinstein; `"City, ST (Hybrid)"` — Lincoln (a note in
  parentheses, sometimes missing its closing bracket)

`pipeline/geo.py` handles all of these. When a new company shows wrong locations, add
its format to `tests/test_geo.py` and extend `resolve_location`.

`scripts/audit_locations.py` reports a before/after against the published feed and
can write a preview feed (`--preview path.xml`), writing nothing live. Run it after
any `geo.py` change:

    python -m scripts.audit_locations --preview output/feed_preview.xml

It compares against `output/feed.xml` - what is actually published - rather than
against a previous version of the parser, so the report reflects reality. It also
lists every job whose location could not be resolved, grouped by company.

**A location is only trusted when a US state can be identified.** This is the core
rule, and it exists because the old fallback returned *any* unrecognised string as
the city. That put property names ("Trellis House"), corporate offices
("Corporate - CloudTen") and employers' own names ("Redstone Residential") into
`<city>` — 26 of 269 published jobs on 2026-09-18. `resolve_location` returns
`("", "")` with `needs_review=True` instead. Affected companies: Berkshire Group,
Sunrise Management, CloudTen Residential, Redstone Residential, Birgo.

**Never infer a location from the job description.** Descriptions name the
*employer's headquarters*: a Redstone posting for a property in another state says
"Headquartered in Provo, Utah". Mining descriptions would attach Provo to jobs that
are nowhere near it. An unresolved location is reported, not guessed.

**A real city with no state is still rejected.** Birgo posts bare `"Greensburg"`,
which is a real place — but Greensburg exists in PA, KS, IN, KY and LA, so the state
cannot be inferred. Flag it; don't pick one. (The one exception is the Greenhouse
office fallback below: if the employer itself tagged that requisition with an office
named "Greensburg" whose address is in Pennsylvania, that is the employer's data,
not a guess.)

**Greenhouse offices stand in for a bare property name - under two guards**
(added 2026-09-26, `_office_location` in `pipeline/sources/greenhouse.py`).
Greenhouse lets employers tag each requisition with an office, and offices carry an
address. When the location field doesn't resolve, the fetcher uses an office address
only if (1) the location field names no state at all (`geo.mentions_us_state`), is
not "remote", and (2) the office *name* shares a distinctive word with the location
field, ignoring generic words and the employer's own name. Guard 2 is what keeps
headquarters out: Avanath files a "San Diego" posting under its Irvine "Corporate"
office, Fairstead files remote roles under "Fairstead Communities" in New York, and
Lincoln files "Remote" under Charlotte - none of those match, so they stay
unresolved. Guard 1 leaves real-but-garbled places ("Concord, NC (Charlotte area)")
to the parser rather than swapping in a nearby office city. Measured across all 1,700
Greenhouse postings on 2026-09-26: 38 went from unresolved to resolved (Avanath 36,
Birgo 1, Comstock 1) and no already-resolved location changed. The fetcher changes
only newly discovered jobs; published ones keep their location on JobBoardly.

**A trailing note in parentheses is dropped - but only when the location fails
without it** (fixed 2026-09-26, `_parse_segment_ignoring_notes` in `geo.py`). Weinstein
writes "Richmond, VA (Henrico/West End)" and "Mount Juliet, TN (Nashville)"; the note
glued onto the state ("VA (Henrico/West End)") so nothing resolved. Only notes at the
*end* are removed, and only after the full string fails, so an already-working value
("Chicago, Illinois, United States (Remote)") is untouched. Measured on all 2,468 live
postings across the 61 boards: 16 went from unresolved to resolved (Weinstein 14,
Lincoln 2 "Charlotte, NC (Hybrid)"), none changed that already resolved. None was in
the published feed at the time, so this helps future jobs only.

**A property name with its city and state in parentheses resolves to that place**
(added 2026-10-04, `_parse_place_in_note` in `geo.py`; this reverses the 2026-09-26
choice to leave such places unresolved). Bigos writes every location this way: "Era
on Excelsior (Saint Louis Park, Minnesota)". It is the last thing tried, and only when
the text outside the parentheses names no US state (so "Concord, NC (Charlotte area)"
keeps Concord, not the metro), is not a remote role, and the text inside is a complete
city and state. Measured before adopting it, on 2,271 live postings across every board
plus Bigos, Brighthaven (since removed), Evergreen and Morguard: 32 went from unresolved to resolved,
all Bigos, all correct; nothing that already resolved changed; and none of the 1,794
locations ever recorded in `state.json` changed, so the published feed was untouched
(`audit_locations`: 279 of 279 unchanged). "Olympus Grand Crossing (Katy, TX)" now
resolves to Katy, TX too.

**Semicolon-separated multi-location strings must be split first.** Splitting on
commas alone turned `"Reno, NV; Sparks, NV"` into the city `"Nv; Sparks"`. The first
segment that resolves wins.

**No non-US job is ever published - Grayson's rule, "no matter what"** (added
2026-10-04). PeakMade's "Kingston, Ontario" was published once before this existed.
Two layers, both in place for every source:

- Each fetcher drops listings whose ATS-supplied country is explicitly not the US,
  before any detail fetch: Workable `location.countryCode`, SmartRecruiters
  `location.country`, Lever `country`, Ashby `address.postalAddress.addressCountry`
  (primary and secondary locations; kept if any is the US), Recruitee `country_code`.
  A missing country is not evidence either way (`geo.listed_outside_us`).
- `run()` in `main.py` then drops any job whose location text names a foreign country
  or Canadian province (`geo.names_foreign_country`), which is all Greenhouse gives.
  It runs before the age filter, so no later stage ever sees a non-US job. The last
  comma-separated part is the country position: "Perth, WA, Australia" is foreign,
  "Lebanon, PA" and "Mexico, MO" are US towns, and Georgia is a state. A requisition
  that is also listed somewhere in the US ("Toronto, ON; Seattle, WA") is kept.

Measured on all 1,794 jobs ever recorded in `state.json`: exactly one flagged, the
Kingston one. Yugo, added the same day, lists its UK and European jobs on the same
Workable account, which is why the Workable filter matters. Only full country names
are matched in text - two-letter codes collide with states ("CA") - so add a country
to `_FOREIGN_COUNTRIES` if a new employer's format slips through.

**Berkshire Group has no city/state in their location field** — only property names. We suppress `<country>` in the XML when both city and state are empty to avoid JobBoardly showing "United States." The strict-state rule above makes that suppression fire correctly for every such company, not just the ones whose parse happened to fail.

---

## ATS APIs (verification gotchas)

**Workable's v3 jobs endpoint only answers POST** — GET returns 404 for every account, indistinguishable from a bad slug. The list response has no description; each fresh job needs a per-job GET to the v2 detail endpoint. The fetcher only detail-fetches jobs published within `JOB_MAX_AGE_DAYS + 1` days to keep HTTP volume low.

**SmartRecruiters returns 200 with zero postings for nonexistent company slugs** — an empty result proves nothing. Only a non-empty postings list confirms a slug. The list response has no `jobAd` (description) and its `ref` is an API URL, so each fresh posting needs a detail fetch for content and the real apply page.

**Ashby and Recruitee have essentially no US property management companies** (verified July 2026 via site: searches). Don't spend time hunting for PM slugs there.

**Dead slugs in `sources.yaml` as of 2026-09-17.** `lever/belong` (Belong Home) and
`workable/bluecrestresidential` (Bluecore Residential) both returned **404** — the
2 boards the daily census always reported as unavailable. Vacasa (Greenhouse),
Entrata and Tripalink (Lever) resolve fine but have never produced a job. All are
candidates for removal; `lever/belong` is safe to delete outright.

**Bluecore was a rename, not a dead board** (fixed 2026-09-23). The account moved to
`bluecoreresidential`, and job shortcodes carried over (the one Bluecore job in state
had apply URL `/bluecrestresidential/j/10C55DDF3C/`; that shortcode now lives under
the new slug). Before deleting a 404 slug, search `site:<ats domain> "<company>"` -
the company may simply have renamed its ATS account.

**The 1-per-company cap runs BEFORE classification**, so a company whose board is mostly non-PM roles (construction, corporate, finance) wastes its daily slot on jobs the classifier rejects. Prefer companies whose boards are majority PM/leasing titles.

### Adding companies (research notes, 2026-09-23)

**Search the ATS domains; don't guess slugs.** Guessing slugs for ~365 well-known
PM and HOA companies found almost nothing: most large operators (Greystar, RPM,
FirstService, Associa, Bell, ZRS...) use Workday, iCIMS, UKG, Paycom or Paylocity,
which the pipeline does not support. Every company added on 2026-09-23 came from
searches like `site:job-boards.greenhouse.io "leasing consultant" apartments`, and
likewise for `jobs.lever.co`, `jobs.smartrecruiters.com` and `apply.workable.com`.
Short slugs (`peak`, `access`, `omni`) belong to unrelated companies - read the titles.

**Most HOA / community-association managers use Paylocity** (e.g. Keystone Pacific)
or in-house pages. Only Action Property Management (Lever) and Rise AMG (Greenhouse)
turned up active on supported systems. A Paylocity fetcher would unlock far more.

**Check posting velocity, not board size.** Many small SmartRecruiters boards
(Community Management Associates, The Manor Association, Omni Management Services,
Mercy Housing, AGM Management...) list only evergreen postings older than 30 days;
with `JOB_MAX_AGE_DAYS=2` they would never contribute a job. Run candidates through
the real fetchers and count postings from the last 2/7/30 days.

**Workable throttles probing hard.** Probing a few hundred Workable slugs from one
IP brought sustained 429s for 30+ minutes, which also broke local runs of the
existing Workable fetchers. Probe Workable last, slowly, and only for specific leads.
On 2026-10-04 a few dozen `apply.workable.com` requests from one IP earned 429s
carrying `Retry-After: 57415` (16 hours). The cross-company search at
`jobs.workable.com` (below) is a separate service and was not throttled.

**Looked at and rejected on 2026-09-23**, so nobody re-researches them: Evernest
(posts Philippines/Mexico roles that would publish with no US location), Flow, CIM
Group, Intrinsic Development, AVE by Korman (mostly corporate or hospitality roles),
RXR (location field unparseable), Twin Pines (NYC addresses without a state, ~2
jobs/month), and the dormant SmartRecruiters boards above.

**Don't add employers who pay for listings on the board.** As of 2026-09-23: MAA,
Federal Realty Investment Trust, Lloyd Management, Pennrose, Ciminelli Real Estate
Services.

**Avanath's Greenhouse board token is `communitymanager`**, and about half its
location fields are property names ("Northpointe"). Since 2026-09-26 the fetcher
resolves these through Greenhouse offices (see "Greenhouse offices stand in for a bare
property name"). Berkshire, Sunrise and CloudTen have no location in `offices`, so
the fallback does not help them. One Avanath job published before the fix ("Maintenance
Supervisor", listed as "Canvas", really Austin, TX) went live with no city.

**LivCor and AIR Communities are both Blackstone operators**, and livcor.com redirects
to AIR, but their SmartRecruiters boards list different communities (no overlapping
postings on 2026-09-23). They are not duplicates.

### Adding companies (second round, 2026-10-04)

**Use the ATS vendors' own cross-company job search - it finds what `site:` searches
miss.** By this round, `site:` searches on Greenhouse returned almost only companies
already in `sources.yaml`.
- Workable: `GET https://jobs.workable.com/api/v1/jobs?query=leasing%20consultant&location=United%20States`,
  paged with `pageToken` = the response's `nextPageToken`. It covers every Workable
  account and gives the company name and website, but not the account slug: find that
  with `site:apply.workable.com "<company>"`, or confirm a guess with
  `GET https://apply.workable.com/api/v1/widget/accounts/<slug>` (counts against the
  apply.workable.com throttle).
- SmartRecruiters: `GET https://jobs.smartrecruiters.com/sr-jobs/search?keyword=...&limit=100&offset=...&country=us`
  returns each posting's company identifier, which is the slug. Matching is loose, so
  filter by title.
- Greenhouse and Lever have no public cross-company search. After title searches were
  exhausted, searching for board front pages still found new ones
  (`site:job-boards.greenhouse.io "Jobs at" Residential`, likewise Properties,
  Communities, Homes, Apartments).
- Measure every candidate the same way: PM-titled postings in the last 7 and 30 days,
  share of PM titles, how many locations `geo.resolve_location` resolves, and US share.
  The boards already in `sources.yaml` had a median of 8.5 PM postings per 30 days.
- **Check that a candidate's postings aren't already on a board in `sources.yaml`**
  (same title and location). An affiliate can run a mirror of a parent's board under
  its own name - Brighthaven's is a full copy of Avanath's - and every one of its jobs
  would publish twice. That check was missed for Brighthaven; see below.

**Most large operators are on ATSs the pipeline cannot read.** Crawling the careers
pages of ~150 multifamily, affordable, student, single-family, HOA, commercial and
manufactured-housing operators found UKG/UltiPro (34), Workday (26), Harri (17), ADP
(17), iCIMS (12), Fountain (12), Dayforce (7), RentCafe (7) and Paylocity (7). Only
Morguard and Bigos were on supported systems. Don't repeat the crawl; reaching the big
operators needs a new fetcher (UKG, Workday, Harri or Paylocity).

**SmartRecruiters accounts being retired show `TBD2026...` identifiers.** FPI
Management, Maryland Management Company and Aimco Apartment Homes return zero postings,
and looking up one of their old postings says the identifier is now e.g.
`TBD20260518FPIManagementInc`. They have left SmartRecruiters. WinnCompanies' Lever
board is empty too; its careers page now points to Dayforce.

**Added on 2026-10-04** (PM postings in the prior 30 days, at research time):
Yugo (Workable, 13 US only), Morguard (SmartRecruiters,
11), Evergreen Residential (Greenhouse, 9, single-family rentals), Bigos Management
(Greenhouse, 7), DePaul Housing Management (Workable, 6, all in one week, so its
usual pace is unknown), Denton Floyd Real Estate Group (Workable, 6, only 27% PM
titles), LV Collective (Workable, 5, 41% PM titles, student housing), Taylor
Management Company (Workable, 4, NJ condo/HOA), Lynco Properties (Workable, 3) and
Mountain Valley (Workable, 3, Colorado HOA/resort). The last few were added for HOA
and regional coverage, not volume. Yugo's Workable account also lists UK and European
jobs - see "No non-US job is ever published". Bigos depends on the parentheses rule
above.

**Brighthaven was added on 2026-10-04 and removed on 2026-10-05: its board is a copy
of Avanath's.** Brighthaven is an Avanath / BRIDGE Housing venture, and all 52 of its
live postings were also on Avanath's board (`communitymanager`) with the same title,
location and description, created a second apart under a different requisition ID.
The first run published one of them twice ("Community Manager", Stockton, CA:
Avanath `greenhouse_5256090007`, Brighthaven `greenhouse_5256110007`). The
Brighthaven copy was closed by hand in `state.json` (`closed_at`, with the reason in
`closure_reason`), from a fresh copy of `main` before that day's run started, so the
next feed drops it and JobBoardly deletes the listing. It stays closed: with
Brighthaven out of `sources.yaml` the census reports it `unknown`, which changes
nothing. Re-adding Brighthaven would reopen it and duplicate everything again.

**Looked at and rejected on 2026-10-04**, so nobody re-researches them:
- Too few PM postings (1-2 a month): Allmark Property Management, Farbman Group,
  SoLa Impact, Boulder Housing Partners, F&F Properties, Encore Property Management,
  Keeley Properties.
- Empty or dormant boards: Grand Peaks, ATC Development, Holton-Wise, Billingsley,
  Elysian Living, Hercules Living, Waypoint Residential, Aria Luxury Apartments,
  American Property Management, Real Equity Management (404), ResProp (`resprop`,
  404), Campus Advantage (Lever 404).
- Staffing agencies and aggregators: NoGigiddy (1,800+ jobs for clients), HOA Talent,
  Samazon Staffing, Classet.
- Not property management: Second Nature (Ashby, proptech), Experience Senior Living,
  Colorado Coalition for the Homeless, LISC, DivcoWest, BioMed Realty, Empire State
  Realty Trust, Hazel Valley Homes (corporate roles only), Collective Residential.

**Logos for this round, and how to source them.** ATS-uploaded logos (Workable,
SmartRecruiters) are only 120px, and the LinkedIn card enlarges logos up to 3x, so
prefer the company's own SVG. In a cloud session, render SVGs with node Playwright
from local files only: Chromium there does not trust the session's proxy
certificate, so it cannot load live sites (don't disable TLS checks to get around
it). LV Collective's site logo is white, so its file is their vector logo in the gold
(#9F7E50) of their own Workable upload. Yugo's is the badge from parent company GSA
Group's launch announcement (Workable's wordmark is only 91x39px). bigos.com is a
parked domain; Bigos's site is tbigos.com (behind a Cloudflare challenge), and its
logo came from the inline SVG on rentals.tbigos.com.

---

## Salary Extraction

**Always use `description_text`, never `rewritten_description`**
The rewrite intentionally strips salary details. The original description must be used. Salary can appear 8,000+ characters into a long description — the extractor sends up to 12,000 chars.

**Never estimate salary — return null if not explicitly stated**
Earlier versions had a STEP 2 that estimated salary ranges by category. This produced hallucinated values like `$24,000–$38,400/hr` for jobs with no salary info. The current prompt returns null when no explicit salary is found. Do not re-add estimation.

**Null salary handling**: When the AI correctly returns `{"salary_min": null, ...}`, calling `int(None)` throws TypeError. The extractor handles this explicitly — don't "simplify" that null check away.

**Salary is cached in `data/rewrite_cache.json`** alongside the rewrite under the same source_id. If salary extraction logic changes, clear the old salary fields from the cache:
```python
for entry in cache.values():
    entry.pop('salary_min', None); entry.pop('salary_max', None)
    entry.pop('salary_currency', None); entry.pop('salary_schedule', None)
```

---

## State & Deduplication

**Deduplication happens BEFORE classification** — jobs already in state are filtered out before any AI calls. This is critical for token efficiency. Don't move that filter.

**`description_text` is stored in state** (added during the April 2026 session). This allows salary repair scripts to re-run extraction without re-fetching from the ATS.

**Multiple simultaneous pipeline runs corrupt state.** If you kill a run and start a new one, the old process may still be running in the background and will write to `state.json` when it finishes, overwriting the new run's results. Always confirm old processes are dead before re-running (`pkill -f pipeline.main`).

**`JOB_MAX_AGE_DAYS` in `.env` overrides `config.py` default.** If the date filter seems wrong, check `.env` first — it takes precedence over the default in `config.py`.

**The age cutoff is a rolling `now - JOB_MAX_AGE_DAYS`, so the time of day you run matters.** It is recomputed on every run, not anchored to a calendar day. On 2026-08-23 the 16:14 UTC run found 48 jobs inside a 2-day window; a manual re-run at 23:31 UTC the same day found **6** — about 42 jobs published during the US afternoon two days earlier fell out the back of the window in those seven hours. Consequence: re-running a failed job later the same day does **not** recover what the failed run would have published. If a run fails and you care about its jobs, either re-run immediately or temporarily raise `JOB_MAX_AGE_DAYS`.

**A daily publish cap is enforced by `MAX_JOBS_PER_RUN`** (`config.py`, default 13 since 2026-10-04 - it was 11 from 2026-09-26, and 9 before that - override via env). It is applied **after classification and before rewrite** — after, so the cap counts real PM jobs rather than candidates the classifier would reject; before, so deferred jobs cost no Sonnet rewrite tokens. Two things to preserve if you touch it:

- **Don't reassign `kept`.** The health-check block computes `rejection_rate` from `classified` vs `kept`; capping `kept` in place makes every capped run look like a >50% rejection spike and writes a bogus NOTICE.txt. The capped list lives in `publishable`.
- **The selection rotates by date and must keep doing so.** `kept` follows `sources.yaml` order and the 1-per-company cap means each entry is a different company, so a plain `kept[:N]` would hand every slot to the same top-of-file companies daily and starve the ones at the bottom. The offset steps by the cap size per day so consecutive days take near-disjoint slices; with 15–20 qualifying jobs this covers every company within about 6 days.

**Deferred jobs get one extra chance, not an unlimited queue.** They aren't written to state, so the next run reconsiders them — but only while they remain inside the `JOB_MAX_AGE_DAYS` window. At ~15–20 qualifying jobs/day against a cap of 9, expect a persistent surplus that ages out unpublished. After 15 companies were added on 2026-09-24 the first run found 32 qualifying jobs, and the cap of 11 was reached every day from 2026-09-28. It went to 13 on 2026-10-04, when 11 more companies were added; expect a surplus most days still. The cap is a ceiling, not a backlog.

---

## Classification

**Building Engineers should be REJECTED.** "Mobile Building Engineer", "Chief Engineer", "Operating Engineer" — these are commercial HVAC/mechanical plant operators, not property management roles. They were incorrectly passing as "Maintenance Technician Jobs." The reject rule is in the classifier system prompt.

**Classification cache is at `data/classification_cache.json`.** To force a specific job to be re-classified (e.g., after updating the classifier prompt), delete its entry from this file.

---

## Rate Limiting (Anthropic API)

The account has a **50,000 input tokens/minute** and **50 RPM** limit on Haiku.

- Classifier: `MAX_CONCURRENT=1`, `REQUEST_INTERVAL=3.0s` → ~20 RPM. Don't lower the interval.
- Salary extractor: `SALARY_MAX_CONCURRENT=2`, `SALARY_REQUEST_INTERVAL=3.0s`. The extractor originally had no throttling at all (`MAX_CONCURRENT=10`, no sleep) — this caused cascading 429s.
- Rewriter uses Sonnet, not Haiku, and has its own semaphore (`MAX_CONCURRENT=3`).

When rate limit errors appear during a run, jobs are skipped and logged. They won't be retried on the next run because they get added to state with null salary (or marked as REJECT in classification cache). Run the salary repair script if needed.

---

## GitHub Actions / Automation

**`data/rewrite_cache.json` is gitignored** — it's persisted between GitHub Actions runs via the `actions/cache` step. If that cache is evicted (GitHub evicts caches after 7 days of no access), all jobs will be re-rewritten on the next run, consuming significant tokens.

**The pipeline commits `state.json` and `feed.xml` back to the repo.** This commit triggers `deploy-pages.yml` which redeploys GitHub Pages. The full chain is: pipeline run → commit → Pages deploy → JobBoardly import.

**`requirements.txt` must stay pinned — the runner installs fresh every run.** `anthropic` is pinned to `==1.0.0`. It was previously unpinned, and when SDK 1.0.0 released between the Aug 20 and Aug 21 2026 runs, the runner picked it up automatically and the pipeline broke for three days: 1.0.0 removed `temperature`/`top_p`/`top_k` from `messages.create()` entirely, and `ai_rewriter.py` was passing `temperature=0.85`. It failed as a Python `TypeError` before any HTTP request, not as an API error. Notes for future debugging:

- **Local `.venv` is not evidence.** It lags far behind whatever the runner installs, so a break like this reproduces only in CI. Check the `Install dependencies` step in the run log for the actual version (`anthropic-1.0.0`).
- **Don't re-add sampling parameters.** `temperature`, `top_p`, and `top_k` are gone from `messages.create()` in 1.x. Rewrite variety comes from per-job persona/structure selection in `_pick_persona_and_structure()`, not sampling.
- The remaining requirements are still unpinned and can break the same way.

**`ANTHROPIC_API_KEY` must be set as a GitHub repo secret** (Settings → Secrets and variables → Actions). If the daily run shows 400 errors with "credit balance too low", top up at console.anthropic.com.

**GitHub's cron scheduler is unreliable and can be delayed by minutes to hours.** The scheduled run at `0 16 * * *` does not always fire on time. If a run appears missing, check the Actions tab before assuming a bug — it may just be delayed. The GitHub Actions API also has a lag before new runs appear.

**"The job was not acquired by Runner of type hosted" is a GitHub outage, not a bug.** GitHub failed to allocate a hosted runner; the job sits queued (~15 min) and is then cancelled. Diagnostic: `gh api repos/OWNER/REPO/actions/runs/RUN_ID/attempts/N/jobs --jq '[.jobs[].steps[]?] | length'` returns **0** — no step ever executed. Because no step ran, the `if: failure()` email step inside `run-pipeline.yml` never fires either, so the only notification is GitHub's own "Run failed" email. `pipeline-watchdog.yml` exists to cover this: it triggers on `workflow_run` completion, uses that zero-steps check to distinguish infra failures from real pipeline failures, retries infra failures (up to attempt 3), and emails. It deliberately ignores ordinary step failures, which `run-pipeline.yml` already emails about. Don't make the watchdog retry those — it would burn Anthropic credits re-running the same bug.

**Ubuntu 26.04 was verified on 2026-10-05; the workflows deliberately stay on
`ubuntu-latest`.** GitHub moves `ubuntu-latest` to Ubuntu 26.04 gradually between
2026-10-19 and 2026-11-19 (actions/runner-images#14748). A throwaway workflow ran the
same steps on 24.04 and 26.04 side by side: identical Python (3.11.16) and library
versions, all 343 tests passing, a byte-identical feed built from that day's
`state.json` (283 jobs), a pixel-identical LinkedIn card, and a working LinkedIn
preview run (site fetch, page checks, Claude caption). Every action the workflows use
runs on GitHub's bundled Node, independent of the OS. Pinning `ubuntu-26.04` early was
considered and rejected: it gains nothing, and the first 26.04 attempt that day was
cancelled for lack of a runner while 24.04 got one. If a run breaks after Oct 19,
compare against this baseline; `runs-on: ubuntu-24.04` is the fallback while GitHub
still offers it.

**The GitHub runner never has a `.env` file** — it's gitignored. `JOB_MAX_AGE_DAYS` and other non-secret config must be set via `config.py` defaults (or added as GitHub env vars in the workflow). Changes to local `.env` do not affect automated runs.

**`classification_cache.json` and `rewrite_cache.json` on the runner are NOT committed to the repo** — they're only persisted via GitHub Actions cache. The local copies reflect only what was cached during local runs, not what the runner has classified. If you're trying to debug why the runner rejected a specific job, you can't check the local cache for it.

**Silent early returns used to hide failures — fixed Aug 2026, don't regress it.** `run()` returns a process exit code and `__main__` does `sys.exit(run())`. Classification and rewrite failures return `1` so the `if: failure()` email step fires; benign paths ("no new jobs this run") return `0`. Before this, every stage failure returned exit 0, GitHub reported "success," and no email was sent — an SDK break ran undetected for three days that way. If you add an early `return` to `run()`, give it an explicit exit code; a bare `return` is now a bug.

**Changing `JOB_MAX_AGE_DAYS` and the cron schedule at the same time creates a gap.** Jobs published in the window between the last old-schedule run and the first new-schedule run can fall outside the new age window and be permanently missed. If you need to change both, temporarily increase `JOB_MAX_AGE_DAYS` to cover the gap, then lower it after the first new-schedule run.

**`JOB_MAX_AGE_DAYS` must be at least 2 when the 2-per-company cap is active.** With a 1-day window, any jobs dropped by the cap today are outside the window tomorrow — permanently lost. A 2-day window gives capped jobs a second chance the following run.

---

## Closure Detection (employer availability check)

**A job leaving the feed requires a complete, successful board census — never an HTTP page check.**
`pipeline/census.py` asks each ATS for the full set of requisition IDs it currently
lists; a job is open iff its `source_id` suffix is in that set. Fetching the apply
URL instead is actively wrong, and was measured to be wrong in three different
ways on 2026-09-17:

- Greenhouse and Workable serve **HTTP 200** for a dead requisition and redirect to
  the general board (`/assetliving?error=true`, `/peak-made/?not_found=true`).
  Status code alone says "open".
- SmartRecruiters answered **403** to a plain apply-page GET (bot block). The page
  tells you nothing; the API answered definitively.
- Lever serves the **full, correct job page at 200 with no redirect** for a closed
  posting — correct `<title>`, correct headline, 724 KB of real content. Only
  `api.lever.co/v0/postings/{slug}/{id}` reveals it (`404 Document not found`).
  Any HTML-based check marks these jobs open forever.

**Absence from a discovery batch is NOT evidence of closure.** The discovery
fetchers in `pipeline/sources/` are filtered by `JOB_MAX_AGE_DAYS`, by the
1-per-company cap, and (Workable, SmartRecruiters) by only detail-fetching jobs
inside the age window. `pipeline/census.py` deliberately re-queries the boards
rather than reusing those results. Never "optimise" the census away by reusing the
discovery list — it would close most of the feed on the first run.

**The discovery fetchers silently truncate; the census must not.** `MAX_PAGES = 10`
in `workable.py` with 10 results per page caps discovery at 100 listings, and
peak-made already has 101. The census uses `MAX_PAGES = 200` and additionally
verifies the collected count against the total the board declares (`meta.total`,
`total`, `totalFound`), raising `CensusError` on a short walk. A partial listing
looks exactly like mass closure.

**Three-valued verdicts. Only `closed` removes anything.** Timeouts, non-200s,
bot blocks, non-JSON bodies, short pagination, a company dropped from
`sources.yaml`, and a company that changed ATS all yield `unknown`, which leaves
the job in the feed and retries next run. `unknown` deliberately neither advances
nor resets the strike counter.

**SmartRecruiters' empty response is treated as an error, not an empty board** —
a nonexistent slug returns 200 with zero postings, so an empty result would
otherwise close every job for that company.

**Removal needs `CLOSURE_STRIKES` consecutive confirmed absences (default 2).**
One anomalous-but-complete-looking listing cannot delist inventory. Seeing a job
open again resets the counter to 0. A closed job stays in `state.json` (it is
excluded from the feed, not deleted) so a reposted requisition with the same ID is
reinstated with its original `published_at` — closure is reversible and never
mints a new ID or a new publication date.

**`get_active_jobs()` vs `get_published_jobs()`** — the feed uses `get_active_jobs()`
(in-window and not closed). The closure check uses `get_published_jobs()`, which
*includes* closed jobs, because it has to see them to reinstate reopened ones.
Don't collapse these two.

**Retention is unchanged:** a job stays in the feed until it is confirmed closed or
ages out via `ACTIVE_DAYS` (60). It does not need to be rediscovered daily. The
`MAX_JOBS_PER_RUN` and 1-per-company caps gate *admission of new jobs only* and
never cause retention loss.

**Workable rate-limits the census (HTTP 429), and the daily runner does hit it.**
First seen 2026-09-17 while testing locally; then on the 2026-09-18 scheduled run
all 7 Workable accounts returned 429 at once, so 8 boards were unavailable and 60
jobs (10% of the feed) went `unknown` for that run. Nothing was wrongly removed -
a 429 surfaces as `HTTP 429` on the `BoardCensus`, so those jobs are `unknown` and
the strike counter neither advances nor resets. But a persistent 429 means Workable
jobs stop being closure-checked, so dead ones would linger.

Mitigated in `census.py` two ways: `_request` retries `RETRYABLE_STATUSES`
(429/500/502/503/504, deliberately **not** 404) with exponential backoff, and the
Workable page walk sleeps `WORKABLE_PAGE_DELAY` between pages so ~77 rapid requests
across 7 accounts stop arriving as one burst. After that change a full local census
established 44 of 46 boards with 0 unknown, in about 16 seconds.

Note the retry predicate must be wrapped in tenacity's `retry_if_exception(...)`.
Passing a bare function to `retry=` silently never fires - tenacity hands that
callable a `RetryCallState`, not the exception, so every `isinstance` check is
False. A test caught this; keep `test_request_retries_a_429_then_succeeds`.

**A Workable lockout is not retried into, and stops further Workable requests for
the run** (added 2026-10-04, `pipeline/throttle.py`). Workable's 429 can carry a
`Retry-After` of hours (57,415 seconds was seen), and it applies to the whole IP, not
one account. So:
- A 429 whose `Retry-After` is over `MAX_RETRY_AFTER` (60s) is not retried. A shorter
  one is waited out exactly. With no header, the old exponential backoff applies.
- Once a Workable census is still 429 after that, `run_census` skips the remaining
  Workable boards ("skipped: workable was rate-limiting this run"). They are
  `unknown` like any unavailable board: nothing is removed, and strike counts are
  untouched. Other ATSs keep the per-board behaviour; none has been seen to limit by IP.
- Discovery (`sources/workable.py`) now spaces its page and detail requests by
  `REQUEST_DELAY` (0.3s), retries a short 429 or 5xx, and after a lasting 429 skips
  the remaining Workable accounts for the run. Their new jobs are picked up on the next
  run, inside the `JOB_MAX_AGE_DAYS` window.

The first live test hit an IP that was already locked out: one request, no retries,
and the other six accounts skipped. The old code would have sent up to 28.
Workable rejects a larger page size (`limit` in the body is HTTP 400), so it stays at
10 per request.

### Feed guard

`generate_feed_xml(..., max_removal_fraction=...)` refuses to publish and raises
`FeedGuardTripped` when a run would empty the feed, or drop more than
`MAX_FEED_REMOVAL_FRACTION` (default 0.15) of it. On a trip **nothing is written**,
so the previous feed stays live, `NOTICE.txt` is written and `run()` returns 1 so
the failure email fires. The guard is opt-in via that keyword so
`scripts/make_test_feed.py` can still write a deliberate 3-job feed.

Writes go to `feed.xml.tmp` then `os.replace`, so a crash mid-write cannot leave a
truncated feed being served.

**Sizing:** measured steady-state churn is ~11 removals/day on a ~253-job feed
(~4.5%) — about 9 closures matching the 9 new jobs admitted, plus ~2 age-outs. 15%
is roughly 3x headroom. **A multi-day outage will trip the guard by design**:
closures accumulate while the pipeline is down, so a catch-up run may want to
remove 30%+. That is the intended behaviour — it fails closed, keeps the last good
feed and emails. Remedy is a manual `workflow_dispatch` with
`allow_mass_removal: true`, not a bigger threshold.

**State is saved only after the feed write succeeds.** A tripped guard rolls the
whole check back rather than leaving `state.json` marking jobs closed that are
still in the live feed. Don't move `state.save()` above `_publish_feed()`.

### Enabling and rolling back

`CLOSURE_CHECK_ENABLED` is **false** by default in `config.py`, and is set
explicitly in the `Run pipeline` step of `run-pipeline.yml` (the runner has no
`.env`). Flipping that one line to `"true"` enables removal; back to `"false"`
disables it and the feed reverts to age-based expiry only on the next run. No state
migration either way — `closed_at` keys already written are simply ignored while
the flag is off.

Dry run, which writes nothing:

    python -m scripts.check_closures --strikes 1

It accepts `--state` / `--feed` to reconcile a snapshot instead of the working
tree, which is how the 2026-09-17 dry run was produced without touching the repo.

**Status: enabled and the backlog cleanup is done.** `CLOSURE_CHECK_ENABLED` is
`"true"` in the workflow as of 2026-09-17. What actually ran that day:

- The first census found **353 of 606 published jobs (58.3%) already closed** — 86%
  of the 46–60 day bucket was dead, versus 4% of the 0–7 day bucket.
- **Run 1** (flag on, no override) gave all 353 their first strike and removed
  nothing — a free production validation pass. Do this first if you ever re-enable
  from cold; it costs one run and proves the census before anything is deleted.
- **Run 2** (`workflow_dispatch` with `allow_mass_removal: true`) removed all 353.
  Feed went **615 → 269**.

The cleanup is historical now. **Steady-state runs must use the default 2 strikes
and no override** — measured churn afterwards is ~11 removals/day (~4%). A run
proposing hundreds of removals again means something is wrong, not that another
cleanup is due.

The one-time form, for reference only: `--strikes 1 --allow-mass-removal`.

**Never run the cleanup from a local checkout.** `--apply` locally writes
`data/state.json`, and committing that from a stale checkout reverts however many
days of pipeline commits you were behind. The runner always checks out fresh; use
`workflow_dispatch`.

---

## JobBoardly Integration

- Feed URL: `https://graysontu.github.io/pmj-pipeline/feed.xml`
- Root element must be `<source>` — changing it breaks stored field mappings.
- JobBoardly requires `<publisher>`, `<publisherurl>`, `<lastBuildDate>` to recognize the feed format.
- JobBoardly has a "require salary on all posts" setting — keep this **OFF** or jobs without salary data won't import.
- **Removal semantics are confirmed empirically, not just documented.** JobBoardly
  **hard-deletes** a listing that disappears from the feed: on 2026-09-17 an import
  took the site from **611 job pages to 275**, and the 352 removed URLs now return
  **404** (verified against `sitemap.xml` before and after, plus per-URL checks).
  Omitting a `<job>` element is all that is required. No backdated
  `<expiration_date>` tombstone is needed — that fallback was considered and is
  **not** necessary.
- The import is **not instant**. It happens on the feed's configured refresh cycle,
  or when triggered by hand in the JobBoardly admin. If closed jobs seem to linger,
  check that refresh frequency before suspecting the pipeline — the feed can be
  correct for hours while the board is stale.
- The feed keeps emitting the same schema and the same `<source>` root — closure only
  changes *which* jobs appear, never the field structure, so stored field mappings
  are unaffected.
- **The board carries paid employer posts that are not in the XML feed.** As of
  2026-09-17 there are 6 (MAA, Ciminelli Real Estate Services, Pennrose x2, and two
  more). They are posted through employer accounts, have no ATS links, and are
  **correctly invisible to closure detection** — it only reconciles jobs it sourced.
  Any comparison of feed size to site job count must allow for them: 275 site pages
  = 269 feed jobs + 6 employer posts. Do not "fix" that gap.
- **Do not map site pages back to jobs by title.** Two traps, both hit on
  2026-09-17:
  - **Titles are not unique.** Lessen had 4 postings titled exactly "Experienced
    Field Maintenance Technician (3+ Years Required)". Matching by title picked the
    wrong one and produced a false "this closed job is still live" alarm.
  - **JobBoardly slugifies differently than `_slugify` in `main.py`.**
    `Groundskeeper/Porter` becomes `groundskeeper-porter` there but
    `groundskeeperporter` here; `$1,500` becomes `1-500` not `1500`. Parentheses,
    colons and slashes all differ.

  To identify which job a site page belongs to, fetch the page and read the
  requisition ID out of its apply URL. That is exact; titles and slugs are not.
- `<referencenumber>` is the stable employer requisition ID (`{ats}_{native_id}`)
  and is what JobBoardly matches on between imports. Closure never changes it, and
  a reinstated job returns with the same reference number and the same `<date>`.
- **Location mapping is already correct — do not change it.** Verified against the
  live admin on 2026-09-18: Location type = `source/job → remotetype` (fallback
  Onsite), Country = `source/job → country`, Region = `source/job → state`, City =
  `source/job → city`. The separate "Location" field stays **empty** — that one is
  for a combined `"Memphis, Tennessee, US"` string, and we send discrete fields.
  "Location limits" also stays empty; it restricts remote roles to regions.
- **JobBoardly geocodes `<city>` against a gazetteer and silently drops what it
  cannot resolve**, keeping the state. This is the single most important thing to
  know when a job page shows no city. Measured on live pages 2026-09-18:

  | Sent | Result |
  |---|---|
  | `Boca Raton`, `Rexburg`, `Soddy-Daisy`, `Winooski` | kept — small towns are fine |
  | `Mckinney` | kept, and normalised to `McKinney` |
  | `St. Petersburg`, `Saint Paul` | kept |
  | `St Augustine` (no period) | **dropped** — canonical form is `St. Augustine` |
  | `Mt. Juliet` | **dropped** — canonical form is `Mount Juliet` |
  | `Pheonix` | **dropped** — employer typo for Phoenix |
  | `Trellis House`, `Nv; Sparks` | **dropped** — not places |

  So a missing city on a job page is usually *our* bad value, not a JobBoardly bug.
  Check what the feed actually sends before blaming the importer.
- **JobBoardly sets a job's location on FIRST import and never updates it.** This is
  the most consequential thing to know before planning any location work. Proven by
  control on 2026-09-18: after correcting `Pheonix` → `Phoenix` in the feed and
  re-importing, the corrected job's page still showed no city, while two other jobs
  whose feed value had *always* been `Phoenix` displayed `Phoenix` correctly. Same
  value, same feed, same import - the only difference was whether the job already
  existed on the board. Re-checked 17 minutes after the import; it is not a queue
  delay.

  Note this is specifically about *updating fields*. Removal on disappearance does
  work (352 jobs were delisted on 2026-09-17), so existing jobs are not ignored
  wholesale. Only field updates were tested for location; other fields are unverified.

  **Consequence:** a location fix only benefits jobs imported *after* it ships.
  Already-imported jobs must be corrected by hand in JobBoardly's job editor, or
  deleted so the next import re-adds them (which changes their URL). Budget for this
  whenever changing `geo.py` - the 2026-09-18 fix left 8 jobs needing manual edits.

- **The 8 jobs left stale on 2026-09-18 were a deliberate decision, not an oversight.**
  When the location fix shipped, 8 already-imported jobs kept their old (missing)
  city because of the first-import-only behaviour above. Grayson chose not to hand-
  edit them - they age out within 60 days and the pipeline is correct going forward.
  If you check job pages against the feed and find location mismatches on jobs
  published before 2026-09-18, that is expected, not a regression. Jobs imported
  after that date should match; if one of *those* does not, that is a real bug.

- **Some correct cities are genuinely unsupported.** `Whistler, AL` is a real but
  unincorporated community and JobBoardly drops it. There is nothing to fix in the
  pipeline for these; correct them in JobBoardly's job editor if they matter.
- **A company with no `logo_url` gets the ATS's logo, not a blank.** JobBoardly
  scrapes a logo from the apply URL's domain when `<companylogo>` is absent: Logan
  Property Management's jobs display an image named `lever.co.png` (checked
  2026-09-23). Give every new company in `sources.yaml` a logo, self-hosted in
  `output/logos/`. Check it on a white background first - many company sites only
  serve a white logo meant for a dark header.
- Field mapping syntax: `source/job → fieldname` (e.g., `source/job/title → Title`).

---

## LinkedIn Posts

`.github/workflows/linkedin-post.yml` + `pipeline/linkedin/` post one job to the
LinkedIn company page every Monday, Wednesday and Friday (added 2026-09-27).

**It posts through Buffer, not the LinkedIn API.** LinkedIn only lets approved
apps post to a company page: the Community Management API is limited to
"registered legal organizations" with a verified business email (not Gmail), its
starter tier expires after 12 months unless a screencast review is passed, and its
tokens expire every 60 days without a separate refresh-token approval. Buffer
already holds that approval; its free plan gives a personal API key (repo secret
`BUFFER_API_KEY`). Buffer's API is labelled "public beta".

**One switch: `LINKEDIN_POSTING_ENABLED` in the workflow's `env`.** `"false"` is
preview mode: every run still picks, draws and records the post, but nothing is
sent to Buffer (if a key is saved, preview only checks the connection, read-only).
Preview and live history are kept apart, so preview posts don't count against
the live company cooldown or colour rotation.

**Status: live since 2026-09-28.** Grayson approved after reviewing previews. To
stop posting, set the flag back to `"false"` - nothing else needs undoing, and a
post already scheduled in Buffer can be deleted from Buffer's queue.

**Everything it writes goes to the `linkedin-posts` branch, never to main.**
`images/`, `history.json` and a `README.md` that shows every post with its image,
caption and why it was picked. That keeps it from ever colliding with the daily
pipeline's commit of `state.json`/`feed.xml`. The workflow checks the branch out
and fails without it - don't delete it (if it is ever lost, recreate it as an
orphan branch). Images are served from `raw.githubusercontent.com`, which works
because the repo is public; Buffer needs a public image URL, which is why the
workflow pushes the image *before* scheduling. If the repo goes private, the
images need another host.

**Posts link to the job's page on the site, never to the ATS.** The site
publishes `https://propertymanagementjobs.us/jobs.xml` with each page's `url`
next to the employer's `application_link`; matching on the apply link is exact.
On 2026-09-27, 262 of the site's 271 jobs matched a feed job; the other 9 were the
6 paid employer posts and 3 jobs the pipeline had just closed. Posting from inside the
main pipeline run would be wrong: at that moment JobBoardly hasn't imported the
new jobs, so their pages don't exist yet.

**`jobs.xml`'s `<location>` is empty for every feed-imported job** - only paid
employer posts fill it, because the feed sends discrete city/state fields. So the
city check reads each candidate's page instead: its JobPosting structured data
(`addressLocality`/`addressRegion`) is exactly what the page displays. A city
JobBoardly dropped (see "JobBoardly geocodes `<city>`") is therefore never put on
a card. Only the 10 best-ranked candidates get a page fetch.

**What qualifies** (`pipeline/linkedin/picker.py`): live on the site and not
pinned, recent (below), explicit salary
within sane bounds, a city and state the page shows, a usable logo, and a title
that reads cleanly. **Maintenance roles are excluded for now** - Maintenance
Technician, Maintenance Supervisor and Groundskeeper & Porter categories, plus a
title backstop (Grayson's call, 2026-09-27). Remove a category from
`EXCLUDED_CATEGORIES` to start posting it. **Low-paying jobs are not highlighted**:
a job is skipped when the bottom of its pay range is $21/hr or less, or $45,000/yr
or less (`LINKEDIN_MIN_HOURLY_PAY_FLOOR` / `LINKEDIN_MIN_YEARLY_PAY_FLOOR`, Grayson's
call 2026-09-28 - the bottom, not the top, so "$20-$30/hr" is skipped). No company repeats within
`LINKEDIN_COMPANY_COOLDOWN_DAYS` (14). Role types are balanced, not prioritised:
the category posted least in the last 12 posts goes first.

**Titles are cut back to the role, or skipped.** Property names, requisition
numbers, bonuses and shift notes are removed ("Leasing Consultant - Lakeline at
Bartram Park" -> "Leasing Consultant"; "Assistant Community Manager Manufactured
Housing Community and RV Park" -> "Assistant Community Manager"). Job fairs,
non-English postings, and titles still too long for two lines on the image are
skipped. Measured on 2026-09-27: of 462 distinct non-maintenance titles in
state, 445 come out usable. Add a case to `tests/test_linkedin.py` when a new
title format comes out wrong.

**Logos are trimmed of padding and checked.** Several logo files are mostly empty
margin (CommonPlace, Griffis, IPG, KPM). `logo_problem` rejects a file whose
artwork is missing: `reside-living.png` used to be Reside's white wordmark
flattened onto white, leaving only the blue "i". It was replaced on 2026-09-27
with the dark version from resideliving.com (rendered from their SVG, since
Pillow can't read SVG - which is also why `fairstead.svg` is skipped).
JobBoardly keeps its own copy of the logo for each job (the two live Reside
listings had two different cdn.jobboardly.com URLs), so a replaced file should
only show on jobs imported afterwards.

**The caption's facts are templated; only the opening line is Claude's**
(`claude-opus-5`, low effort, about 13 calls a month). Location, pay, company and
link come straight from the job record, so the model can't misstate them. Any
failure - API error, refusal, SDK mismatch, unusable output - falls back to a
template line; a post never fails because of its caption. As in the rewriter, do
not add `temperature`.

**Caption tone is Grayson's call, and three rules are enforced in code**
(2026-09-28). The first prompt asked for a line that would "make someone want to
open the listing" and produced cocky, AI-sounding lines ("630 units, a waitlist
longer than the vacancies... the kind of compliance depth that leads to..."). The
prompt now asks for a helpful, professional, somewhat personable summary, like a
recruiter describing the role to a colleague, and not opening with the company
(it is listed below). `_clean_hook` rejects any line with an em or en dash (or a
spaced/doubled hyphen standing in for one; hyphenated words are fine), "it's not X,
it's Y" / "not just X, but Y" framing, or an exclamation point. A rejected line
gets one retry, then the template line is used. The rewritten site descriptions
the model reads often use "This isn't a traditional role..." framing, which is
why the guard exists - don't remove it. Tested on 10 real jobs: 10 of 10 usable.

**Timing: three triggers per posting day, because GitHub's scheduler is not
reliable here.** Measured in the first week: Monday 2026-09-28's single 12:17 UTC
run started 7h24m late, Wednesday 2026-09-30's never started at all (no post
that day), and the main pipeline's 16:00 UTC run started 3-5 hours late every
day. So the workflow fires at 04:17, 08:17 and 12:17 UTC on Mon/Wed/Fri. The
first run to start schedules the post in Buffer for `LINKEDIN_POST_TIME` 10:00
`America/New_York`; the others hit the one-post-per-day guard and stop. In winter
the 04:17 trigger lands on the previous evening in Eastern time; `next_post_time`
handles that by aiming at the next posting day (but never more than 12 hours
ahead, `MAX_EARLY`). A run delayed past 10 AM posts 15 minutes later. If no run
starts before `LINKEDIN_LATEST_POST_HOUR` (6 PM), Monday's post moves to Tuesday
and Wednesday's to Thursday at 10 AM (`LINKEDIN_FALLBACK_WEEKDAYS`), and Friday's
is missed rather than posted on a weekend, with one "Friday's post was missed"
email (Grayson, 2026-09-30). No randomisation - nothing suggests LinkedIn rewards it,
and posting guidance favours consistency. If posts still go missing, the next
step is an external scheduler calling `workflow_dispatch` (needs a GitHub token).

**Colours rotate through five themes** (slate, coral, cream, teal, plum - all
built on the site's coral and slate) with "NEW JOB"/"NOW HIRING" alternating, so
consecutive posts never look identical. Rendering uses the bundled Open Sans (SIL
OFL, `pipeline/linkedin/fonts/`); the Segoe UI in the first mockups is Microsoft's
and can't be committed.

**Recency is judged by the label the site shows, at the moment the post goes
live.** When the post publishes (10:00 AM Eastern), the job's page must read
"posted 4 days ago" or newer (`LINKEDIN_MAX_POSTED_DAYS_AGO`; Grayson, 2026-09-28:
"4 days ago is the oldest that can go live" - and he explicitly does not want
the pool widened). The date is `published_at` from `jobs.xml` (the employer's
posting date, at midnight Central), not our `state.json` `published_at`.
**The site rounds**: measured on live pages, 2.97 days reads "3 days ago" and
4.97 reads "5 days ago", so "5 days ago" starts at 4.5 days
(`site_days_ago`). In practice a Monday post can use jobs dated Thursday or later.

**A day with no qualifying job is skipped, not failed - and that is expected.**
Replaying the ten posting days Sep 7-27 with the 4-day rule and the pay floor gave
6 posts and 4 empty days (roughly two posts a week). Empty days come from real
gaps: most fresh jobs list no salary, and the rest are often under the floor or
from a company in the 14-day cooldown. Grayson is fine with empty days. On one,
`prepare` exits 0, nothing is posted, and the email says "no post today" with
the skip reasons; if the reasons look wrong (say, every job "site page shows no
city"), something upstream broke. Only a run within 3 hours of the post time
sends that email (earlier triggers stay quiet, since the day's jobs may not have
imported yet), and only once per day - `skipped_days.json` on the branch records it.

**One post per posting day.** Each history entry records the posting day it
serves (`slot`, which differs from the post date when a post was moved to the
next day). Once a posting day has a post, or was written off (no qualifying job,
or missed - `skipped_days.json`), every further run for it does nothing and
sends no email. Checking the slot rather than the post date is what stops a late
Monday straggler from adding a Tuesday post when Monday already posted. Entries
from before `slot` existed fall back to their `due_at` date. Grayson's computer
plays no part in any of this (he asked): the runs are on GitHub, and Buffer publishes.

**Failure modes.** A Buffer failure leaves the job unrecorded,
and the next run picks afresh. If Buffer accepted a post but the final push of
`history.json` failed, the next run could pick the same job again. The pipeline
watchdog does not watch this workflow; a runner outage just means a missed post.
Buffer's documented schema can't tell a LinkedIn page from a personal profile
(`service` is "linkedin" for both), so with more than one LinkedIn channel
connected it refuses to guess - set the repo variable `BUFFER_CHANNEL_ID`.
