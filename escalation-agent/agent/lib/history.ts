import type { AlertRecord } from "./feedback";
import { currentVerdict } from "./feedback";
import type { Severity, TrackedItem } from "./triage";

/** WATCH < SMOLDERING < LIVE_FIRE. */
export const SEVERITY_RANK: Record<Severity, number> = { WATCH: 0, SMOLDERING: 1, LIVE_FIRE: 2 };

export interface AlertHistoryEntry {
  /** Highest severity rank already alerted for this thread/issue. */
  maxRank: number;
  /** Highest rank the team marked a false alarm (-1 when none). Muted until the item gets strictly worse. */
  mutedRank: number;
}

/** Alerts already posted, keyed per partner by conversation and by headline. */
export type AlertHistory = Map<string, AlertHistoryEntry>;

/** Keys one item is filed under: its stable id, its Vitally conversation (when linked) and its headline. */
export function historyKeys(
  partnerId: string,
  item: Pick<TrackedItem, "vitallyConversationId" | "headline"> & { id?: string },
): string[] {
  const keys = [`${partnerId}|h:${item.headline}`];
  if (item.id) keys.push(`${partnerId}|i:${item.id}`);
  if (item.vitallyConversationId) keys.push(`${partnerId}|c:${item.vitallyConversationId}`);
  return keys;
}

export function buildAlertHistory(records: AlertRecord[]): AlertHistory {
  const history: AlertHistory = new Map();
  for (const record of records) {
    if (!record.item || !record.partnerId) continue;
    const rank = SEVERITY_RANK[record.item.severity] ?? 0;
    const falseAlarm = currentVerdict(record)?.label === "false_alarm";
    for (const key of historyKeys(record.partnerId, record.item)) {
      const entry = history.get(key) ?? { maxRank: -1, mutedRank: -1 };
      entry.maxRank = Math.max(entry.maxRank, rank);
      if (falseAlarm) entry.mutedRank = Math.max(entry.mutedRank, rank);
      history.set(key, entry);
    }
  }
  return history;
}

/**
 * Whether an item that is notable this run should actually post.
 *  - Never alerted before (by conversation or headline): yes.
 *  - Already alerted at this severity or higher: no (a reworded headline can't re-alert).
 *  - The team marked an earlier alert a false alarm: stay muted until the item is
 *    strictly worse than the severity that was flagged.
 */
export function shouldAlert(item: TrackedItem, partnerId: string, history: AlertHistory): boolean {
  const rank = SEVERITY_RANK[item.severity] ?? 0;
  let maxRank = -1;
  let mutedRank = -1;
  for (const key of historyKeys(partnerId, item)) {
    const entry = history.get(key);
    if (!entry) continue;
    maxRank = Math.max(maxRank, entry.maxRank);
    mutedRank = Math.max(mutedRank, entry.mutedRank);
  }
  if (mutedRank >= 0 && rank <= mutedRank) return false;
  return rank > maxRank;
}
