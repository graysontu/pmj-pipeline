# pmj-pipeline

Scrapes property management job listings from company ATS systems, classifies and rewrites them with Claude, and outputs an XML feed and CSV file for [propertymanagementjobs.us](https://propertymanagementjobs.us).

The pipeline runs automatically twice daily via GitHub Actions and publishes `output/feed.xml` to GitHub Pages.

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

# Test source fetching without rewriting
python -m pipeline.main --dry-run --limit 20

# Skip Google Indexing API pings (use when JobBoardly handles indexing)
python -m pipeline.main --skip-indexing
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

---

## GitHub Actions setup

### 1. Set the API key secret

Go to your repo on GitHub:
**Settings > Secrets and variables > Actions > New repository secret**

- Name: `ANTHROPIC_API_KEY`
- Value: your Anthropic API key

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

The pipeline runs at **6am and 6pm Pacific** (cron: `0 13,1 * * *` UTC):

- Fetches all configured sources
- Classifies new jobs with Claude Haiku
- Rewrites PM jobs with Claude Sonnet (cached, no re-cost for existing jobs)
- Extracts salary data with Claude Haiku
- Updates `data/state.json` and `output/feed.xml`
- Commits changes back to `main`
- Triggers GitHub Pages deployment

AI responses are cached locally in `data/classification_cache.json` and `data/rewrite_cache.json` and persisted across runs via GitHub Actions cache. Each job is only processed once.

## Manual trigger

Go to **Actions > Run Pipeline > Run workflow** to trigger a run immediately.

## Monitoring

- Check the **Actions** tab for run status and logs
- Download the `pipeline-output-*` artifact from any run for that run's feed, CSV, and quality samples
- If something looks wrong, a `NOTICE.txt` will appear in the repo root explaining what triggered the alert

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ANTHROPIC_API_KEY` error in logs | Secret not set | Add secret in Settings > Secrets |
| Zero new jobs every run | All jobs already in state, or sources returning no results | Check source ATS URLs manually |
| High rejection rate notice | Classifier degraded or source feed changed | Review Actions log, run locally with `--limit 5` |
| Pages not deploying | GitHub Pages not enabled | Enable in Settings > Pages |
| `data/state.json` missing after run | First run or cache miss | Normal - will be created and committed |

## Running tests

```bash
pytest tests/
```
