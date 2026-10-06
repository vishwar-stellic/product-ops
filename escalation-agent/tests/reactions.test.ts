import { describe, expect, it } from "vitest";
import { alertKey, type AlertRecord } from "../agent/lib/feedback";
import { handleReactionEvent, toReactionChange } from "../agent/lib/reactions";
import { sanitizeItem, type TrackedItem } from "../agent/lib/triage";
import { itemJson, memoryStore } from "./helpers";

const event = (over: Record<string, unknown> = {}) => ({
  type: "reaction_added",
  user: "U1",
  reaction: "+1",
  item: { type: "message", channel: "C1", ts: "100.1" },
  event_ts: "1700000000.000200",
  ...over,
});

describe("toReactionChange", () => {
  it("parses reaction_added and reaction_removed on messages", () => {
    expect(toReactionChange(event())).toEqual({
      channel: "C1",
      ts: "100.1",
      user: "U1",
      reaction: "+1",
      added: true,
      at: 1700000000.0002,
    });
    expect(toReactionChange(event({ type: "reaction_removed" }))?.added).toBe(false);
  });
  it("rejects other event types, files, and malformed payloads", () => {
    expect(toReactionChange({ type: "team_join" })).toBeNull();
    expect(toReactionChange(event({ item: { type: "file", file: "F1" } }))).toBeNull();
    expect(toReactionChange(event({ user: undefined }))).toBeNull();
    expect(toReactionChange(event({ item: { type: "message", channel: "C1" } }))).toBeNull();
  });
});

describe("handleReactionEvent", () => {
  it("applies a reaction to a tracked alert and ignores unrelated events", async () => {
    const store = memoryStore();
    const rec: AlertRecord = {
      partnerId: "p1",
      partnerName: "Acme U",
      channel: "C1",
      ts: "100.1",
      postedAt: "now",
      item: sanitizeItem(itemJson() as Record<string, unknown>) as TrackedItem,
      reactions: {},
    };
    await store.putJson(alertKey("C1", "100.1"), rec);
    expect(await handleReactionEvent(store, { type: "channel_created" })).toBeNull();
    const result = await handleReactionEvent(store, event({ reaction: "arrow_down" }));
    expect(result).toMatchObject({ status: "updated", verdict: { label: "too_severe" } });
  });
});
