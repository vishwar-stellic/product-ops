import { applyReaction, type ReactionChange, type ReactionResult } from "./feedback";
import type { Store } from "./store";

/**
 * Slack `reaction_added` / `reaction_removed` payload (Events API). Only the
 * fields this agent reads.
 */
export interface SlackReactionEvent {
  type: string;
  user?: string;
  reaction?: string;
  item?: { type?: string; channel?: string; ts?: string };
  event_ts?: string;
  [key: string]: unknown;
}

/** Turns a raw Slack event into a ReactionChange, or null when it isn't a reaction on a message. */
export function toReactionChange(event: { type: string; [key: string]: unknown }): ReactionChange | null {
  if (event.type !== "reaction_added" && event.type !== "reaction_removed") return null;
  const e = event as SlackReactionEvent;
  if (e.item?.type !== "message" || !e.item.channel || !e.item.ts || !e.user || !e.reaction) return null;
  const at = Number.parseFloat(e.event_ts ?? "");
  return {
    channel: e.item.channel,
    ts: e.item.ts,
    user: e.user,
    reaction: e.reaction,
    added: e.type === "reaction_added",
    at: Number.isFinite(at) ? at : Date.now() / 1000,
  };
}

export async function handleReactionEvent(
  store: Store,
  event: { type: string; [key: string]: unknown },
): Promise<ReactionResult | null> {
  const change = toReactionChange(event);
  if (!change) return null;
  return applyReaction(store, change);
}
