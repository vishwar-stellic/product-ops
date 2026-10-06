# Escalation agent (eve)

A parallel, read-only version of the Partner Insights escalation triage, built on
[Vercel eve](https://eve.dev). It runs next to the Python job (`product_status/escalation_report.py`),
posts Live Fire / Smoldering alerts to **its own Slack channel** so the two can be compared side by side, and
**learns from emoji reactions** on those alerts.

It never writes to the dashboard's cache, never posts to the Python job's Slack target, and only reads
Vitally.

## How it works

```
schedule (hourly UTC cron, gated to Mon-Fri 8/10/12/14/16/18 ET)
  -> GET {PRODUCT_OPS_BASE_URL}/api/internal/partner-registry     same partner set as the Python job
  -> feedback snapshot from Slack reactions                        (agent/lib/feedback.ts)
  -> per partner (8 in parallel):
       Vitally: new partner-authored, non-auto-generated email      (agent/lib/vitally.ts)
       model: update the tracked items (same rubric + call as Python) (agent/lib/triage.ts, llm.ts)
       save state, then post one Slack message per item that newly reached Live Fire / Smoldering
Slack reaction_added / reaction_removed on an alert
  -> agent/channels/slack.ts -> agent/lib/reactions.ts -> agent/lib/feedback.ts
```

Same as the Python job: 3-day lookback, only `google`/`intercom` Vitally sources, partner-authored detection
(including the mislabelled-`outbound` rescue), calendar/OOO filtering, incremental per-partner state, one
message per newly notable item, same model and request parameters (`gpt-5-mini`, reasoning `low`).

The rubric is **not retyped**: `agent/lib/triage-rubric.generated.ts` is generated from the Python prompt.
After changing `_TRIAGE_SYSTEM_PROMPT` run, from the repo root:

```sh
PYTHONPATH=. .venv/bin/python escalation-agent/scripts/export-rubric.py
```

## Slack feedback

React to an alert in the channel:

| Reaction | Meaning | Calibration example says |
|---|---|---|
| `:+1:` | right call | the score was right |
| `:-1:` | false alarm | should have scored 0-2 |
| `:arrow_down:` | real, but too severe | one level lower (5 -> 4, 4 -> 3) |
| `:arrow_up:` | under-rated | one level higher |
| `:white_check_mark:` | resolved | not a calibration example; the item is dropped from the tracked list |

Rules: the **latest** reaction on an alert wins (removing it reveals the previous one); unknown emoji are
ignored; reactions on any other message are ignored. At the start of each sweep, the 20 most recent
verdicts (balanced across labels, capped in size) are rendered as a `TEAM FEEDBACK CALIBRATION` block in the
prompt. The block says the rubric still governs. **The rubric text itself is never edited.** The vocabulary
lives in `EMOJI_VERDICTS` in `agent/lib/feedback.ts`.

## Setup

1. **Slack app** (new, or reuse the existing one): bot scopes `chat:write`, `reactions:read`,
   `channels:history` (`groups:history` for a private channel). Event Subscriptions -> Request URL
   `https://<this-deployment>/eve/v1/slack`, subscribe to `reaction_added` and `reaction_removed`. Create the
   alert channel, `/invite` the bot, and copy the channel ID into `SLACK_ALERT_CHANNEL_AGENT` (the Python
   job keeps using its own `SLACK_ALERT_TARGET`; this agent never reads that one).
2. **Dashboard project (Python)**: it must have `CRON_SECRET` set. The new `/api/internal/partner-registry`
   endpoint returns 503 without it and 401 for a wrong secret.
3. **This project**: `eve link`, then set the env vars in `.env.example` on the Vercel project (including a
   new, dedicated Blob store for `BLOB_READ_WRITE_TOKEN`), then `eve deploy`.
4. First run: the sweep looks back 3 days, so expect a burst of alerts for currently-open Live Fire /
   Smoldering issues, same as the Python job's first run.

## Local development

```sh
npm install
npm test            # unit + pipeline tests (no network)
npm run typecheck
npm run calibrate   # LIVE: scores synthetic emails modelled on the rubric's human-scored examples (needs OPENAI_API_KEY)
cp .env.example .env.local   # fill in; leave SLACK_* unset to dry-run (alerts are logged, not posted)
```

State goes to `.state/` locally (no `BLOB_READ_WRITE_TOKEN`). `ESCALATION_IGNORE_WINDOW=1` runs the sweep
outside the Eastern business-hours slots; `ESCALATION_DRY_RUN=1` logs alerts instead of posting.

## Running on demand

The schedule follows the Python job (every 2 hours, Mon-Fri 8am-6pm ET). To run a sweep at any other
time, call the authenticated endpoint with the same `CRON_SECRET` the dashboard uses:

```sh
# start a sweep now (returns 202 immediately; the sweep finishes in the background)
curl -X POST https://<deployment>/escalation/run -H "Authorization: Bearer $CRON_SECRET"
# same, but only log what would be posted
curl -X POST https://<deployment>/escalation/run -H "Authorization: Bearer $CRON_SECRET" \
  -H "content-type: application/json" -d '{"dryRun": true}'
# is one running? what did the last run do?
curl https://<deployment>/escalation/run -H "Authorization: Bearer $CRON_SECRET"
```

Manual and scheduled runs share one lock, so they never overlap (a second request gets `409`; a lock
older than 30 minutes is treated as abandoned). A manual run can ask for a dry run, but it can never
turn `ESCALATION_DRY_RUN=1` off. Manual runs don't change the schedule.

## Comparing against the Python job

Both run at the same slots against the same partners with the same model, so differences come from the
framework and the feedback. Expect small timing skew (they run independently). Each eve alert ends with a
reaction legend, which is how you tell the channels apart at a glance.

## Things to watch

- **Run time.** The sweep runs inside the schedule handler. State is saved per partner and `lastMessageAt`
  only advances on success, so a run that is cut off simply resumes next slot; check the first few runs'
  duration in Vercel's Observability -> Cron Jobs.
- **eve is in beta** (Node 24); APIs may change.
- **Reaction races.** Two near-simultaneous reactions on the *same* alert can overwrite each other
  (read-modify-write on one blob). Rare in practice.
- Reactions are attributed to Slack user ids but the block shown to the model contains no names.
