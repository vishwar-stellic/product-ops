import { describe, expect, it, vi } from "vitest";
import { decodeHtmlEntities, stripHtml } from "../agent/lib/filters";
import { fetchPartnerRegistry } from "../agent/lib/registry";
import { formatSlackMessage, slackEscape } from "../agent/lib/slack";
import type { TrackedItem } from "../agent/lib/triage";

describe("decodeHtmlEntities / stripHtml", () => {
  it("decodes named, decimal and hex entities and leaves unknown ones alone", () => {
    expect(decodeHtmlEntities("Missouri S&amp;T")).toBe("Missouri S&T");
    expect(decodeHtmlEntities("St. Mary&#39;s &amp; Co &#x27;x&#x27;")).toBe("St. Mary's & Co 'x'");
    expect(decodeHtmlEntities("a&nbsp;b &rsquo;")).toBe("a b \u2019");
    expect(decodeHtmlEntities("&bogus; &#99999999999;")).toBe("&bogus; &#99999999999;");
  });
  it("strips tags before decoding so escaped markup stays text", () => {
    expect(stripHtml("<p>Don&#39;t &lt;b&gt;panic&lt;/b&gt;</p>")).toBe("Don't <b>panic</b>");
  });
});

describe("slackEscape", () => {
  it("escapes the three characters Slack requires", () => {
    expect(slackEscape("S&T <!channel> <@U1> a > b")).toBe("S&amp;T &lt;!channel&gt; &lt;@U1&gt; a &gt; b");
  });
});

const item = (over: Partial<TrackedItem> = {}): TrackedItem =>
  ({
    headline: "Registration <!here> broken & slow",
    severity: "SMOLDERING",
    evidence: [{ quote: "ping <!channel> please & thanks" }],
    vitallyConversationId: "c-1",
    ...over,
  }) as TrackedItem;

describe("formatSlackMessage escaping", () => {
  it("escapes partner name, headline and quote but keeps the source link intact", () => {
    vi.stubEnv("VITALLY_APP_SUBDOMAIN", "stellic");
    const text = formatSlackMessage(item(), { partnerName: "Missouri S&T", vitallyAccountUrl: null });
    expect(text).toContain("*Missouri S&amp;T*");
    expect(text).toContain("Registration &lt;!here&gt; broken &amp; slow");
    expect(text).toContain("> ping &lt;!channel&gt; please &amp; thanks");
    expect(text).not.toContain("<!channel>");
    expect(text).not.toContain("<!here>");
    expect(text).toMatch(/\(<https:\/\/[^|>]+\|source>\)/);
  });
});

describe("fetchPartnerRegistry names", () => {
  it("decodes HTML-escaped partner names", async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            partners: [
              { partnerId: "p1", name: "Missouri S&amp;T", vitallyAccountId: "v1" },
              { partnerId: "p2", name: "St. Mary&#39;s College of Maryland", vitallyAccountId: "v2" },
            ],
          }),
          { status: 200 },
        ),
    ) as unknown as typeof fetch;
    const partners = await fetchPartnerRegistry("https://x", "s", fetchImpl);
    expect(partners.map((p) => p.name)).toEqual(["Missouri S&T", "St. Mary's College of Maryland"]);
  });
});
