---
name: match-card
description: Show the full project-pilot overview card for one stored listing - every fact, the stored verdict with score and threshold, and the LinkedIn and Impressum research links. Use when the user names a listing id and wants to see it, asks "zeig mir Listing 447", "was ist das für ein Projekt", "Karte zu 447", or opens a match chat from the Telegram alert - also on "/match-card".
---

# match-card

Prints the overview card for one stored listing, so a chat starts on the same
picture the Telegram alert showed.

**This skill owns no layout of its own.** The card is rendered by project-pilot
and handed over finished by `project_pilot_match_card`. Print what comes back,
verbatim. Never rebuild it from `project_pilot_get_listing` fields and never
re-order or re-style it - that is what keeps this skill and the alert from
drifting into two different cards.

## 1. Get the listing id

| What the user gives you | What to do |
|---|---|
| **A number** (`/match-card 447`, "zeig mir 447") | that is the listing id |
| **A project-pilot URL or a card** naming `listing_id` | read the id off it |
| **Nothing, or a title** | `project_pilot_list_matches` and ask which one, unless exactly one matches |
| **A listing that is not stored** | there is no card to fetch; say so and offer `check-project`, which handles pasted text |

## 2. Fetch and print it

1. `project_pilot_match_card(listing_id)`.
2. Print the `card` field exactly as returned - one block, no preamble, no
   summary of it afterwards, no commentary on the score.
3. Tool missing or erroring: say so in one line and stop. Do not assemble a
   substitute card from other tools; a card that differs from the alert's is
   worse than no card.

## 3. What the card already contains

So you do not repeat any of it in your own words:

- Headline with score, role and company
- Every listing fact: company, contact, client type, location, remote share,
  contract, workload, duration, start, posted, apply-by, industry, language, skills
- The stored verdict: score, what fits, matching skills, gaps, risk flags
- The listing link, the database coordinates, the match threshold, the posting
  time in Berlin, and the LinkedIn company / LinkedIn person / Impressum searches

A missing contact person shows as `K.A.` rather than a dropped line - an ad that
names nobody is itself worth seeing.

## 4. Afterwards

One short line offering the obvious next step - the application draft
(`write-application`) for a match, contact research (`enrich-company`) when the
recipient is unknown. Describe the step in plain words, not by tool or function
name. Nothing is sent from here, and sending is never offered.
