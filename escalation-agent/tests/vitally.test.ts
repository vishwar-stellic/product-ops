import { describe, expect, it } from "vitest";
import { collectNewHumanEmails } from "../agent/lib/vitally";
import { conversation, fakeVitally, inbound } from "./helpers";

describe("collectNewHumanEmails", () => {
  const since = "2026-10-03T00:00:00.000Z";

  it("returns only partner-authored, non-auto, newer-than-since messages, oldest first", async () => {
    const api = fakeVitally({
      acct: [
        conversation("c2", "Registration down", "2026-10-05T10:00:00Z", [
          inbound("2026-10-05T10:00:00Z", "Second message"),
          inbound("2026-10-04T09:00:00Z", "First message"),
          inbound("2026-10-02T09:00:00Z", "Too old"),
          { type: "outbound", timestamp: "2026-10-05T11:00:00Z", message: "Stellic reply", from: { id: "admin" } },
        ]),
        conversation("c3", "Invitation: Sync", "2026-10-05T08:00:00Z", [inbound("2026-10-05T08:00:00Z", "Join us")]),
      ],
    });
    const emails = await collectNewHumanEmails(api, "acct", since);
    expect(emails.map((e) => e.body)).toEqual(["First message", "Second message"]);
    expect(emails[0]).toMatchObject({ from: "Pat Partner", subject: "Registration down", vitallyConversationId: "c2" });
  });

  it("skips ineligible sources without fetching them", async () => {
    const api = fakeVitally({
      acct: [conversation("slackish", "Hi", "2026-10-05T10:00:00Z", [inbound("2026-10-05T10:00:00Z", "x")], "slack")],
    });
    expect(await collectNewHumanEmails(api, "acct", since)).toEqual([]);
    expect(api.fullFetches).toEqual([]);
  });

  it("includes intercom-sourced conversations", async () => {
    const api = fakeVitally({
      acct: [conversation("ic", "Bug", "2026-10-05T10:00:00Z", [inbound("2026-10-05T10:00:00Z", "It is broken")], "intercom")],
    });
    expect((await collectNewHumanEmails(api, "acct", since)).length).toBe(1);
  });

  it("stops walking at the first conversation older than since (sorted desc)", async () => {
    const api = fakeVitally({
      acct: [
        conversation("new", "A", "2026-10-05T10:00:00Z", [inbound("2026-10-05T10:00:00Z", "n")]),
        conversation("old", "B", "2026-10-01T10:00:00Z", [inbound("2026-10-01T10:00:00Z", "o")]),
        conversation("older-but-listed-after", "C", "2026-10-05T11:00:00Z", [inbound("2026-10-05T11:00:00Z", "z")]),
      ],
    });
    const emails = await collectNewHumanEmails(api, "acct", since);
    expect(emails.map((e) => e.vitallyConversationId)).toEqual(["new"]);
    expect(api.fullFetches).toEqual(["new"]);
  });

  it("compares timestamps across formats (Z vs +00:00)", async () => {
    const api = fakeVitally({
      acct: [conversation("c", "S", "2026-10-05T10:00:00+00:00", [inbound("2026-10-05T10:00:00+00:00", "ok")])],
    });
    expect((await collectNewHumanEmails(api, "acct", "2026-10-05T09:59:59.000Z")).length).toBe(1);
    expect((await collectNewHumanEmails(api, "acct", "2026-10-05T10:00:00.000Z")).length).toBe(0);
  });

  it("caps each body at 4000 chars", async () => {
    const api = fakeVitally({
      acct: [conversation("c", "Long", "2026-10-05T10:00:00Z", [inbound("2026-10-05T10:00:00Z", "x".repeat(9000))])],
    });
    const [email] = await collectNewHumanEmails(api, "acct", since);
    expect(email?.body.length).toBe(4000);
  });
});
