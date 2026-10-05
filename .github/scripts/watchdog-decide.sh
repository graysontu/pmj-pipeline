#!/usr/bin/env bash
# Decides what pipeline-watchdog.yml does about one failed workflow run:
# retry it, email about it, or do nothing. Writes action, subject and body to
# $GITHUB_OUTPUT. Kept in a script so it can be run locally against real runs:
#
#   GITHUB_OUTPUT=/tmp/out REPO=graysontu/pmj-pipeline WORKFLOW="LinkedIn Post" \
#   WORKFLOW_ID=... RUN_ID=... ATTEMPT=1 CREATED_AT=... RUN_URL=... \
#   bash .github/scripts/watchdog-decide.sh
#
# The goal (Grayson, 2026-10-05) is that GitHub's own "Run failed" emails can be
# turned off: every failure that needs attention gets one of our emails, and a
# day that worked out in the end sends none.
set -euo pipefail

MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}  # overridable only to test the out-of-retries path locally

# A run that never got a machine has zero recorded steps. That is what separates
# "GitHub had no runner" from "our code failed".
steps_run=$(gh api "repos/$REPO/actions/runs/$RUN_ID/attempts/$ATTEMPT/jobs" --jq '[.jobs[].steps[]?] | length')
if [ "$steps_run" -eq 0 ]; then kind=infrastructure; else kind=step; fi
retries_left=false
if [ "$ATTEMPT" -lt "$MAX_ATTEMPTS" ]; then retries_left=true; fi

# Successful runs of the same workflow created at or after a given time.
successes_since() {
  gh api -X GET "repos/$REPO/actions/workflows/$WORKFLOW_ID/runs" \
    -f status=success -f created=">=$1" --jq '.total_count'
}

action=none
subject=""
body=""
case "$WORKFLOW" in
  "Run Pipeline")
    # Step failures email from run-pipeline.yml itself, and retrying them would
    # only burn Anthropic credits on the same bug. A missing runner is retried.
    if [ "$kind" = infrastructure ]; then
      if $retries_left; then
        action=retry
      else
        action=email
        subject="PMJ Pipeline: GitHub couldn't start the daily run"
        body="GitHub couldn't give the daily pipeline run a machine, even after $MAX_ATTEMPTS tries, so it didn't run. Nothing is wrong with the code; this is a GitHub capacity problem.

One missed day is recoverable, but two in a row drop some jobs for good (JOB_MAX_AGE_DAYS is 2). Ask Claude to re-run it, or use \"Re-run jobs\" on the run page once GitHub recovers.

Run: $RUN_URL"
      fi
    fi
    ;;
  "LinkedIn Post")
    # Step failures email from linkedin-post.yml itself. A missing runner is
    # retried: that is always safe, since a run for a day that already posted
    # does nothing. Only if no LinkedIn run has worked in the last 20 hours is
    # it worth an email.
    if [ "$kind" = infrastructure ]; then
      if $retries_left; then
        action=retry
      elif [ "$(successes_since "$(date -u -d '-20 hours' +%Y-%m-%dT%H:%M:%SZ)")" -eq 0 ]; then
        action=email
        subject="LinkedIn: GitHub couldn't start today's LinkedIn runs"
        body="None of today's LinkedIn runs could start: GitHub didn't give them a machine, even after retries. So today's post probably didn't go out. Nothing is wrong with the code, and the next posting day runs as normal.

Run: $RUN_URL"
      fi
    fi
    ;;
  "Deploy Pages")
    # Publishing the feed costs nothing to retry, so any failure is retried.
    # If a deploy that started later has already succeeded (say, after a merge),
    # the site is current and there is nothing to do.
    if [ "$(successes_since "$CREATED_AT")" -gt 0 ]; then
      action=none
    elif $retries_left; then
      action=retry
    else
      action=email
      subject="PMJ site: the job feed wasn't published"
      body="Publishing the job feed to GitHub Pages failed $MAX_ATTEMPTS times, so JobBoardly is still reading the previous feed, and today's new jobs won't reach the site until a deploy works. This is usually a temporary GitHub problem. Ask Claude to look, or use \"Re-run jobs\" on the run page.

Run: $RUN_URL"
    fi
    ;;
esac

echo "$WORKFLOW run $RUN_ID attempt $ATTEMPT: $kind failure ($steps_run steps recorded). Decision: $action"
{
  echo "action=$action"
  echo "subject=$subject"
  echo "body<<WATCHDOG_BODY_END"
  echo "$body"
  echo "WATCHDOG_BODY_END"
} >> "$GITHUB_OUTPUT"
