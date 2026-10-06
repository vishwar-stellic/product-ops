import type { Severity, TrackedItem } from "./triage";
import { vitallyConversationUrl } from "./vitally";

const SLACK_API_BASE = "https://slack.com/api";

const SEVERITY_LABEL: Record<string, string> = { LIVE_FIRE: "Live Fire", SMOLDERING: "Smoldering" };
// Fire for Live Fire, firecracker for Smoldering - same as the Python alerts.
const SEVERITY_EMOJI: Record<string, string> = { LIVE_FIRE: ":fire:", SMOLDERING: ":firecracker:" };

/**
 * Slack's required escaping for message text. Without it a partner email
 * containing "<!channel>" or "<@U123>" would trigger a real mention, and a
 * literal "&" can garble the message. Applied only to untrusted text, never to
 * our own `<url|source>` link.
 */
export function slackEscape(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Posted once at the end of a sweep that raised alerts (not repeated on every alert). */
export const REACTION_LEGEND =
  "React: :+1: right call, :-1: false alarm, :arrow_down: too severe, :arrow_up: under-rated";

export interface AlertContext {
  partnerName: string;
  vitallyAccountUrl: string | null;
}

/**
 * Slack mrkdwn for ONE newly-notable item - same layout as the Python
 * `_format_slack_message`, plus a small footer so people comparing the two
 * channels can tell which job posted it.
 */
export function formatSlackMessage(item: TrackedItem, ctx: AlertContext): string {
  const severity: Severity = item.severity;
  const label = SEVERITY_LABEL[severity] ?? severity;
  const emoji = SEVERITY_EMOJI[severity];
  const prefix = emoji ? `${emoji} ` : "";
  let header = `${prefix}*${label}* \u2014 *${slackEscape(ctx.partnerName)}*: ${slackEscape(item.headline)}`;
  const sourceUrl = vitallyConversationUrl(item.vitallyConversationId ?? "") ?? ctx.vitallyAccountUrl;
  if (sourceUrl) header += ` (<${sourceUrl}|source>)`;
  const lines = [header];
  const quote = item.evidence[0]?.quote;
  if (quote) lines.push(`> ${slackEscape(quote)}`);
  return lines.join("\n");
}

export interface PostedMessage {
  channel: string;
  ts: string;
}

/** `chat.postMessage` to one channel; returns the channel + message ts (the alert's identity for reactions). */
export async function postSlackMessage(
  text: string,
  channel = process.env.SLACK_ALERT_CHANNEL_AGENT,
  token = process.env.SLACK_BOT_TOKEN,
): Promise<PostedMessage> {
  if (!token || !channel) throw new Error("SLACK_BOT_TOKEN/SLACK_ALERT_CHANNEL_AGENT not set - see .env.example");
  const response = await fetch(`${SLACK_API_BASE}/chat.postMessage`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify({ channel, text, unfurl_links: false }),
    signal: AbortSignal.timeout(15_000),
  });
  const payload = (await response.json().catch(() => ({}))) as {
    ok?: boolean;
    error?: string;
    ts?: string;
    channel?: string;
  };
  if (!response.ok || !payload.ok || !payload.ts) {
    throw new Error(`Slack API error: ${payload.error ?? response.statusText}`);
  }
  return { channel: payload.channel ?? channel, ts: payload.ts };
}
