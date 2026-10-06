import { describe, expect, it, vi } from "vitest";
import { fetchPartnerRegistry } from "../agent/lib/registry";

const ok = (body: unknown) => vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })) as unknown as typeof fetch;

describe("fetchPartnerRegistry", () => {
  it("calls the internal endpoint with the bearer secret and returns the partners", async () => {
    const fetchImpl = ok({ partners: [{ partnerId: "p1", name: "Acme", vitallyAccountId: "v1" }] });
    const partners = await fetchPartnerRegistry("https://ops.example.com/", "s3cret", fetchImpl);
    expect(partners).toEqual([{ partnerId: "p1", name: "Acme", vitallyAccountId: "v1" }]);
    const [url, init] = (fetchImpl as any).mock.calls[0];
    expect(url).toBe("https://ops.example.com/api/internal/partner-registry");
    expect(init.headers.Authorization).toBe("Bearer s3cret");
  });
  it("throws (rather than shrinking coverage) on a non-200", async () => {
    const fetchImpl = vi.fn(async () => new Response("nope", { status: 401 })) as unknown as typeof fetch;
    await expect(fetchPartnerRegistry("https://x", "s", fetchImpl)).rejects.toThrow(/401/);
  });
  it("throws on an empty registry and on missing config", async () => {
    await expect(fetchPartnerRegistry("https://x", "s", ok({ partners: [] }))).rejects.toThrow(/no partners/);
    await expect(fetchPartnerRegistry("", "s", ok({}))).rejects.toThrow(/PRODUCT_OPS_BASE_URL/);
    await expect(fetchPartnerRegistry("https://x", "", ok({}))).rejects.toThrow(/CRON_SECRET/);
  });
});
