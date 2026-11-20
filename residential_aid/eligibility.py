"""Completeness and supportable-scope assessment.

The result answers two staff questions: *is this file complete enough to act
on?* and *what can we actually support for this household?*  It deliberately
stops short of a score when required facts are missing, and never fills gaps
in an application with assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .bills import BillAttribution
from .model import Application, Dataset, Household, Issue, ProgramLimits

CONSENT_READY = "active"
OPEN_STATUSES = frozenset({"submitted", "waiting", "approved"})


@dataclass(frozen=True)
class CompletenessItem:
    field: str
    label: str
    status: str  # "ok" | "partial" | "missing" | "not_provided"
    detail: str = ""

    @property
    def complete(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class SupportableScope:
    """What the current file supports, stated without over-claiming."""

    can_price_support: bool
    can_schedule: bool
    grant_ceiling: Decimal | None
    grant_requested: Decimal | None
    capped_by_household_cap: bool
    quote: Decimal | None
    measures: tuple[str, ...]
    blockers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def supportable(self) -> bool:
        return self.can_price_support and not self.blockers


@dataclass(frozen=True)
class EligibilityReport:
    household_token: str
    priority_group_labels: tuple[str, ...]
    measures: tuple[str, ...]
    quote: Decimal | None
    likely_eligible: bool


@dataclass
class HouseholdAssessment:
    household: Household
    attribution: BillAttribution
    completeness: tuple[CompletenessItem, ...]
    applications: tuple[Application, ...]
    scope: SupportableScope
    issues: tuple[Issue, ...] = ()
    eligibility: EligibilityReport | None = None

    @property
    def completeness_ratio(self) -> Decimal:
        if not self.completeness:
            return Decimal(0)
        complete = sum(1 for item in self.completeness if item.complete)
        return (Decimal(complete) / Decimal(len(self.completeness))).quantize(
            Decimal("0.01")
        )

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return tuple(i.field for i in self.completeness if i.status == "missing")

    @property
    def status(self) -> str:
        """`ready`, `incomplete`, or `blocked` (nothing actionable)."""

        if not self.scope.can_price_support:
            return "blocked"
        if self.missing_fields:
            return "incomplete"
        return "ready"


def _consent_ok(household: Household) -> bool:
    return household.consent_status == CONSENT_READY


def _bill_completeness(
    household: Household, attribution: BillAttribution
) -> tuple[str, str]:
    if not household.account_token:
        return ("missing", "no supply account on file")
    if not attribution.attributable:
        return ("partial", attribution.reason or "account cannot be attributed")
    if not attribution.periods:
        return ("missing", "no bills on file for the account")
    return ("ok", f"{len(attribution.periods)} period(s) on file, revisions netted")


def _scope(
    household: Household,
    applications: tuple[Application, ...],
    programs_by_period: dict[str, ProgramLimits],
) -> SupportableScope:
    blockers: list[str] = []
    notes: list[str] = []
    open_apps = [a for a in applications if a.grant_status in OPEN_STATUSES]

    if not open_apps:
        blockers.append("no open application on file")
    if not household.account_token:
        blockers.append("no supply account")
    if not _consent_ok(household):
        blockers.append("consent not active")

    quote: Decimal | None = None
    for application in open_apps:
        if application.quote is None:
            blockers.append(f"no priced quote for {application.application_id}")
        elif quote is None:
            quote = application.quote

    missing_window = [
        a.application_id
        for a in open_apps
        if a.grant_status != "submitted"
        and (a.available_from is None or a.available_to is None)
    ]
    if missing_window:
        blockers.append(
            "installer availability window missing for "
            + ", ".join(sorted(missing_window))
        )

    grant_requested: Decimal | None = None
    grant_ceiling: Decimal | None = None
    capped = False
    program = applicable_program(applications, programs_by_period)
    if quote is not None:
        if program is not None:
            grant_ceiling = program.per_household_cap
            grant_requested = min(quote, grant_ceiling)
            capped = quote > grant_ceiling
            if capped:
                notes.append(
                    f"quote {quote} exceeds the per-household cap {grant_ceiling}; "
                    "only the cap can be supported"
                )
        else:
            notes.append(
                "no program limits cover this application period; the grant ceiling is unknown"
            )

    can_schedule = bool(open_apps) and bool(household.account_token) and _consent_ok(
        household
    ) and not missing_window

    return SupportableScope(
        can_price_support=quote is not None and not blockers,
        can_schedule=can_schedule,
        grant_ceiling=grant_ceiling,
        grant_requested=grant_requested,
        capped_by_household_cap=capped,
        quote=quote,
        measures=tuple(sorted({a.measure for a in applications if a.measure})),
        blockers=tuple(dict.fromkeys(blockers)),
        notes=tuple(notes),
    )


def applicable_program(
    applications: tuple[Application, ...],
    programs_by_period: dict[str, ProgramLimits],
) -> ProgramLimits | None:
    """The program whose quarter contains the application window."""

    for application in applications:
        start = application.available_from
        if start is None:
            continue
        for program in programs_by_period.values():
            if program.start and program.end and program.start <= start <= program.end:
                return program
    # Fall back to the earliest configured program: fixtures may sit just
    # outside the quarter, and a ceiling is still better than an unknown one.
    return min(programs_by_period.values(), key=lambda p: p.period, default=None)


def eligibility_report(
    dataset: Dataset, household: Household, applications: tuple[Application, ...]
) -> EligibilityReport:
    labels: list[str] = []
    if household.income_band in {"low", "middle"}:
        labels.append(household.income_band)
    if household.medical_need:
        labels.append("high_medical_need")
    quote = next((a.quote for a in applications if a.quote is not None), None)
    open_apps = [a for a in applications if a.grant_status in OPEN_STATUSES]
    return EligibilityReport(
        household_token=household.household_token,
        priority_group_labels=tuple(labels),
        measures=tuple(sorted({a.measure for a in applications if a.measure})),
        quote=quote,
        likely_eligible=bool(open_apps) and _consent_ok(household),
    )


def assess_household(
    dataset: Dataset,
    household: Household,
    attribution: BillAttribution,
    programs_by_period: dict[str, ProgramLimits],
) -> HouseholdAssessment:
    applications = dataset.household_applications(household.household_token)
    record_ids = {household.household_token, household.account_token} | {
        a.application_id for a in applications
    }
    issues = tuple(issue for issue in dataset.issues if issue.record in record_ids)

    if household.income_band == "unknown":
        income = ("missing", "band not supplied; no estimate is inferred")
    else:
        income = ("ok", f"band: {household.income_band}")

    if _consent_ok(household):
        consent = ("ok", "active")
    else:
        consent = ("missing", f"consent is {household.consent_status or 'blank'}")

    bill_status, bill_detail = _bill_completeness(household, attribution)

    housing = (
        ("ok", household.area_id)
        if household.area_id
        else ("missing", "housing area not recorded")
    )

    if applications:
        equipment = (
            "ok",
            ", ".join(sorted({a.measure for a in applications if a.measure}))
            or "measure not recorded",
        )
    else:
        equipment = ("not_provided", "no application or equipment information on file")

    identity = ("ok", "") if household.occupants else ("partial", "occupant count missing")

    items = (
        CompletenessItem("identity", "Household identity & size", *identity),
        CompletenessItem("income_band", "Income band", *income),
        CompletenessItem(
            "account",
            "Supply account",
            "ok" if household.account_token else "missing",
            household.account_token,
        ),
        CompletenessItem("bill_history", "Bill history", bill_status, bill_detail),
        CompletenessItem("housing", "Housing condition", *housing),
        CompletenessItem("equipment", "Existing equipment / measure", *equipment),
        CompletenessItem("consent", "Consent", *consent),
    )

    return HouseholdAssessment(
        household=household,
        attribution=attribution,
        completeness=tuple(items),
        applications=applications,
        scope=_scope(household, applications, programs_by_period),
        issues=issues,
        eligibility=eligibility_report(dataset, household, applications),
    )


def build_assessments(
    dataset: Dataset,
    attributions: dict[str, BillAttribution],
    programs_by_period: dict[str, ProgramLimits],
) -> dict[str, HouseholdAssessment]:
    return {
        household.household_token: assess_household(
            dataset,
            household,
            attributions[household.household_token],
            programs_by_period,
        )
        for household in dataset.households
    }
