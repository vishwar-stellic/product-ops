export interface TriagePartner {
  partnerId: string;
  name: string;
  vitallyAccountId: string;
}

/**
 * The partner set the Python escalation triage covers, served by the
 * dashboard's `GET /api/internal/partner-registry` (same registry + same
 * "has a Vitally account" filter). Throws on any failure - the sweep aborts
 * rather than falling back to a different partner list, so the side-by-side
 * comparison stays honest.
 */
export async function fetchPartnerRegistry(
  baseUrl = process.env.PRODUCT_OPS_BASE_URL,
  secret = process.env.CRON_SECRET,
  fetchImpl: typeof fetch = fetch,
): Promise<TriagePartner[]> {
  if (!baseUrl) throw new Error("PRODUCT_OPS_BASE_URL is not set - see .env.example");
  if (!secret) throw new Error("CRON_SECRET is not set - see .env.example");
  const url = `${baseUrl.replace(/\/+$/, "")}/api/internal/partner-registry`;
  const response = await fetchImpl(url, {
    headers: { Authorization: `Bearer ${secret}` },
    signal: AbortSignal.timeout(120_000),
  });
  if (!response.ok) {
    throw new Error(`Partner registry request failed: ${response.status} ${(await response.text()).slice(0, 200)}`);
  }
  const payload = (await response.json()) as { partners?: TriagePartner[] };
  const partners = (payload.partners ?? []).filter((p) => p.partnerId && p.vitallyAccountId);
  if (partners.length === 0) throw new Error("Partner registry returned no partners");
  return partners;
}
