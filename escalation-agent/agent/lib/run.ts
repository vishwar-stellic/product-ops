import { createOpenAiLlm } from "./llm";
import { fetchPartnerRegistry } from "./registry";
import { startRun, type RunRecord, type RunTrigger } from "./runs";
import { postSlackMessage } from "./slack";
import { getStore } from "./store";
import { runSweep, type SweepSummary } from "./sweep";
import { createVitallyClient } from "./vitally";

/**
 * Whether a sweep posts to Slack. A dry run (alerts logged only) is forced by
 * ESCALATION_DRY_RUN=1, by a caller asking for one (manual runs may request a
 * dry run but can never override the env into a live one), or by Slack not
 * being configured.
 */
export function resolveDryRun(requestedDryRun = false): boolean {
  const slackConfigured = Boolean(process.env.SLACK_BOT_TOKEN && process.env.SLACK_ALERT_CHANNEL_AGENT);
  return process.env.ESCALATION_DRY_RUN === "1" || requestedDryRun || !slackConfigured;
}

/**
 * Builds the real dependencies from environment variables and runs one sweep.
 * Throws (before doing any triage) when the partner registry can't be loaded,
 * so a broken registry never silently shrinks coverage.
 */
export async function runConfiguredSweep(options: { dryRun?: boolean } = {}): Promise<SweepSummary> {
  const registry = await fetchPartnerRegistry();
  const slackConfigured = Boolean(process.env.SLACK_BOT_TOKEN && process.env.SLACK_ALERT_CHANNEL_AGENT);
  const dryRun = resolveDryRun(options.dryRun);
  if (!slackConfigured && process.env.ESCALATION_DRY_RUN !== "1") {
    console.warn("[escalation-agent] Slack not configured - running as a dry run (alerts are logged only)");
  }
  const summary = await runSweep({
    store: getStore(),
    vitally: createVitallyClient(),
    llm: createOpenAiLlm(),
    post: dryRun ? null : (text) => postSlackMessage(text),
    registry,
  });
  console.log(`[escalation-agent] sweep complete ${JSON.stringify(summary)}`);
  return summary;
}

/**
 * Starts a sweep under the shared lock (see runs.ts). Returns null when another
 * sweep is already running. Await `.complete(...)`'s result (or hand it to
 * waitUntil) to let the run finish.
 */
export async function startConfiguredRun(trigger: RunTrigger, requestedDryRun = false) {
  const dryRun = resolveDryRun(requestedDryRun);
  const active = await startRun(getStore(), { trigger, dryRun });
  if (!active) return null;
  return {
    record: active.record,
    finished: (): Promise<RunRecord> => active.complete(() => runConfiguredSweep({ dryRun })),
  };
}
