"""Phase 1 entry point: launch Chromium with the saved session, let the
user navigate to a student and open the note panel, then fill, submit,
and close the Salesforce workspace tab.

Scenarios (and their hotkeys, for the upcoming launcher) live in
notes.yaml at the project root. Edit that file to change note text, add
new scenarios, or rebind hotkeys.

Usage:
    python -m scripts.fill_note                       # default: welcome
    python -m scripts.fill_note --scenario approval
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import Page

from src.browser import persistent_context
from src.config import CASELOAD_URL
from src.scenarios import load_scenarios, run_scenario
from src.student_lookup import detect_course_code, get_active_student_name


def _resolve_course_code(target: Page, typed: str) -> str | None:
    if typed:
        return typed
    student = get_active_student_name(target)
    if not student:
        print("No visible note panel found. Open one and try again.")
        return None
    print(f"Active student: {student}")
    detected = detect_course_code(target, student)
    if detected:
        print(f"Auto-detected course code: {detected}")
        return detected
    print(f"Could not auto-detect for {student}. Type a course code:")
    fallback = input("[Course code] > ").strip()
    return fallback or None


def main() -> None:
    scenarios = load_scenarios()

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--scenario",
        choices=sorted(scenarios),
        default="welcome" if "welcome" in scenarios else next(iter(scenarios)),
        help="Which scenario to run (defined in notes.yaml).",
    )
    args = parser.parse_args()
    scenario = scenarios[args.scenario]

    with persistent_context() as context:
        page = context.pages[0] if context.pages else context.new_page()
        if CASELOAD_URL:
            page.goto(CASELOAD_URL)

        print()
        print(f"Scenario: {scenario.name}  (hotkey: {scenario.hotkey or 'none'})")
        for i, n in enumerate(scenario.notes, 1):
            print(f"  Note {i}: {n.interaction_type!r} — {n.body!r}")
        print()
        print("Browser is open. Navigate to a student and open the New Student Note panel.")
        print("Press Enter to auto-detect the course code, or type one explicitly.")
        print("Type 'q' + Enter to quit.")

        while True:
            try:
                typed = input("\n[Enter to auto-detect, type code, or q] > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if typed.lower() == "q":
                break

            target = context.pages[-1] if context.pages else page
            course_code = _resolve_course_code(target, typed)
            if not course_code:
                continue

            try:
                run_scenario(target, scenario, course_code)
                print(f"Scenario {scenario.name!r} complete (course {course_code!r}).")
            except RuntimeError as e:
                print(f"Failed: {e}")


if __name__ == "__main__":
    main()
