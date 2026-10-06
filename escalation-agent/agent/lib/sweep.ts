import { ESCALATION_LOOKBACK_DAYS, PARTNER_CONCURRENCY, RECENT_EMAILS_MAX } from "./config";
import {
  alertKey,
  loadFeedbackSnapshot,
  partnerStateKey,
  type AlertRecord,
  type FeedbackSnapshot,
} from "./feedback";
import type { LlmFn } from "./llm";
import type { TriagePartner } from "./registry";
import { formatSlackMessage, REACTION_LEGEND, type PostedMessage } from "./slack";
import type { Store } from "./store";
import {
  enrichItemsWithConversations,
  notableSeverityChanges,
  updateEscalations,
  type TrackedItem,
} from "./triage";
import { collectNewHumanEmails, vitallyAccountUrl, type SourceEmail, type VitallyApi } from "./vitally";

/** Persisted per partner at `partners/<partnerId>.json`. */
export interface PartnerState {
  items: TrackedItem[];
  /** Newest message already incorporated (ISO). */
  lastMessageAt: string | null;
  checkedAt: string;
  /** The raw emails the latest LLM batch looked at. */
  recentEmails?: SourceEmail[];
}

export interface SweepDeps {
  store: Store;
  vitally: VitallyApi;
  llm: LlmFn;
  /** Posts one Slack message; omitted in dry-run mode. */
  post: ((text: string) => Promise<PostedMessage>) | null;
  registry: TriagePartner[];
  now?: Date;
}

export interface SweepSummary {
  partners: number;
  withNewEmails: number;
  llmFailures: number;
  fetchFailures: number;
  alertsPosted: number;
  alertFailures: number;
  feedbackExamples: number;
}

const latestIso = (a: string | null | undefined, b: string): string => {
  if (!a) return b;
  return Date.parse(a) >= Date.parse(b) ? a : b;
};

async function mapWithConcurrency<T>(items: T[], limit: number, fn: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const index = next++;
      await fn(items[index] as T);
    }
  });
  await Promise.all(workers);
}

/**
 * One triage pass over every partner: fetch new partner email, ask the model
 * to update the tracked items (with the team's feedback calibration injected),
 * persist, and post one Slack message per item that newly reached Live Fire /
 * Smoldering. State is saved per partner and BEFORE that partner's alerts are
 * posted (matching the Python job), so a timeout midway never re-reads or
 * double-alerts the partners already done.
 */
export async function runSweep(deps: SweepDeps): Promise<SweepSummary> {
  const now = deps.now ?? new Date();
  const nowIso = now.toISOString();
  const lookbackCutoffIso = new Date(now.getTime() - ESCALATION_LOOKBACK_DAYS * 86_400_000).toISOString();

  let feedback: FeedbackSnapshot = { block: "", exampleCount: 0 };
  try {
    feedback = await loadFeedbackSnapshot(deps.store);
  } catch (error) {
    // Feedback is an enhancement; never let it block the triage itself.
    console.error(`[escalation-agent] failed to load feedback, continuing without it: ${String(error)}`);
  }

  const summary: SweepSummary = {
    partners: deps.registry.length,
    withNewEmails: 0,
    llmFailures: 0,
    fetchFailures: 0,
    alertsPosted: 0,
    alertFailures: 0,
    feedbackExamples: feedback.exampleCount,
  };

  let dryRunAlerts = 0;

  async function processPartner(partner: TriagePartner): Promise<void> {
    const stateKey = partnerStateKey(partner.partnerId);
    const prior = (await deps.store.getJson<PartnerState>(stateKey)) ?? {
      items: [],
      lastMessageAt: null,
      checkedAt: nowIso,
    };
    const priorItems = prior.items ?? [];
    // Never reach back further than the lookback window, never re-fetch what's already incorporated.
    const sinceIso = latestIso(prior.lastMessageAt, lookbackCutoffIso);

    let newEmails: SourceEmail[];
    try {
      newEmails = await collectNewHumanEmails(deps.vitally, partner.vitallyAccountId, sinceIso);
    } catch (error) {
      summary.fetchFailures += 1;
      console.error(`[escalation-agent] fetch failed for ${partner.name}: ${String(error)}`);
      return;
    }

    if (newEmails.length === 0) {
      let items = priorItems;
      if (items.some((i) => !i.vitallyConversationId)) {
        const backfill = await collectNewHumanEmails(deps.vitally, partner.vitallyAccountId, lookbackCutoffIso);
        items = enrichItemsWithConversations(items, backfill, items);
      }
      // Only write when something actually changed, to keep blob writes down.
      if (items.length !== (prior.items ?? []).length || items.some((i, n) => i !== (prior.items ?? [])[n])) {
        await deps.store.putJson(stateKey, { ...prior, items, checkedAt: nowIso });
      }
      return;
    }

    summary.withNewEmails += 1;
    const updated = await updateEscalations(deps.llm, {
      previousItems: priorItems,
      newEmails,
      feedbackBlock: feedback.block,
    });
    if (updated === null) {
      // Keep the prior items and DON'T advance lastMessageAt, so these emails are retried next run.
      summary.llmFailures += 1;
      return;
    }

    let items = enrichItemsWithConversations(updated, newEmails, priorItems);
    if (items.some((i) => !i.vitallyConversationId)) {
      const backfill = await collectNewHumanEmails(deps.vitally, partner.vitallyAccountId, lookbackCutoffIso);
      items = enrichItemsWithConversations(items, backfill, items);
    }

    const newestSeen = newEmails.reduce((max, e) => latestIso(max, e.date), newEmails[0]?.date as string);
    const payload: PartnerState = {
      items,
      lastMessageAt: newestSeen,
      checkedAt: nowIso,
      recentEmails: newEmails.slice(-RECENT_EMAILS_MAX),
    };
    await deps.store.putJson(stateKey, payload);

    const notable = notableSeverityChanges(priorItems, items);
    let postedForPartner = 0;
    let failedForPartner = 0;
    for (const item of notable) {
      const text = formatSlackMessage(item, {
        partnerName: partner.name,
        vitallyAccountUrl: vitallyAccountUrl(partner.vitallyAccountId),
      });
      if (!deps.post) {
        console.log(`[escalation-agent] (dry run) would post:\n${text}`);
        dryRunAlerts += 1;
        continue;
      }
      try {
        const posted = await deps.post(text);
        const record: AlertRecord = {
          partnerId: partner.partnerId,
          partnerName: partner.name,
          channel: posted.channel,
          ts: posted.ts,
          postedAt: nowIso,
          item,
          reactions: {},
        };
        await deps.store.putJson(alertKey(posted.channel, posted.ts), record);
        summary.alertsPosted += 1;
        postedForPartner += 1;
      } catch (error) {
        summary.alertFailures += 1;
        failedForPartner += 1;
        console.error(`[escalation-agent] Slack alert failed for ${JSON.stringify(item.headline)}: ${String(error)}`);
      }
    }

    // Nothing reached Slack for this partner (e.g. bad token, bot not in the channel):
    // put the previous state back so the same emails are re-read and the alerts retried
    // next run instead of being silently lost. (If some alerts did post, keep the new
    // state - retrying would duplicate them.)
    if (failedForPartner > 0 && postedForPartner === 0) {
      await deps.store.putJson(stateKey, prior);
    }
  }

  await mapWithConcurrency(deps.registry, PARTNER_CONCURRENCY, async (partner) => {
    try {
      await processPartner(partner);
    } catch (error) {
      // One partner's unexpected error must not stop the rest of the batch.
      summary.fetchFailures += 1;
      console.error(`[escalation-agent] unexpected error for ${partner.name}: ${String(error)}`);
    }
  });

  // One reaction legend per sweep, after the alerts, instead of a footer on every message.
  if (summary.alertsPosted > 0 && deps.post) {
    try {
      await deps.post(REACTION_LEGEND);
    } catch (error) {
      console.error(`[escalation-agent] failed to post the reaction legend: ${String(error)}`);
    }
  } else if (dryRunAlerts > 0) {
    console.log(`[escalation-agent] (dry run) would post:\n${REACTION_LEGEND}`);
  }

  return summary;
}
