import { partnerStateKey } from "./feedback";
import type { Store } from "./store";
import type { PartnerState } from "./sweep";

/** What the dashboard's Partner Insights tab shows for one partner. */
export interface PartnerEscalationView {
  items: PartnerState["items"];
  /** When the agent last looked at this partner (ISO). */
  checkedAt: string | null;
  lastMessageAt: string | null;
  /** The raw emails the latest triage batch read. */
  recentEmails: NonNullable<PartnerState["recentEmails"]>;
}

/**
 * Read-only: the saved tracked items for the given partners, keyed by partner id. A partner the
 * agent has never processed (or has no saved state yet) maps to null, so the caller can tell
 * "nothing found" (empty items) from "never checked". Lets the dashboard read the agent's state
 * instead of running its own triage.
 */
export async function exportPartnerStates(
  store: Store,
  partnerIds: string[],
): Promise<Record<string, PartnerEscalationView | null>> {
  const out: Record<string, PartnerEscalationView | null> = {};
  const unique = [...new Set(partnerIds)];
  const batchSize = 10;
  for (let i = 0; i < unique.length; i += batchSize) {
    await Promise.all(
      unique.slice(i, i + batchSize).map(async (partnerId) => {
        const state = await store.getJson<PartnerState>(partnerStateKey(partnerId)).catch(() => null);
        out[partnerId] = state
          ? {
              items: state.items ?? [],
              checkedAt: state.checkedAt ?? null,
              lastMessageAt: state.lastMessageAt ?? null,
              recentEmails: state.recentEmails ?? [],
            }
          : null;
      }),
    );
  }
  return out;
}
