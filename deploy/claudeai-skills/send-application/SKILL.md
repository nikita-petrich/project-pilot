---
name: send-application
description: Send a previously drafted project-pilot application via the project-pilot MCP, after explicit confirmation. Use ONLY when the user explicitly asks to send - "schick die Bewerbung ab", "sende Bewerbung 12", "verschick sie", "/send-application". Never use for drafting or revising; that is write-application's job.
disable-model-invocation: true
---

# send-application

Sends one drafted application through `project_pilot_send_application`. This
skill is user-invoked only (`disable-model-invocation: true`): sending is
always Nik's decision, never the model's. The drafting skill
(`write-application`) never sends - this skill is the single, deliberate
send button.

## Preconditions - check all three before anything else

1. **A draft exists.** Identify the `application_id` - from the current chat,
   or ask. If only a `listing_id` is known, say that drafting comes first
   (`write-application`) and stop.
2. **A recipient is set.** If none is set on the application, ask for the
   e-mail address and call `project_pilot_set_recipient(application_id, email)`
   first. Never guess or invent a recipient.
3. **The MCP tools are connected.** Without them there is nothing to send -
   say so and stop. There is no local fallback for sending, by design.

## Confirmation - the hard rule

Before sending, show a compact summary and get an explicit yes **in this
conversation**:

```
Bewerbung #12 → paul.franzke@agentur.de
Betreff: <subject>
Listing: ⭐ 95 · Backend/REST-API Developer · One Day Ahead GmbH (Listing 42)
```

Then ask: "Senden? (ja/nein)". Only an unambiguous yes to this exact summary
counts. A general "mach mal" from earlier in the chat does not. Never send
more than one application per confirmation, and never resend one that the
status guard reports as already sent - report that instead.

## Send and report

1. `project_pilot_send_application(application_id)`.
2. Report the result in one line: sent + timestamp, or the exact error.
3. On error: report it plainly and stop. Do not retry silently, do not try
   another delivery route (no Gmail, no SMTP tools) - the MCP pipeline is the
   only sender.
