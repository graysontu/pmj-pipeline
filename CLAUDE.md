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

`pipeline/geo.py` handles all of these. If a new company shows wrong locations, add their format to the test cases in that file and extend `parse_location`.

**Berkshire Group has no city/state in their location field** — only property names. We suppress `<country>` in the XML when both city and state are empty to avoid JobBoardly showing "United States."

---

## ATS APIs (verification gotchas)

**Workable's v3 jobs endpoint only answers POST** — GET returns 404 for every account, indistinguishable from a bad slug. The list response has no description; each fresh job needs a per-job GET to the v2 detail endpoint. The fetcher only detail-fetches jobs published within `JOB_MAX_AGE_DAYS + 1` days to keep HTTP volume low.

**SmartRecruiters returns 200 with zero postings for nonexistent company slugs** — an empty result proves nothing. Only a non-empty postings list confirms a slug. The list response has no `jobAd` (description) and its `ref` is an API URL, so each fresh posting needs a detail fetch for content and the real apply page.

**Ashby and Recruitee have essentially no US property management companies** (verified July 2026 via site: searches). Don't spend time hunting for PM slugs there.

**The 1-per-company cap runs BEFORE classification**, so a company whose board is mostly non-PM roles (construction, corporate, finance) wastes its daily slot on jobs the classifier rejects. Prefer companies whose boards are majority PM/leasing titles.

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

**A daily publish cap is enforced by `MAX_JOBS_PER_RUN`** (`config.py`, default 9, override via env). It is applied **after classification and before rewrite** — after, so the cap counts real PM jobs rather than candidates the classifier would reject; before, so deferred jobs cost no Sonnet rewrite tokens. Two things to preserve if you touch it:

- **Don't reassign `kept`.** The health-check block computes `rejection_rate` from `classified` vs `kept`; capping `kept` in place makes every capped run look like a >50% rejection spike and writes a bogus NOTICE.txt. The capped list lives in `publishable`.
- **The selection rotates by date and must keep doing so.** `kept` follows `sources.yaml` order and the 1-per-company cap means each entry is a different company, so a plain `kept[:N]` would hand every slot to the same top-of-file companies daily and starve the ones at the bottom. The offset steps by the cap size per day so consecutive days take near-disjoint slices; with 15–20 qualifying jobs this covers every company within about 6 days.

**Deferred jobs get one extra chance, not an unlimited queue.** They aren't written to state, so the next run reconsiders them — but only while they remain inside the `JOB_MAX_AGE_DAYS` window. At ~15–20 qualifying jobs/day against a cap of 9, expect a persistent surplus that ages out unpublished. The cap is a ceiling, not a backlog.

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

## JobBoardly Integration

- Feed URL: `https://graysontu.github.io/pmj-pipeline/feed.xml`
- Root element must be `<source>` — changing it breaks stored field mappings.
- JobBoardly requires `<publisher>`, `<publisherurl>`, `<lastBuildDate>` to recognize the feed format.
- JobBoardly has a "require salary on all posts" setting — keep this **OFF** or jobs without salary data won't import.
- Field mapping syntax: `source/job → fieldname` (e.g., `source/job/title → Title`).
