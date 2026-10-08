import { describe, expect, it } from "vitest";
import type { AlertRecord } from "../agent/lib/feedback";
import { buildAlertHistory, shouldAlert } from "../agent/lib/history";
import { notableSeverityChanges, type TrackedItem } from "../agent/lib/triage";

const item = (over: Partial<TrackedItem> = {}): TrackedItem => ({
  headline: "Day Two meetings", score: 4, severity: "SMOLDERING", severityReason: "r", evidence: [], blockedOn: "us",
  blockedOnReason: "", lastMovementAt: null, from: "x", subject: "s", lastEmailDate: null, vitallyConversationId: "conv-1", ...over,
});
const record = (it: TrackedItem, label?: string, partnerId = "p1"): AlertRecord => ({
  partnerId, partnerName: "Wes", channel: "C", ts: "1", postedAt: "t", item: it,
  reactions: label ? { "u:x": { user: "u", reaction: "x", label: label as never, at: 1 } } : {},
});

describe("alert history", () => {
  it("suppresses a reworded re-alert for a conversation that already alerted", () => {
    const history = buildAlertHistory([record(item())]);
    const reworded = item({ headline: "Something else entirely" });
    expect(shouldAlert(reworded, "p1", history)).toBe(false);
    expect(notableSeverityChanges([], [reworded], { partnerId: "p1", history })).toEqual([]);
  });
  it("still alerts when the same thread gets strictly worse", () => {
    const history = buildAlertHistory([record(item())]);
    expect(shouldAlert(item({ severity: "LIVE_FIRE", score: 5 }), "p1", history)).toBe(true);
  });
  it("keeps a false-alarmed item muted until it is strictly worse", () => {
    const history = buildAlertHistory([record(item(), "false_alarm")]);
    expect(shouldAlert(item({ headline: "reworded" }), "p1", history)).toBe(false);
    expect(shouldAlert(item({ severity: "LIVE_FIRE", score: 5 }), "p1", history)).toBe(true);
  });
  it("is scoped per partner and per conversation", () => {
    const history = buildAlertHistory([record(item(), "false_alarm")]);
    expect(shouldAlert(item(), "p2", history)).toBe(true);
    expect(shouldAlert(item({ headline: "Other", vitallyConversationId: "conv-2" }), "p1", history)).toBe(true);
  });
  it("falls back to the headline when an item has no conversation link", () => {
    const history = buildAlertHistory([record(item({ vitallyConversationId: null }))]);
    expect(shouldAlert(item({ vitallyConversationId: null }), "p1", history)).toBe(false);
  });
});
