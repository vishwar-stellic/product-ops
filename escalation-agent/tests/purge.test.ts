import { describe, expect, it } from "vitest";
import { partnerStateKey } from "../agent/lib/feedback";
import { purgeForeignConversation } from "../agent/lib/purge";
import type { TriagePartner } from "../agent/lib/registry";
import { sanitizeItem, type TrackedItem } from "../agent/lib/triage";
import type { PartnerState } from "../agent/lib/sweep";
import { fakeVitally, itemJson, memoryStore } from "./helpers";

const registry: TriagePartner[] = [
  { partnerId: "lax", name: "La Crosse", vitallyAccountId: "a-lax" },
  { partnerId: "osh", name: "Oshkosh", vitallyAccountId: "a-osh" },
  { partnerId: "other", name: "Elsewhere", vitallyAccountId: "a-other" },
];

const shared = {
  id: "thread",
  subject: "Re: Pre-reqs",
  source: "google",
  updatedAt: "2026-10-05T20:00:00Z",
  accounts: [{ id: "a-lax" }, { id: "a-osh" }],
  users: [
    { id: "leanne", name: "Leanne", accounts: [{ id: "a-lax" }] },
    { id: "jenna", name: "Jenna", accounts: [{ id: "a-lax" }, { id: "a-osh" }] },
  ],
  messages: [
    { type: "inbound", timestamp: "2026-10-05T20:00:00Z", message: "<p>People are losing trust in Stellic</p>", from: { id: "leanne" } },
  ],
};

const item = (over: Partial<TrackedItem>): TrackedItem => ({
  ...(sanitizeItem(itemJson({ severity: "SMOLDERING", score: 4 }) as Record<string, unknown>) as TrackedItem),
  ...over,
});
const state = (items: TrackedItem[], fromThread = false): PartnerState => ({
  items,
  lastMessageAt: "2026-10-06T00:00:00Z",
  checkedAt: "x",
  recentEmails: fromThread ? [{ from: "f", subject: "s", date: "d", body: "b", vitallyConversationId: "thread" }] : [],
});

describe("purgeForeignConversation", () => {
  async function seed() {
    const store = memoryStore();
    const lacrosse = item({ headline: "LAX", vitallyConversationId: "thread", evidence: [{ quote: "People are losing trust in Stellic", sender: "Leanne", date: "d" }] });
    const linked = item({ headline: "OSH linked", vitallyConversationId: "thread" });
    // Re-linked to another thread but still carrying the other school's quotes.
    const relinked = item({
      headline: "OSH relinked",
      vitallyConversationId: "agenda",
      evidence: [{ quote: "People are losing trust in Stellic", sender: "Leanne", date: "d" }],
    });
    const own = item({ headline: "OSH own", vitallyConversationId: "own", evidence: [{ quote: "Something entirely different here", sender: "M", date: "d" }] });
    await store.putJson(partnerStateKey("lax"), state([lacrosse]));
    await store.putJson(partnerStateKey("osh"), state([linked, relinked, own], true));
    await store.putJson(partnerStateKey("other"), state([own]));
    return store;
  }

  it("removes foreign items (linked or carrying the thread's quotes), keeps the owner's and unrelated ones", async () => {
    const store = await seed();
    const result = await purgeForeignConversation(store, fakeVitally({ x: [shared as never] }), registry, "thread");
    expect(result.ownerPartners).toEqual(["La Crosse"]);
    const osh = result.cleaned.find((c) => c.partnerId === "osh")!;
    expect(osh.removedItems.map((i) => [i.headline, i.reason])).toEqual([
      ["OSH linked", "linked"],
      ["OSH relinked", "evidence"],
    ]);
    expect(osh.removedRecentEmails).toBe(1);
    expect((await store.getJson<PartnerState>(partnerStateKey("osh")))!.items.map((i) => i.headline)).toEqual(["OSH own"]);
    expect((await store.getJson<PartnerState>(partnerStateKey("lax")))!.items).toHaveLength(1);
    expect(result.cleaned.find((c) => c.partnerId === "other")).toBeUndefined();
  });

  it("writes nothing on a dry run", async () => {
    const store = await seed();
    const before = store.data.get(partnerStateKey("osh"));
    const result = await purgeForeignConversation(store, fakeVitally({ x: [shared as never] }), registry, "thread", { dryRun: true });
    expect(result.dryRun).toBe(true);
    expect(result.cleaned).toHaveLength(1);
    expect(store.data.get(partnerStateKey("osh"))).toBe(before);
  });

  it("refuses a conversation with no accounts rather than guessing an owner", async () => {
    const store = await seed();
    const bare = { ...shared, accounts: [] };
    await expect(purgeForeignConversation(store, fakeVitally({ x: [bare as never] }), registry, "thread")).rejects.toThrow(/no accounts/);
  });
});
