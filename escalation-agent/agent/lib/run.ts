import { createOpenAiLlm } from "./llm";
import { fetchPartnerRegistry } from "./registry";
import { postSlackMessage } from "./slack";
import { getStore } from "./store";
import { runSweep, type SweepSummary } from "./sweep";
import { createVitallyClient } from "./vitally";

/**
 * Builds the real dependencies from environment variables and runs one sweep.
 * Throws (before doing any triage) when the partner registry can't be loaded,
 * so a broken registry never silently shrinks coverage.
 */
export async function runConfiguredSweep(): Promise<SweepSummary> {
  const registry = await fetchPartnerRegistry();
  const slackConfigured = Boolean(process.env.SLACK_BOT_TOKEN && process.env.SLACK_ALERT_CHANNEL_AGENT);
  const explicitDryRun = process.env.ESCALATION_DRY_RUN === "1";
  const dryRun = explicitDryRun || !slackConfigured;
  if (!explicitDryRun && !slackConfigured) {
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
