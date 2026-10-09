import { randomUUID } from "node:crypto";

import { normalizeSubject } from "./filters";
import type { LlmFn } from "./llm";
import { TRIAGE_PROMPT_TEMPLATE } from "./triage-rubric";
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
  /**
   * Stable identity, assigned by us (never by the model) and kept when the model updates an item in
   * place. Rewording a headline or re-linking the thread therefore cannot make an old item look new.
   */
  id?: string;
  headline: string;
  /** 0-5, per the rubric. Kept so feedback can show the agent's own score. */
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
  const id = str(raw.id, 64).trim();
  return {
    ...(id ? { id } : {}),
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

/** Each email is numbered ("Email #1", ...) so the model can refer to it in its per-email reviews. */
export function formatEmailsForPrompt(emails: SourceEmail[]): string {
  return emails
    .map((e, n) => `Email #${n + 1}\nFrom: ${e.from}\nDate: ${e.date}\nSubject: ${e.subject}\nBody:\n${e.body}`)
    .join("\n\n---\n\n");
}

/** The model's own read of one new email: what it says and how it scores under the rubric. */
export interface EmailReview {
  /** 1-based position of the email in the prompt. */
  email: number;
  score: number | null;
  summary: string;
  why: string;
}

/** One analyzed email with its review, for the per-sweep Slack thread. */
export interface ReviewedEmail {
  partnerName: string;
  from: string;
  subject: string;
  date: string;
  vitallyConversationId: string;
  /** null when the model gave no (valid) review for this email. */
  score: number | null;
  summary: string;
  why: string;
}

export function sanitizeEmailReviews(raw: unknown, emailCount: number): EmailReview[] {
  if (!Array.isArray(raw)) return [];
  const seen = new Set<number>();
  const out: EmailReview[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const r = entry as Record<string, unknown>;
    const email = Number(r.email);
    if (!Number.isInteger(email) || email < 1 || email > emailCount || seen.has(email)) continue;
    seen.add(email);
    const score = Number(r.score);
    out.push({
      email,
      score: Number.isInteger(score) && score >= 0 && score <= 5 ? score : null,
      summary: str(r.summary, 300).trim(),
      why: str(r.why, 300).trim(),
    });
  }
  return out;
}

/** Pairs every analyzed email with its review; an email the model skipped still appears, unscored. */
export function mergeEmailReviews(partnerName: string, emails: SourceEmail[], reviews: EmailReview[]): ReviewedEmail[] {
  const byEmail = new Map(reviews.map((r) => [r.email, r]));
  return emails.map((e, n) => {
    const review = byEmail.get(n + 1);
    return {
      partnerName,
      from: e.from,
      subject: e.subject,
      date: e.date,
      vitallyConversationId: e.vitallyConversationId,
      score: review?.score ?? null,
      summary: review?.summary ?? "",
      why: review?.why ?? "",
    };
  });
}

/** Appended to the shared rubric: kept separate from the rubric text itself. */
export const ITEM_IDENTITY_NOTE = `ITEM IDENTITY
Every previously tracked item above has an "id". When you update a tracked item in place, return it with the SAME "id", even if you reword its headline, change its score, or the newest email is in a different thread. Only an item that is genuinely new gets "id": null. Never reuse one item's id for a different issue.
Evidence: each item's "evidence" must be quotes the emails actually contain. Quote the new emails when they support the item; keep older quotes only for the same issue and the same partner. Never move quotes from one tracked item onto another.`;

/** Asks for a per-email score and reason alongside the tracked items (shown in the sweep's Slack thread). */
export const EMAIL_REVIEW_NOTE = `EMAIL REVIEWS
Each new email above is numbered ("Email #1", "Email #2", ...). In the SAME JSON object that holds "items", also return a top-level "emailReviews" array with one entry for EVERY new email, in this shape:
{"email": <the email's number>, "score": 0 | 1 | 2 | 3 | 4 | 5, "summary": "<what this email says, one short sentence, 140 characters max>", "why": "<why it got this score under the rubric - blast radius, environment, any dated event - one short sentence, 160 characters max>"}
Score each email by the consequence of the issue it raises, with the same rubric and rules as for items. An email that only continues an already-tracked issue is scored for that issue; logistics, FYIs and acknowledgements are 0-2. When an email is the evidence for an item, its score must match that item's score. Keep every entry short.`;

export function buildTriagePrompt(opts: {
  previousItems: TrackedItem[];
  newEmails: SourceEmail[];
  feedbackBlock: string;
}): string {
  // Function replacers so "$&"-style sequences in emails are never interpreted.
  return TRIAGE_PROMPT_TEMPLATE.replace("__FEEDBACK__", () => (opts.feedbackBlock ? `${opts.feedbackBlock}\n\n` : ""))
    .replace("__PREVIOUS_ITEMS__", () => JSON.stringify(opts.previousItems, null, 2))
    .replace("__NEW_EMAILS__", () => formatEmailsForPrompt(opts.newEmails))
    .concat(`\n\n${ITEM_IDENTITY_NOTE}\n\n${EMAIL_REVIEW_NOTE}\n`);
}

/** Gives every item an id; items that already have one are returned untouched (same object). */
export function assignItemIds(items: TrackedItem[]): TrackedItem[] {
  return items.map((item) => (item.id ? item : { ...item, id: randomUUID() }));
}

/**
 * After the model rewrote the list: an updated item keeps its prior id when the model echoed it
 * (or, failing that, when the headline is unchanged); everything else is new and gets a fresh id.
 * An id is never given to two items.
 */
export function reconcileItemIds(prior: TrackedItem[], updated: TrackedItem[]): TrackedItem[] {
  const priorIds = new Set(prior.map((p) => p.id).filter((id): id is string => !!id));
  const priorByHeadline = new Map(prior.filter((p) => p.id).map((p) => [p.headline, p.id as string]));
  const used = new Set<string>();
  return updated.map((item) => {
    let id: string | undefined;
    if (item.id && priorIds.has(item.id) && !used.has(item.id)) id = item.id;
    else {
      const byHeadline = priorByHeadline.get(item.headline);
      if (byHeadline && !used.has(byHeadline)) id = byHeadline;
    }
    id ??= randomUUID();
    used.add(id);
    return { ...item, id };
  });
}

// ---------------------------------------------------------------------------
// Is the evidence actually in the emails we were just given?
// ---------------------------------------------------------------------------

const normalizeText = (text: string): string =>
  text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();

/** Whether (most of) a quote appears in one of the emails; short quotes are too weak to count. */
export function quoteInEmails(quote: string, emails: SourceEmail[]): boolean {
  const q = normalizeText(quote);
  if (q.length < 12) return false;
  const head = q.slice(0, 60);
  return emails.some((e) => {
    const body = normalizeText(e.body);
    return body.includes(q) || body.includes(head);
  });
}

/**
 * True when at least one piece of the item's evidence comes from \`emails\` (the batch the model just
 * read): the quote is in an email body, or the evidence is dated at exactly one of the emails'
 * timestamps. An item with no evidence at all can't be shown stale, so it passes.
 *
 * This is what stops a tracked item from re-alerting because the model re-scored it on the strength
 * of quotes that were already days old.
 */
export function hasFreshEvidence(item: TrackedItem, emails: SourceEmail[]): boolean {
  if (item.evidence.length === 0) return true;
  const stamps = new Set(emails.map((e) => Date.parse(e.date)).filter((ms) => !Number.isNaN(ms)));
  return item.evidence.some((ev) => {
    const ms = Date.parse(ev.date);
    return (!Number.isNaN(ms) && stamps.has(ms)) || quoteInEmails(ev.quote, emails);
  });
}

export interface TriageResult {
  items: TrackedItem[];
  /** One per email the model reviewed (may be empty if it skipped them; items never depend on it). */
  reviews: EmailReview[];
}

/**
 * One LLM call producing the updated tracked-items list plus the per-email reviews; null on any
 * failure (bad response, timeout, malformed JSON) so one partner's flaky call never blocks the
 * rest of the batch. A missing or malformed "emailReviews" never fails the call.
 */
export async function triageEmails(
  llm: LlmFn,
  opts: { previousItems: TrackedItem[]; newEmails: SourceEmail[]; feedbackBlock: string },
): Promise<TriageResult | null> {
  try {
    const text = await llm(buildTriagePrompt(opts));
    const parsed = JSON.parse(extractJsonObject(text)) as { items?: unknown; emailReviews?: unknown };
    const items = (Array.isArray(parsed.items) ? parsed.items : [])
      .filter((i): i is Record<string, unknown> => !!i && typeof i === "object")
      .map(sanitizeItem)
      .filter((i): i is TrackedItem => i !== null);
    return { items, reviews: sanitizeEmailReviews(parsed.emailReviews, opts.newEmails.length) };
  } catch (error) {
    console.error(`[escalation-agent] LLM triage failed: ${String(error)}`);
    return null;
  }
}

/** The tracked items only; see `triageEmails` for the full result. */
export async function updateEscalations(
  llm: LlmFn,
  opts: { previousItems: TrackedItem[]; newEmails: SourceEmail[]; feedbackBlock: string },
): Promise<TrackedItem[] | null> {
  return (await triageEmails(llm, opts))?.items ?? null;
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

/**
 * Links each item to the conversation it came from. An item that already has a link keeps it
 * unless one of its own quotes actually appears in the newly matched conversation: the newest
 * email about a similar topic is not proof that the old evidence belongs to that thread (this is
 * how an old item from one thread got re-pointed at an unrelated agenda email).
 */
export function enrichItemsWithConversations(
  items: TrackedItem[],
  sourceEmails: SourceEmail[],
  priorItems: TrackedItem[],
): TrackedItem[] {
  const priorById = new Map(priorItems.filter((i) => i.id).map((i) => [i.id as string, i]));
  const priorByHeadline = new Map(priorItems.map((i) => [i.headline, i]));
  return items.map((item) => {
    const prior = (item.id ? priorById.get(item.id) : undefined) ?? priorByHeadline.get(item.headline);
    const priorLink = prior?.vitallyConversationId ?? null;
    let matched = matchConversationId(item, sourceEmails);
    if (matched && priorLink && matched !== priorLink) {
      const inMatched = sourceEmails.filter((e) => e.vitallyConversationId === matched);
      if (!item.evidence.some((ev) => quoteInEmails(ev.quote, inMatched))) matched = null;
    }
    const conversationId = matched ?? priorLink;
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
 * "Same item" is decided by id, headline OR Vitally conversation: the model
 * often rewords a headline when it updates an item in place, and an exact
 * headline match alone would then treat it as brand new and alert again.
 *
 * With `partnerId` + `history` (the alerts already posted), an item is also
 * suppressed when its conversation/headline already alerted at this severity or
 * higher, and when the team marked that alert a false alarm until it gets
 * strictly worse (see `shouldAlert`). With `newEmails`, an item whose evidence is all older than
 * this run's emails is never alerted (see `hasFreshEvidence`).
 */
export function notableSeverityChanges(
  prior: TrackedItem[],
  updated: TrackedItem[],
  opts: { partnerId?: string; history?: AlertHistory; newEmails?: SourceEmail[] } = {},
): TrackedItem[] {
  return updated.filter((item) => {
    if (!NOTABLE.has(item.severity)) return false;
    const rank = SEVERITY_RANK[item.severity];
    const before = prior.filter(
      (p) =>
        (!!item.id && p.id === item.id) ||
        p.headline === item.headline ||
        (item.vitallyConversationId !== null && p.vitallyConversationId === item.vitallyConversationId),
    );
    const beforeRank = before.reduce((max, p) => Math.max(max, SEVERITY_RANK[p.severity]), -1);
    if (rank <= beforeRank) return false;
    // Alerting is for something that just happened: at least one quote must come from the emails read this run.
    if (opts.newEmails && !hasFreshEvidence(item, opts.newEmails)) {
      console.log(`[escalation-agent] not alerting on ${JSON.stringify(item.headline)}: none of its evidence is from the new emails`);
      return false;
    }
    if (opts.history && opts.partnerId && !shouldAlert(item, opts.partnerId, opts.history)) return false;
    return true;
  });
}
