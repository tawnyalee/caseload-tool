# Caseload Note Automation

## Goal
Automate entering repetitive notes into WGU's Caseload (a Salesforce-based
interface) using Playwright with Python. No admin permissions available in
Salesforce — automation must run client-side as the user.

## Current scope (Phase 1)
Script that, given a student identifier, navigates to their record in
Caseload and enters two standard notes (one "to" and one "from") documenting
that an approval form was signed and returned via email.

## Future scope (Phase 2+)
Scale up to a full pipeline:
- Detect approval-form emails in Outlook (via Microsoft Graph or pywin32)
- Open form for human review
- Attach signature image if prompted
- Email signed form back to student
- Log notes in Caseload (Phase 1 piece)

## Technical decisions made
- **Playwright over Selenium**: better auto-waiting, modern API
- **Python**: standard ecosystem, good Outlook/PDF libraries for Phase 2
- **Persistent browser context**: log into Salesforce/SSO once, reuse session
- **Selector strategy**: prioritize role/label-based selectors
  (get_by_role, get_by_label), then stable data-* attributes. Avoid
  auto-generated Lightning IDs and long XPath chains.
- **Centralized selectors**: all selectors in one file for easy maintenance
- **Defensive patterns**: assert student identity before writing notes,
  verify note appears after save, screenshot on failure, explicit timeouts

## What I still need to provide
- URL pattern for student pages in Caseload
- How students are searched/found (search box, ID paste, etc.)
- Note entry form structure (single textarea? note type dropdown?
  separate "to"/"from" fields?)
- Exact text of the two standard notes
- A signature image file (for Phase 2)

## Background context
- I'm a Senior Course Faculty at WGU's IT College
- Currently using Pulover's macro creator, which breaks on Salesforce
  slowness and minor UI changes
- Want open-source tooling, runs locally, no admin permissions needed
