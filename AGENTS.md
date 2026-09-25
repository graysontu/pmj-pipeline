# PMJ Pipeline — Non-Obvious Findings & Gotchas

Reference this when troubleshooting. These are things that aren't obvious from reading the code.

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
cannot be inferred. Flag it; don't pick one.

**Semicolon-separated multi-location strings must be split first.** Splitting on
commas alone turned `"Reno, NV; Sparks, NV"` into the city `"Nv; Sparks"`. The first
segment that resolves wins.

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

**Looked at and rejected on 2026-09-23**, so nobody re-researches them: Evernest
(posts Philippines/Mexico roles that would publish with no US location), Flow, CIM
Group, Intrinsic Development, AVE by Korman (mostly corporate or hospitality roles),
RXR (location field unparseable), Twin Pines (NYC addresses without a state, ~2
jobs/month), and the dormant SmartRecruiters boards above.

**Don't add employers who pay for listings on the board.** As of 2026-09-23: MAA,
Federal Realty Investment Trust, Lloyd Management, Pennrose, Ciminelli Real Estate
Services.

**Avanath's Greenhouse board token is `communitymanager`**, and about half its
location fields are property names ("Northpointe"). Greenhouse's `offices[].location`
(returned with `content=true`) has the real address for 40 of those 43 jobs, but the
fetcher does not read `offices`. Berkshire, Sunrise and CloudTen have no location in
`offices` either, so that fallback would only help Avanath.

**LivCor and AIR Communities are both Blackstone operators**, and livcor.com redirects
to AIR, but their SmartRecruiters boards list different communities (no overlapping
postings on 2026-09-23). They are not duplicates.

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

**A daily publish cap is enforced by `MAX_JOBS_PER_RUN`** (`config.py`, default 11 since 2026-09-26 - it was 9 - override via env). It is applied **after classification and before rewrite** — after, so the cap counts real PM jobs rather than candidates the classifier would reject; before, so deferred jobs cost no Sonnet rewrite tokens. Two things to preserve if you touch it:

- **Don't reassign `kept`.** The health-check block computes `rejection_rate` from `classified` vs `kept`; capping `kept` in place makes every capped run look like a >50% rejection spike and writes a bogus NOTICE.txt. The capped list lives in `publishable`.
- **The selection rotates by date and must keep doing so.** `kept` follows `sources.yaml` order and the 1-per-company cap means each entry is a different company, so a plain `kept[:N]` would hand every slot to the same top-of-file companies daily and starve the ones at the bottom. The offset steps by the cap size per day so consecutive days take near-disjoint slices; with 15–20 qualifying jobs this covers every company within about 6 days.

**Deferred jobs get one extra chance, not an unlimited queue.** They aren't written to state, so the next run reconsiders them — but only while they remain inside the `JOB_MAX_AGE_DAYS` window. At ~15–20 qualifying jobs/day against a cap of 9, expect a persistent surplus that ages out unpublished. After 15 companies were added on 2026-09-24 the first run found 32 qualifying jobs, so even at 11 most days still leave a surplus. The cap is a ceiling, not a backlog.

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
