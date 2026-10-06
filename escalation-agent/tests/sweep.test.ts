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
    expect(posts).toHaveLength(1);
    expect(posts[0]).toContain(":fire: *Live Fire* \u2014 *Acme University*: Registration blocked in Prod");
    expect(posts[0]).toContain("https://stellic.vitally.io/conversations/active/conv1|source");
    expect(posts[0]).toContain("> Students cannot register and the deadline passed");
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
    const smolder = itemJson({ severity: "SMOLDERING", score: 4 });
    const { deps, post } = setup({ llmReplies: [reply(watch), reply(smolder), reply(smolder)] });
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
    expect(post).toHaveBeenCalledTimes(1);

    const vit2 = fakeVitally({
      acct1: [
        conversation("conv1", "Registration down", "2026-10-05T19:00:00Z", [
          inbound("2026-10-05T19:00:00Z", "Still broken"),
        ]),
      ],
    });
    await runSweep({ ...deps, vitally: vit2, now: new Date("2026-10-05T20:00:00Z") });
    expect(post).toHaveBeenCalledTimes(1); // same severity -> no re-alert
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

  it("drops a resolved item from what the model sees and from state", async () => {
    const { deps, prompts, store } = setup({ llmReplies: [reply(itemJson()), reply()] });
    await runSweep(deps);
    await applyReaction(store, {
      channel: "CALERT",
      ts: "1001.0001",
      user: "U9",
      reaction: "white_check_mark",
      added: true,
      at: 60,
    });
    const afterReaction = (await store.getJson<PartnerState>(partnerStateKey("p1")))!;
    expect(afterReaction.items).toEqual([]); // dropped immediately by the reaction handler

    const vit = fakeVitally({
      acct1: [conversation("conv9", "Another", "2026-10-05T19:00:00Z", [inbound("2026-10-05T19:00:00Z", "fyi")])],
    });
    await runSweep({ ...deps, vitally: vit, now: new Date("2026-10-05T20:00:00Z") });
    const secondPrompt = prompts[1]!;
    const tracked = secondPrompt.slice(secondPrompt.indexOf("PREVIOUSLY TRACKED ITEMS"), secondPrompt.indexOf("NEW EMAILS FOR"));
    expect(tracked).not.toContain("Registration blocked in Prod");
  });

  it("still triages when the feedback store cannot be read", async () => {
    const { deps, store, posts } = setup();
    const broken = { ...store, listKeys: async () => { throw new Error("blob down"); } };
    const summary = await runSweep({ ...deps, store: broken as any });
    expect(summary.alertsPosted).toBe(1);
    expect(posts).toHaveLength(1);
  });
});

async function* boom(): AsyncGenerator<never> {
  throw new Error("vitally exploded");
}
