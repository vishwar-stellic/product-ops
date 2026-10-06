import { mkdtemp, rm, utimes } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { createFileStore } from "../agent/lib/store";

let dir: string | undefined;
afterEach(async () => {
  if (dir) await rm(dir, { recursive: true, force: true });
  dir = undefined;
});

describe("file store", () => {
  it("round-trips JSON and returns null for missing keys", async () => {
    dir = await mkdtemp(path.join(tmpdir(), "esc-store-"));
    const store = createFileStore(dir);
    expect(await store.getJson("nope.json")).toBeNull();
    await store.putJson("partners/a%2Fb.json", { n: 1 });
    expect(await store.getJson("partners/a%2Fb.json")).toEqual({ n: 1 });
  });

  it("lists keys under a prefix, newest first, honoring the limit", async () => {
    dir = await mkdtemp(path.join(tmpdir(), "esc-store-"));
    const store = createFileStore(dir);
    await store.putJson("alerts/C1/1.json", 1);
    await store.putJson("alerts/C1/2.json", 2);
    await store.putJson("partners/x.json", 3);
    const old = new Date(Date.now() - 60_000);
    await utimes(path.join(dir, "alerts/C1/1.json"), old, old);
    expect(await store.listKeys("alerts/")).toEqual(["alerts/C1/2.json", "alerts/C1/1.json"]);
    expect(await store.listKeys("alerts/", 1)).toEqual(["alerts/C1/2.json"]);
    expect(await store.listKeys("missing/")).toEqual([]);
  });

  it("rejects path traversal", async () => {
    dir = await mkdtemp(path.join(tmpdir(), "esc-store-"));
    const store = createFileStore(dir);
    await expect(store.putJson("../escape.json", 1)).rejects.toThrow(/Invalid store key/);
  });
});
