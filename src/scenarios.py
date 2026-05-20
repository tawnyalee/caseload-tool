"""Load scenario definitions from notes.yaml and run them against an
open note panel. Each scenario is one or more NoteData entries filed in
sequence, optionally followed by closing the Salesforce workspace tab.
"""
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml
from playwright.sync_api import Page

from src.config import NOTES_YAML
from src.note_form import NoteData, close_workspace_tab, fill_note


@dataclass
class ScenarioConfig:
    name: str
    hotkey: str
    close_tab_after: bool
    notes: list[NoteData] = field(default_factory=list)


def _note_from_dict(d: dict) -> NoteData:
    return NoteData(
        interaction_format=d.get("interaction_format", "Single Interaction"),
        interaction_type=d.get("interaction_type", ""),
        course_code="",  # supplied at runtime
        subject=d.get("subject", ""),
        academic_activities=list(d.get("academic_activities", [])),
        body=d.get("body", ""),
        submit=bool(d.get("submit", True)),
    )


def load_scenarios(path: Path = NOTES_YAML) -> dict[str, ScenarioConfig]:
    """Load all scenarios from notes.yaml, keyed by name."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sections = raw.get("scenarios", {})
    if not isinstance(sections, dict):
        raise ValueError(
            f"{path}: 'scenarios' must be a mapping of name -> config"
        )
    out: dict[str, ScenarioConfig] = {}
    for name, cfg in sections.items():
        notes = [_note_from_dict(n) for n in cfg.get("notes", [])]
        if not notes:
            raise ValueError(f"Scenario {name!r} has no notes defined")
        out[name] = ScenarioConfig(
            name=name,
            hotkey=cfg.get("hotkey", ""),
            close_tab_after=bool(cfg.get("close_tab_after", True)),
            notes=notes,
        )
    return out


def run_scenario(target: Page, scenario: ScenarioConfig, course_code: str) -> None:
    """Fill (and optionally submit) every note in the scenario against
    the active student. The workspace tab is only closed if all notes
    were submitted — otherwise the user is reviewing/editing and we
    don't want to discard their unsubmitted form."""
    all_submitted = True
    for template in scenario.notes:
        note = replace(template, course_code=course_code)
        fill_note(target, note)
        if not template.submit:
            all_submitted = False
    if scenario.close_tab_after and all_submitted:
        close_workspace_tab(target)
