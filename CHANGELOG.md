# Changelog

Notable changes per release. Versions follow the scheme in `src/version.py`
(MAJOR = scenarios.yaml format break, MINOR = new features, PATCH = fixes).

## 0.9.0 — 2026-06-10

A large feature release: Essential Actions support, live task pass/fail,
a new local caseload-history store, follow-up field editing, richer filters,
and rich paste in the email editor.

### Major features
- **Essential Actions (EA) in the caseload viewer** — scrape the EA dashboard
  and show/filter students by their Essential Action; **EA-aware single-action
  fire** (opt-in), with a per-note "attach to EA" toggle.
- **Caseload history (new)** — a local SQLite store snapshots the dynamic
  fields (Momentum, task status, follow-up, notes) on each caseload reload, at
  a **user-set interval** (Settings → Off / 6 / 8 / 12 h / Daily). A
  **Departures** view lists students who left the caseload since the last
  capture, split into *completed* (passed) vs *needs follow-up*; one-click
  **Export history to CSV** for pandas/Excel.
- **Live task pass/fail** — a background scrape reads each task cell's real
  pass / returned / in-process state (the colour the CSV export drops) and
  shows it on the grid + quick-view badges, with per-task status filters.
- **Follow-up Date & Note editing from the viewer** — set a student's
  Salesforce follow-up date and note in place (writes back to Salesforce); an
  empty note clears it.
- **Richer filters** — column-vs-column comparison (e.g. `{Task 2}` as a value)
  and inclusive date operators (on-or-before / on-or-after).
- **Email editor: rich paste** — pasting now keeps links, bold/italic/
  underline, headings and lists when the clipboard carries formatted content
  (plain-text paste is unchanged when it doesn't).
- **Template variables** — added `{{preferred_name}}` (falls back to first
  name) and a name-capitalization setting that cleans up CSV casing.

### Improvements
- Sign-in detection, the calendar picker opens near the mouse, and the grid
  updates instantly after an edit.
- Browser focuses for login on startup, then minimizes once the caseload loads.
- Batch review gets a viewer-style "select all" checkbox and a "Student (N)" tag.
- Single-instance guard + clean Playwright shutdown (fixes a launch error).
- Performance: blind filter/navigation sleeps replaced with bounded
  readiness waits.

### Fixes
- Editor Save/Done bar stays reachable when the window is wide/maximized.
- Course code now auto-fills on a main-window note fire.
- Caseload column chooser show/hide now works.
