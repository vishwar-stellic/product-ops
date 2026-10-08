import { describe, expect, it } from "vitest";
import { debugConversation } from "../agent/lib/debug";
import { alertKey, partnerStateKey } from "../agent/lib/feedback";
import { memoryStore } from "./helpers";

const item = (conv: string | null) => ({
  headline: "h", score: 4, severity: "SMOLDERING", severityReason: "r", evidence: [], blockedOn: "us", blockedOnReason: "",
  lastMovementAt: null, from: "x", subject: "s", lastEmailDate: null, vitallyConversationId: conv,
});

describe("debugConversation", () => {
  it("returns only alerts and states linked to the conversation", async () => {
    const store = memoryStore();
    await store.putJson(alertKey("C1", "1.1"), { partnerId: "p1", partnerName: "Wes", channel: "C1", ts: "1.1", postedAt: "2026-10-07T22:00:00Z", item: item("conv-a"), reactions: {} });
    await store.putJson(alertKey("C1", "2.2"), { partnerId: "p2", partnerName: "Other", channel: "C1", ts: "2.2", postedAt: "2026-10-07T22:00:00Z", item: item("conv-b"), reactions: {} });
    await store.putJson(partnerStateKey("p1"), { items: [item("conv-a")], lastMessageAt: "t", checkedAt: "t", recentEmails: [{ from: "f", subject: "s", date: "d", body: "b", vitallyConversationId: "conv-a" }] });
    await store.putJson(partnerStateKey("p2"), { items: [item("conv-b")], lastMessageAt: "t", checkedAt: "t" });
    const result = await debugConversation(store, "conv-a");
    expect(result.alerts.map((a) => a.partnerName)).toEqual(["Wes"]);
    expect(result.partnerStates).toHaveLength(1);
    expect(result.partnerStates[0]?.recentEmailsFromConversation).toHaveLength(1);
    expect(result.scanned).toEqual({ alerts: 2, partnerStates: 2 });
  });
});
