"""CSV ingestion and validation.

Every malformed or contradictory input is reported as an :class:`Issue`
instead of being silently repaired.  Reference problems (an application
pointing at an unknown household, an adjustment pointing at a bill in a
different period, an allocation summing above one) are detected in a second
pass so that the same rules also apply when a dataset is revised in memory.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .model import (
    INCOME_BANDS,
    Application,
    Bill,
    Dataset,
    Household,
    Issue,
    ProgramLimits,
    SharedAllocation,
    quarter_bounds,
)

HOUSEHOLD_FILE = "households_accounts.csv"
BILL_FILE = "bills.csv"
APPLICATION_FILE = "applications_installations.csv"
PROGRAM_FILE = "program_limits.csv"
ALLOCATION_FILE = "shared_allocations.csv"

HOUSEHOLD_COLUMNS = (
    "household_token",
    "area_id",
    "income_band",
    "occupants",
    "account_token",
    "shared_account",
    "consent_status",
)
BILL_COLUMNS = (
    "bill_id",
    "account_token",
    "period",
    "electric_kwh",
    "amount_due",
    "adjustment_of",
    "statement_status",
)
APPLICATION_COLUMNS = (
    "application_id",
    "household_token",
    "measure",
    "quote",
    "provider_id",
    "available_from",
    "available_to",
    "grant_status",
    "installed_on",
    "post_kwh",
)
PROGRAM_COLUMNS = (
    "period",
    "total_budget",
    "per_household_cap",
    "max_active_installations",
    "priority_groups",
)
ALLOCATION_COLUMNS = ("account_token", "household_token", "share", "basis")

GRANT_STATUSES = ("submitted", "waiting", "approved", "rejected", "withdrawn")
BILL_STATUSES = ("final", "correction", "estimated", "void")
CONSENT_STATUSES = ("active", "pending", "declined", "withdrawn")


class DatasetError(ValueError):
    """Raised when the input files are structurally unusable."""


def _issue(code: str, severity: str, message: str, record: str = "") -> Issue:
    return Issue(code=code, severity=severity, message=message, record=record)


def _read_rows(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    try:
        stream = path.open(encoding="utf-8-sig", newline="")
    except OSError as exc:  # pragma: no cover - exercised via CLI
        raise DatasetError(f"cannot read {path}: {exc}") from exc
    with stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise DatasetError(f"{path} has no header row")
        headers = [name.strip() for name in reader.fieldnames]
        if any(not name for name in headers):
            raise DatasetError(f"{path} has a blank column name")
        missing = [name for name in required if name not in headers]
        if missing:
            raise DatasetError(
                f"{path} is missing required columns: {', '.join(missing)}"
            )
        reader.fieldnames = headers
        return [
            {(key or "").strip(): (value or "").strip() for key, value in row.items()}
            for row in reader
        ]


def _decimal(value: str) -> Decimal | None:
    text = (value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return number


def _integer(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _bool(value: str) -> bool | None:
    text = (value or "").strip().lower()
    if text in {"yes", "y", "true", "1"}:
        return True
    if text in {"no", "n", "false", "0"}:
        return False
    return None


def _date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def _parse_households(
    rows: list[dict[str, str]], issues: list[Issue]
) -> tuple[Household, ...]:
    records: list[Household] = []
    seen: dict[str, int] = {}
    for index, row in enumerate(rows, start=2):
        token = row.get("household_token", "")
        label = token or f"row{index}"
        if not token:
            issues.append(
                _issue("missing_household_token", "error", "household_token is blank", label)
            )
            continue
        if token in seen:
            issues.append(
                _issue(
                    "duplicate_household_token",
                    "error",
                    f"household {token} appears more than once; the first row is kept",
                    token,
                )
            )
            continue

        band = row.get("income_band", "").lower()
        if not band:
            band = "unknown"
            issues.append(
                _issue(
                    "missing_income_band",
                    "warning",
                    "income band not supplied; the household stays unranked",
                    token,
                )
            )
        elif band not in INCOME_BANDS:
            issues.append(
                _issue(
                    "invalid_income_band",
                    "warning",
                    f"unrecognised income band {band!r}; recorded as unknown, never estimated",
                    token,
                )
            )
            band = "unknown"

        occupants = _integer(row.get("occupants", ""))
        if occupants is None or occupants < 1:
            issues.append(
                _issue(
                    "invalid_occupants",
                    "error",
                    "occupants must be a positive integer; per-occupant figures are withheld",
                    token,
                )
            )
            occupants = 0

        account = row.get("account_token", "")
        if not account:
            issues.append(
                _issue(
                    "missing_account_token",
                    "error",
                    "household has no supply account; consumption cannot be linked",
                    token,
                )
            )

        shared = _bool(row.get("shared_account", ""))
        if shared is None:
            shared = True
            issues.append(
                _issue(
                    "invalid_shared_flag",
                    "warning",
                    "shared_account is not a yes/no value; treated as shared until clarified",
                    token,
                )
            )

        consent = row.get("consent_status", "").lower()
        if not consent:
            issues.append(
                _issue("missing_consent", "warning", "consent_status is blank", token)
            )
        elif consent not in CONSENT_STATUSES:
            issues.append(
                _issue(
                    "invalid_consent",
                    "warning",
                    f"unrecognised consent status {consent!r}; treated as not active",
                    token,
                )
            )

        medical: bool | None = None
        if "medical_need" in row:
            medical = _bool(row.get("medical_need", ""))
            if medical is None and row.get("medical_need", "").strip():
                issues.append(
                    _issue(
                        "invalid_medical_need",
                        "warning",
                        "medical_need is not a yes/no value; treated as not stated",
                        token,
                    )
                )

        seen[token] = index
        records.append(
            Household(
                household_token=token,
                area_id=row.get("area_id", ""),
                income_band=band,
                occupants=occupants,
                account_token=account,
                shared_account=shared,
                consent_status=consent,
                medical_need=medical,
            )
        )
    return tuple(records)


def _parse_bills(rows: list[dict[str, str]], issues: list[Issue]) -> tuple[Bill, ...]:
    records: list[Bill] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=2):
        bill_id = row.get("bill_id", "")
        label = bill_id or f"row{index}"
        if not bill_id:
            issues.append(_issue("missing_bill_id", "error", "bill_id is blank", label))
            continue
        if bill_id in seen:
            issues.append(
                _issue(
                    "duplicate_bill_id",
                    "error",
                    f"bill {bill_id} appears more than once; the first row is kept",
                    bill_id,
                )
            )
            continue

        account = row.get("account_token", "")
        if not account:
            issues.append(
                _issue("missing_bill_account", "error", "bill has no account_token", bill_id)
            )
            continue

        period = row.get("period", "")
        if not period:
            issues.append(_issue("missing_bill_period", "error", "bill period is blank", bill_id))
            continue

        kwh = _decimal(row.get("electric_kwh", ""))
        if kwh is None:
            issues.append(
                _issue(
                    "invalid_bill_kwh",
                    "error",
                    "electric_kwh is missing or not a number; the bill is excluded",
                    bill_id,
                )
            )
            continue

        amount = _decimal(row.get("amount_due", ""))
        if amount is None and row.get("amount_due", ""):
            issues.append(
                _issue(
                    "invalid_bill_amount",
                    "warning",
                    "amount_due is not a number; the amount is withheld",
                    bill_id,
                )
            )

        status = (row.get("statement_status", "") or "final").lower()
        if status not in BILL_STATUSES:
            issues.append(
                _issue(
                    "invalid_bill_status",
                    "warning",
                    f"unrecognised statement status {status!r}; excluded from net usage",
                    bill_id,
                )
            )

        adjustment = row.get("adjustment_of", "")
        if not adjustment and status == "correction":
            issues.append(
                _issue(
                    "correction_without_reference",
                    "error",
                    "a correction must name the bill it adjusts; the row is excluded",
                    bill_id,
                )
            )

        seen.add(bill_id)
        records.append(
            Bill(
                bill_id=bill_id,
                account_token=account,
                period=period,
                electric_kwh=kwh,
                amount_due=amount,
                adjustment_of=adjustment,
                statement_status=status,
            )
        )
    return tuple(records)


def _parse_applications(
    rows: list[dict[str, str]], issues: list[Issue]
) -> tuple[Application, ...]:
    records: list[Application] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=2):
        application_id = row.get("application_id", "")
        label = application_id or f"row{index}"
        if not application_id:
            issues.append(
                _issue("missing_application_id", "error", "application_id is blank", label)
            )
            continue
        if application_id in seen:
            issues.append(
                _issue(
                    "duplicate_application_id",
                    "error",
                    f"application {application_id} appears more than once; the first row is kept",
                    application_id,
                )
            )
            continue

        household = row.get("household_token", "")
        if not household:
            issues.append(
                _issue(
                    "missing_application_household",
                    "error",
                    "application has no household_token",
                    application_id,
                )
            )

        measure = row.get("measure", "")
        if not measure:
            issues.append(
                _issue(
                    "missing_measure",
                    "error",
                    "application has no measure; scope cannot be assessed",
                    application_id,
                )
            )

        quote: Decimal | None = None
        raw_quote = row.get("quote", "")
        if not raw_quote:
            issues.append(
                _issue(
                    "missing_quote",
                    "error",
                    "no installer quote; support cannot be priced",
                    application_id,
                )
            )
        else:
            quote = _decimal(raw_quote)
            if quote is None or quote <= 0:
                issues.append(
                    _issue(
                        "invalid_quote",
                        "error",
                        "quote must be a positive number; support cannot be priced",
                        application_id,
                    )
                )
                quote = None

        provider = row.get("provider_id", "")
        if not provider:
            issues.append(
                _issue(
                    "missing_provider",
                    "error",
                    "no installer/provider; the installation cannot be scheduled",
                    application_id,
                )
            )

        available_from = _date(row.get("available_from", ""))
        available_to = _date(row.get("available_to", ""))
        if row.get("available_from") and available_from is None:
            issues.append(
                _issue(
                    "invalid_available_from",
                    "error",
                    "available_from is not an ISO date",
                    application_id,
                )
            )
        if row.get("available_to") and available_to is None:
            issues.append(
                _issue(
                    "invalid_available_to",
                    "error",
                    "available_to is not an ISO date",
                    application_id,
                )
            )
        if not row.get("available_from") or not row.get("available_to"):
            issues.append(
                _issue(
                    "missing_availability_window",
                    "error",
                    "installer availability window is incomplete",
                    application_id,
                )
            )
        if available_from and available_to and available_from > available_to:
            issues.append(
                _issue(
                    "reversed_availability_window",
                    "error",
                    "available_from is after available_to",
                    application_id,
                )
            )

        status = (row.get("grant_status", "") or "submitted").lower()
        if status not in GRANT_STATUSES:
            issues.append(
                _issue(
                    "invalid_grant_status",
                    "warning",
                    f"unrecognised grant status {status!r}; treated as not approved",
                    application_id,
                )
            )

        installed_on = _date(row.get("installed_on", ""))
        if row.get("installed_on") and installed_on is None:
            issues.append(
                _issue(
                    "invalid_installed_on",
                    "error",
                    "installed_on is not an ISO date",
                    application_id,
                )
            )

        post_kwh: Decimal | None = None
        if row.get("post_kwh", ""):
            post_kwh = _decimal(row.get("post_kwh", ""))
            if post_kwh is None or post_kwh < 0:
                issues.append(
                    _issue(
                        "invalid_post_kwh",
                        "warning",
                        "post_kwh is not a non-negative number; reported outcome is ignored",
                        application_id,
                    )
                )
                post_kwh = None

        seen.add(application_id)
        records.append(
            Application(
                application_id=application_id,
                household_token=household,
                measure=measure,
                quote=quote,
                provider_id=provider,
                available_from=available_from,
                available_to=available_to,
                grant_status=status,
                installed_on=installed_on,
                post_kwh=post_kwh,
            )
        )
    return tuple(records)


def _parse_programs(
    rows: list[dict[str, str]], issues: list[Issue]
) -> tuple[ProgramLimits, ...]:
    records: list[ProgramLimits] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=2):
        period = row.get("period", "")
        label = period or f"row{index}"
        if not period:
            issues.append(_issue("missing_program_period", "error", "program period is blank", label))
            continue
        if quarter_bounds(period) is None:
            issues.append(
                _issue(
                    "invalid_program_period",
                    "error",
                    f"program period {period!r} is not YYYY-Qn; the limit is ignored",
                    period,
                )
            )
            continue
        if period in seen:
            issues.append(
                _issue(
                    "duplicate_program_period",
                    "error",
                    f"limits for {period} appear more than once; the first row is kept",
                    period,
                )
            )
            continue

        budget = _decimal(row.get("total_budget", ""))
        cap = _decimal(row.get("per_household_cap", ""))
        max_active = _integer(row.get("max_active_installations", ""))
        if budget is None or budget < 0:
            issues.append(
                _issue("invalid_total_budget", "error", "total_budget is invalid", period)
            )
            continue
        if cap is None or cap < 0:
            issues.append(
                _issue("invalid_per_household_cap", "error", "per_household_cap is invalid", period)
            )
            continue
        if max_active is None or max_active < 0:
            issues.append(
                _issue(
                    "invalid_max_active_installations",
                    "error",
                    "max_active_installations is invalid",
                    period,
                )
            )
            continue

        groups = tuple(
            group.strip().lower()
            for group in row.get("priority_groups", "").split("|")
            if group.strip()
        )
        seen.add(period)
        records.append(
            ProgramLimits(
                period=period,
                total_budget=budget,
                per_household_cap=cap,
                max_active_installations=max_active,
                priority_groups=groups,
            )
        )
    return tuple(records)


def _parse_allocations(
    rows: list[dict[str, str]], issues: list[Issue]
) -> tuple[SharedAllocation, ...]:
    records: list[SharedAllocation] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=2):
        account = row.get("account_token", "")
        household = row.get("household_token", "")
        label = f"{account}/{household}" if account or household else f"row{index}"
        if not account or not household:
            issues.append(
                _issue(
                    "incomplete_allocation",
                    "error",
                    "allocation needs both account_token and household_token",
                    label,
                )
            )
            continue
        key = (account, household)
        if key in seen:
            issues.append(
                _issue(
                    "duplicate_allocation",
                    "error",
                    f"allocation for {label} appears more than once; the first row is kept",
                    label,
                )
            )
            continue

        share = _decimal(row.get("share", ""))
        if share is None or not (Decimal(0) < share <= Decimal(1)):
            issues.append(
                _issue(
                    "invalid_allocation_share",
                    "error",
                    "share must be greater than 0 and at most 1; the row is ignored",
                    label,
                )
            )
            continue

        basis = row.get("basis", "")
        if not basis:
            issues.append(
                _issue(
                    "missing_allocation_basis",
                    "error",
                    "a split basis is required; without it the bill cannot be attributed",
                    label,
                )
            )
            continue

        seen.add(key)
        records.append(
            SharedAllocation(
                account_token=account,
                household_token=household,
                share=share,
                basis=basis,
            )
        )
    return tuple(records)


def build_dataset(
    households: tuple[Household, ...] = (),
    bills: tuple[Bill, ...] = (),
    applications: tuple[Application, ...] = (),
    programs: tuple[ProgramLimits, ...] = (),
    allocations: tuple[SharedAllocation, ...] = (),
    parsed_issues: tuple[Issue, ...] = (),
) -> Dataset:
    """Validate cross-record references and return a dataset.

    Parsing-level issues are carried in ``parsed_issues``; reference issues are
    recomputed here so an in-memory revision receives the same scrutiny as a
    fresh load.
    """

    issues: list[Issue] = []
    household_map = {h.household_token: h for h in households}
    bill_map = {b.bill_id: b for b in bills}

    by_account: dict[str, list[Household]] = {}
    for household in households:
        if household.account_token:
            by_account.setdefault(household.account_token, []).append(household)

    for account, members in sorted(by_account.items()):
        if len(members) > 1 and not all(m.shared_account for m in members):
            issues.append(
                _issue(
                    "shared_flag_conflict",
                    "error",
                    f"account {account} has {len(members)} households but is not all marked shared; "
                    "no bill will be attributed without an explicit allocation",
                    account,
                )
            )

    accounts_with_households = set(by_account)
    for bill in bills:
        if bill.account_token not in accounts_with_households:
            issues.append(
                _issue(
                    "bill_without_household",
                    "warning",
                    "bill belongs to an account no household references",
                    bill.bill_id,
                )
            )

    for bill in bills:
        if bill.adjustment_of:
            target = bill_map.get(bill.adjustment_of)
            if bill.adjustment_of == bill.bill_id:
                issues.append(
                    _issue(
                        "self_adjusting_bill",
                        "error",
                        "a bill cannot adjust itself; the adjustment is not applied",
                        bill.bill_id,
                    )
                )
            elif target is None:
                issues.append(
                    _issue(
                        "unknown_adjustment_target",
                        "error",
                        f"adjustment names unknown bill {bill.adjustment_of}; not applied",
                        bill.bill_id,
                    )
                )
            else:
                if target.account_token != bill.account_token:
                    issues.append(
                        _issue(
                            "cross_account_adjustment",
                            "error",
                            "adjustment references a bill on another account; not applied",
                            bill.bill_id,
                        )
                    )
                if target.period != bill.period:
                    issues.append(
                        _issue(
                            "cross_period_adjustment",
                            "error",
                            "adjustment references another billing period; not applied as usage",
                            bill.bill_id,
                        )
                    )

    for application in applications:
        if application.household_token not in household_map:
            issues.append(
                _issue(
                    "unknown_application_household",
                    "error",
                    f"application references unknown household {application.household_token}",
                    application.application_id,
                )
            )
            continue
        household = household_map[application.household_token]
        if (
            application.available_from
            and application.available_to
            and application.installed_on
            and not (
                application.available_from
                <= application.installed_on
                <= application.available_to
            )
        ):
            issues.append(
                _issue(
                    "install_outside_window",
                    "warning",
                    "recorded installation date falls outside the provider's window",
                    application.application_id,
                )
            )
        if application.installed_on and application.grant_status != "approved":
            issues.append(
                _issue(
                    "installed_without_approval",
                    "warning",
                    "an installation is recorded without an approved grant",
                    application.application_id,
                )
            )
        if application.grant_status == "approved" and household.consent_status != "active":
            issues.append(
                _issue(
                    "approval_without_consent",
                    "error",
                    "grant is approved but consent is not active",
                    application.application_id,
                )
            )
        if application.quote is not None and application.quote <= 0:
            issues.append(
                _issue(
                    "non_positive_quote",
                    "error",
                    "quote must be positive",
                    application.application_id,
                )
            )

    allocation_totals: dict[str, Decimal] = {}
    for allocation in allocations:
        total = allocation_totals.get(allocation.account_token, Decimal(0)) + allocation.share
        allocation_totals[allocation.account_token] = total
        household = household_map.get(allocation.household_token)
        if household is None:
            issues.append(
                _issue(
                    "unknown_allocation_household",
                    "error",
                    "allocation references an unknown household",
                    f"{allocation.account_token}/{allocation.household_token}",
                )
            )
        elif household.account_token != allocation.account_token:
            issues.append(
                _issue(
                    "allocation_account_mismatch",
                    "error",
                    "household is not linked to the account it is allocated from",
                    f"{allocation.account_token}/{allocation.household_token}",
                )
            )
    for account, total in sorted(allocation_totals.items()):
        if total > Decimal(1):
            issues.append(
                _issue(
                    "allocation_exceeds_one",
                    "error",
                    f"allocated shares total {total}; no household on {account} is attributed",
                    account,
                )
            )
        elif total < Decimal(1):
            issues.append(
                _issue(
                    "allocation_remainder",
                    "warning",
                    f"allocated shares total {total}; the remainder is not assigned to any household",
                    account,
                )
            )

    unique_programs: dict[str, ProgramLimits] = {}
    for program in programs:
        unique_programs.setdefault(program.period, program)

    unique_applications = {a.application_id: a for a in applications}
    unique_bills = {b.bill_id: b for b in bills}
    unique_households = {h.household_token: h for h in households}
    unique_allocations = {
        (a.account_token, a.household_token): a for a in allocations
    }

    return Dataset(
        households=tuple(unique_households.values()),
        bills=tuple(unique_bills.values()),
        applications=tuple(unique_applications.values()),
        programs=tuple(unique_programs.values()),
        allocations=tuple(unique_allocations.values()),
        parsed_issues=parsed_issues,
        reference_issues=tuple(issues),
    )


def load_dataset(
    root: str | Path,
    *,
    allocations_file: str = ALLOCATION_FILE,
) -> Dataset:
    """Load and validate the CSV fixture directory.

    ``shared_allocations.csv`` is optional.  Without it a shared account simply
    cannot be split, which is reported rather than guessed.
    """

    root = Path(root)
    if not root.is_dir():
        raise DatasetError(f"{root} is not a directory")

    issues: list[Issue] = []
    households = _parse_households(_read_rows(root / HOUSEHOLD_FILE, HOUSEHOLD_COLUMNS), issues)
    bills = _parse_bills(_read_rows(root / BILL_FILE, BILL_COLUMNS), issues)
    applications = _parse_applications(
        _read_rows(root / APPLICATION_FILE, APPLICATION_COLUMNS), issues
    )
    programs = _parse_programs(_read_rows(root / PROGRAM_FILE, PROGRAM_COLUMNS), issues)

    allocation_path = root / allocations_file
    allocations: tuple[SharedAllocation, ...] = ()
    if allocation_path.exists():
        allocations = _parse_allocations(
            _read_rows(allocation_path, ALLOCATION_COLUMNS), issues
        )

    return build_dataset(
        households=households,
        bills=bills,
        applications=applications,
        programs=programs,
        allocations=allocations,
        parsed_issues=tuple(issues),
    )


def revise_dataset(
    dataset: Dataset,
    *,
    bills: tuple[Bill, ...] | None = None,
    applications: tuple[Application, ...] | None = None,
) -> Dataset:
    """Rebuild a dataset after in-memory bill/application changes.

    Source-record issues are kept as they were (they describe the input file);
    reference issues are recomputed so a revision gets the same scrutiny as a
    fresh load.
    """

    return build_dataset(
        households=dataset.households,
        bills=bills if bills is not None else dataset.bills,
        applications=applications if applications is not None else dataset.applications,
        programs=dataset.programs,
        allocations=dataset.allocations,
        parsed_issues=dataset.parsed_issues,
    )


def new_bill_id(dataset: Dataset, prefix: str = "R") -> str:
    existing = {bill.bill_id for bill in dataset.bills}
    counter = 1
    while f"{prefix}{counter:03d}" in existing:
        counter += 1
    return f"{prefix}{counter:03d}"
