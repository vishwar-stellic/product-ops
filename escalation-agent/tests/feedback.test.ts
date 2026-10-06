import { describe, expect, it } from "vitest";
import {
  alertKey,
  applyReaction,
  correctedScoreText,
  currentVerdict,
  loadFeedbackSnapshot,
  partnerStateKey,
  renderFeedbackBlock,
  selectBalanced,
  verdictForReaction,
  type AlertRecord,
  type FeedbackExample,
  type VerdictLabel,
} from "../agent/lib/feedback";
import { sanitizeItem, type TrackedItem } from "../agent/lib/triage";
import { itemJson, memoryStore } from "./helpers";

const item = (over: Partial<TrackedItem> = {}): TrackedItem => ({
  ...(sanitizeItem(itemJson() as Record<string, unknown>) as TrackedItem),
  ...over,
});

const record = (over: Partial<AlertRecord> = {}): AlertRecord => ({
  partnerId: "p1",
  partnerName: "Acme U",
  channel: "C1",
  ts: "100.1",
  postedAt: "2026-10-05T12:00:00Z",
  item: item(),
  reactions: {},
  ...over,
});

const reaction = (user: string, name: string, label: VerdictLabel, at: number) => ({
  [`${user}:${name}`]: { user, reaction: name, label, at },
});

describe("verdictForReaction", () => {
  it("maps the vocabulary and ignores unknown emoji", () => {
    expect(verdictForReaction("+1")).toBe("correct");
    expect(verdictForReaction("-1")).toBe("false_alarm");
    expect(verdictForReaction("arrow_down")).toBe("too_severe");
    expect(verdictForReaction("arrow_up")).toBe("under_rated");
    expect(verdictForReaction("white_check_mark")).toBeNull(); // no "resolved" emoji
    expect(verdictForReaction("eyes")).toBeNull();
  });
  it("handles skin-tone variants", () => {
    expect(verdictForReaction("+1::skin-tone-3")).toBe("correct");
    expect(verdictForReaction("-1::skin-tone-2")).toBe("false_alarm");
  });
});

describe("applyReaction", () => {
  const base = { channel: "C1", ts: "100.1", user: "U1" };

  it("ignores reactions on messages that are not tracked alerts", async () => {
    const store = memoryStore();
    expect(await applyReaction(store, { ...base, reaction: "+1", added: true, at: 1 })).toEqual({
      status: "ignored",
      reason: "unknown_alert",
    });
  });

  it("ignores emoji outside the vocabulary without touching the record", async () => {
    const store = memoryStore();
    await store.putJson(alertKey("C1", "100.1"), record());
    const before = store.data.get(alertKey("C1", "100.1"));
    expect(await applyReaction(store, { ...base, reaction: "eyes", added: true, at: 1 })).toEqual({
      status: "ignored",
      reason: "unknown_emoji",
    });
    expect(store.data.get(alertKey("C1", "100.1"))).toBe(before);
  });

  it("records, replaces by recency, and removes votes", async () => {
    const store = memoryStore();
    await store.putJson(alertKey("C1", "100.1"), record());
    await applyReaction(store, { ...base, reaction: "+1", added: true, at: 10 });
    await applyReaction(store, { channel: "C1", ts: "100.1", user: "U2", reaction: "-1", added: true, at: 20 });
    let saved = (await store.getJson<AlertRecord>(alertKey("C1", "100.1")))!;
    expect(currentVerdict(saved)?.label).toBe("false_alarm"); // latest wins across people
    await applyReaction(store, { channel: "C1", ts: "100.1", user: "U2", reaction: "-1", added: false, at: 30 });
    saved = (await store.getJson<AlertRecord>(alertKey("C1", "100.1")))!;
    expect(currentVerdict(saved)?.label).toBe("correct"); // removing the vote reveals the earlier one
    await applyReaction(store, { ...base, reaction: "+1", added: false, at: 40 });
    saved = (await store.getJson<AlertRecord>(alertKey("C1", "100.1")))!;
    expect(currentVerdict(saved)).toBeNull();
  });
});

describe("selectBalanced / renderFeedbackBlock", () => {
  const ex = (label: VerdictLabel, at: number, headline = `h${at}`): FeedbackExample => ({
    record: record({ item: item({ headline }) }),
    verdict: { user: "U", reaction: "x", label, at },
  });

  it("round-robins labels so one frequent verdict cannot crowd out the rest", () => {
    const examples = [
      ...Array.from({ length: 10 }, (_, n) => ex("correct", 100 + n)),
      ex("false_alarm", 1),
      ex("too_severe", 2),
    ];
    const picked = selectBalanced(examples, 5);
    const labels = picked.map((p) => p.verdict.label);
    expect(labels).toContain("false_alarm");
    expect(labels).toContain("too_severe");
    expect(picked.length).toBe(5);
  });

  it("orders the picked examples newest first", () => {
    const picked = selectBalanced([ex("correct", 1), ex("under_rated", 9), ex("false_alarm", 5)], 10);
    expect(picked.map((p) => p.verdict.at)).toEqual([9, 5, 1]);
  });

  it("renders a bounded, rubric-subordinate block", () => {
    const long = "word ".repeat(200);
    const block = renderFeedbackBlock([ex("false_alarm", 5, long)]);
    expect(block).toContain("TEAM FEEDBACK CALIBRATION");
    expect(block).toContain("SCORING RUBRIC above still governs");
    expect(block).toContain("FALSE ALARM");
    expect(block).toContain("true score 0-2");
    expect(block.length).toBeLessThan(1500);
    expect(renderFeedbackBlock([])).toBe("");
  });

  it("computes the corrected score text", () => {
    expect(correctedScoreText("too_severe", 5)).toContain("true score 4");
    expect(correctedScoreText("too_severe", 4)).toContain("true score 3");
    expect(correctedScoreText("under_rated", 4)).toContain("true score 5");
    expect(correctedScoreText("under_rated", 5)).toContain("true score 5");
    expect(correctedScoreText("correct", 4)).toContain("score 4 was right");
  });
});

describe("loadFeedbackSnapshot", () => {
  it("builds the block from reviewed alerts only", async () => {
    const store = memoryStore();
    await store.putJson(
      alertKey("C1", "1.1"),
      record({ ts: "1.1", item: item({ headline: "Bad call" }), reactions: reaction("U1", "-1", "false_alarm", 10) }),
    );
    await store.putJson(
      alertKey("C1", "2.2"),
      record({
        ts: "2.2",
        partnerId: "p2",
        item: item({ headline: "Too loud" }),
        reactions: reaction("U1", "arrow_down", "too_severe", 11),
      }),
    );
    await store.putJson(alertKey("C1", "3.3"), record({ ts: "3.3", item: item({ headline: "No reaction yet" }) }));
    const snapshot = await loadFeedbackSnapshot(store);
    expect(snapshot.exampleCount).toBe(2);
    expect(snapshot.block).toContain("Bad call");
    expect(snapshot.block).toContain("Too loud");
    expect(snapshot.block).not.toContain("No reaction yet");
  });
  it("is empty when nothing has been reviewed", async () => {
    const snapshot = await loadFeedbackSnapshot(memoryStore());
    expect(snapshot).toMatchObject({ block: "", exampleCount: 0 });
  });
});
