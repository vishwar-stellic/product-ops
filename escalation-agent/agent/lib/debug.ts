import type { Store } from "./store";
import { ALERT_PREFIX, PARTNER_STATE_PREFIX, type AlertRecord } from "./feedback";
import type { PartnerState } from "./sweep";

export interface DebugResult {
  conversationId: string;
  /** Alerts that were posted for an item linked to this conversation (newest first). */
  alerts: Array<Pick<AlertRecord, "partnerId" | "partnerName" | "channel" | "ts" | "postedAt" | "item" | "reactions">>;
  /** Partner states holding an item linked to this conversation, or raw emails from it. */
  partnerStates: Array<{
    key: string;
    lastMessageAt: string | null;
    checkedAt: string;
    items: PartnerState["items"];
    recentEmailsFromConversation: NonNullable<PartnerState["recentEmails"]>;
    recentEmailCount: number;
  }>;
  scanned: { alerts: number; partnerStates: number };
}

async function readAll<T>(store: Store, keys: string[]): Promise<Array<{ key: string; value: T }>> {
  const out: Array<{ key: string; value: T }> = [];
  const batchSize = 10;
  for (let i = 0; i < keys.length; i += batchSize) {
    const batch = await Promise.all(
      keys.slice(i, i + batchSize).map(async (key) => ({ key, value: await store.getJson<T>(key).catch(() => null) })),
    );
    for (const entry of batch) if (entry.value) out.push({ key: entry.key, value: entry.value });
  }
  return out;
}

/**
 * Read-only: everything the agent saved that relates to one Vitally conversation -
 * which alerts it caused (with the model's score, reason and evidence) and what the
 * partner's tracked state looked like. Used to answer "why was this flagged?".
 */
export async function debugConversation(store: Store, conversationId: string): Promise<DebugResult> {
  const alertKeys = await store.listKeys(ALERT_PREFIX, 1000);
  const alerts = await readAll<AlertRecord>(store, alertKeys);
  const stateKeys = await store.listKeys(PARTNER_STATE_PREFIX, 2000);
  const states = await readAll<PartnerState>(store, stateKeys);

  return {
    conversationId,
    alerts: alerts
      .map((a) => a.value)
      .filter((a) => a.item?.vitallyConversationId === conversationId)
      .sort((a, b) => (a.postedAt < b.postedAt ? 1 : -1))
      .map(({ partnerId, partnerName, channel, ts, postedAt, item, reactions }) => ({
        partnerId,
        partnerName,
        channel,
        ts,
        postedAt,
        item,
        reactions,
      })),
    partnerStates: states
      .filter(({ value }) => {
        const linked = (value.items ?? []).some((i) => i.vitallyConversationId === conversationId);
        const fromConversation = (value.recentEmails ?? []).some((e) => e.vitallyConversationId === conversationId);
        return linked || fromConversation;
      })
      .map(({ key, value }) => ({
        key,
        lastMessageAt: value.lastMessageAt,
        checkedAt: value.checkedAt,
        items: value.items ?? [],
        recentEmailsFromConversation: (value.recentEmails ?? []).filter((e) => e.vitallyConversationId === conversationId),
        recentEmailCount: (value.recentEmails ?? []).length,
      })),
    scanned: { alerts: alerts.length, partnerStates: states.length },
  };
}
