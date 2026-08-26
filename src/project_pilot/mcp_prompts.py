"""The workflow prompts, exposed over MCP so every surface shares one source.

A prompt here is the *procedure* for one job — which tools to call, in which
order, with which guardrails. The judgment itself is not here: the rules live in
``evaluation/prompts/`` and ``application/prompts/`` and are applied by the tools
these prompts call, so a rule changes in exactly one file and every consumer sees
it at once.

MCP prompts are discoverable (``prompts/list``) and invocable by name, which is
what makes one definition serve three surfaces:

* Claude Code lists them as ``/mcp__project-pilot__<name>``
* a bot renders them into its own command menu from ``prompts/list``
* n8n and other automation call them like any other MCP prompt

The bodies stay short on purpose. They tell the model what to do, not what to
think — anything longer belongs in the prompt files behind the tools.
"""

CHECK_PROJECT = """\
Judge whether this project listing is a genuine match, exactly as the scan
pipeline would: {listing}

1. Not stored yet (pasted text, a mail, a PDF, a transcribed screenshot, a
   fetched link)? Call `project_pilot_ingest_listing` first, with the `origin`
   that actually applies (chat, mail, pdf, image, url, api). It returns the
   listing_id. A listing that is only in the chat cannot be applied to,
   reported on, or found again.
   Only for an explicitly throwaway look ("nur kurz gucken, nicht speichern"):
   `project_pilot_check_text`, which stores nothing.
2. Call `project_pilot_check_listing(listing_id)`. That tool runs the real
   rules and the real profile — do not re-judge its verdict from your own
   reading of the listing.
3. Report verdict, score, reasons, matching_skills, missing_requirements and
   risk_flags, naming the listing_id so later steps can use it. Add one plain
   line on what you would do with it, and offer the application draft when it
   is a match. Do not draft unasked.
"""

WRITE_APPLICATION = """\
Draft an application for this listing: {listing}

1. Not stored yet? `project_pilot_ingest_listing` first (see the origin values
   in check_project); it returns the listing_id.
2. `project_pilot_draft_application(listing_id)`. The tool holds the style
   guide and the profile — do not invent facts about the applicant, and do not
   rewrite its output in your own voice.
3. Show project_title, subject, body and linkedin_message, each as its own
   copyable block, and name the application_id.
4. Changes: `project_pilot_revise_application(application_id, instruction)`,
   returning the full corrected draft rather than a diff.

Never send here, and never offer to. Sending is its own deliberate step
(send_application) that the user starts in their own words.
"""

SEND_APPLICATION = """\
Send this drafted application: {application}

Preconditions, all three, before anything else:
1. A draft exists — identify the application_id. Only a listing_id? Drafting
   comes first; say so and stop.
2. A recipient is set. If not, ask for the address and call
   `project_pilot_set_recipient(application_id, email)`. Never guess one.
3. The tools are reachable. There is no fallback route for sending, by design.

Then show a compact summary — recipient, subject, the listing it belongs to —
and ask for an explicit yes to that exact summary. A general "mach mal" from
earlier in the conversation does not count. Only then call
`project_pilot_send_application(application_id)`.

Report the result in one line: sent with its timestamp, or the exact error. On
error, stop; never retry silently and never reach for another delivery route.
"""

ENRICH_COMPANY = """\
Find contact data for: {target}

Call `project_pilot_enrich_company` with the listing_id when the target is a
stored listing, so the lead is linked to it, or with the company name
otherwise. Report e-mails, phones, persons and research links compactly, best
first.

Empty result: say so plainly and point at the research links for a manual look.
Do not search further on your own and do not invent contacts.

This finds and reports only. Using an address for an application is the send
flow's job.
"""

PROMPTS: dict[str, tuple[str, str]] = {
    "check_project": (
        "Judge one project listing against the profile, using the scan pipeline's own rules.",
        CHECK_PROJECT,
    ),
    "write_application": (
        "Draft subject, body and LinkedIn message for a listing. Drafts only, never sends.",
        WRITE_APPLICATION,
    ),
    "send_application": (
        "Send a drafted application after an explicit confirmation.",
        SEND_APPLICATION,
    ),
    "enrich_company": (
        "Find contact data for a company or a stored listing.",
        ENRICH_COMPANY,
    ),
}

# Every prompt body names exactly one slot; filling them all with the single
# argument keeps one signature across prompts whose slot happens to differ.
PROMPT_SLOTS = ("listing", "application", "target")


def render(name: str, argument: str = "") -> str:
    """One prompt, with its slot filled — the text a surface hands to the model.

    Shared so the MCP server and the Telegram command menu run the same
    procedure rather than two copies that drift.
    """
    _, body = PROMPTS[name]
    return body.format(**dict.fromkeys(PROMPT_SLOTS, argument or "(not given)"))
