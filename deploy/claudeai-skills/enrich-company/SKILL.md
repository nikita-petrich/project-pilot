---
name: enrich-company
description: Find contact data (e-mails, phone numbers, contact persons, research links) for a company or a stored listing via project-pilot's enrichment. Use on "finde kontaktdaten", "wer ist der ansprechpartner", "such mir die mail der firma", "/enrich-company".
---

# enrich-company

Looks up contact data for a company through `project_pilot_enrich_company`,
which runs project-pilot's website enrichment and stores every lookup as an
append-only `contact_leads` record.

## 1. Identify the target

- **Company name** given → `project_pilot_enrich_company(company="...")`.
- **Listing id or a stored listing** → `project_pilot_enrich_company(listing_id=N)`,
  so the lead is linked to the listing.
- Neither → ask which company or listing to enrich.

## 2. Report

Present what came back, compactly:

```
🏢 <Company>
✉️  e-mails found (best first)
📞 phones
👤 persons / roles
🔗 research links worth opening
```

- Empty result: say so plainly and suggest the research links for a manual
  look. Do not scrape further on your own or invent contacts.
- Mention in one line that the lookup is stored (append-only), so repeated
  runs are cheap to compare.

## 3. Hand off, don't act

If the user wants to use a found address for an application, point to
`set_recipient` via the send flow (`send-application`). This skill only finds
and reports - it never sets recipients and never sends.

If the MCP tools are absent or the enrichment feature is disabled
(`ENRICHMENT_ENABLED` off), say exactly that instead of improvising a web
search.
