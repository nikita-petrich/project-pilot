---
name: write-application
description: Draft a personalized German or English freelance application (subject, body, LinkedIn message) for a project listing, using Nik's profile and project-pilot's own bid-writing style guide. Use when the user asks for an application, cover letter, Bewerbung, Anschreiben, or LinkedIn outreach for a project - also on "bewirb dich", "schreib die Bewerbung", "/write-application". Drafts only; never sends.
---

# write-application (claude.ai account variant)

Drafts one application for one listing. This is the account-synced variant: it
works in any chat or Code session because the writing rules do not live here -
they live in the project-pilot MCP server (`project_pilot_*` tools), whose
draft pipeline holds the canonical style guide and profile. In a session where
the project-pilot repository is checked out, the repo's own `write-application`
skill takes precedence over this one; both follow the same flow.

## Hard rule: draft only, never send

This skill produces text. It does **not** send, and does not offer to.

- Never call a mail, SMTP, Gmail, or other delivery tool.
- Never offer sending as a next step, and never ask for permission to send.
- Asked to send anyway ("schick sie ab", "mail das raus"): say that sending is
  not this skill's job, and that Nik sends it himself from the draft.

This is an architectural invariant, not a preference. The agent reads untrusted
third-party listing text and holds Nik's profile data - so it must not also
hold the outbound channel. `project_pilot_send_application` is Nik's call, made
in his own words, never offered by this skill.

## 1. Get the listing, in whatever form it arrives

| Form | What to do |
|---|---|
| **Listing id, or a stored listing's URL** | `project_pilot_get_listing` for the facts |
| **Other URL** | fetch with a web-fetch tool; otherwise ask for the text |
| **Pasted text, recruiter mail** | use it directly |
| **PDF** | read it and use its text |
| **Screenshot or photo** | read it as an image and transcribe the listing text, then draft from that |
| **Already checked in this chat** | reuse it; do not re-fetch |

**Too thin to write from** (a single line, no tasks, no stack): say what is
missing and ask, rather than inventing a project.

If `check-project` returned `no_match` for this listing earlier in the chat,
say so and ask whether to draft anyway before writing.

## 2. Store it, then draft through the pipeline

Draft through the MCP tools - that draft is persisted, revisable, and the only
one Nik can actually send:

1. Listing not stored yet → `project_pilot_ingest_listing(text, origin, …)`
   first, with the `origin` that applies (`chat`, `mail`, `pdf`, `image`,
   `url`, `api`). It returns the `listing_id`; re-ingesting the same text or
   URL returns the existing one rather than a duplicate.
2. `project_pilot_draft_application(listing_id)`.
3. Changes → `project_pilot_revise_application(application_id, instruction)`.
4. Recipient → `project_pilot_set_recipient(application_id, email)`.

Say in one line which path you took (`via MCP, application_id N`).

### The recipient is part of the job, not a leftover

A draft with no recipient sits on `awaiting_email` and cannot be sent, so
finding the address is this flow's work and not Nik's. Unless the contact data
is already in this chat (the `enrich-company` skill ran, or the address stands
in the listing text):

1. `project_pilot_enrich_company(listing_id)` — read-only research, sends
   nothing. Failing → say so in one line and carry on drafting; a draft without
   a recipient still beats no draft.
2. Set the **first** address of the list with `project_pilot_set_recipient`, and
   say in one line which one and where it came from. The list is ordered: the
   contact person's own address if one was found, otherwise the company page's.
   Each e-mail carries a `source`:
   - `freelancermap` — the agency published it on their own company page. Take it.
   - `web` — the Impressum crawl found it. Take it too, but say it is our own
     find, so Nik can glance at it before the send he has to confirm anyway.
   Nothing usable → leave the recipient unset and name it as the open point.
3. **An address that belongs to someone other than the listing's contact is
   expected, not a problem.** An agency posts under a named contact but receives
   mail in a colleague's or a shared mailbox and forwards it internally. So the
   salutation stays with the contact person named in the listing, the draft goes to
   the best address found, and the recipient line says both:
   `Empfänger: <address> (<source>) — Anrede an <contact> aus dem Inserat, <company> leitet intern weiter.`
   Never list this as an open point, and never suggest re-addressing the letter to
   whoever owns the mailbox. (Pattern L1 in the repo's `blueprint/context/learnings.md`.)

## 3. If the MCP tools are absent or erroring

- **The project-pilot repository is checked out in this session**: follow the
  repo's `write-application` skill instead - it carries the full local drafting
  path against the canonical style guide and profile files.
- **Neither MCP nor repo available**: say so plainly. Without the connector
  there is no profile and no style guide - do not improvise an application from
  memory. Name the fix: enable the project-pilot connector for this chat, or
  retry.

## 4. Output

Show the draft's fields, same names as the pipeline's structured output. Subject,
body and LinkedIn message go **each in its own fenced code block**, so the chat's
copy button takes exactly that text and nothing else:

- **project_title** - one line, role plus defining focus.
- **subject** - one line, no "Betreff:" prefix, in a code block.
- **body** - the complete application as plain text, salutation through
  signature and confidentiality note, in a code block. No markdown, no
  commentary inside it.
- **linkedin_message** - max 300 characters, booking link included, in a code
  block. Print the LinkedIn people search (`linkedin_search` from the draft)
  **directly below** it, as a markdown link named after the contact person: a
  connection note is useless without the profile it goes to, and that search is
  otherwise a manual step every single time.

Below the output, one short line offering a revision. Nothing about sending.
