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

**`ANTHROPIC_API_KEY` must be set as a GitHub repo secret** (Settings → Secrets and variables → Actions). If the daily run shows 400 errors with "credit balance too low", top up at console.anthropic.com.

**GitHub's cron scheduler is unreliable and can be delayed by minutes to hours.** The scheduled run at `0 0 * * *` does not always fire at exactly midnight UTC. If a run appears missing, check the Actions tab before assuming a bug — it may just be delayed. The GitHub Actions API also has a lag before new runs appear.

**The GitHub runner never has a `.env` file** — it's gitignored. `JOB_MAX_AGE_DAYS` and other non-secret config must be set via `config.py` defaults (or added as GitHub env vars in the workflow). Changes to local `.env` do not affect automated runs.

**`classification_cache.json` and `rewrite_cache.json` on the runner are NOT committed to the repo** — they're only persisted via GitHub Actions cache. The local copies reflect only what was cached during local runs, not what the runner has classified. If you're trying to debug why the runner rejected a specific job, you can't check the local cache for it.

**Silent early returns don't trigger the failure email.** If classification or rewrite fails (rate limit, API error, etc.), `main.py` catches the exception and returns with exit code 0. GitHub considers the run "successful," no failure email is sent, and no jobs are committed. If the feed hasn't updated and there's no failure email, check the run logs — a silent error is likely.

**Changing `JOB_MAX_AGE_DAYS` and the cron schedule at the same time creates a gap.** Jobs published in the window between the last old-schedule run and the first new-schedule run can fall outside the new age window and be permanently missed. If you need to change both, temporarily increase `JOB_MAX_AGE_DAYS` to cover the gap, then lower it after the first new-schedule run.

**`JOB_MAX_AGE_DAYS` must be at least 2 when the 3-per-company cap is active.** With a 1-day window, any jobs dropped by the cap today are outside the window tomorrow — permanently lost. A 2-day window gives capped jobs a second chance the following run.

---

## JobBoardly Integration

- Feed URL: `https://graysontu.github.io/pmj-pipeline/feed.xml`
- Root element must be `<source>` — changing it breaks stored field mappings.
- JobBoardly requires `<publisher>`, `<publisherurl>`, `<lastBuildDate>` to recognize the feed format.
- JobBoardly has a "require salary on all posts" setting — keep this **OFF** or jobs without salary data won't import.
- Field mapping syntax: `source/job → fieldname` (e.g., `source/job/title → Title`).
