"""The analysis facade used by the CLI and by callers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from .bills import build_attributions
from .eligibility import build_assessments
from .loader import DatasetError, load_dataset, new_bill_id, revise_dataset
from .model import Application, Bill, Dataset, Issue, ProgramLimits, utc_timestamp
from .planning import (
    Scenario,
    ScenarioResult,
    compare_scenarios,
    default_scenarios,
    run_scenario,
)
from .priority import build_priority
from .registry import (
    PublicationImpact,
    PublishedOutcome,
    Registry,
    StatusRecord,
    comparable_bill_kwh,
    status_for,
)

PUBLISHED_MEASUREMENTS = frozenset(
    {"measured improvement", "measured increase", "measured unchanged"}
)


@dataclass
class Analysis:
    dataset: Dataset
    registry: Registry = field(default_factory=Registry)
    _publications: list[PublishedOutcome] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._refresh()

    # -- state ---------------------------------------------------------------

    def _refresh(self) -> None:
        self.attributions, attribution_issues = build_attributions(self.dataset)
        self._attribution_issues = attribution_issues
        self.priority = build_priority(self.dataset, self.attributions)
        self.programs = {program.period: program for program in self.dataset.programs}
        self.assessments = build_assessments(
            self.dataset, self.attributions, self.programs
        )
        self.statuses: dict[str, StatusRecord] = {}
        for application in self.dataset.applications:
            attribution = self.attributions.get(application.household_token)
            self.statuses[application.application_id] = status_for(
                self.dataset,
                application,
                attribution,
                comparable_bill_kwh(self.dataset, application, attribution),
            )

    # -- reporting -----------------------------------------------------------

    @property
    def issues(self) -> tuple[Issue, ...]:
        return self.dataset.issues + tuple(self._attribution_issues)

    def data_quality(self) -> dict:
        errors = [i for i in self.issues if i.severity == "error"]
        warnings = [i for i in self.issues if i.severity == "warning"]
        return {
            "errors": len(errors),
            "warnings": len(warnings),
            "by_code": _count_by(self.issues, "code"),
            "issues": [issue.as_dict() for issue in self.issues],
            "records": {
                "households": len(self.dataset.households),
                "bills": len(self.dataset.bills),
                "applications": len(self.dataset.applications),
                "programs": len(self.dataset.programs),
                "allocations": len(self.dataset.allocations),
            },
        }

    def completeness_report(self) -> list[dict]:
        rows = []
        for token, assessment in sorted(self.assessments.items()):
            rows.append(
                {
                    "household_token": token,
                    "status": assessment.status,
                    "completeness": str(assessment.completeness_ratio),
                    "missing": list(assessment.missing_fields),
                    "items": [
                        {"field": item.field, "status": item.status, "detail": item.detail}
                        for item in assessment.completeness
                    ],
                    "scope": _scope_dict(assessment.scope),
                    "priority_tier": self.priority.tier_of(token),
                    "eligibility": _eligibility_dict(assessment.eligibility),
                }
            )
        return rows

    def priority_report(self) -> dict:
        return {
            "notice": self.priority.notice,
            "tiers": [
                {
                    "tier": group.tier,
                    "label": group.label,
                    "households": list(group.households),
                    "drivers": list(group.drivers),
                }
                for group in self.priority.groups()
            ],
            "unranked": list(self.priority.unranked()),
            "high_burden": list(self.priority.high_burden()),
            "members": [
                {
                    "household_token": m.household_token,
                    "tier": m.tier,
                    "drivers": list(m.drivers),
                    "income_band": m.income_band,
                    "burden_band": m.burden_band,
                    "kwh_per_occupant_month": _num(m.kwh_per_occupant_month),
                    "exclusion": m.exclusion,
                    "rankable": m.rankable,
                }
                for m in sorted(
                    self.priority.members, key=lambda m: (m.tier, m.household_token)
                )
            ],
        }

    def status_report(self) -> list[dict]:
        rows = []
        for application in self.dataset.applications:
            status = self.statuses[application.application_id]
            rows.append(
                {
                    "application_id": status.application_id,
                    "household_token": application.household_token,
                    "grant_status": status.grant_status,
                    "install_status": status.install_status,
                    "measurement_status": status.measurement_status,
                    "installed_on": status.installed_on,
                    "basis": status.basis,
                    "reported_post_kwh": _num(status.reported_post_kwh),
                    "baseline_kwh": _num(status.baseline_kwh),
                    "post_bill_kwh": _num(status.post_bill_kwh),
                    "change_kwh": _num(status.change_kwh),
                    "change_pct": _num(status.change_pct),
                    "states": status.three_states,
                }
            )
        return rows

    # -- planning ------------------------------------------------------------

    def program_for(self, application_id: str) -> ProgramLimits | None:
        application = self.dataset.application(application_id)
        if application is None:
            return None
        from .eligibility import applicable_program

        return applicable_program((application,), self.programs)

    def _default_program(self) -> ProgramLimits | None:
        if not self.dataset.programs:
            return None
        return sorted(self.dataset.programs, key=lambda p: p.period)[0]

    def default_scenarios(self) -> list[Scenario]:
        program = self._default_program()
        if program is None:
            return []
        return default_scenarios(self.dataset, program)

    def plans(
        self, scenarios: list[Scenario] | None = None
    ) -> tuple[ScenarioResult, ...]:
        program = self._default_program()
        if program is None:
            return ()
        return compare_scenarios(self.dataset, self.priority, program, scenarios)

    def plan(
        self, scenario: Scenario, program: ProgramLimits | None = None
    ) -> ScenarioResult:
        program = program or self._default_program()
        if program is None:
            raise ValueError("no program limits configured")
        return run_scenario(self.dataset, self.priority, program, scenario)

    # -- registry ------------------------------------------------------------

    def _apply_application(self, application: Application) -> None:
        applications = tuple(
            application if a.application_id == application.application_id else a
            for a in self.dataset.applications
        )
        self.dataset = revise_dataset(self.dataset, applications=applications)
        self._refresh()

    def approve(self, application_id: str, *, approved_by: str = "") -> Application:
        application = self.registry.approve(
            self.dataset, application_id, approved_by=approved_by
        )
        self._apply_application(application)
        return application

    def register_install(
        self, application_id: str, installed_on: str, *, recorded_by: str = ""
    ) -> Application:
        application = self.registry.register_install(
            self.dataset, application_id, installed_on, recorded_by=recorded_by
        )
        self._apply_application(application)
        return application

    def revise_bill(
        self,
        *,
        account_token: str,
        period: str,
        electric_kwh: Decimal,
        amount_due: Decimal | None = None,
        correction_of: str | None = None,
        reason: str = "",
    ) -> str:
        """Append a correction (signed delta) against a base bill."""

        base = correction_of
        if base is None:
            candidates = [
                b
                for b in self.dataset.bills_for_account(account_token)
                if b.period == period
                and not b.adjustment_of
                and b.statement_status != "void"
            ]
            if not candidates:
                raise ValueError(
                    f"no base bill for {account_token} in {period}; supply correction_of"
                )
            base = candidates[-1].bill_id
        target = {b.bill_id: b for b in self.dataset.bills}.get(base)
        if target is None:
            raise ValueError(f"unknown bill {base}")
        if target.account_token != account_token or target.period != period:
            raise ValueError(
                f"bill {base} belongs to {target.account_token} / {target.period}"
            )

        # ``amount_due`` None means "no amount stated for this revision", not
        # a zero adjustment; storing zero would claim a zero bill.
        bill_id = new_bill_id(self.dataset)
        bill = Bill(
            bill_id=bill_id,
            account_token=account_token,
            period=period,
            electric_kwh=electric_kwh,
            amount_due=amount_due,
            adjustment_of=base,
            statement_status="correction",
        )
        self.registry.decisions.append(
            {
                "at": utc_timestamp(),
                "action": "bill revised",
                "bill_id": bill_id,
                "adjustment_of": base,
                "electric_kwh": str(electric_kwh),
                "reason": reason,
            }
        )
        self.dataset = revise_dataset(
            self.dataset, bills=self.dataset.bills + (bill,)
        )
        self._refresh()
        return bill_id

    def delay_install(
        self,
        application_id: str,
        new_available_to: str | None = None,
        *,
        new_available_from: str | None = None,
        reason: str = "",
    ) -> Application:
        application = self.dataset.application(application_id)
        if application is None:
            raise ValueError(f"unknown application {application_id}")
        if new_available_to is None and new_available_from is None:
            raise ValueError("supply a new window start or end")

        def _parse(value: str, field: str) -> date:
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{field} {value!r} is not an ISO date") from exc

        start = (
            _parse(new_available_from, "available_from")
            if new_available_from is not None
            else application.available_from
        )
        end = (
            _parse(new_available_to, "available_to")
            if new_available_to is not None
            else application.available_to
        )
        if start and end and start > end:
            raise ValueError("new window closes before it opens")
        if application.installed_on and end and end < application.installed_on:
            raise ValueError(
                "moving the window before the recorded installation would "
                "contradict the installation record"
            )
        if application.installed_on and start and start > application.installed_on:
            raise ValueError(
                "moving the window start past the recorded installation would "
                "contradict the installation record"
            )
        updated = Application(
            **{
                **application.__dict__,
                "available_from": start,
                "available_to": end,
            }
        )
        self.registry.decisions.append(
            {
                "at": utc_timestamp(),
                "action": "installation window moved",
                "application_id": application_id,
                "from": (
                    application.available_from.isoformat()
                    if application.available_from
                    else "",
                    application.available_to.isoformat()
                    if application.available_to
                    else "",
                ),
                "to": (
                    start.isoformat() if start else "",
                    end.isoformat() if end else "",
                ),
                "reason": reason,
            }
        )
        self._apply_application(updated)
        return updated

    def publish(
        self,
        label: str,
        *,
        approvals: list[dict] | None = None,
        plan: list[dict] | None = None,
        outcomes: list[dict] | None = None,
        overrides: list[dict] | None = None,
    ) -> PublishedOutcome:
        if approvals is None:
            approvals = [
                {
                    "application_id": a.application_id,
                    "household_token": a.household_token,
                    "quote": _num(a.quote),
                    "grant_status": a.grant_status,
                }
                for a in self.dataset.applications
                if a.grant_status == "approved"
            ]
        if plan is None:
            results = self.plans()
            primary = results[0] if results else None
            plan = (
                [
                    {
                        "application_id": line.application_id,
                        "household_token": line.household_token,
                        "provider_id": line.provider_id,
                        "grant": _num(line.grant),
                        "available_to": _available_to(
                            line.application_id, self.dataset
                        ),
                        "installed_on": _installed_on(
                            line.application_id, self.dataset
                        ),
                    }
                    for line in primary.awarded
                ]
                if primary
                else []
            )
        if outcomes is None:
            outcomes = [
                {
                    "application_id": row["application_id"],
                    "household_token": row["household_token"],
                    "measurement_status": row["measurement_status"],
                    "baseline_kwh": row["baseline_kwh"],
                    "post_bill_kwh": row["post_bill_kwh"],
                    "change_kwh": row["change_kwh"],
                }
                for row in self.status_report()
                if row["measurement_status"] in PUBLISHED_MEASUREMENTS
            ]
        publication = self.registry.publish(
            self.dataset,
            label=label,
            approvals=tuple(approvals),
            plan=tuple(plan),
            outcomes=tuple(outcomes),
            overrides=tuple(overrides or ()),
        )
        self._publications.append(publication)
        return publication

    def impact(self, label: str | None = None) -> list[PublicationImpact]:
        reports = []
        for publication in self._publications:
            if label and publication.label != label:
                continue
            reports.append(
                self.registry.impact(publication, self.dataset, self.statuses)
            )
        return reports

    @property
    def publications(self) -> tuple[PublishedOutcome, ...]:
        return tuple(self._publications)

    @property
    def decisions(self) -> tuple[dict, ...]:
        return tuple(self.registry.decisions)


def _scope_dict(scope) -> dict:
    return {
        "can_price_support": scope.can_price_support,
        "can_schedule": scope.can_schedule,
        "supportable": scope.supportable,
        "grant_ceiling": _num(scope.grant_ceiling),
        "grant_requested": _num(scope.grant_requested),
        "quote": _num(scope.quote),
        "capped_by_household_cap": scope.capped_by_household_cap,
        "measures": list(scope.measures),
        "blockers": list(scope.blockers),
        "notes": list(scope.notes),
    }


def _eligibility_dict(report) -> dict | None:
    if report is None:
        return None
    return {
        "priority_group_labels": list(report.priority_group_labels),
        "likely_eligible": report.likely_eligible,
    }


def _available_to(application_id: str, dataset: Dataset) -> str | None:
    application = dataset.application(application_id)
    if application is None or application.available_to is None:
        return None
    return application.available_to.isoformat()


def _installed_on(application_id: str, dataset: Dataset) -> str | None:
    application = dataset.application(application_id)
    if application is None or application.installed_on is None:
        return None
    return application.installed_on.isoformat()


def _num(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _count_by(items, attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = getattr(item, attribute)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def analyze(root: str | Path) -> Analysis:
    return Analysis(load_dataset(root))


__all__ = ["Analysis", "analyze", "DatasetError"]
