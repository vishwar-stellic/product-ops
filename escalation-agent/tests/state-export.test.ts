import { describe, expect, it } from "vitest";
import { partnerStateKey } from "../agent/lib/feedback";
import { exportPartnerStates } from "../agent/lib/state-export";
import { memoryStore } from "./helpers";

describe("exportPartnerStates", () => {
  it("returns saved state per partner, null for a partner never processed, and handles ':' in ids", async () => {
    const store = memoryStore();
    await store.putJson(partnerStateKey("intercom:abc"), {
      items: [{ headline: "h" }],
      lastMessageAt: "2026-10-08T00:00:00Z",
      checkedAt: "2026-10-08T01:00:00Z",
      recentEmails: [{ from: "f", subject: "s", date: "d", body: "b", vitallyConversationId: "c" }],
    });
    const out = await exportPartnerStates(store, ["intercom:abc", "intercom:new", "intercom:abc"]);
    expect(Object.keys(out).sort()).toEqual(["intercom:abc", "intercom:new"]);
    expect(out["intercom:abc"]).toMatchObject({ checkedAt: "2026-10-08T01:00:00Z", lastMessageAt: "2026-10-08T00:00:00Z" });
    expect(out["intercom:abc"]?.items).toHaveLength(1);
    expect(out["intercom:abc"]?.recentEmails).toHaveLength(1);
    expect(out["intercom:new"]).toBeNull();
  });
});
