import { FEEDBACK_MAX_ALERTS_SCANNED, FEEDBACK_MAX_EXAMPLES, FEEDBACK_TEXT_CLIP } from "./config";
import type { Store } from "./store";
import type { TrackedItem } from "./triage";

/**
 * Slack-reaction feedback loop.
 *
 * Every alert the agent posts gets an AlertRecord (keyed by the Slack message
 * it produced). Reactions on that message become verdicts; at the start of
 * each sweep the most recent verdicts are rendered as a bounded
 * "TEAM FEEDBACK CALIBRATION" block injected into the triage prompt. The
 * rubric text itself is never edited.
 */

export type VerdictLabel = "correct" | "false_alarm" | "too_severe" | "under_rated";

/** Slack reaction name -> verdict. Edit here to change the vocabulary. */
export const EMOJI_VERDICTS: Record<string, VerdictLabel> = {
  "+1": "correct",
  thumbsup: "correct",
  "-1": "false_alarm",
  thumbsdown: "false_alarm",
  arrow_down: "too_severe",
  arrow_down_small: "too_severe",
  small_red_triangle_down: "too_severe",
  arrow_up: "under_rated",
  arrow_up_small: "under_rated",
  small_red_triangle: "under_rated",
};

/** Slack sends skin-tone variants as "+1::skin-tone-3"; the verdict only depends on the base name. */
export function verdictForReaction(reaction: string): VerdictLabel | null {
  const base = reaction.split("::")[0] ?? reaction;
  return EMOJI_VERDICTS[base] ?? null;
}

export interface ReactionEntry {
  user: string;
  reaction: string;
  label: VerdictLabel;
  /** Epoch seconds of the Slack event. */
  at: number;
}

export interface AlertRecord {
  partnerId: string;
  partnerName: string;
  channel: string;
  ts: string;
  postedAt: string;
  item: TrackedItem;
  /** Keyed `${user}:${reaction}` so removing a reaction removes exactly that vote. */
  reactions: Record<string, ReactionEntry>;
}

export const alertKey = (channel: string, ts: string) => `${ALERT_PREFIX}${channel}/${ts}.json`;
export const PARTNER_STATE_PREFIX = "partners/";
export const ALERT_PREFIX = "alerts/";
/** Partner ids look like "intercom:6746..."; anything outside [A-Za-z0-9._-] becomes "_" so the key round-trips through Blob. */
export const partnerStateKey = (partnerId: string) =>
  `${PARTNER_STATE_PREFIX}${partnerId.replace(/[^A-Za-z0-9._-]/g, "_")}.json`;

/** The latest still-present reaction wins. */
export function currentVerdict(record: AlertRecord): ReactionEntry | null {
  const entries = Object.values(record.reactions ?? {});
  if (entries.length === 0) return null;
  return entries.reduce((latest, e) => (e.at >= latest.at ? e : latest));
}

export interface ReactionChange {
  channel: string;
  ts: string;
  user: string;
  reaction: string;
  added: boolean;
  /** Epoch seconds. */
  at: number;
}

export type ReactionResult =
  | { status: "ignored"; reason: "unknown_alert" | "unknown_emoji" }
  | { status: "updated"; record: AlertRecord; verdict: ReactionEntry | null };

/**
 * Applies one reaction_added / reaction_removed to the matching alert record.
 * Ignores reactions on messages that aren't tracked alerts and emojis that
 * aren't part of the vocabulary.
 */
export async function applyReaction(store: Store, change: ReactionChange): Promise<ReactionResult> {
  const label = verdictForReaction(change.reaction);
  if (!label) return { status: "ignored", reason: "unknown_emoji" };
  const key = alertKey(change.channel, change.ts);
  const record = await store.getJson<AlertRecord>(key);
  if (!record) return { status: "ignored", reason: "unknown_alert" };

  record.reactions ??= {};
  const entryKey = `${change.user}:${change.reaction.split("::")[0]}`;
  if (change.added) {
    record.reactions[entryKey] = { user: change.user, reaction: change.reaction, label, at: change.at };
  } else {
    delete record.reactions[entryKey];
  }
  await store.putJson(key, record);

  return { status: "updated", record, verdict: currentVerdict(record) };
}

// ---------------------------------------------------------------------------
// Building the calibration block
// ---------------------------------------------------------------------------

export interface FeedbackExample {
  record: AlertRecord;
  verdict: ReactionEntry;
}

const clip = (text: string, max = FEEDBACK_TEXT_CLIP) => {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}\u2026` : flat;
};

/** What the team's verdict implies the score should have been. */
export function correctedScoreText(label: VerdictLabel, agentScore: number): string {
  switch (label) {
    case "correct":
      return `score ${agentScore} was right`;
    case "false_alarm":
      return "true score 0-2 (not an escalation)";
    case "too_severe":
      return `true score ${agentScore >= 5 ? 4 : 3} (real, but less severe than ${agentScore})`;
    case "under_rated":
      return `true score ${Math.min(agentScore + 1, 5)} (should have been rated higher)`;
  }
}

const VERDICT_TEXT: Record<VerdictLabel, string> = {
  correct: "CORRECT - the severity was right",
  false_alarm: "FALSE ALARM - this should not have been flagged",
  too_severe: "TOO SEVERE - real, but over-scored",
  under_rated: "UNDER-RATED - should have scored higher",
};

/**
 * Newest-first, round-robin across labels so one frequent verdict can't crowd
 * out the rest.
 */
export function selectBalanced(examples: FeedbackExample[], max = FEEDBACK_MAX_EXAMPLES): FeedbackExample[] {
  const byLabel = new Map<VerdictLabel, FeedbackExample[]>();
  for (const ex of examples) {
    const list = byLabel.get(ex.verdict.label) ?? [];
    list.push(ex);
    byLabel.set(ex.verdict.label, list);
  }
  for (const list of byLabel.values()) list.sort((a, b) => b.verdict.at - a.verdict.at);
  const queues = [...byLabel.values()];
  const picked: FeedbackExample[] = [];
  while (picked.length < max && queues.some((q) => q.length > 0)) {
    for (const queue of queues) {
      const next = queue.shift();
      if (next && picked.length < max) picked.push(next);
    }
  }
  return picked.sort((a, b) => b.verdict.at - a.verdict.at);
}

export function renderFeedbackBlock(examples: FeedbackExample[]): string {
  if (examples.length === 0) return "";
  const lines = [
    "TEAM FEEDBACK CALIBRATION",
    "These are earlier alerts from this system that the team reviewed in Slack. Use them only to calibrate how",
    "severity is judged for similar situations. The SCORING RUBRIC above still governs: a new issue must meet",
    "its own criteria, and you must not copy an example's headline or score. Examples are newest first.",
    "",
  ];
  examples.forEach(({ record, verdict }, index) => {
    const item = record.item;
    const quote = item.evidence[0]?.quote;
    lines.push(
      `${index + 1}. The agent scored this ${item.score} (${item.severity}). Team verdict: ${VERDICT_TEXT[verdict.label]}; ${correctedScoreText(verdict.label, item.score)}.`,
      `   Headline: ${clip(item.headline)}`,
      `   Agent's reason: ${clip(item.severityReason)}`,
    );
    if (quote) lines.push(`   Evidence: "${clip(quote)}"`);
  });
  return lines.join("\n");
}

export interface FeedbackSnapshot {
  /** Text to inject into the prompt ("" when there is no usable feedback). */
  block: string;
  exampleCount: number;
}

/** Reads recent alert records once per sweep and derives everything the sweep needs from feedback. */
export async function loadFeedbackSnapshot(store: Store): Promise<FeedbackSnapshot> {
  const keys = await store.listKeys("alerts/", FEEDBACK_MAX_ALERTS_SCANNED);
  const records: AlertRecord[] = [];
  const batchSize = 10;
  for (let i = 0; i < keys.length; i += batchSize) {
    const batch = await Promise.all(
      keys.slice(i, i + batchSize).map((key) => store.getJson<AlertRecord>(key).catch(() => null)),
    );
    for (const record of batch) if (record) records.push(record);
  }
  const examples: FeedbackExample[] = [];
  for (const record of records) {
    const verdict = currentVerdict(record);
    if (!verdict) continue;
    examples.push({ record, verdict });
  }
  const selected = selectBalanced(examples);
  return { block: renderFeedbackBlock(selected), exampleCount: selected.length };
}
