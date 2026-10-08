import { normalizeSubject } from "./filters";
import type { LlmFn } from "./llm";
import { TRIAGE_PROMPT_TEMPLATE } from "./triage-rubric.generated";
import { type AlertHistory, SEVERITY_RANK, shouldAlert } from "./history";
import type { SourceEmail } from "./vitally";

export type Severity = "LIVE_FIRE" | "SMOLDERING" | "WATCH";
export type BlockedOn = "us" | "them" | "unclear";

export interface Evidence {
  quote: string;
  sender: string;
  date: string;
}

export interface TrackedItem {
  headline: string;
  /** 0-5, per the rubric. Kept (unlike the Python state) so feedback can show the agent's own score. */
  score: number;
  severity: Severity;
  severityReason: string;
  evidence: Evidence[];
  blockedOn: BlockedOn;
  blockedOnReason: string;
  lastMovementAt: string | null;
  from: string;
  subject: string;
  lastEmailDate: string | null;
  vitallyConversationId: string | null;
}

const VALID_SEVERITIES = new Set<Severity>(["LIVE_FIRE", "SMOLDERING", "WATCH"]);
const VALID_BLOCKED_ON = new Set<BlockedOn>(["us", "them", "unclear"]);
const SEVERITY_DEFAULT_SCORE: Record<Severity, number> = { LIVE_FIRE: 5, SMOLDERING: 4, WATCH: 3 };

export function extractJsonObject(text: string): string {
  const match = text.match(/\{[\s\S]*\}/);
  return match ? match[0] : text;
}

const str = (value: unknown, max: number): string => String(value ?? "").slice(0, max);

/** Validates one raw LLM item; returns null when it has no headline. */
export function sanitizeItem(raw: Record<string, unknown>): TrackedItem | null {
  const headline = String(raw.headline ?? "").trim();
  if (!headline) return null;
  const severityText = String(raw.severity ?? "").toUpperCase().replace(/ /g, "_") as Severity;
  const severity: Severity = VALID_SEVERITIES.has(severityText) ? severityText : "WATCH";
  const blockedText = String(raw.blockedOn ?? "unclear").toLowerCase() as BlockedOn;
  const blockedOn: BlockedOn = VALID_BLOCKED_ON.has(blockedText) ? blockedText : "unclear";
  const rawScore = Number(raw.score);
  // Severity is derived strictly from score per the rubric; if the score is
  // missing or out of range, fall back to the severity's canonical score.
  const score =
    Number.isInteger(rawScore) && rawScore >= 0 && rawScore <= 5 ? rawScore : SEVERITY_DEFAULT_SCORE[severity];
  const evidence: Evidence[] = (Array.isArray(raw.evidence) ? raw.evidence : [])
    .filter((e): e is Record<string, unknown> => !!e && typeof e === "object" && !!(e as any).quote)
    .map((e) => ({ quote: str(e.quote, 300), sender: str(e.sender, 200), date: str(e.date, 64) }))
    .slice(0, 2);
  return {
    headline: headline.slice(0, 300),
    score,
    severity,
    severityReason: str(raw.severityReason, 400),
    evidence,
    blockedOn,
    blockedOnReason: str(raw.blockedOnReason, 400),
    lastMovementAt: str(raw.lastMovementAt, 64) || null,
    from: str(raw.from, 200),
    subject: str(raw.subject, 300),
    lastEmailDate: str(raw.lastEmailDate, 64) || null,
    vitallyConversationId: str(raw.vitallyConversationId, 100) || null,
  };
}

export function formatEmailsForPrompt(emails: SourceEmail[]): string {
  return emails
    .map((e) => `From: ${e.from}\nDate: ${e.date}\nSubject: ${e.subject}\nBody:\n${e.body}`)
    .join("\n\n---\n\n");
}

export function buildTriagePrompt(opts: {
  previousItems: TrackedItem[];
  newEmails: SourceEmail[];
  feedbackBlock: string;
}): string {
  // Function replacers so "$&"-style sequences in emails are never interpreted.
  return TRIAGE_PROMPT_TEMPLATE.replace("__FEEDBACK__", () => (opts.feedbackBlock ? `${opts.feedbackBlock}\n\n` : ""))
    .replace("__PREVIOUS_ITEMS__", () => JSON.stringify(opts.previousItems, null, 2))
    .replace("__NEW_EMAILS__", () => formatEmailsForPrompt(opts.newEmails));
}

/**
 * One LLM call producing the updated tracked-items list; null on any failure
 * (bad response, timeout, malformed JSON) so one partner's flaky call never
 * blocks the rest of the batch.
 */
export async function updateEscalations(
  llm: LlmFn,
  opts: { previousItems: TrackedItem[]; newEmails: SourceEmail[]; feedbackBlock: string },
): Promise<TrackedItem[] | null> {
  try {
    const text = await llm(buildTriagePrompt(opts));
    const parsed = JSON.parse(extractJsonObject(text)) as { items?: unknown };
    const items = (Array.isArray(parsed.items) ? parsed.items : [])
      .filter((i): i is Record<string, unknown> => !!i && typeof i === "object")
      .map(sanitizeItem)
      .filter((i): i is TrackedItem => i !== null);
    return items;
  } catch (error) {
    console.error(`[escalation-agent] LLM triage failed: ${String(error)}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Linking items back to the Vitally conversation they came from
// ---------------------------------------------------------------------------

export function matchConversationId(item: TrackedItem, sourceEmails: SourceEmail[]): string | null {
  if (sourceEmails.length === 0) return null;
  const subjectKey = normalizeSubject(item.subject.trim());
  const lastDate = item.lastEmailDate || item.lastMovementAt;
  const sender = item.from.trim();

  const bySubject = subjectKey ? sourceEmails.filter((e) => normalizeSubject(e.subject) === subjectKey) : [];
  const subjectMatched = bySubject.length > 0;
  const candidates = subjectMatched ? bySubject : sourceEmails;

  if (lastDate) {
    // Without a subject match, require the exact timestamp AND sender to agree.
    const hit = candidates.find(
      (e) => e.date === lastDate && e.vitallyConversationId && (subjectMatched || (!!sender && e.from === sender)),
    );
    if (hit) return hit.vitallyConversationId;
  }
  // Everything below is only safe when the subject already narrowed it to this thread. Without a
  // subject match, a sender / "only email" / "newest email" guess links the item to an UNRELATED
  // conversation (an old item would inherit whatever thread happened to be newest) - so give up
  // and let the caller keep the item's existing link, or none.
  if (!subjectMatched) return null;
  if (sender) {
    const hit = candidates.find((e) => e.from === sender && e.vitallyConversationId);
    if (hit) return hit.vitallyConversationId;
  }
  if (candidates.length === 1 && candidates[0]?.vitallyConversationId) return candidates[0].vitallyConversationId;
  const dated = candidates.filter((e) => e.vitallyConversationId);
  if (dated.length > 0) {
    return dated.reduce((a, b) => (Date.parse(b.date) > Date.parse(a.date) ? b : a)).vitallyConversationId;
  }
  return null;
}

export function enrichItemsWithConversations(
  items: TrackedItem[],
  sourceEmails: SourceEmail[],
  priorItems: TrackedItem[],
): TrackedItem[] {
  const priorByHeadline = new Map(priorItems.map((i) => [i.headline, i]));
  return items.map((item) => {
    const conversationId =
      matchConversationId(item, sourceEmails) ?? priorByHeadline.get(item.headline)?.vitallyConversationId ?? null;
    return conversationId ? { ...item, vitallyConversationId: conversationId } : item;
  });
}

// ---------------------------------------------------------------------------
// Which items should alert
// ---------------------------------------------------------------------------

const NOTABLE: ReadonlySet<Severity> = new Set(["LIVE_FIRE", "SMOLDERING"]);

/**
 * Items that just became LIVE_FIRE/SMOLDERING this run: brand new or escalated
 * to a HIGHER severity than before. An item that stays at, or drops to, the
 * same or a lower severity never re-alerts.
 *
 * "Same item" is decided by headline OR by Vitally conversation: the model
 * often rewords a headline when it updates an item in place, and an exact
 * headline match alone would then treat it as brand new and alert again.
 *
 * With `partnerId` + `history` (the alerts already posted), an item is also
 * suppressed when its conversation/headline already alerted at this severity or
 * higher, and when the team marked that alert a false alarm until it gets
 * strictly worse (see `shouldAlert`).
 */
export function notableSeverityChanges(
  prior: TrackedItem[],
  updated: TrackedItem[],
  opts: { partnerId?: string; history?: AlertHistory } = {},
): TrackedItem[] {
  return updated.filter((item) => {
    if (!NOTABLE.has(item.severity)) return false;
    const rank = SEVERITY_RANK[item.severity];
    const before = prior.filter(
      (p) =>
        p.headline === item.headline ||
        (item.vitallyConversationId !== null && p.vitallyConversationId === item.vitallyConversationId),
    );
    const beforeRank = before.reduce((max, p) => Math.max(max, SEVERITY_RANK[p.severity]), -1);
    if (rank <= beforeRank) return false;
    if (opts.history && opts.partnerId && !shouldAlert(item, opts.partnerId, opts.history)) return false;
    return true;
  });
}
