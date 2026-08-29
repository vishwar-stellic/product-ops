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
"""

import re
from typing import Any, Dict, List, Optional

from .intercom_client import IntercomClient
from .linear_client import LinearClient

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


def build_partner_registry(
    intercom_client: IntercomClient,
    linear_client: LinearClient,
) -> List[Dict[str, Any]]:
    """The canonical partner list `partner_insights.py` is keyed on: one
    entry per real institution, cross-referencing Intercom's company list
    (the same curated source `support_report.py` uses) against Linear's
    `Customer` objects.

    Matching, in priority order:
      1. A Linear Customer's `externalIds` containing the Intercom
         `company_id` short code - see module docstring.
      2. A normalized exact name match, for customers created by hand
         rather than through whatever populated `externalIds`.

    Every Intercom company is included even with no Linear match (Support
    score only, no Product score). Linear customers with no Intercom match
    are included too, but only if they carry real signal (an external id,
    a domain, or at least one linked customer request) - a customer record
    with none of those is almost certainly a stray/test entry rather than
    an actual partner, so those are the only thing this leaves out
    entirely (see live data: e.g. bare "uw"/"uwp" duplicates with no
    domain, need count, or external id).
    """
    companies = list(intercom_client.list_companies())
    customers = list_linear_customers(linear_client)

    customers_by_external_id: Dict[str, Dict[str, Any]] = {}
    customers_by_norm_name: Dict[str, Dict[str, Any]] = {}
    for customer in customers:
        for external_id in customer.get("externalIds") or []:
            if external_id:
                customers_by_external_id.setdefault(str(external_id).strip().lower(), customer)
        norm = _normalize_name(customer.get("name"))
        if norm:
            customers_by_norm_name.setdefault(norm, customer)

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
        registry.append(
            {
                "partnerId": f"intercom:{intercom_id}",
                "name": name,
                "intercomCompanyId": company_id or None,
                "linearCustomerId": linear_customer["id"] if linear_customer else None,
                "matched": linear_customer is not None,
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
        registry.append(
            {
                "partnerId": f"linear:{customer['id']}",
                "name": customer.get("name") or "(unnamed customer)",
                "intercomCompanyId": None,
                "linearCustomerId": customer["id"],
                "matched": False,
            }
        )

    registry.sort(key=lambda p: p["name"].lower())
    return registry
