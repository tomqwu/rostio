"""Data loader for YAML/JSON/CSV files to Pydantic models."""

import csv
import json
from pathlib import Path
from typing import Any

import yaml

from api.core.models import (
    Availability,
    ConstraintBinding,
    EventsFile,
    HolidayFile,
    Org,
    PeopleFile,
    ResourcesFile,
    SolutionBundle,
    TeamsFile,
)


def load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML file to dict."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path: Path) -> dict[str, Any]:
    """Load JSON file to dict."""
    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
        return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    """Save dict to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def save_yaml(path: Path, data: dict[str, Any]) -> None:
    """Save dict to YAML file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def load_org(workspace: Path) -> Org:
    """Load org.yaml."""
    data = load_yaml(workspace / "org.yaml")
    return Org(**data)


def load_people(workspace: Path) -> PeopleFile:
    """Load people.yaml."""
    data = load_yaml(workspace / "people.yaml")
    return PeopleFile(**data)


def load_teams(workspace: Path) -> TeamsFile | None:
    """Load teams.yaml if exists."""
    path = workspace / "teams.yaml"
    if not path.exists():
        return None
    data = load_yaml(path)
    return TeamsFile(**data)


def load_resources(workspace: Path) -> ResourcesFile | None:
    """Load resources.yaml if exists."""
    path = workspace / "resources.yaml"
    if not path.exists():
        return None
    data = load_yaml(path)
    return ResourcesFile(**data)


def load_events(workspace: Path) -> EventsFile:
    """Load events.yaml."""
    data = load_yaml(workspace / "events.yaml")
    return EventsFile(**data)


def load_holidays(workspace: Path) -> HolidayFile | None:
    """Load holidays.yaml if exists."""
    path = workspace / "holidays.yaml"
    if not path.exists():
        return None
    data = load_yaml(path)
    return HolidayFile(**data)


def load_availability_files(workspace: Path) -> list[Availability]:
    """Load all availability/*.yaml files."""
    avail_dir = workspace / "availability"
    if not avail_dir.exists():
        return []

    results = []
    for path in avail_dir.glob("*.yaml"):
        data = load_yaml(path)
        results.append(Availability(**data))
    return results


def load_constraint_files(workspace: Path) -> list[ConstraintBinding]:
    """Load all constraints/*.yaml files."""
    constraints_dir = workspace / "constraints"
    if not constraints_dir.exists():
        return []

    results = []
    for path in constraints_dir.glob("*.yaml"):
        data = load_yaml(path)
        results.append(ConstraintBinding(**data))
    return results


def load_solution(path: Path) -> SolutionBundle:
    """Load solution from JSON."""
    data = load_json(path)
    return SolutionBundle(**data)


def save_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    """Save rows to CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
