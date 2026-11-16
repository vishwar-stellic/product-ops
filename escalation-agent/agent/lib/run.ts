import { createOpenAiLlm } from "./llm";
import { fetchPartnerRegistry } from "./registry";
import { startRun, type RunRecord, type RunTrigger } from "./runs";
import { formatSweepFailedMessage, postSlackMessage } from "./slack";
import { createDryRunStore, getStore } from "./store";
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
 * Posts the failure to the alert channel when a live sweep throws. Dry runs
 * stay quiet. A Slack outage while reporting is logged and swallowed so the
 * recorded failure (and the schedule's retry) still stand.
 */
export async function reportFailedRun(
  result: Pick<RunRecord, "status" | "dryRun" | "error">,
  post: (text: string) => Promise<unknown> = (text) => postSlackMessage(text),
): Promise<boolean> {
  if (result.status !== "failed" || result.dryRun) return false;
  try {
    await post(formatSweepFailedMessage(result.error ?? "sweep failed"));
    return true;
  } catch (error) {
    console.error(`[escalation-agent] failed to post the sweep-failure message: ${String(error)}`);
    return false;
  }
}

/**
 * Builds the real dependencies from environment variables and runs one sweep.
 * Throws (before doing any triage) when the partner registry can't be loaded,
 * so a broken registry never silently shrinks coverage. The caller posts that
 * failure to Slack (see reportFailedRun).
 */
export async function runConfiguredSweep(
  options: { dryRun?: boolean; seed?: boolean } = {},
): Promise<SweepSummary> {
  const registry = await fetchPartnerRegistry();
  const slackConfigured = Boolean(process.env.SLACK_BOT_TOKEN && process.env.SLACK_ALERT_CHANNEL_AGENT);
  const dryRun = resolveDryRun(options.dryRun);
  if (!slackConfigured && process.env.ESCALATION_DRY_RUN !== "1") {
    console.warn("[escalation-agent] Slack not configured - running as a dry run (alerts are logged only)");
  }
  const summary = await runSweep({
    // A dry run reads the real state but persists nothing (see createDryRunStore).
    store: dryRun ? createDryRunStore(getStore()) : getStore(),
    vitally: createVitallyClient(),
    llm: createOpenAiLlm(),
    // A seed run saves state but posts nothing (see startConfiguredRun).
    post:
      dryRun || options.seed
        ? null
        : (text, threadTs) => postSlackMessage(text, undefined, undefined, threadTs),
    registry,
  });
  console.log(`[escalation-agent] sweep complete ${JSON.stringify(summary)}`);
  return summary;
}

/**
 * `seed` triages and SAVES state without posting anything: used to establish a
 * baseline (e.g. after the state was reset) so the next scheduled sweeps alert
 * only on changes instead of re-announcing everything already posted. It is
 * ignored for a dry run, which never saves.
 *
 * Starts a sweep under the shared lock (see runs.ts). Returns null when another
 * sweep is already running. Await `.complete(...)`'s result (or hand it to
 * waitUntil) to let the run finish.
 */
export async function startConfiguredRun(trigger: RunTrigger, requestedDryRun = false, seed = false) {
  const dryRun = resolveDryRun(requestedDryRun);
  const active = await startRun(getStore(), { trigger, dryRun, seed });
  if (!active) return null;
  return {
    record: active.record,
    finished: async (): Promise<RunRecord> => {
      const result = await active.complete(() => runConfiguredSweep({ dryRun, seed }));
      await reportFailedRun(result);
      return result;
    },
  };
}
