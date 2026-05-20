# Caseload Note Automation

A Windows desktop launcher that fills repetitive student notes into WGU's
Caseload (a Salesforce Lightning interface) using Playwright. Press a
hotkey, and a pre-defined note is filled and submitted on the active
student's note panel — no typing, no clicking through fields.

Built to replace AutoHotkey / Pulover macros that break on Salesforce
slowness and UI changes. Runs client-side under your own login, so no
Salesforce admin permissions are required.

## What it does

- Auto-fills the Caseload **Student Note** form (Interaction Format,
  Interaction Type, Course Code, Subject, Academic Activities, Body)
- Auto-detects the **Course Code** from the Caseload table — no need
  to type it per student
- Submits the note and closes the workspace tab when done
- Triggered by a **global hotkey** or a button click in the launcher
- Comes with three predefined workflows you can edit or extend:
  - **Welcome** (F3) — one welcome note to a newly assigned student
  - **Approval** (F2) — two notes back-to-back (Email from Student +
    Email to Student) documenting an approval form round-trip
  - **Custom** (F4) — a blank slate; edit the body / type in the
    launcher and use as a third hotkey

## Quick start (end users)

### 1. Download and extract

Download the `CaseloadNotes` zip from
[Releases](https://github.com/ashejim/WGU-Caseload-Note-Tool/releases) and
extract it anywhere — your Desktop, Documents, wherever.

### 2. First-run setup — Smart App Control

Windows 11 may block the launcher on first run because the build isn't
signed with a known publisher certificate. The fix is one of:

- **Recommended:** Right-click `CaseloadNotes.exe` → **Properties** →
  check **Unblock** at the bottom of the General tab → click OK. Done.
- **If that doesn't work:** Open Windows Security → **App & browser
  control** → **Smart App Control settings** → switch to **Off**.
  *Note: this is system-wide, not per-app.*

We're working on a signed build (via SignPath for open-source projects)
that will remove this step.

### 3. First-run setup — Caseload URL

The launcher opens directly to Caseload, but it needs to know your
specific URL. Edit `%APPDATA%\caseload-notes\.env` (you may need to
create it) and add:

```
CASELOAD_URL=https://srm.lightning.force.com/lightning/n/Caseload_App_Page
```

Adjust the URL to match what you see in your browser when on the
Caseload page.

### 4. First-run setup — sign in

Double-click `CaseloadNotes.exe`. A Chromium window opens. Sign in to
Caseload as you normally would (SSO etc.). The session is saved to
`%APPDATA%\caseload-notes\browser_data\` and reused on every subsequent
launch — you won't have to sign in again.

## Daily use

1. **Launch** CaseloadNotes — the window opens, a Chromium window
   opens, and after a moment the status changes to "Browser ready" and
   "Hotkeys active".
2. **Find a student** in the Chromium window using your normal Caseload
   workflow (the student list table, search, etc.). Click into the
   student record.
3. **Open the New Student Note panel** for that student.
4. **Press the hotkey** for the scenario you want — anywhere on your
   system:
   - **F3** → welcome note
   - **F2** → approval (two notes filed back-to-back)
   - **F4** → custom note
5. The script fills the note, submits, closes the tab.
6. Move on to the next student.

If the launcher doesn't have keyboard focus, hotkeys still work — they
are global.

### Course code

The launcher detects the course code from the Caseload table (the
*Course Code* column on the row for the active student). You don't need
to type it. If you want to override it (e.g., the student is in
multiple courses and you want to file against the other), type the code
into the **Course code** field before pressing the hotkey.

If the table isn't reachable (you navigated directly to a Salesforce
contact rather than via Caseload), the launcher will ask you to type a
code.

## Editing notes

Each scenario lives in `%APPDATA%\caseload-notes\notes.yaml`. You can
edit it in two ways:

### Via the launcher

The right pane of the window shows one tab per scenario. Edit any
field — Interaction Format, Interaction Type, Academic Activities,
Body, Submit toggle, Hotkey — and click **Save changes**. The launcher
re-registers hotkeys immediately; no restart needed.

Click "Hide editor" to collapse the right pane when you don't need it.
Each Note section can also be collapsed with the ▼/▶ button.

### Direct YAML edit

If you prefer, open `notes.yaml` in any text editor. The format is
straightforward:

```yaml
scenarios:
  welcome:
    hotkey: F3
    close_tab_after: true
    notes:
      - interaction_type: Email to Student
        body: general welcome email sent
        academic_activities: []
        submit: true
```

After editing the file directly, click **Revert** in the launcher to
reload, or restart the app.

## Adding new scenarios

To add a fourth scenario (e.g., for a different course or a follow-up
note), copy the `custom` block in `notes.yaml`, rename it, set a unique
hotkey, and edit the body. The launcher picks it up on next save/revert
or restart.

## Hotkey notes

- **Bare F-keys (F3, F2, F4...)** are claimed system-wide while the
  launcher is running. They won't trigger their normal browser/app
  behavior. When you close the launcher, they return to normal.
- **F1** is reserved by Chromium for Help and can't be reliably
  reclaimed by this app — pick F2–F12 or a modifier combo.
- **Modifier combos** like `Ctrl+Shift+1` or `Ctrl+Alt+W` work too. Set
  them in the editor's Hotkey field.

## Known issues

- **Smart App Control blocks the .exe** on first run — see Quick Start
  step 2. Will be resolved once the project is signed.
- **F1 hotkey** is unreliable due to Chromium's built-in help binding.
- **Comments in notes.yaml are lost** when you save via the launcher.
  Edit directly in a text editor if you need them.
- **Multi-tab DOM**: if you have many open Salesforce app tabs, the
  launcher targets the visible note panel. Don't have a stale note
  panel open in the background while filling — close those first.

## Developer setup

Clone the repo, then:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Run from source:

```powershell
python -m scripts.launcher
```

Or the CLI (one scenario per run):

```powershell
python -m scripts.fill_note --scenario approval
```

## Building a distributable .exe

Two paths, both produce a one-folder distribution with bundled Chromium.

**PyInstaller** (fast build, may be blocked by SAC):

```powershell
python build.py
```

Output at `dist\CaseloadNotes\`.

**Nuitka** (slow build, less likely flagged by SAC):

```powershell
python build_nuitka.py
```

Output at `dist-nuitka\CaseloadNotes\`. First run downloads MinGW64 if
no C compiler is found.

In both cases: zip the output folder and share.

## License

See [LICENSE](LICENSE).
