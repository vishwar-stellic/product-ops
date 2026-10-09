import { beforeEach, describe, expect, it } from "vitest";
import { formatEmailReviewThread, formatSweepCompleteMessage } from "../agent/lib/slack";
import {
  buildTriagePrompt,
  EMAIL_REVIEW_NOTE,
  formatEmailsForPrompt,
  mergeEmailReviews,
  sanitizeEmailReviews,
  triageEmails,
  type ReviewedEmail,
} from "../agent/lib/triage";
import type { SourceEmail } from "../agent/lib/vitally";
import { itemJson } from "./helpers";

const mail = (over: Partial<SourceEmail> = {}): SourceEmail => ({
  from: "Pat Partner",
  subject: "Registration down",
  date: "2026-10-08T21:56:00Z",
  body: "Students cannot register.",
  vitallyConversationId: "c1",
  ...over,
});

const reviewed = (over: Partial<ReviewedEmail> = {}): ReviewedEmail => ({
  partnerName: "Acme University",
  from: "Pat Partner",
  subject: "Registration down",
  date: "2026-10-08T21:56:00Z",
  vitallyConversationId: "c1",
  score: 4,
  summary: "Students cannot register",
  why: "Production and many students",
  ...over,
});

beforeEach(() => {
  process.env.VITALLY_APP_SUBDOMAIN = "stellic";
});

describe("email reviews in the prompt and the parser", () => {
  it("numbers the emails and asks for emailReviews", () => {
    const text = formatEmailsForPrompt([mail(), mail({ subject: "Second" })]);
    expect(text).toContain("Email #1\nFrom:");
    expect(text).toContain("Email #2\nFrom:");
    const prompt = buildTriagePrompt({ previousItems: [], newEmails: [mail()], feedbackBlock: "" });
    expect(prompt).toContain(EMAIL_REVIEW_NOTE);
    expect(prompt).toContain('"emailReviews"');
  });

  it("keeps valid reviews, drops out-of-range / duplicate ones, and nulls a bad score", () => {
    const out = sanitizeEmailReviews(
      [
        { email: 1, score: 4, summary: "a", why: "b" },
        { email: 1, score: 2, summary: "dup", why: "dup" },
        { email: 9, score: 3, summary: "out of range", why: "x" },
        { email: 2, score: 12, summary: "bad score", why: "y" },
        "junk",
      ],
      2,
    );
    expect(out).toEqual([
      { email: 1, score: 4, summary: "a", why: "b" },
      { email: 2, score: null, summary: "bad score", why: "y" },
    ]);
    expect(sanitizeEmailReviews(undefined, 3)).toEqual([]);
  });

  it("merges reviews onto emails; a skipped email stays in, unscored", () => {
    const merged = mergeEmailReviews("Acme", [mail(), mail({ subject: "Second" })], [{ email: 2, score: 1, summary: "s", why: "w" }]);
    expect(merged.map((m) => m.score)).toEqual([null, 1]);
    expect(merged[1]).toMatchObject({ partnerName: "Acme", subject: "Second", summary: "s", why: "w" });
  });

  it("returns reviews with the items, and never fails the call over malformed reviews", async () => {
    const ok = await triageEmails(
      async () => JSON.stringify({ items: [itemJson()], emailReviews: [{ email: 1, score: 5, summary: "s", why: "w" }] }),
      { previousItems: [], newEmails: [mail()], feedbackBlock: "" },
    );
    expect(ok?.items).toHaveLength(1);
    expect(ok?.reviews).toHaveLength(1);
    const bad = await triageEmails(async () => JSON.stringify({ items: [], emailReviews: "nope" }), {
      previousItems: [],
      newEmails: [mail()],
      feedbackBlock: "",
    });
    expect(bad).toEqual({ items: [], reviews: [] });
  });
});

describe("formatSweepCompleteMessage", () => {
  const base = { partners: 104, withNewEmails: 17, llmFailures: 0, fetchFailures: 0, alertFailures: 0, liveFire: 0, smoldering: 0, emailsAnalyzed: 0 };
  it("says nothing new when nothing alerted", () => {
    expect(formatSweepCompleteMessage(base)).toBe(
      ":white_check_mark: Sweep complete: no new Live Fire or Smoldering escalations. Checked 104 partners, 17 with new email.",
    );
  });
  it("counts alerts by severity, and calls out failures and the thread", () => {
    const text = formatSweepCompleteMessage({ ...base, liveFire: 1, smoldering: 2, llmFailures: 1, fetchFailures: 1, alertFailures: 1, emailsAnalyzed: 20 });
    expect(text).toContain("3 new escalations posted above (1 Live Fire, 2 Smoldering)");
    expect(text).toContain(":warning: 2 could not be checked");
    expect(text).toContain(":warning: 1 alert failed to post");
    expect(text).toContain("20 emails analyzed - scores in the thread.");
  });
});

describe("formatEmailReviewThread", () => {
  it("is empty with no emails", () => {
    expect(formatEmailReviewThread([])).toEqual([]);
  });

  it("groups by partner, highest-scoring partner first, highest-scoring email first, in Pacific time", () => {
    const [text] = formatEmailReviewThread([
      reviewed({ partnerName: "Low U", score: 1, subject: "FYI" }),
      reviewed({ partnerName: "Hot U", score: 2, subject: "Minor", date: "2026-10-08T22:00:00Z" }),
      reviewed({ partnerName: "Hot U", score: 5, subject: "Down" }),
    ]);
    expect(text!.indexOf("*Hot U*")).toBeLessThan(text!.indexOf("*Low U*"));
    expect(text!.indexOf("Down")).toBeLessThan(text!.indexOf("Minor"));
    expect(text).toContain("3 emails across 2 partners");
    expect(text).toContain("Oct 8, 2:56 PM PT");
  });

  it("escapes untrusted text and keeps link text valid", () => {
    const [text] = formatEmailReviewThread([
      reviewed({ subject: "Re: a | b <!channel>", summary: "ping <@U123> & co", from: "Pat <pat@x.edu>" }),
    ]);
    expect(text).not.toContain("<!channel>");
    expect(text).not.toContain("<@U123>");
    expect(text).toContain("&lt;!channel&gt;");
    expect(text).toContain("a / b");
  });

  it("splits a long review into several replies and summarizes what is cut off", () => {
    const many = Array.from({ length: 400 }, (_, n) =>
      reviewed({ partnerName: `Partner ${String(n).padStart(3, "0")}`, summary: "x".repeat(120), why: "y".repeat(120), subject: `Subject ${n}` }),
    );
    const chunks = formatEmailReviewThread(many);
    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.every((c) => c.length <= 3700)).toBe(true);
    expect(chunks[chunks.length - 1]).toMatch(/more partners? not shown/);
  });
});
