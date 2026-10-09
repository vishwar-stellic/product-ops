import type { ReviewedEmail, Severity, TrackedItem } from "./triage";
import { vitallyConversationUrl } from "./vitally";

const SLACK_API_BASE = "https://slack.com/api";

const SEVERITY_LABEL: Record<string, string> = { LIVE_FIRE: "Live Fire", SMOLDERING: "Smoldering" };
// Fire for Live Fire, firecracker for Smoldering.
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

export interface SweepCounts {
  partners: number;
  withNewEmails: number;
  llmFailures: number;
  fetchFailures: number;
  alertFailures: number;
  /** Alerts posted this run, by severity. */
  liveFire: number;
  smoldering: number;
  /** Emails analyzed this run (their per-email review goes in the thread). */
  emailsAnalyzed: number;
}

/**
 * Posted at the end of EVERY sweep, so the channel always shows that a run happened and what it
 * found. Partners that couldn't be checked, and alerts that failed to post, are called out.
 */
export function formatSweepCompleteMessage(counts: SweepCounts): string {
  const alerts = counts.liveFire + counts.smoldering;
  const found =
    alerts === 0
      ? "no new Live Fire or Smoldering escalations."
      : `${alerts} new escalation${alerts === 1 ? "" : "s"} posted above (${counts.liveFire} Live Fire, ${counts.smoldering} Smoldering).`;
  const checked = `Checked ${counts.partners} partners, ${counts.withNewEmails} with new email.`;
  const notes: string[] = [];
  const unchecked = counts.llmFailures + counts.fetchFailures;
  if (unchecked > 0) notes.push(`:warning: ${unchecked} could not be checked this run and will be retried.`);
  if (counts.alertFailures > 0) notes.push(`:warning: ${counts.alertFailures} alert${counts.alertFailures === 1 ? "" : "s"} failed to post (see logs).`);
  if (counts.emailsAnalyzed > 0) {
    notes.push(`${counts.emailsAnalyzed} email${counts.emailsAnalyzed === 1 ? "" : "s"} analyzed - scores in the thread.`);
  }
  return [`:white_check_mark: Sweep complete: ${found} ${checked}`, ...notes].join(" ");
}

const PACIFIC = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

function formatPacific(iso: string): string {
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? iso : `${PACIFIC.format(new Date(ms))} PT`;
}

/** Slack link text can't contain these without breaking the link. */
const linkText = (text: string): string => slackEscape(text).replace(/\|/g, "/");

const THREAD_CHUNK_CHARS = 3500;
const THREAD_MAX_CHUNKS = 12;

function formatReviewedEmail(e: ReviewedEmail): string {
  const url = vitallyConversationUrl(e.vitallyConversationId);
  const subject = e.subject || "(no subject)";
  const title = url ? `<${url}|${linkText(subject)}>` : `*${slackEscape(subject)}*`;
  const score = e.score === null ? "?" : String(e.score);
  const lines = [`\u2022 *${score}* ${title} \u2014 ${slackEscape(e.from)}, ${formatPacific(e.date)}`];
  if (e.summary) lines.push(`    ${slackEscape(e.summary)}`);
  if (e.score === null) lines.push("    _Not scored: the model returned no review for this email._");
  else if (e.why) lines.push(`    _Why:_ ${slackEscape(e.why)}`);
  return lines.join("\n");
}

/**
 * The thread under the sweep-complete message: every email analyzed this run with its score and the
 * model's reason, grouped by partner (highest-scoring partner first). Split into several replies when
 * it would be too long to read; whatever doesn't fit in THREAD_MAX_CHUNKS is summarized as a count.
 */
export function formatEmailReviewThread(reviews: ReviewedEmail[]): string[] {
  if (reviews.length === 0) return [];
  const byPartner = new Map<string, ReviewedEmail[]>();
  for (const r of reviews) byPartner.set(r.partnerName, [...(byPartner.get(r.partnerName) ?? []), r]);
  const rank = (e: ReviewedEmail): number => e.score ?? -1;
  const sections = [...byPartner.entries()]
    .map(([partner, emails]) => ({
      partner,
      top: Math.max(...emails.map(rank)),
      emails: emails.slice().sort((a, b) => rank(b) - rank(a) || Date.parse(b.date) - Date.parse(a.date)),
    }))
    .sort((a, b) => b.top - a.top || a.partner.localeCompare(b.partner));

  const blocks: string[] = [
    `*Email-by-email review* \u2014 ${reviews.length} email${reviews.length === 1 ? "" : "s"} across ${sections.length} partner${sections.length === 1 ? "" : "s"}, highest score first. Scores use the same 0-5 rubric (4-5 are fires).`,
  ];
  for (const s of sections) {
    blocks.push(`*${slackEscape(s.partner)}*\n${s.emails.map(formatReviewedEmail).join("\n")}`);
  }

  const chunks: string[] = [];
  let current = "";
  let consumed = 0;
  for (const block of blocks) {
    const pieces = block.length > THREAD_CHUNK_CHARS ? block.split("\n") : [block];
    for (const piece of pieces) {
      if (current && current.length + piece.length + 2 > THREAD_CHUNK_CHARS) {
        chunks.push(current);
        current = "";
      }
      current = current ? `${current}\n${piece}` : piece;
    }
    consumed += 1;
    if (chunks.length >= THREAD_MAX_CHUNKS) break;
  }
  if (current && chunks.length < THREAD_MAX_CHUNKS) chunks.push(current);
  const omittedPartners = blocks.length - consumed;
  if (omittedPartners > 0) chunks.push(`_...and ${omittedPartners} more partner${omittedPartners === 1 ? "" : "s"} not shown._`);
  return chunks;
}

export interface AlertContext {
  partnerName: string;
  vitallyAccountUrl: string | null;
}

/**
 * Slack mrkdwn for ONE newly-notable item.
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
  /** Post as a reply in the thread of the message with this ts. */
  threadTs?: string,
): Promise<PostedMessage> {
  if (!token || !channel) throw new Error("SLACK_BOT_TOKEN/SLACK_ALERT_CHANNEL_AGENT not set - see .env.example");
  const response = await fetch(`${SLACK_API_BASE}/chat.postMessage`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify({ channel, text, unfurl_links: false, ...(threadTs ? { thread_ts: threadTs } : {}) }),
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
