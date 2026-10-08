import { beforeEach, describe, expect, it, vi } from "vitest";
import { alertKey, applyReaction, partnerStateKey, type AlertRecord } from "../agent/lib/feedback";
import type { TriagePartner } from "../agent/lib/registry";
import { runSweep, type PartnerState, type SweepDeps } from "../agent/lib/sweep";
import { conversation, fakeVitally, inbound, itemJson, memoryStore } from "./helpers";

const NOW = new Date("2026-10-05T16:00:00Z");
const partner: TriagePartner = { partnerId: "p1", name: "Acme University", vitallyAccountId: "acct1" };
const reply = (...items: Record<string, unknown>[]) => JSON.stringify({ items });

function setup(opts: { emails?: ReturnType<typeof inbound>[]; llmReplies?: Array<string | Error> } = {}) {
  const store = memoryStore();
  const emails = opts.emails ?? [inbound("2026-10-05T14:00:00Z", "Students cannot register")];
  const vitally = fakeVitally({
    acct1: [conversation("conv1", "Registration down", "2026-10-05T14:00:00Z", emails)],
  });
  const replies = [...(opts.llmReplies ?? [reply(itemJson())])];
  const prompts: string[] = [];
  const llm = vi.fn(async (prompt: string) => {
    prompts.push(prompt);
    const next = replies.length > 1 ? replies.shift()! : replies[0]!;
    if (next instanceof Error) throw next;
    return next;
  });
  let n = 0;
  const posts: string[] = [];
  const post = vi.fn(async (text: string) => {
    posts.push(text);
    n += 1;
    return { channel: "CALERT", ts: `${1000 + n}.0001` };
  });
  const deps: SweepDeps = { store, vitally, llm, post, registry: [partner], now: NOW };
  return { store, vitally, llm, prompts, post, posts, deps };
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "log").mockImplementation(() => {});
  process.env.VITALLY_APP_SUBDOMAIN = "stellic";
});

describe("runSweep", () => {
  it("triages new email, saves state, and posts one alert with a source link and quote", async () => {
    const { deps, store, posts } = setup();
    const summary = await runSweep(deps);
    expect(summary).toMatchObject({ partners: 1, withNewEmails: 1, alertsPosted: 1, llmFailures: 0, fetchFailures: 0 });
    expect(posts).toHaveLength(2); // the alert + the once-per-sweep legend
    expect(posts[0]).toContain(":fire: *Live Fire* \u2014 *Acme University*: Registration blocked in Prod");
    expect(posts[0]).toContain("https://stellic.vitally.io/conversations/active/conv1|source");
    expect(posts[0]).toContain("> Students cannot register and the deadline passed");
    expect(posts[0]).not.toContain("React:"); // the legend is posted once per sweep, not on each alert
    const state = (await store.getJson<PartnerState>(partnerStateKey("p1")))!;
    expect(state.items).toHaveLength(1);
    expect(state.items[0]?.vitallyConversationId).toBe("conv1");
    expect(state.lastMessageAt).toBe("2026-10-05T14:00:00Z");
    const record = (await store.getJson<AlertRecord>(alertKey("CALERT", "1001.0001")))!;
    expect(record).toMatchObject({ partnerId: "p1", partnerName: "Acme University", reactions: {} });
  });

  it("uses the firecracker for Smoldering", async () => {
    const { deps, posts } = setup({
      llmReplies: [reply(itemJson({ severity: "SMOLDERING", score: 4, headline: "Partner is escalating" }))],
    });
    await runSweep(deps);
    expect(posts[0]).toContain(":firecracker: *Smoldering*");
  });

  it("does not call the LLM or re-alert when there is no new email", async () => {
    const { deps, llm, post } = setup();
    await runSweep(deps);
    llm.mockClear();
    post.mockClear();
    const summary = await runSweep({ ...deps, now: new Date("2026-10-05T18:00:00Z") });
    expect(llm).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
    expect(summary).toMatchObject({ withNewEmails: 0, alertsPosted: 0 });
  });

  it("alerts again when an existing item escalates, but not when it stays put", async () => {
    const watch = itemJson({ severity: "WATCH", score: 3 });
    const fresh = (quote: string, date: string) => [{ quote, sender: "Pat Partner", date }];
    const smolder = itemJson({
      severity: "SMOLDERING",
      score: 4,
      evidence: fresh("Now it affects every campus", "2026-10-05T17:00:00Z"),
    });
    const smolderAgain = itemJson({
      severity: "SMOLDERING",
      score: 4,
      evidence: fresh("Still broken", "2026-10-05T19:00:00Z"),
    });
    const { deps, post } = setup({ llmReplies: [reply(watch), reply(smolder), reply(smolderAgain)] });
    await runSweep(deps); // WATCH: no alert
    expect(post).not.toHaveBeenCalled();

    const vit = fakeVitally({
      acct1: [
        conversation("conv1", "Registration down", "2026-10-05T17:00:00Z", [
          inbound("2026-10-05T17:00:00Z", "Now it affects every campus"),
          inbound("2026-10-05T14:00:00Z", "Students cannot register"),
        ]),
      ],
    });
    await runSweep({ ...deps, vitally: vit, now: new Date("2026-10-05T18:00:00Z") });
    expect(post).toHaveBeenCalledTimes(2); // the escalated alert + the legend

    const vit2 = fakeVitally({
      acct1: [
        conversation("conv1", "Registration down", "2026-10-05T19:00:00Z", [
          inbound("2026-10-05T19:00:00Z", "Still broken"),
        ]),
      ],
    });
    await runSweep({ ...deps, vitally: vit2, now: new Date("2026-10-05T20:00:00Z") });
    expect(post).toHaveBeenCalledTimes(2); // same severity -> no re-alert, and so no new legend
  });

  it("does not re-alert a saved item the model rewords, re-links and re-scores on stale evidence (UW-Oshkosh case)", async () => {
    const stale = [{ quote: "People are losing trust in Stellic", sender: "Leanne", date: "2026-10-05T20:00:00Z" }];
    const original = itemJson({
      headline: "Prereq display wrong; partner losing trust",
      severity: "SMOLDERING",
      score: 4,
      evidence: stale,
    });
    const { deps, store, posts } = setup({ llmReplies: [reply(original)] });
    await runSweep(deps);
    const saved = (await store.getJson<PartnerState>(partnerStateKey("p1")))!.items[0]!;
    const savedId = saved.id;
    expect(savedId).toBeTruthy();
    expect(saved.vitallyConversationId).toBe("conv1");
    posts.length = 0;

    // Next sweep: an agenda email marks pre-reqs DONE; the model rewords + keeps the stale quotes.
    const vit = fakeVitally({
      acct1: [conversation("agenda", "Stellic agenda items", "2026-10-06T15:00:00Z", [inbound("2026-10-06T15:00:00Z", "DONE - Pre-reqs from the last meeting")])],
    });
    const reworded = itemJson({
      id: savedId,
      headline: "Incorrect prereq/co-req display in Production; partner reports loss of trust",
      severity: "SMOLDERING",
      score: 4,
      evidence: stale,
      subject: "Stellic agenda items",
      lastEmailDate: "2026-10-06T15:00:00Z",
    });
    const { deps: deps2 } = setup({ llmReplies: [reply(reworded)] });
    await runSweep({ ...deps, vitally: vit, llm: deps2.llm, now: new Date("2026-10-06T16:00:00Z") });
    expect(posts).toHaveLength(0);
    const state = (await store.getJson<PartnerState>(partnerStateKey("p1")))!;
    expect(state.items[0]).toMatchObject({ id: savedId, vitallyConversationId: "conv1" });
  });

  it("gives legacy saved items an id and shows it to the model", async () => {
    const { deps, store, prompts } = setup({ llmReplies: [reply(itemJson({ severity: "WATCH", score: 3 }))] });
    const legacy = { ...itemJson({ severity: "WATCH", score: 3 }), vitallyConversationId: "conv1" };
    await store.putJson(partnerStateKey("p1"), { items: [legacy], lastMessageAt: "2026-10-01T00:00:00Z", checkedAt: "x" });
    await runSweep(deps);
    const id = (await store.getJson<PartnerState>(partnerStateKey("p1")))!.items[0]!.id;
    expect(id).toBeTruthy();
    expect(prompts[0]).toContain(`"id": "${id}"`);
  });

  it("keeps prior items and does not advance lastMessageAt when the LLM fails", async () => {
    const { deps, store } = setup({ llmReplies: [new Error("timeout")] });
    const summary = await runSweep(deps);
    expect(summary).toMatchObject({ llmFailures: 1, alertsPosted: 0 });
    expect(await store.getJson(partnerStateKey("p1"))).toBeNull(); // nothing advanced; retried next run
  });

  it("isolates a failing partner from the rest of the batch", async () => {
    const good: TriagePartner = { partnerId: "p2", name: "Good U", vitallyAccountId: "acct2" };
    const vit = fakeVitally({
      acct2: [conversation("conv2", "Down", "2026-10-05T14:00:00Z", [inbound("2026-10-05T14:00:00Z", "help")])],
    });
    const failing = { ...vit, listAccountConversations: (id: string) => (id === "acct1" ? boom() : vit.listAccountConversations(id)) };
    const { deps } = setup();
    const summary = await runSweep({ ...deps, vitally: failing as any, registry: [partner, good] });
    expect(summary.fetchFailures).toBe(1);
    expect(summary.alertsPosted).toBe(1);
  });

  it("dry run logs instead of posting and records no alert", async () => {
    const { deps, store } = setup();
    const summary = await runSweep({ ...deps, post: null });
    expect(summary.alertsPosted).toBe(0);
    expect(await store.listKeys("alerts/")).toEqual([]);
  });

  it("continues if one Slack post fails and counts it", async () => {
    const { deps } = setup({ llmReplies: [reply(itemJson(), itemJson({ headline: "Second fire" }))] });
    const post = vi
      .fn()
      .mockRejectedValueOnce(new Error("channel_not_found"))
      .mockResolvedValueOnce({ channel: "CALERT", ts: "9.9" });
    const summary = await runSweep({ ...deps, post });
    expect(summary).toMatchObject({ alertsPosted: 1, alertFailures: 1 });
  });
});

describe("reaction legend", () => {
  const LEGEND = "React: :+1: right call, :-1: false alarm, :arrow_down: too severe, :arrow_up: under-rated";

  it("is posted once after the alerts, and is not an alert record", async () => {
    const { deps, posts, store } = setup({ llmReplies: [reply(itemJson(), itemJson({ headline: "Second fire" }))] });
    const summary = await runSweep(deps);
    expect(summary.alertsPosted).toBe(2);
    expect(posts).toHaveLength(3);
    expect(posts.filter((p) => p === LEGEND)).toHaveLength(1);
    expect(posts[2]).toBe(LEGEND);
    expect(posts[2]).not.toContain("resolved");
    expect((await store.listKeys("alerts/")).length).toBe(2); // the legend is not tracked
  });

  it("is not posted when the sweep raised no alerts", async () => {
    const { deps, posts } = setup({ llmReplies: [reply(itemJson({ severity: "WATCH", score: 3 }))] });
    await runSweep(deps);
    expect(posts).toEqual([]);
  });

  it("is not posted in a dry run (only logged)", async () => {
    const { deps } = setup();
    const post = vi.fn();
    await runSweep({ ...deps, post: null });
    expect(post).not.toHaveBeenCalled();
  });
});

describe("failed alerts are retried", () => {
  it("restores the previous state when no alert for the partner reached Slack, so the next run retries", async () => {
    const { deps, store } = setup();
    const failingPost = vi.fn().mockRejectedValue(new Error("not_in_channel"));
    const first = await runSweep({ ...deps, post: failingPost });
    expect(first).toMatchObject({ alertsPosted: 0, alertFailures: 1 });
    const state = await store.getJson<PartnerState>(partnerStateKey("p1"));
    expect(state?.lastMessageAt).toBeNull(); // not advanced
    expect(state?.items).toEqual([]);

    const second = await runSweep({ ...deps, now: new Date("2026-10-05T18:00:00Z") }); // Slack now works
    expect(second).toMatchObject({ alertsPosted: 1, alertFailures: 0 });
  });

  it("keeps the new state when at least one alert posted (retrying would duplicate it)", async () => {
    const { deps, store } = setup({ llmReplies: [reply(itemJson(), itemJson({ headline: "Second fire" }))] });
    const post = vi
      .fn()
      .mockResolvedValueOnce({ channel: "CALERT", ts: "1.1" })
      .mockRejectedValueOnce(new Error("rate_limited"));
    await runSweep({ ...deps, post });
    const state = await store.getJson<PartnerState>(partnerStateKey("p1"));
    expect(state?.items).toHaveLength(2);
    expect(state?.lastMessageAt).toBe("2026-10-05T14:00:00Z");
  });

  it("a dry run (via the dry-run store) leaves state untouched so a live run still alerts", async () => {
    const { createDryRunStore } = await import("../agent/lib/store");
    const { deps, store, post } = setup();
    const dry = await runSweep({ ...deps, store: createDryRunStore(store), post: null });
    expect(dry.withNewEmails).toBe(1);
    expect(await store.getJson(partnerStateKey("p1"))).toBeNull();
    const live = await runSweep({ ...deps, now: new Date("2026-10-05T18:00:00Z") });
    expect(live.alertsPosted).toBe(1);
    expect(post).toHaveBeenCalledTimes(2); // the alert + the legend
  });
});

describe("feedback loop", () => {
  it("injects reviewed alerts into the next prompt, and not before any review exists", async () => {
    const { deps, prompts, store } = setup({
      llmReplies: [reply(itemJson()), reply(itemJson({ headline: "Different issue" }))],
    });
    await runSweep(deps);
    expect(prompts[0]).not.toContain("TEAM FEEDBACK CALIBRATION");

    await applyReaction(store, { channel: "CALERT", ts: "1001.0001", user: "U9", reaction: "-1", added: true, at: 50 });

    const vit = fakeVitally({
      acct1: [
        conversation("conv9", "Another", "2026-10-05T19:00:00Z", [inbound("2026-10-05T19:00:00Z", "something new")]),
      ],
    });
    await runSweep({ ...deps, vitally: vit, now: new Date("2026-10-05T20:00:00Z") });
    expect(prompts[1]).toContain("TEAM FEEDBACK CALIBRATION");
    expect(prompts[1]).toContain("FALSE ALARM");
    expect(prompts[1]).toContain("Registration blocked in Prod");
  });

  it("ignores a white_check_mark reaction (there is no resolved emoji)", async () => {
    const { deps, store } = setup();
    await runSweep(deps);
    await applyReaction(store, {
      channel: "CALERT",
      ts: "1001.0001",
      user: "U9",
      reaction: "white_check_mark",
      added: true,
      at: 60,
    });
    const state = (await store.getJson<PartnerState>(partnerStateKey("p1")))!;
    expect(state.items).toHaveLength(1); // item is still tracked
    const record = (await store.getJson<AlertRecord>(alertKey("CALERT", "1001.0001")))!;
    expect(record.reactions).toEqual({});
  });

  it("still triages when the feedback store cannot be read", async () => {
    const { deps, store, posts } = setup();
    const broken = { ...store, listKeys: async () => { throw new Error("blob down"); } };
    const summary = await runSweep({ ...deps, store: broken as any });
    expect(summary.alertsPosted).toBe(1);
    expect(posts).toHaveLength(2); // the alert + the legend
  });
});

async function* boom(): AsyncGenerator<never> {
  throw new Error("vitally exploded");
}
