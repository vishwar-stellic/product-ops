"""Shared partner (institution) identity resolution, used by both
`support_report.py` (Intercom-only, per-conversation) and
`partner_insights.py` (cross-references Intercom with Linear).

## Intercom-side resolution
`partner_name` / `build_company_map` / `DOMAIN_TO_PARTNER` were originally
private to `support_report.py` - see that module's docstring for the full
3-step cascade (`company.name` -> a contact's `external_id` code looked up
against `company_id -> name` -> a manual email-domain map).

## Cross-referencing with Linear
Linear's "Customer Requests" feature (`Customer`/`CustomerNeed` objects -
https://linear.app/developers/managing-customers) is a separate identity
system with no built-in link to Intercom's *our* Intercom workspace
(Linear's own optional Intercom integration might populate one, but this
project doesn't rely on that being installed). `build_partner_registry`
cross-references the two by the same short human-set code Intercom calls
`company_id` (e.g. "fsu") - in practice this shows up verbatim in a Linear
Customer's `externalIds` for institutions whose Linear customer record
was created by pasting in that code (confirmed against live data: Linear
customer names/externalIds like `fsu`, `uwsp`, `tufts` line up exactly with
Intercom's `company_id`s), falling back to a normalized exact name match
for the rest.

Both sides are kept even when unmatched (see `build_partner_registry`)
rather than silently dropping data - `partner_insights.py` shows Product
score only, or Support score only, for a partner it can't fully link.

## Cross-referencing with Vitally
Optional third source (`vitally_client` param - `None` skips it entirely,
e.g. when `VITALLY_ACCESS_TOKEN` isn't configured). Vitally Accounts turned
out to use that exact same short code as their own `externalId` (confirmed
against live data: `uc`, `fsu`, `virginia`, ...), so matching reuses the
identical externalId-then-normalized-name cascade as the Linear side,
independently for each - a partner can be Vitally-matched without being
Linear-matched or vice versa.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .intercom_client import IntercomClient
from .linear_client import LinearClient
from .vitally_client import VitallyClient

# Fallback for when a requester's email domain doesn't obviously map to
# their institution's name (Partner resolution's last resort - see
# `partner_name`). Carried over from the support-sla-dashboard skill's
# manual map.
DOMAIN_TO_PARTNER = {
    "uchicago.edu": "University of Chicago",
    "uc": "University of Chicago",
    "jh.edu": "Johns Hopkins",
    "uon.edu.au": "The University of Newcastle",
    "osu.edu": "The Ohio State University",
    "case.edu": "Case Western Reserve",
    "csc.edu": "Chadron State College",
    "academyart.edu": "Academy of Art University",
}


def build_company_map(client: IntercomClient) -> Dict[str, str]:
    """`company_id` (a short human-set code, e.g. "fsu", "udel") -> company
    name, for every company in the workspace - see `partner_name`."""
    mapping: Dict[str, str] = {}
    for company in client.list_companies():
        company_id = company.get("company_id")
        name = company.get("name")
        if company_id and name:
            mapping[company_id.lower()] = name
    return mapping


def partner_name(conversation: Dict[str, Any], company_map: Dict[str, str]) -> str:
    """Resolve a conversation's institution: `company.name` if present, else
    the partner code embedded in a contact's `external_id` (commonly
    `<user>@<code>`, e.g. `cjp260@newcastle`) looked up against
    `company_map`, else the requester's email domain against
    `DOMAIN_TO_PARTNER`. Unmatched stays "(unknown)" rather than guessing."""
    company = conversation.get("company") or {}
    if company.get("name"):
        return company["name"]

    for contact in ((conversation.get("contacts") or {}).get("contacts")) or []:
        external_id = contact.get("external_id") or ""
        if "@" in external_id:
            code = external_id.rsplit("@", 1)[1].strip().lower()
            if code in company_map:
                return company_map[code]

    email = ((conversation.get("source") or {}).get("author") or {}).get("email") or ""
    if "@" in email:
        domain = email.rsplit("@", 1)[1].strip().lower()
        if domain in DOMAIN_TO_PARTNER:
            return DOMAIN_TO_PARTNER[domain]

    return "(unknown)"


_LINEAR_CUSTOMERS_QUERY = """
query PartnerIdentityCustomers($first: Int!, $after: String) {
  customers(first: $first, after: $after) {
    nodes { id name domains externalIds approximateNeedCount }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def list_linear_customers(client: LinearClient) -> List[Dict[str, Any]]:
    """Every `Customer` in the Linear workspace - id, name, domains,
    externalIds, and a rough count of linked requests (used by
    `build_partner_registry` to decide whether an Intercom-unmatched
    customer is worth surfacing at all, vs. a stray/test record)."""
    return client.paginate(_LINEAR_CUSTOMERS_QUERY, variables={}, path=["customers"], page_size=100)


def _normalize_name(name: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _index_by_external_id_and_name(
    records: List[Dict[str, Any]],
    external_id_field: str,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Shared helper for the Linear-customer and Vitally-account indexes
    below - both get looked up the same way (short code first, normalized
    name as fallback), just with a different field holding the code(s)."""
    by_external_id: Dict[str, Dict[str, Any]] = {}
    by_norm_name: Dict[str, Dict[str, Any]] = {}
    for record in records:
        raw = record.get(external_id_field)
        codes = raw if isinstance(raw, list) else [raw]
        for code in codes:
            if code:
                by_external_id.setdefault(str(code).strip().lower(), record)
        norm = _normalize_name(record.get("name"))
        if norm:
            by_norm_name.setdefault(norm, record)
    return by_external_id, by_norm_name


def build_partner_registry(
    intercom_client: IntercomClient,
    linear_client: LinearClient,
    vitally_client: Optional[VitallyClient] = None,
) -> List[Dict[str, Any]]:
    """The canonical partner list `partner_insights.py` is keyed on: one
    entry per real institution, cross-referencing Intercom's company list
    (the same curated source `support_report.py` uses) against Linear's
    `Customer` objects, and optionally Vitally's `Account` objects (see
    module docstring - `vitally_client=None` just skips that part).

    Matching, in priority order, independently for Linear and Vitally:
      1. The other side's short code (Linear's `externalIds`, Vitally's
         `externalId`) matching the Intercom `company_id` short code - see
         module docstring.
      2. A normalized exact name match, for records created by hand rather
         than through whatever populated the short code.

    Every Intercom company is included even with no Linear/Vitally match
    (Support score only, no Product score/health score). Linear customers
    with no Intercom match are included too, but only if they carry real
    signal (an external id, a domain, or at least one linked customer
    request) - a customer record with none of those is almost certainly a
    stray/test entry rather than an actual partner, so those are the only
    thing this leaves out entirely (see live data: e.g. bare "uw"/"uwp"
    duplicates with no domain, need count, or external id). Vitally
    accounts with no Intercom match aren't separately surfaced as their own
    rows (unlike Linear) - Vitally's `externalId` is 1:1 with the same
    institution set Intercom already covers in this workspace, so there's
    no analogous "signal-bearing but unmatched" case to rescue.
    """
    companies = list(intercom_client.list_companies())
    customers = list_linear_customers(linear_client)
    vitally_accounts = list(vitally_client.list_accounts()) if vitally_client else []

    customers_by_external_id, customers_by_norm_name = _index_by_external_id_and_name(customers, "externalIds")
    vitally_by_external_id, vitally_by_norm_name = _index_by_external_id_and_name(vitally_accounts, "externalId")

    def _find_vitally_account(candidate_codes: List[Optional[str]], name: str) -> Optional[Dict[str, Any]]:
        for code in candidate_codes:
            if code and str(code).strip().lower() in vitally_by_external_id:
                return vitally_by_external_id[str(code).strip().lower()]
        return vitally_by_norm_name.get(_normalize_name(name))

    registry: List[Dict[str, Any]] = []
    matched_customer_ids = set()

    for company in companies:
        company_id = (company.get("company_id") or "").strip().lower()
        intercom_id = company.get("id")
        name = company.get("name") or company.get("company_id") or intercom_id
        if not name or not intercom_id:
            continue
        linear_customer = customers_by_external_id.get(company_id) if company_id else None
        if linear_customer is None:
            linear_customer = customers_by_norm_name.get(_normalize_name(name))
        vitally_account = _find_vitally_account([company_id], name)
        registry.append(
            {
                "partnerId": f"intercom:{intercom_id}",
                "name": name,
                "intercomCompanyId": company_id or None,
                "linearCustomerId": linear_customer["id"] if linear_customer else None,
                "matched": linear_customer is not None,
                "vitallyAccountId": vitally_account["id"] if vitally_account else None,
                "vitallyHealthScore": vitally_account.get("healthScore") if vitally_account else None,
            }
        )
        if linear_customer:
            matched_customer_ids.add(linear_customer["id"])

    for customer in customers:
        if customer["id"] in matched_customer_ids:
            continue
        has_signal = customer.get("externalIds") or customer.get("domains") or customer.get("approximateNeedCount")
        if not has_signal:
            continue
        vitally_account = _find_vitally_account(customer.get("externalIds") or [], customer.get("name") or "")
        registry.append(
            {
                "partnerId": f"linear:{customer['id']}",
                "name": customer.get("name") or "(unnamed customer)",
                "intercomCompanyId": None,
                "linearCustomerId": customer["id"],
                "matched": False,
                "vitallyAccountId": vitally_account["id"] if vitally_account else None,
                "vitallyHealthScore": vitally_account.get("healthScore") if vitally_account else None,
            }
        )

    registry.sort(key=lambda p: p["name"].lower())
    return registry
