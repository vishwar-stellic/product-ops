import { stripHtml } from "./filters";
import { partnerStateKey } from "./feedback";
import type { TriagePartner } from "./registry";
import type { Store } from "./store";
import type { PartnerState } from "./sweep";
import { quoteInEmails } from "./triage";
import { conversationBelongsToAccount, type VitallyApi } from "./vitally";

export interface PurgeResult {
  conversationId: string;
  subject: string;
  dryRun: boolean;
  /** Accounts the conversation is attributed to (the thread starter's). */
  ownerPartners: string[];
  /** Partners whose saved state was cleaned (or would be, in a dry run). */
  cleaned: Array<{
    partnerId: string;
    partnerName: string;
    removedItems: Array<{ id?: string; headline: string; score: number; reason: "linked" | "evidence" }>;
    removedRecentEmails: number;
  }>;
}

/**
 * Removes saved tracked items that came from a Vitally conversation that does NOT belong to the
 * partner (see `conversationBelongsToAccount`). Before the starter-account fix, one thread shared
 * by several accounts was ingested by all of them; those items are still sitting in each partner's
 * state and can re-alert when the partner's own email touches the same topic.
 *
 * An item is removed from a foreign partner when it is linked to the conversation OR one of its
 * evidence quotes appears in the conversation (the model may have re-linked it to a newer thread
 * while keeping the old quotes). The partner that owns the conversation is never touched, and
 * alert records are kept so the same thread can't alert twice. `dryRun` reports without writing.
 */
export async function purgeForeignConversation(
  store: Store,
  vitally: VitallyApi,
  registry: TriagePartner[],
  conversationId: string,
  opts: { dryRun?: boolean } = {},
): Promise<PurgeResult> {
  const dryRun = opts.dryRun === true;
  const conversation = await vitally.getConversation(conversationId);
  const accountIds = (conversation.accounts ?? []).map((a) => a.id).filter((id): id is string => !!id);
  if (accountIds.length === 0) throw new Error("conversation has no accounts; refusing to guess an owner");

  const asEmails = (conversation.messages ?? [])
    .map((m) => ({
      from: "",
      subject: conversation.subject ?? "",
      date: m.timestamp || m.createdAt || "",
      body: stripHtml(m.message),
      vitallyConversationId: conversationId,
    }))
    .filter((e) => e.body);

  const ownerPartners: string[] = [];
  const cleaned: PurgeResult["cleaned"] = [];

  for (const partner of registry) {
    const owns = accountIds.includes(partner.vitallyAccountId) && conversationBelongsToAccount(conversation, partner.vitallyAccountId);
    if (owns) {
      ownerPartners.push(partner.name);
      continue;
    }
    const key = partnerStateKey(partner.partnerId);
    const state = await store.getJson<PartnerState>(key);
    if (!state) continue;

    const removedItems: PurgeResult["cleaned"][number]["removedItems"] = [];
    const keptItems = (state.items ?? []).filter((item) => {
      const linked = item.vitallyConversationId === conversationId;
      const evidenced = item.evidence.some((ev) => quoteInEmails(ev.quote, asEmails));
      if (!linked && !evidenced) return true;
      removedItems.push({
        ...(item.id ? { id: item.id } : {}),
        headline: item.headline,
        score: item.score,
        reason: linked ? "linked" : "evidence",
      });
      return false;
    });
    const keptEmails = (state.recentEmails ?? []).filter((e) => e.vitallyConversationId !== conversationId);
    const removedRecentEmails = (state.recentEmails ?? []).length - keptEmails.length;
    if (removedItems.length === 0 && removedRecentEmails === 0) continue;

    cleaned.push({ partnerId: partner.partnerId, partnerName: partner.name, removedItems, removedRecentEmails });
    if (!dryRun) await store.putJson(key, { ...state, items: keptItems, recentEmails: keptEmails });
  }

  return { conversationId, subject: conversation.subject ?? "", dryRun, ownerPartners, cleaned };
}
