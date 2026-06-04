# Request: API / MCP access for caseload note automation

**To:** WGU Salesforce Administrator / IT
**From:** _[your name, role — e.g., Course Instructor]_  ·  _[email]_
**Date:** _[date]_
**Re:** A supported, API-based way to read my caseload and file Student
Notes (replacing browser automation)

---

## Summary

I've built a small desktop tool that helps me file **Student Notes** in
Salesforce faster and more consistently for the students on my caseload
(e.g., logging "welcome email sent," course outreach, follow-up calls). It
works today, but it drives the Salesforce **web UI** through my own logged-in
browser session, which is brittle (it breaks whenever the Lightning page
changes) and isn't an officially supported integration path.

I'd like to move to a **supported API integration**. Salesforce's
**hosted Model Context Protocol (MCP) servers** (GA in 2026) look like a
clean fit, but enabling and configuring them is an org-admin function — hence
this request. A plain connected-app / API approach would also work if you
prefer.

## What the tool does today

- **Reads my caseload** — currently by exporting a Caseload list view to CSV.
- **Files Student Notes** on the student I'm working with — currently by
  filling and submitting the Lightning note panel via browser automation.
- (Email sending is handled separately through Outlook — **not** part of this
  request.)

It only ever acts as **me**, on **my** caseload, using **my** existing
permissions. No new data is exposed beyond what I can already see and do by
hand.

## The request

Please advise on / enable one of the following so the tool can use a
supported API instead of scraping the UI:

1. **Preferred — a hosted MCP server** scoped to expose just:
   - a **query** for my caseload (the students assigned to me + the fields
     below), and
   - an **action** that creates a Student Note.
2. **Or — a connected app (OAuth 2.0)** with least-privilege access so the
   tool can call the same two capabilities via the REST/Apex API directly.

### Specifically, the two capabilities

- **Caseload read:** a SOQL query (or report/flow) returning my assigned
  students with: Name, Student ID, student email, Program Mentor, Course
  Code, Term End date, Task 1/2/3 status, Timezone, (and Phone if available).
- **Create Student Note:** the note today is created through a **custom
  Aura/Apex** path in the Lightning UI, so a direct API insert may not be
  enough. The cleanest server-side option is an **invocable Apex action or a
  Flow** that creates the note with the standard fields (interaction
  type/format, body text, academic activities, course code, submit) — exposed
  as the single MCP/API action above.

## Security, governance, FERPA

- Access should be **least-privilege** and **permission-enforced** — limited
  to my own caseload and the note-creation action, nothing more. (Salesforce
  hosted MCP enforces the running user's permissions automatically and lets
  you allowlist exactly which tools are exposed.)
- Student data stays within Salesforce's governance. I want to follow WGU's
  **FERPA / data-handling** requirements — please tell me what review or
  approval this needs and I'll complete it.
- Happy to use whatever **authentication** you require (SSO-backed OAuth,
  IP/scope restrictions, audited connected app, etc.).

## Why this helps (beyond just me)

- **Reliability:** API calls don't break when the Lightning UI changes
  (the current automation does).
- **Auditability & control:** a sanctioned connected app / MCP server is
  visible to IT, permission-scoped, rate-limited, and revocable — unlike
  browser automation.
- **Reusable:** if it works, the same supported pattern could help other
  Course Instructors who do the same repetitive note-logging.

## Questions for you

1. Is **hosted MCP** enabled (or enableable) on our org edition, and is it an
   approach you'd support here?
2. Is there an existing **invocable Apex action / Flow** for creating a
   Student Note, or would one need to be built? (I can share exactly which
   fields the note uses.)
3. What **FERPA / security review** does this require, and who do I work with?
4. If MCP isn't the route, would a **least-privilege connected app** be
   acceptable instead?

Thank you — happy to demo the current tool and walk through exactly what it
reads and writes.

_[your name]_
