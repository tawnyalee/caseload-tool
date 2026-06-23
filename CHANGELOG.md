# Changelog

Notable changes per release. Versions follow the scheme in `src/version.py`
(MAJOR = scenarios.yaml format break, MINOR = new features, PATCH = fixes).

## 0.12.0 — 2026-06-23

Momentum calibration and a new **Data** tab, support for students who aren't on
your caseload, and a batch of viewer polish — building on the 0.11.0 notes
release.

### Major features
- **Momentum calibration** — the tool ingests WGU's "Archive (last 30 days)"
  results export (`results_archive*.csv`) into a local outcomes store and
  measures how well the **Momentum** prediction actually tracks who passes. It
  freezes each student's *entry-time* Momentum — the only fair basis, since
  Momentum self-corrects as a student progresses — and reports pass-rate vs.
  predicted band, per course. Fresh downloads are auto-detected and ingested on
  reload, with a staleness reminder. A **📈 Momentum** button opens the
  calibration report (entry-fair / entry-proxy / exit-diagnostic modes, filtered
  by course and by student course-load).
- **Data tab** — a new "Data" tab in the activity area with four views:
  **Pass-rate vs prediction** (per-course calibration bars), **At-risk**
  students (current Low / Med-Low, with juggling-course flags), **Momentum
  trajectory** (entry→exit drift), and **Pass rate over time**. Charts draw on a
  native canvas (no matplotlib, keeping the build lean), pop out to a second
  monitor, and support a course picker and preset date ranges.
- **Off-caseload students** — look up and act on students in your course who are
  assigned to another instructor: type a Student ID or email in the viewer
  search to **find and open their record via Salesforce global search**, and
  **file notes** for them through that nav path.

### Improvements
- **Viewer polish** — clicking a student email now CCs the PM, row scrolling is
  smoother, and note filling is sturdier.
- **Note subject** UI and **Success Path** foundations; a **Salesforce session
  pre-check** runs before fires.
- More robust data-collection scrapes and fresher caseload tooltips.

### Internal
- Temporary task-unlock probe wired behind a hidden viewer-search keyword
  (feature on hold pending a live example).

## 0.11.0 — 2026-06-15

Faster note filing, smoother fires, and a snappier window — building on the
0.10.0 texting release.

### Major features
- **Faster note filing (Salesforce Contact id)** — when a student's Contact id
  is known, the tool **deep-links straight to their record** (~2.6s to a ready
  note panel) instead of searching the caseload list. Ids come from the
  Mongoose segment export and are also **harvested as you work** ("collect-as-
  you-go"), so coverage grows automatically and notes/texts get faster over
  time. Gated notes ("Email from Student", which need an Academic Activity) and
  email fires keep using the reliable search path.
- **All input up front** — combined batch *and* single fires now show every
  prompt/review (scenario prompts, note edits, email review, text review) at
  the start, then send texts/emails and file notes **unattended**. No more
  reviewing texts, waiting for them to send, then being pulled back for more.

### Improvements
- **Mongoose sign-in handling** — a text-bearing action checks the Mongoose
  session **before** the review (clear "sign in, then re-fire" message + opens
  Mongoose if logged out, instead of timing out); Mongoose also opens in the
  background at startup with a sign-in heads-up. New **🐭 Mongoose** button.
- **Snappier UI** — action editors build lazily (only the one you open, not all
  ~20 at once), the activity log batches its redraws during a run, and email
  templates are cached so a batch review doesn't re-read the file per student.

### Fixes
- Note "Submit is disabled" failures — the Academic Activity checkbox tick is
  now verified and re-clicked if a Lightning re-render drops it; the note course
  comes from the caseload row so filing doesn't depend on an on-page table.

## 0.10.0 — 2026-06-14

Text messaging. An action can now **send a text** through Mongoose (the SMS
platform behind "Cadence"), alongside the existing note + email channels —
composed, reviewed, and sent entirely inside the tool. You never touch the
Mongoose UI.

### Major features
- **Send texts (single & batch)** — add a "Send text (Mongoose)" step to any
  action. The body is a plain-text template with the same `{{variables}}` as
  email (`first_name`, `preferred_name`, `course_code`, …), rendered tool-side.
  Single texts are personalized and shown in an in-app review/edit dialog;
  batches send one shared message per group. The tool drives Mongoose
  (open → pick inbox → add recipients → message → schedule) to completion.
- **Always-scheduled, with an acceptable window** — texts are scheduled (never
  sent immediately, a Mongoose limitation). Each action defines an acceptable
  **window in the student's local time** (default 10 AM–4 PM); the text goes out
  **ASAP within it** — at least ~10 min from now, rolling to the window's start
  the next day if it's already too late today.
- **Timezone-aware + smart grouping** — sends land at the right local time per
  timezone; when several timezones resolve to the *same* absolute send time
  (a wide window fired mid-day), they **merge into one Mongoose compose** for a
  faster, smoother send. Reviews now show **Today / Tomorrow / a date**.
- **Contact-ID matching (blank-mobile-proof)** — students are matched to their
  Salesforce Contact ID from a Mongoose **segment export**, so texting reaches
  students even when their caseload mobile is blank or differs. The new
  **⬇ Texting IDs** button auto-exports each caseload department's
  `all <course> students` segment, joins it to the caseload (by mobile, then
  name), and persists the map locally (SQLite); a pop-up walks you through
  creating a segment for any department that has none.
- **Combined actions** — one action can file a note **and** send an email **and**
  send a text, reviewed together.

### Improvements
- Non-opted-in students are skipped up front (faster batches); students with an
  unknown timezone default to Mountain rather than being dropped.
- **↻ Browser** one-click restart for hang recovery; firing is blocked while the
  live task pass/fail scrape is updating (prevents stalls/contention).
- Auto department switching in Mongoose before composing each group.

### Notes
- Texting needs the `tzdata` package (bundled in the build) for timezone math.

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
