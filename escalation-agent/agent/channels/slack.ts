import { slackChannel } from "eve/channels/slack";

import { handleReactionEvent } from "../lib/reactions";
import { getStore } from "../lib/store";

/**
 * Slack channel used ONLY to receive emoji reactions on the escalation alerts
 * (credentials come from SLACK_BOT_TOKEN / SLACK_SIGNING_SECRET). Every
 * conversational entry point is closed: this agent never answers mentions,
 * DMs, or thread replies, so the only thing that can change its behavior from
 * Slack is a reaction on one of its own alert messages.
 *
 * Slack app setup: Event Subscriptions -> Request URL
 * https://<deployment>/eve/v1/slack, subscribe to `reaction_added` and
 * `reaction_removed`; bot scopes `chat:write`, `reactions:read`, plus
 * `channels:history` (or `groups:history` for a private channel).
 */
export default slackChannel({
  onAppMention: () => null,
  onDirectMessage: () => null,
  onMessage: () => null,
  async onEvent(_ctx, event) {
    try {
      const result = await handleReactionEvent(getStore(), event);
      if (result?.status === "updated") {
        console.log(
          `[escalation-agent] reaction ${event.type} on ${result.record.item.headline.slice(0, 80)} -> ${result.verdict?.label ?? "no verdict"}`,
        );
      }
    } catch (error) {
      // Never let a feedback hiccup surface as a failed webhook.
      console.error(`[escalation-agent] failed to record reaction: ${String(error)}`);
    }
  },
});
