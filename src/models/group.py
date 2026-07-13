from dataclasses import dataclass, field
#group class
@dataclass
class Group:
    """User-defined grouping of scenarios in the launcher UI. Groups
    have a display name + a color (hex string) that scenarios in
    the group adopt for their button background. Scenarios are
    referenced by name; ones not in any group render as "ungrouped"
    at the top of the launcher's scenario list.

    Stored in scenarios.yaml under a top-level `groups:` block parallel
    to `scenarios:`. Order matters — groups display top-to-bottom
    in the order they appear in the list."""
    name: str
    color: str = "#7a7a7a"
    scenarios: list[str] = field(default_factory=list)