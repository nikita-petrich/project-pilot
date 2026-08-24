---
name: check-project
description: Judge whether a single freelance project listing is a genuine match for Nik's profile, using project-pilot's own scan rules. Use when the user pastes or uploads a project description, recruiter mail, or freelancermap listing and asks whether it fits, is worth applying to, or should be checked - also on "prüf mal", "passt das?", "/check-project".
---

# check-project (claude.ai account variant)

Reproduces project-pilot's scan verdict for one listing. This is the
account-synced variant: it works in any chat or Code session because the
judgment does not live here - it lives in the project-pilot MCP server
(`project_pilot_*` tools), which runs the real scan code against the real
profile. In a session where the project-pilot repository is checked out, the
repo's own `check-project` skill takes precedence over this one; both follow
the same flow.

**This skill owns no judgment rules.** The MCP tools are the authority.

## 1. Get the listing, in whatever form it arrives

Turn each form into listing text first - the judgment is the same afterwards:

| Form | What to do |
|---|---|
| **A listing URL** (any board) | `project_pilot_get_listing` if it is stored; otherwise fetch it with a web-fetch tool. Never guess a listing's content from its URL. |
| **Listing id** (from the feed) | `project_pilot_get_listing` |
| **Pasted text, recruiter mail** | use it directly |
| **PDF** | read it and use its text |
| **Screenshot or photo** | read it as an image and transcribe the listing text yourself, then judge that text. Say in one line that the text came from an image. |

Empty or near-empty input: ask for the description rather than judging nothing.

## 2. Store it, then let the pipeline judge it

1. Not stored yet → `project_pilot_ingest_listing(text, origin, …)` first. Pass
   the `origin` that actually applies - `chat` for something pasted here, `mail`
   for a forwarded recruiter mail, `pdf`, `image` for a transcribed screenshot,
   `url` for a fetched link, `api` for an automation. Add `title`, `url` and
   `company` when you have them, `source` when you know the platform, and `note`
   for anything worth remembering about how it arrived. It returns a
   `listing_id`, and `already_known: true` when this text or URL was stored
   before.
2. Then `project_pilot_check_listing(listing_id)`.
3. Report that verdict, in the step 4 format, naming the `listing_id` so the
   listing can be drafted from and sent later.

Ingest is the default even for a quick question, because a listing that is only
in the chat cannot be applied to, reported on, or found again. Skip it only when
the user explicitly wants a throwaway look ("nur mal kurz gucken, nicht
speichern") - then use `project_pilot_check_text(text)`, which stores nothing.

## 3. If the MCP tools are absent or erroring

- **The project-pilot repository is checked out in this session** (its
  `.claude/skills/check-project/` and `src/project_pilot/evaluation/prompts/`
  exist): follow the repo skill instead - it carries the full local judgment
  path against the canonical prompt and profile files.
- **Neither MCP nor repo available**: say so plainly. Without the connector
  there is no profile and no rules to judge against - do not improvise a
  verdict from memory. Name the fix: enable the project-pilot connector for
  this chat, or retry.

Always say in one line which path you took
(`via MCP, listing_id 42` / `lokal über Repo` / `keine Quelle verfügbar`).

## 4. Output

One compact block. Field names mirror `MatchVerdict` so results stay comparable
across the chat, the scan pipeline, and the eval suite:

```
verdict:              match | no_match
score:                0-100
reasons:              - two to four short lines
matching_skills:      - profile skills the listing explicitly asks for
missing_requirements: - listing requirements the profile does not cover
risk_flags:           - concerns (unclear rate, on-site load, vague scope, ...)
```

Add one line above the block naming the listing. Below the block, one short
plain-language line on what you would do with it. Offer `write-application`
when the verdict is a match; do not draft unasked.
