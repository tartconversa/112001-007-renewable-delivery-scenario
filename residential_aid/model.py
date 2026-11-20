"""Domain records for the residential assistance analysis."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal


INCOME_BANDS = ("low", "middle", "high", "unknown")

TIER_VALUES = {"high": 3, "medium": 2, "low": 1, "unranked": 0}
TIER_LABELS = {
    "high": "high",
    "medium": "medium",
    "low": "lower",
    "unranked": "not rankable",
}

_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def quarter_bounds(period: str) -> tuple[date, date] | None:
    """Return the inclusive calendar bounds of ``YYYY-Qn`` or ``None``."""

    match = _QUARTER_RE.match(period.strip().upper())
    if not match:
        return None
    year = int(match.group(1))
    quarter = int(match.group(2))
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    start = date(year, start_month, 1)
    if end_month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, end_month + 1, 1).toordinal()
        end = date.fromordinal(end - 1)
    return start, end


def month_start(period: str) -> date | None:
    """Return the first day of a ``YYYY-MM`` period or ``None``."""

    match = _MONTH_RE.match(period.strip())
    if not match:
        return None
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return date(int(match.group(1)), month, 1)


def period_sort_key(period: str) -> tuple[int, int]:
    start = month_start(period)
    if start is None:
        return (9999, 12)
    return (start.year, start.month)


@dataclass(frozen=True)
class Issue:
    """A data-quality note tied to a record (never a silent correction)."""

    code: str
    severity: str
    message: str
    record: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "record": self.record,
        }


@dataclass(frozen=True)
class Household:
    household_token: str
    area_id: str
    income_band: str
    occupants: int
    account_token: str
    shared_account: bool
    consent_status: str
    medical_need: bool | None = None


@dataclass(frozen=True)
class Bill:
    bill_id: str
    account_token: str
    period: str
    electric_kwh: Decimal
    amount_due: Decimal
    adjustment_of: str = ""
    statement_status: str = "final"


@dataclass(frozen=True)
class Application:
    application_id: str
    household_token: str
    measure: str
    quote: Decimal | None
    provider_id: str
    available_from: date | None
    available_to: date | None
    grant_status: str
    installed_on: date | None = None
    post_kwh: Decimal | None = None


@dataclass(frozen=True)
class ProgramLimits:
    period: str
    total_budget: Decimal
    per_household_cap: Decimal
    max_active_installations: int
    priority_groups: tuple[str, ...]

    @property
    def start(self) -> date | None:
        bounds = quarter_bounds(self.period)
        return bounds[0] if bounds else None

    @property
    def end(self) -> date | None:
        bounds = quarter_bounds(self.period)
        return bounds[1] if bounds else None

    @property
    def key(self) -> str:
        return self.period


@dataclass(frozen=True)
class SharedAllocation:
    """An explicit, documented basis for splitting a shared account bill."""

    account_token: str
    household_token: str
    share: Decimal
    basis: str


@dataclass(frozen=True)
class Dataset:
    households: tuple[Household, ...] = ()
    bills: tuple[Bill, ...] = ()
    applications: tuple[Application, ...] = ()
    programs: tuple[ProgramLimits, ...] = ()
    allocations: tuple[SharedAllocation, ...] = ()
    parsed_issues: tuple[Issue, ...] = ()
    reference_issues: tuple[Issue, ...] = ()

    @property
    def issues(self) -> tuple[Issue, ...]:
        return self.parsed_issues + self.reference_issues

    def household(self, token: str) -> Household | None:
        return self._household_map().get(token)

    def application(self, token: str) -> Application | None:
        return self._application_map().get(token)

    def household_applications(self, token: str) -> tuple[Application, ...]:
        return tuple(a for a in self.applications if a.household_token == token)

    def bills_for_account(self, account_token: str) -> tuple[Bill, ...]:
        return tuple(b for b in self.bills if b.account_token == account_token)

    def households_for_account(self, account_token: str) -> tuple[Household, ...]:
        return tuple(h for h in self.households if h.account_token == account_token)

    def errors(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    def _household_map(self) -> dict[str, Household]:
        return {h.household_token: h for h in self.households}

    def _application_map(self) -> dict[str, Application]:
        return {a.application_id: a for a in self.applications}

    def fingerprint(self) -> str:
        """Stable digest of the input records, excluding derived issues."""

        payload = {
            "households": sorted(
                (asdict(h) for h in self.households),
                key=lambda row: row["household_token"],
            ),
            "bills": sorted((asdict(b) for b in self.bills), key=lambda row: row["bill_id"]),
            "applications": sorted(
                (asdict(a) for a in self.applications),
                key=lambda row: row["application_id"],
            ),
            "programs": sorted((asdict(p) for p in self.programs), key=lambda row: row["period"]),
            "allocations": sorted(
                (asdict(s) for s in self.allocations),
                key=lambda row: (row["account_token"], row["household_token"]),
            ),
        }
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def utc_timestamp() -> str:
    """Second-resolution UTC timestamp used in decision logs."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
