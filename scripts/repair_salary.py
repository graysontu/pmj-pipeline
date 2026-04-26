"""One-off script: extract salary for all state.json jobs that have null salary_min."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.ai_rewriter import RewrittenJob, batch_extract_salary
from datetime import datetime, timezone

STATE_PATH = Path("data/state.json")


def main():
    state = json.load(open(STATE_PATH, encoding="utf-8"))

    null_jobs = {k: v for k, v in state.items() if v.get("salary_min") is None}
    print(f"Jobs needing salary extraction: {len(null_jobs)}")

    jobs = []
    for source_id, entry in null_jobs.items():
        try:
            job = RewrittenJob(
                source_id=entry["source_id"],
                source_name="",
                title=entry["title"],
                company=entry["company"],
                location=entry["location"],
                category=entry["category"],
                apply_url=entry["apply_url"],
                description_html="",
                description_text="",
                rewritten_description=entry["rewritten_description"],
                date_posted=datetime.fromisoformat(entry["date_posted"]),
                job_type=entry.get("job_type", "fulltime"),
                remote_type=entry.get("remote_type", "onsite"),
                company_url=entry.get("company_url", ""),
                salary_min=None,
                salary_max=None,
                salary_currency="USD",
                salary_schedule="yearly",
                persona_used="",
                structure_used="",
                is_pm_job=True,
                confidence=0,
            )
            jobs.append(job)
        except Exception as e:
            print(f"Skipping {source_id}: {e}")

    print(f"Extracting salary for {len(jobs)} jobs...")
    enriched = batch_extract_salary(jobs)

    updated = 0
    for job in enriched:
        if job.source_id in state:
            state[job.source_id]["salary_min"] = job.salary_min
            state[job.source_id]["salary_max"] = job.salary_max
            state[job.source_id]["salary_currency"] = job.salary_currency
            state[job.source_id]["salary_schedule"] = job.salary_schedule
            if job.salary_min is not None:
                updated += 1

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)

    print(f"Done. {updated}/{len(jobs)} jobs got salary data. state.json saved.")


if __name__ == "__main__":
    main()
