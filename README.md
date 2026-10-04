# pmj-pipeline

Scrapes property management job listings from company ATS systems, classifies and rewrites them with Claude, and outputs an XML feed and CSV file for [propertymanagementjobs.us](https://propertymanagementjobs.us).

The pipeline runs automatically once a day via GitHub Actions and publishes `output/feed.xml` to GitHub Pages, where JobBoardly imports it.

Non-obvious behaviour and past incidents are written up in `CLAUDE.md` (mirrored in `AGENTS.md`). Read it before changing anything.

---

## Local setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Mac/Linux
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

## Running locally

```bash
# Full run
python -m pipeline.main

# Test with a small batch (no API cost for already-cached jobs)
python -m pipeline.main --limit 10

# Test source fetching without rewriting (classification still calls the API)
python -m pipeline.main --dry-run --limit 20

# Skip Google Indexing API pings (use when JobBoardly handles indexing)
python -m pipeline.main --skip-indexing
```

## LinkedIn posts

Every Monday, Wednesday and Friday `.github/workflows/linkedin-post.yml` picks one job,
draws its image and schedules it on the LinkedIn company page through Buffer. Every post
is logged, with its image and caption, on the `linkedin-posts` branch. How it works and
its gotchas: CLAUDE.md, "LinkedIn Posts".

Local dry run (writes to `./linkedin-posts`, sends nothing):

```bash
python -m pipeline.linkedin prepare --no-ai
python -m pipeline.linkedin publish --no-wait
```

## Adding sources

Edit `sources.yaml`. Each ATS type has a list of companies:

```yaml
greenhouse:
  - slug: company-slug
    company_name: Company Name
lever:
  - slug: company-slug
    company_name: Company Name
```

Supported ATS types: `greenhouse`, `lever`, `ashby`, `workable`, `recruitee`, `smartrecruiters`.

Give every new company a `logo_url` (self-hosted in `output/logos/`), or JobBoardly shows the ATS's logo instead. How to find and vet new companies: CLAUDE.md, "Adding companies".

---

## GitHub Actions setup

### 1. Set the repository secrets

Go to your repo on GitHub:
**Settings > Secrets and variables > Actions > New repository secret**

| Secret | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | Classification, rewriting, salary extraction, LinkedIn captions |
| `GMAIL_USERNAME`, `GMAIL_APP_PASSWORD` | Success, failure and watchdog emails |
| `BUFFER_API_KEY` | Scheduling LinkedIn posts |

The runner has no `.env` file. Non-secret settings (`CLOSURE_CHECK_ENABLED`, `LINKEDIN_POSTING_ENABLED`) are set in the workflow files, and everything else uses the defaults in `pipeline/config.py`.

### 2. Enable GitHub Pages

**Settings > Pages > Source: GitHub Actions**

Once enabled, `output/feed.xml` will be available at:
```
https://YOUR-USERNAME.github.io/pmj-pipeline/feed.xml
```

### 3. Initial commit of tracked files

Before the first workflow run, make sure `data/state.json` and `output/feed.xml` exist in the repo. The first pipeline run will create them. After that run commits them, subsequent runs will update them.

---

## Automated schedule

The pipeline is scheduled once a day at **16:00 UTC** (cron: `0 16 * * *`). GitHub starts scheduled runs late, often by 3 to 5 hours, so expect it to land in the afternoon or evening UTC. Each run:

- Fetches all configured sources
- Drops every job outside the US (the board is US-only; see CLAUDE.md)
- Keeps jobs the employer posted in the last `JOB_MAX_AGE_DAYS` (2) days, at most one per company, and skips jobs already in state
- Classifies new jobs with Claude Haiku
- Publishes at most `MAX_JOBS_PER_RUN` (13) new jobs, rotating which companies get the slots each day
- Rewrites PM jobs with Claude Sonnet (cached, no re-cost for existing jobs)
- Extracts salary data with Claude Haiku
- Checks every published job against its employer's board and removes jobs confirmed closed on two runs in a row
- Writes `output/feed.xml`, refusing to publish if the feed would shrink by more than 15% in one run
- Commits `data/state.json` and `output/feed.xml` back to `main`, which triggers the GitHub Pages deployment

Jobs stay in the feed for up to 60 days, or until confirmed closed.

AI responses are cached in `data/classification_cache.json` and `data/rewrite_cache.json` and persisted across runs via GitHub Actions cache. Each job is only processed once.

Other workflows:

- `pipeline-watchdog.yml` retries a pipeline run that GitHub never gave a runner, and emails about it
- `deploy-pages.yml` publishes `output/` (the feed and logos) to GitHub Pages
- `linkedin-post.yml` posts one job to LinkedIn on Mondays, Wednesdays and Fridays (see above)

## Manual trigger

Go to **Actions > Run Pipeline > Run workflow** to trigger a run immediately. Leave **allow_mass_removal** unchecked: it lets one run remove more than 15% of the feed, and is only for an approved cleanup.

## Monitoring

- Every run that updates the feed sends a success email; a failed run sends a failure email
- Check the **Actions** tab for run status and logs
- Download the `pipeline-output-*` artifact from any run for that run's feed, CSV, and quality samples
- If something looks wrong, a `NOTICE.txt` will appear in the repo root explaining what triggered the alert

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ANTHROPIC_API_KEY` error in logs | Secret not set | Add secret in Settings > Secrets |
| Zero new jobs every run | All jobs already in state, or sources returning no results | Check source ATS URLs manually |
| High rejection rate notice | Classifier degraded or source feed changed | Review Actions log, run locally with `--limit 5` |
| "Feed not published" / feed guard notice | The run would have removed more than 15% of the feed | Usually a census problem or a multi-day outage; see CLAUDE.md, "Feed guard" |
| Scheduled run missing or hours late | GitHub's cron scheduler is unreliable | Check the Actions tab; trigger manually if needed |
| Pages not deploying | GitHub Pages not enabled | Enable in Settings > Pages |
| `data/state.json` missing after run | First run or cache miss | Normal - will be created and committed |

## Running tests

```bash
pytest tests/
```
