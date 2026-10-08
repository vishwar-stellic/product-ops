import { describe, expect, it, vi } from "vitest";
import {
  assignItemIds,
  buildTriagePrompt,
  hasFreshEvidence,
  ITEM_IDENTITY_NOTE,
  reconcileItemIds,
  enrichItemsWithConversations,
  extractJsonObject,
  matchConversationId,
  notableSeverityChanges,
  sanitizeItem,
  updateEscalations,
  type TrackedItem,
} from "../agent/lib/triage";
import type { SourceEmail } from "../agent/lib/vitally";
import { itemJson } from "./helpers";

const email = (over: Partial<SourceEmail> = {}): SourceEmail => ({
  from: "Pat Partner",
  subject: "Registration down",
  date: "2026-10-05T14:00:00Z",
  body: "Students cannot register.",
  vitallyConversationId: "c1",
  ...over,
});

const tracked = (over: Partial<TrackedItem> = {}): TrackedItem => ({
  ...(sanitizeItem(itemJson() as Record<string, unknown>) as TrackedItem),
  ...over,
});

describe("sanitizeItem", () => {
  it("keeps the score and normalizes severity/blockedOn", () => {
    const item = sanitizeItem(itemJson({ severity: "live fire", blockedOn: "US" }) as Record<string, unknown>);
    expect(item).toMatchObject({ score: 5, severity: "LIVE_FIRE", blockedOn: "us" });
  });
  it("falls back to WATCH / unclear and a severity-derived score", () => {
    const item = sanitizeItem({ headline: "x", severity: "bogus", blockedOn: "??", score: "nope" });
    expect(item).toMatchObject({ severity: "WATCH", blockedOn: "unclear", score: 3 });
  });
  it("derives the score from severity when missing", () => {
    expect(sanitizeItem({ headline: "x", severity: "SMOLDERING" })?.score).toBe(4);
  });
  it("drops items without a headline and caps evidence at 2", () => {
    expect(sanitizeItem({ headline: "  " })).toBeNull();
    const item = sanitizeItem(
      itemJson({ evidence: [1, 2, 3].map((n) => ({ quote: `q${n}`, sender: "s", date: "d" })) }) as Record<string, unknown>,
    );
    expect(item?.evidence.length).toBe(2);
  });
});

describe("buildTriagePrompt", () => {
  it("fills every placeholder and injects the feedback block before the tracked items", () => {
    const prompt = buildTriagePrompt({
      previousItems: [tracked()],
      newEmails: [email({ body: "price is $& and $1" })],
      feedbackBlock: "TEAM FEEDBACK CALIBRATION\n1. example",
    });
    expect(prompt).not.toContain("__PREVIOUS_ITEMS__");
    expect(prompt).not.toContain("__NEW_EMAILS__");
    expect(prompt).not.toContain("__FEEDBACK__");
    expect(prompt).toContain("price is $& and $1"); // replacement patterns are not interpreted
    expect(prompt.indexOf("TEAM FEEDBACK CALIBRATION")).toBeLessThan(prompt.indexOf("PREVIOUSLY TRACKED ITEMS"));
    expect(prompt).toContain('"headline": "Registration blocked in Prod"');
    expect(prompt).toContain("Subject: Registration down");
  });
  it("omits the feedback section entirely when there is none", () => {
    const prompt = buildTriagePrompt({ previousItems: [], newEmails: [email()], feedbackBlock: "" });
    expect(prompt).not.toContain("TEAM FEEDBACK CALIBRATION");
    expect(prompt).toContain("SCORING RUBRIC");
  });
  it("keeps the rubric's JSON output shape unescaped", () => {
    const prompt = buildTriagePrompt({ previousItems: [], newEmails: [email()], feedbackBlock: "" });
    expect(prompt).toContain('{"items": [');
    expect(prompt).not.toContain("{{");
  });
});

describe("updateEscalations", () => {
  it("parses a fenced JSON reply", async () => {
    const llm = vi.fn(async () => "```json\n" + JSON.stringify({ items: [itemJson()] }) + "\n```");
    const items = await updateEscalations(llm, { previousItems: [], newEmails: [email()], feedbackBlock: "" });
    expect(items?.length).toBe(1);
    expect(items?.[0]?.headline).toBe("Registration blocked in Prod");
  });
  it("returns an empty list when the model finds nothing", async () => {
    const items = await updateEscalations(async () => '{"items": []}', {
      previousItems: [tracked()],
      newEmails: [email()],
      feedbackBlock: "",
    });
    expect(items).toEqual([]);
  });
  it("returns null on malformed JSON or a thrown error", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(await updateEscalations(async () => "not json", { previousItems: [], newEmails: [email()], feedbackBlock: "" })).toBeNull();
    expect(
      await updateEscalations(
        async () => {
          throw new Error("boom");
        },
        { previousItems: [], newEmails: [email()], feedbackBlock: "" },
      ),
    ).toBeNull();
  });
  it("extracts the object from surrounding prose", () => {
    expect(extractJsonObject('Here you go: {"items": []} thanks')).toBe('{"items": []}');
  });
});

describe("notableSeverityChanges", () => {
  const watch = tracked({ headline: "A", severity: "WATCH", score: 3 });
  it("flags brand-new Live Fire / Smoldering items", () => {
    expect(notableSeverityChanges([], [tracked({ headline: "N" })]).map((i) => i.headline)).toEqual(["N"]);
  });
  it("flags escalations from a lower severity", () => {
    const up = tracked({ headline: "A", severity: "SMOLDERING", score: 4 });
    expect(notableSeverityChanges([watch], [up])).toEqual([up]);
  });
  it("never re-alerts an item that stayed at the same severity", () => {
    const same = tracked({ headline: "A", severity: "SMOLDERING", score: 4 });
    expect(notableSeverityChanges([same], [same])).toEqual([]);
  });
  it("ignores WATCH items, including brand-new ones", () => {
    expect(notableSeverityChanges([], [watch])).toEqual([]);
  });
  it("does not re-alert when the model rewords the headline of the same conversation", () => {
    const before = tracked({ headline: "Day Two meetings", vitallyConversationId: "conv-1" });
    const reworded = tracked({ headline: "Stellic Day Two meetings (rescheduled)", vitallyConversationId: "conv-1" });
    expect(notableSeverityChanges([before], [reworded])).toEqual([]);
  });
  it("does not re-alert on a de-escalation to a lower notable severity", () => {
    const fire = tracked({ headline: "A", severity: "LIVE_FIRE", score: 5 });
    const smolder = tracked({ headline: "A", severity: "SMOLDERING", score: 4 });
    expect(notableSeverityChanges([fire], [smolder])).toEqual([]);
  });
  it("alerts for a different issue in a different conversation", () => {
    const before = tracked({ headline: "A", vitallyConversationId: "conv-1" });
    const other = tracked({ headline: "B", vitallyConversationId: "conv-2" });
    expect(notableSeverityChanges([before], [other])).toEqual([other]);
  });
});

describe("conversation matching", () => {
  it("prefers subject then exact date", () => {
    const emails = [
      email({ subject: "Other", vitallyConversationId: "x" }),
      email({ subject: "RE: Registration down", date: "2026-10-05T14:00:00Z", vitallyConversationId: "c-match" }),
      email({ subject: "Re: registration down", date: "2026-10-04T14:00:00Z", vitallyConversationId: "c-older" }),
    ];
    expect(matchConversationId(tracked(), emails)).toBe("c-match");
  });
  it("never links an item to an unrelated thread just because it is the newest email", () => {
    const item = tracked({ subject: "RE: Course numbering problem", lastEmailDate: "2026-10-06T14:17:46Z", from: "Kenny" });
    const unrelated = [email({ subject: "Stellic Day Two meetings", date: "2026-10-07T20:32:38Z", vitallyConversationId: "day-two" })];
    expect(matchConversationId(item, unrelated)).toBeNull();
  });
  it("keeps an untouched item's existing link when no new email matches it", () => {
    const prior = tracked({ subject: "RE: Course numbering problem", vitallyConversationId: "course-thread" });
    const unrelated = [email({ subject: "Stellic Day Two meetings", from: "Roger", date: "2026-10-07T20:00:00Z", vitallyConversationId: "day-two" })];
    const [out] = enrichItemsWithConversations([prior], unrelated, [prior]);
    expect(out?.vitallyConversationId).toBe("course-thread");
  });
  it("falls back to the prior item's conversation id by headline", () => {
    const prior = tracked({ vitallyConversationId: "c-prior" });
    const [out] = enrichItemsWithConversations([tracked({ vitallyConversationId: null })], [], [prior]);
    expect(out?.vitallyConversationId).toBe("c-prior");
  });
});

describe("item identity", () => {
  it("assigns ids once and leaves existing ones alone", () => {
    const [a] = assignItemIds([tracked()]);
    expect(a?.id).toBeTruthy();
    const again = assignItemIds([a as TrackedItem]);
    expect(again[0]).toBe(a);
  });
  it("keeps the prior id when the model echoes it, even with a new headline", () => {
    const [prior] = assignItemIds([tracked({ headline: "Old headline" })]);
    const [out] = reconcileItemIds([prior as TrackedItem], [tracked({ headline: "Reworded", id: prior?.id })]);
    expect(out?.id).toBe(prior?.id);
  });
  it("falls back to the headline, and gives genuinely new or unknown ids a fresh id", () => {
    const [prior] = assignItemIds([tracked({ headline: "Same" })]);
    const out = reconcileItemIds(
      [prior as TrackedItem],
      [tracked({ headline: "Same" }), tracked({ headline: "Different", id: "made-up" }), tracked({ headline: "Other" })],
    );
    expect(out[0]?.id).toBe(prior?.id);
    expect(out[1]?.id).not.toBe("made-up");
    expect(new Set(out.map((i) => i.id)).size).toBe(3);
  });
  it("never gives one id to two items", () => {
    const [prior] = assignItemIds([tracked()]);
    const out = reconcileItemIds([prior as TrackedItem], [tracked({ id: prior?.id }), tracked({ id: prior?.id, headline: "dup" })]);
    expect(out[0]?.id).not.toBe(out[1]?.id);
  });
  it("tells the model to keep ids and shows previous ids in the prompt", () => {
    const [prior] = assignItemIds([tracked()]);
    const prompt = buildTriagePrompt({ previousItems: [prior as TrackedItem], newEmails: [email()], feedbackBlock: "" });
    expect(prompt).toContain(`"id": "${prior?.id}"`);
    expect(prompt).toContain(ITEM_IDENTITY_NOTE);
  });
  it("does not re-alert a reworded, re-linked item that has the same id", () => {
    const [prior] = assignItemIds([tracked({ headline: "Prereqs wrong", vitallyConversationId: "thread-a" })]);
    const updated = tracked({ headline: "Prereq display wrong; trust lost", vitallyConversationId: "thread-b", id: prior?.id });
    expect(notableSeverityChanges([prior as TrackedItem], [updated])).toEqual([]);
  });
});

describe("fresh evidence", () => {
  const emails = [email({ date: "2026-10-08T14:56:24Z", body: "DONE - Pre-reqs from the last meeting. Has been submitted." })];
  const ev = (quote: string, date: string) => ({ quote, sender: "x", date });
  it("accepts a quote from the new emails or an exact timestamp match", () => {
    expect(hasFreshEvidence(tracked({ evidence: [ev("Pre-reqs from the last meeting", "2026-10-07")] }), emails)).toBe(true);
    expect(hasFreshEvidence(tracked({ evidence: [ev("something paraphrased", "2026-10-08T14:56:24Z")] }), emails)).toBe(true);
  });
  it("rejects evidence that is all older than this run's emails", () => {
    const stale = tracked({ evidence: [ev("People are losing trust in Stellic", "2026-10-05T20:00:00Z")] });
    expect(hasFreshEvidence(stale, emails)).toBe(false);
  });
  it("lets an item with no evidence through (it can't be shown stale)", () => {
    expect(hasFreshEvidence(tracked({ evidence: [] }), emails)).toBe(true);
  });
  it("suppresses an alert whose evidence is stale, but not one with fresh evidence", () => {
    const stale = tracked({ evidence: [ev("People are losing trust in Stellic", "2026-10-05T20:00:00Z")] });
    const fresh = tracked({ evidence: [ev("Pre-reqs from the last meeting", "2026-10-08T14:56:24Z")] });
    expect(notableSeverityChanges([], [stale], { newEmails: emails })).toEqual([]);
    expect(notableSeverityChanges([], [fresh], { newEmails: emails })).toEqual([fresh]);
  });
});

describe("keeping an item's original link", () => {
  const emails = [
    email({ subject: "Stellic agenda items", from: "Michelle", date: "2026-10-08T14:56:24Z", body: "DONE - Pre-reqs from the last meeting", vitallyConversationId: "agenda" }),
  ];
  it("does not re-point an old item at a newer thread its evidence is not in", () => {
    const [prior] = assignItemIds([tracked({ vitallyConversationId: "prereqs-thread", subject: "Re: Pre-reqs" })]);
    const updated = tracked({
      id: prior?.id,
      subject: "Stellic agenda items",
      from: "Michelle",
      lastEmailDate: "2026-10-08T14:56:24Z",
      evidence: [{ quote: "People are losing trust in Stellic", sender: "Leanne", date: "2026-10-05T20:00:00Z" }],
    });
    const [out] = enrichItemsWithConversations([updated], emails, [prior as TrackedItem]);
    expect(out?.vitallyConversationId).toBe("prereqs-thread");
  });
  it("does re-point it when its evidence really is in the new thread", () => {
    const [prior] = assignItemIds([tracked({ vitallyConversationId: "prereqs-thread" })]);
    const updated = tracked({
      id: prior?.id,
      subject: "Stellic agenda items",
      from: "Michelle",
      lastEmailDate: "2026-10-08T14:56:24Z",
      evidence: [{ quote: "DONE - Pre-reqs from the last meeting", sender: "Michelle", date: "2026-10-08T14:56:24Z" }],
    });
    const [out] = enrichItemsWithConversations([updated], emails, [prior as TrackedItem]);
    expect(out?.vitallyConversationId).toBe("agenda");
  });
});
