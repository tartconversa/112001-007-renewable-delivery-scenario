"""Optional JSON session state.

The CSV fixtures are treated as source records.  A session file is a full
snapshot of the records *after* staff decisions, plus the append-only decision
log and any publications (with the fingerprints they were issued at).  A
snapshot is deliberate: a published decision must be reproducible even if the
source CSVs are later edited, and the ``source`` key stops a session from one
fixture directory being applied to another.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from .analysis import Analysis
from .loader import build_dataset, load_dataset
from .model import Application, Bill, Household, ProgramLimits, SharedAllocation
from .registry import PublishedOutcome


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _date(value) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _household(row: dict) -> Household:
    return Household(
        household_token=row["household_token"],
        area_id=row.get("area_id", ""),
        income_band=row.get("income_band", "unknown"),
        occupants=int(row.get("occupants", 0)),
        account_token=row.get("account_token", ""),
        shared_account=bool(row.get("shared_account", False)),
        consent_status=row.get("consent_status", ""),
        medical_need=row.get("medical_need"),
    )


def _application(row: dict) -> Application:
    return Application(
        application_id=row["application_id"],
        household_token=row["household_token"],
        measure=row.get("measure", ""),
        quote=_decimal(row.get("quote")),
        provider_id=row.get("provider_id", ""),
        available_from=_date(row.get("available_from")),
        available_to=_date(row.get("available_to")),
        grant_status=row.get("grant_status", "submitted"),
        installed_on=_date(row.get("installed_on")),
        post_kwh=_decimal(row.get("post_kwh")),
    )


def _bill(row: dict) -> Bill:
    return Bill(
        bill_id=row["bill_id"],
        account_token=row["account_token"],
        period=row["period"],
        electric_kwh=Decimal(str(row.get("electric_kwh", "0"))),
        amount_due=_decimal(row.get("amount_due")),
        adjustment_of=row.get("adjustment_of", "") or "",
        statement_status=row.get("statement_status", "final"),
    )


def _program(row: dict) -> ProgramLimits:
    return ProgramLimits(
        period=row["period"],
        total_budget=Decimal(str(row["total_budget"])),
        per_household_cap=Decimal(str(row["per_household_cap"])),
        max_active_installations=int(row["max_active_installations"]),
        priority_groups=tuple(row.get("priority_groups", ())),
    )


def _allocation(row: dict) -> SharedAllocation:
    return SharedAllocation(
        account_token=row["account_token"],
        household_token=row["household_token"],
        share=Decimal(str(row["share"])),
        basis=row.get("basis", ""),
    )


def _publication(row: dict) -> PublishedOutcome:
    return PublishedOutcome(
        label=row["label"],
        published_at=row["published_at"],
        fingerprint=row["fingerprint"],
        approvals=tuple(row.get("approvals", ())),
        plan=tuple(row.get("plan", ())),
        outcomes=tuple(row.get("outcomes", ())),
        overrides=tuple(row.get("overrides", ())),
    )


def load_state(root: str | Path, state_path: str | Path) -> tuple[Analysis, Path]:
    """Load a session snapshot, or the fresh fixtures when none exists."""

    root = Path(root)
    path = Path(state_path)
    if not path.exists():
        return Analysis(load_dataset(root)), path

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc

    source = payload.get("source")
    if source not in (None, str(root.resolve())):
        raise ValueError(
            f"state file {path} belongs to {source}, not {root.resolve()}; "
            "point --data at the matching fixture directory or use a new state file"
        )

    data = build_dataset(
        households=tuple(_household(r) for r in payload.get("households", ())),
        bills=tuple(_bill(r) for r in payload.get("bills", ())),
        applications=tuple(_application(r) for r in payload.get("applications", ())),
        programs=tuple(_program(r) for r in payload.get("programs", ())),
        allocations=tuple(_allocation(r) for r in payload.get("allocations", ())),
    )
    analysis = Analysis(data)
    analysis.registry.decisions.extend(payload.get("decisions", []))
    analysis._publications = [_publication(r) for r in payload.get("publications", [])]
    return analysis, path


def save_state(analysis: Analysis, root: str | Path, path: str | Path) -> None:
    payload = {
        "source": str(Path(root).resolve()),
        "households": [asdict(r) for r in analysis.dataset.households],
        "applications": [asdict(r) for r in analysis.dataset.applications],
        "bills": [asdict(r) for r in analysis.dataset.bills],
        "programs": [asdict(r) for r in analysis.dataset.programs],
        "allocations": [asdict(r) for r in analysis.dataset.allocations],
        "decisions": list(analysis.registry.decisions),
        "publications": [p.as_dict() for p in analysis.publications],
    }
    Path(path).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


__all__ = ["load_state", "save_state"]
