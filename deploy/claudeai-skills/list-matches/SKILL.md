---
name: list-matches
description: Show recent project-pilot matches from the scan feed with score, title, company and listing id. Use on "was gibt es neues", "zeig die letzten matches", "gab es heute matches?", "/list-matches".
---

# list-matches

Shows the recent matches project-pilot's scanner stored, via
`project_pilot_list_matches`. Read-only.

## 1. Fetch

Call `project_pilot_list_matches`. Honor a requested filter when the user
gives one (count, "nur heute", a minimum score) by filtering the result -
do not invent entries and do not pad short lists.

## 2. Render

One line per match, newest first:

```
⭐ 95 · Backend/REST-API Developer (Docker/Microservices) · One Day Ahead GmbH · Listing 42
⭐ 87 · Senior Backend Entwickler (Node.js) · Firma unbekannt · Listing 38
```

- No matches in the window: say so in one line - no filler.
- Always include the `listing_id`; it is the handle every follow-up needs.

## 3. Offer the next step

Close with one short line: a listing can be inspected (`check-project`,
already-stored listings just need the id) or drafted from
(`write-application`). Do not start either unasked.

If the MCP tools are absent, say the connector is missing - there is no
local feed to fall back to.
