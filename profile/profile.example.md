# Profile (LLM matching & applications) — example template

> Copy this file to `profile/profile.md` and fill it with your real data. The
> committed `profile.md` holds the maintainer's own profile, since this repo is a
> public portfolio piece; if you fork it, replace that file with yours. Real secrets
> never belong here — they live in `.env`, which is gitignored.
>
> `profile.md` is the single source for BOTH the matcher and the application generator:
> - The matcher (stage 3) judges fit from positioning, skills, and no-gos.
> - The application generator pulls reference projects, skills, and the signature from here.

## Who I am

<One or two paragraphs: role, focus, experience, industries, working style.>

## Availability & terms

- **Location:** <remote / city, max. X days on-site>
- **Available:** <from when · full-time/part-time>
- **Experience:** <years>
- **Rate:** <on request / range>
- **Qualification:** <degree/certification>
- **Languages:** <e.g. German (native), English (B2)>
- **Working style:** <keywords>

## Focus

<Core topics as keywords, e.g. LLM integration · RAG · TypeScript · ...>

## Core skills / full tech stack

> In applications, only name technologies that appear *both* in the listing
> *and* here.

- **AI & LLM:** <...>
- **Backend & languages:** <...>
- **Frontend:** <...>
- **Databases & data:** <...>
- **DevOps, cloud & infrastructure:** <...>
- **Auth & security:** <...>
- **Architecture & principles:** <...>
- **Testing & QA:** <...>

## Desired projects

- <Which projects/roles you are looking for>

## No-gos

> Context decides — migrating away from a technology, or a role at a different
> layer, can still fit. Match on the role, not on the keyword.

- **<technology>** — <condition/exception>

## Reference projects

> For picking references in applications (max. 4 by relevance, then chronologically).

### 01 · <project name> — <short description>
- **Role:** <...>
- **Industry:** <...>
- **Period:** <MM/YYYY – MM/YYYY>
- **Location/team:** <location · remote/on-site · team size · website/code optional>
- **Responsibilities:** <2–5 core responsibilities>
- **Outcome:** <measurable results / impact>
- **Technologies:** <tech list>

<further projects …>

## Contact & Signature

> Value source for the application's signature block. The layout itself (order,
> blank lines, labels, DE/EN) lives in the prompt under "Grußformel und Signatur" —
> only the values belong here, in any order.
>
> The prompt looks these keys up by name, so keep the labels as written: renaming
> one here means renaming it in
> `src/project_pilot/application/prompts/application.md` too.
>
> `Location German` is used in German applications, `Location English` in English
> ones; a missing line (e.g. `Location German` or `VAT ID`) is simply dropped from
> the signature block.
>
> The last two lines are the Notion Calendar booking links for the free intro call —
> one for German and one for English applications. The application generator picks
> the link matching the application language and places it in the closing sentence,
> the signature block, and the LinkedIn message.

<First Last>
<Title>
Email: <mail@example.com>
Phone: <+49 ...>
Web: <https://example.com>
LinkedIn: <https://linkedin.com/in/...>
GitHub: <https://github.com/...>
Location German: <City, Country in German>
Location English: <City, Country in English>
VAT ID: <DE123456789>
CTA German: <https://calendar.notion.so/meet/<handle>/erstgespraech-30-min>
CTA English: <https://calendar.notion.so/meet/<handle>/initial-consultation-30-min>
