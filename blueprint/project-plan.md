# Project Plan

> One of the two planning docs you provide. Answer each section in a line or two
> (a worksheet, not an essay). Draft it yourself or let the AI help you expand and
> sharpen it; either way, the content is yours to direct. When it's filled in, run
> `/overview` to generate the project overview from this plus `build-plan.md`.

## 1. Problem - What problem are we solving?

Applying to freelance projects and positions (sequenz.io) is a slow, repetitive
manual cycle: watch several platforms, judge fit, research the contact, write a
tailored cover letter, and send across channels. Project Pilot automates the full
loop from an incoming lead to a sent, personalized application, with a
human-in-the-loop gate via Telegram for anything that shouldn't run fully
automatically.

## 2. Users - Who is this for?

A single power user (Nik) as a personal tool. Not multi-user SaaS in the MVP. The
operator watches and approves via Telegram; the system does the sourcing,
matching, writing, and sending.

## 3. Features - What does the MVP need?

- Sourcing: collect leads from freelancermap.de, freelance.de, Malt (IMAP listener
  on notification emails), and recruiter emails
- Filter agent: coarse relevance filter (stack, remote/location, contract type, rate)
- Research agent: match lead against profile (score + justification) and scrape
  contact details (email, phone, contact person)
- Categorization: autonomous vs. approval-required, on configurable criteria
- Telegram control center: approval notifications, in-chat approval, per-application
  threads, thread commands
- Application agent: generate German cover letter (template system) plus a short
  LinkedIn connect message, aligned to the same match
- Sending: email to identified contact and LinkedIn connect request, in parallel
  when both are available (neither is a prerequisite for the other)
- Notion sync: write each application into the existing Sales Pipeline (client,
  status, channel, date, contact data)
- Interview prep (on demand): triggered only by explicit Telegram thread command;
  returns a short company/project summary and prepared questions

## 4. Data - What are we storing?

- **Project/Position** - source, contract type (freelance/permanent), access path
  (recruiter/direct/Malt), status (new -> filtered -> researched -> [awaiting
  approval] -> applied -> interview prep requested -> closed), match score +
  justification, category (autonomous/approval-required), title, description, link
- **Contact** - name, role, email, phone, source of discovery
- **Application** - cover letter text, LinkedIn connect text, sending channels,
  sent date
- **Telegram Thread** - linked to project, message history, triggered commands

## 5. Tech - What stack are we using?

- Language: TypeScript
- Agents: Claude Agent SDK, 3-stage pipeline (Filter -> Research -> Application)
- Scraping: Apify (for platforms without an API/feed)
- Malt: IMAP listener on notification emails
- Control/notifications: Telegram bot, human-in-the-loop approval
- Data storage: PostgreSQL (Neon)
- CRM: Notion (Sales Pipeline sync) - in MVP scope
- Orchestration/queue: OPEN - pg-boss vs. BullMQ vs. Inngest (no n8n)

## 6. Monetize - How will this make money?

Not a commercial product. Internal personal tool to win freelance projects and
positions faster; the return is landed engagements, not direct revenue. Full SaaS
multi-user is explicitly out of MVP scope.

## 7. UI/UX - How should this look and feel?

No traditional UI. Telegram is the entire operator surface: concise notifications
(project, client, match score, source) with an approval action, one thread per
application, and explicit thread commands (e.g. start interview research) that
never fire automatically. Feel: fast, low-noise, decisive - surface only what
needs a human, keep autonomous flows silent.

## Open questions (to resolve during build)

- Concrete score thresholds for autonomous vs. approval-required
- Finalize queue/job technology (pg-boss / BullMQ / Inngest)
- LinkedIn connect automation: rate limits and account-risk mitigation
- Feedback loop: how outcomes (reply/no reply, interview yes/no) feed back into matching

## Out of MVP scope (later)

- Automated follow-up on no response
- Learning-based matching from success rate
- Full SaaS multi-user capability
