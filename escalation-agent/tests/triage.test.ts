import { describe, expect, it, vi } from "vitest";
import {
  buildTriagePrompt,
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
