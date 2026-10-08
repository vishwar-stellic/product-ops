# Escalation agent (eve)

The partner-escalation triage for the Product Ops dashboard, built on [Vercel eve](https://eve.dev). It reads
partner email from Vitally on a schedule, keeps a tracked list of escalation items per partner, posts Live Fire
/ Smoldering alerts to Slack, and **learns from emoji reactions** on those alerts. The dashboard's Partner
Insights tab shows the items it saved (`POST /escalation/state`); the dashboard does no triage of its own.

It is read-only against Vitally. It keeps its own state in a dedicated Vercel Blob store.

## How it works

```
schedule (hourly UTC cron, gated to Mon-Fri 8/10/12/14/16/18 ET)
  -> GET {PRODUCT_OPS_BASE_URL}/api/internal/partner-registry     the partners to triage
  -> feedback snapshot from Slack reactions                        (agent/lib/feedback.ts)
  -> per partner (8 in parallel):
       Vitally: new partner-authored, non-auto-generated email      (agent/lib/vitally.ts)
       model: update the tracked items against the rubric           (agent/lib/triage.ts, llm.ts)
       save state, then post one Slack message per item that newly reached Live Fire / Smoldering
Slack reaction_added / reaction_removed on an alert
  -> agent/channels/slack.ts -> agent/lib/reactions.ts -> agent/lib/feedback.ts
Dashboard Partner Insights tab
  -> POST /escalation/state {"partnerIds": [...]}                  (agent/lib/state-export.ts)
```

3-day lookback, only `google`/`intercom` Vitally sources, partner-authored detection (including the
mislabelled-`outbound` rescue), calendar/OOO filtering, incremental per-partner state, one message per newly
notable item, model `gpt-5-mini` with reasoning `low`.

The rubric lives in `agent/lib/triage-rubric.ts` (a plain template; edit it there). Alerts only fire for an
item whose evidence is in the emails read this run, and a tracked item keeps a stable `id` across rewording.

## Slack feedback

React to an alert in the channel. A one-line legend of these reactions is posted once at the end of each
sweep that raised alerts (not on every alert):

| Reaction | Meaning | Calibration example says |
|---|---|---|
| `:+1:` | right call | the score was right |
| `:-1:` | false alarm | should have scored 0-2 |
| `:arrow_down:` | real, but too severe | one level lower (5 -> 4, 4 -> 3) |
| `:arrow_up:` | under-rated | one level higher |

Rules: the **latest** reaction on an alert wins (removing it reveals the previous one); unknown emoji are
ignored; reactions on any other message are ignored. At the start of each sweep, the 20 most recent
verdicts (balanced across labels, capped in size) are rendered as a `TEAM FEEDBACK CALIBRATION` block in the
prompt. The block says the rubric still governs. **The rubric text itself is never edited.** The vocabulary
lives in `EMOJI_VERDICTS` in `agent/lib/feedback.ts`.

## Setup

1. **Slack app** (new, or reuse the existing one): bot scopes `chat:write`, `reactions:read`,
   `channels:history` (`groups:history` for a private channel). Event Subscriptions -> Request URL
   `https://<this-deployment>/eve/v1/slack`, subscribe to `reaction_added` and `reaction_removed`. Create the
   alert channel, `/invite` the bot, and copy the channel ID into `SLACK_ALERT_CHANNEL_AGENT`.
2. **Dashboard project**: it must have `CRON_SECRET` set (the same value as on this project) and
   `ESCALATION_AGENT_URL` pointing at this deployment. `/api/internal/partner-registry` returns 503 without
   the secret and 401 for a wrong one; this agent's `/escalation/*` routes use the same secret.
3. **This project**: `eve link`, then set the env vars in `.env.example` on the Vercel project (including a
   new, dedicated Blob store for `BLOB_READ_WRITE_TOKEN`), then `eve deploy`.
4. First run: the sweep looks back 3 days, so expect a burst of alerts for currently-open Live Fire /
   Smoldering issues.

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

The schedule is every 2 hours, Mon-Fri 8am-6pm ET. To run a sweep at any other
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

`POST /escalation/reset` with `{"confirm": true}` clears every partner's saved state (the next live sweep
re-triages the whole 3-day window and re-alerts); add `"includeAlerts": true` to also delete the stored
alert records and their reaction feedback (test data).

`POST /escalation/purge` with `{"conversation": "<vitally id>", "confirm": true}` removes saved items that
came from a thread another partner owns (Vitally attaches a thread to every participant's account); add
`"dryRun": true` to preview. `GET /escalation/debug?conversation=<id>` shows why a conversation alerted.

## Things to watch

- **Run time.** The sweep runs inside the schedule handler. State is saved per partner and `lastMessageAt`
  only advances on success, so a run that is cut off simply resumes next slot; check the first few runs'
  duration in Vercel's Observability -> Cron Jobs.
- **eve is in beta** (Node 24); APIs may change.
- **Reaction races.** Two near-simultaneous reactions on the *same* alert can overwrite each other
  (read-modify-write on one blob). Rare in practice.
- Reactions are attributed to Slack user ids but the block shown to the model contains no names.
