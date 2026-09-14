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

## 2. Every datum says where it came from

Each entry under `emails`, `phones` and `persons` is an object, not a string:

| `source` | What it means | How far to trust it |
|---|---|---|
| `freelancermap` | the agent stated it on their own company page | a stated contact — use it |
| `web` | our Impressum/contact crawl found it | plausible, wants a look before anything is sent there |

**Always show the source.** An address is only as good as where it came from,
and that is the one thing the user cannot see for themselves.

The list is already in the right order — the contact person's own address first
(wherever it was found), then the company page, then everything else the crawl
found. Do not re-sort it, and do not present a `web` find as if the company had
given it.

`company_page` is the page those stated contacts came from. Show it: it is
worth opening even when the lookup found nothing.

## 3. Report

Present what came back, compactly:

```
🏢 <Company>
🏷 <company_page, when there is one>
✉️  <address> (<source>) — best first
📞 <number> (<source>)
👤 persons / roles
🙋 <linkedin_search> — the contact person on LinkedIn
👥 <links.linkedin_company> — the company on LinkedIn
🔎 <links.google_contact> — Impressum/contact search
✍️  <linkedin_message> — ready to paste
```

- The `linkedin_message` is a connection note, so it never stands alone: print
  `linkedin_search` directly above it. A note without the profile it goes to is
  half an answer, and finding that profile is otherwise a manual step every
  single time.
- Empty result: say so plainly and suggest the research links for a manual
  look. Do not scrape further on your own or invent contacts.
- Mention in one line that the lookup is stored (append-only), so repeated
  runs are cheap to compare.

## 4. Hand off, don't act

If the user wants to use a found address for an application, point to
`set_recipient` via the send flow (`send-application`). This skill only finds
and reports - it never sets recipients and never sends.

Running inside an application flow is the exception that proves the rule: when
`write-application` calls this research itself, that skill sets the recipient
from what came back. It still never sends.

If the MCP tools are absent or the lookup errors, say exactly that instead of
improvising a web search.
