# Plan: more robust Linear-link detection for "Ticket Type"

Status: **not implemented** (saved for later). Context: the Open Tickets table's
"Ticket Type" column shows the Linear **Issue Type** label of the Linear issue(s)
linked to an Intercom ticket (`product_status/support_report.py`:
`_linear_issue_ids`, `_issue_types_for`, `_ticket_type_label`).

## Today
Linked issues are read from Intercom conversation notes that *start with* the
Linear integration's wording (`Issue <a href=".../issue/PLAN-1/..">PLAN-1</a> was
linked...`, status moves, `<b>Name</b> commented on <a ...>`). It works, but it
depends on the integration's message wording.

## What was verified (Oct 7, 2026)
- Our Intercom client pins `Intercom-Version: 2.11` (`INTERCOM_API_VERSION`).
  At that version parts have **no** `app_package_code`.
- At `2.13` (and `Unstable`) every part carries `app_package_code`, and the
  Linear integration's parts are tagged `linear-8uw0` (notes,
  `note_and_unsnooze`, and `open` parts).
- On sampled tickets, identifiers found via `linear-8uw0` parts matched the
  note-regex results exactly (e.g. PLAN-6290/PLAN-6380, PLAT-11770/A11Y-1215).
- `GET /tickets/{id}` returns `ticket_parts` (same data as `conversation_parts`);
  `linked_objects` is empty/unhelpful for this purpose.

## Proposed changes
1. **Primary signal:** a part with `app_package_code == "linear-8uw0"`. More
   robust than matching note wording and also covers the integration's `open`
   parts.
2. **Per-call API version:** send `Intercom-Version: 2.13` only on the
   conversation-parts fetch used here (`_collect_stellic_responses`), not
   globally, so other fields the report uses can't shift.
3. **Identifier extraction:** keep reading the ID from the body URL
   `linear.app/<workspace>/issue/<ID>`; the app code tells us *who wrote the
   part*, the URL tells us *which issue*.
4. **Narrow fallback** when no `linear-8uw0` parts exist: scan internal notes
   (and ticket attributes) for `linear.app/.../issue/<ID>` URLs.
   - Do **not** match bare `TEAM-1234` patterns (false positives such as
     `SEN-1935` in a customer's title, or "Related: PLAN-6290" from the
     Product Knowledge Triage Agent).
   - Skip notes authored by the triage agent ("[Product Knowledge Triage Agent]").
   - Do **not** scan customer-visible comments (customers can paste Linear URLs
     that nobody linked).
5. **Unlink handling:** keep dropping an issue when a later integration note says
   it was unlinked/removed. No such note has been observed yet, so verify the
   wording/app-code behaviour when one is found.

## Implementation notes
- Bump `SUPPORT_REPORT_CACHE_VERSION`.
- The per-ticket cache entry (`dashboard-support-report-replies`, key `linear`)
  would need a schema marker so existing entries get re-derived (they hold
  identifiers extracted by the old rule). Cold refresh re-reads every ticket's
  parts; open tickets are fetched first and the 235s deadline means it fills in
  over several refreshes ("Loading..." until then).
- Test by comparing old-rule vs new-rule identifiers across all open Key User
  tickets before switching; investigate any difference.
