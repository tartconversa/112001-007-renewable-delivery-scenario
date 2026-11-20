"""Bill normalisation.

Two rules drive everything here:

* A correction or re-issue against an existing bill in the same period is a
  revision of that consumption, not fresh usage.  Re-issued positive
  statements therefore do not add to the period total; corrections adjust it.
* A shared account is never copied in full to each household.  Without an
  explicit allocation the account is reported as unattributable and no
  household-level kWh or bill figure is produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .model import Bill, Dataset, Issue, SharedAllocation, period_sort_key


@dataclass(frozen=True)
class BillAttribution:
    """Normalised consumption for one household.

    ``kwh`` and ``amount`` are ``None`` whenever the underlying account cannot
    be attributed to this household without guessing.
    """

    household_token: str
    account_token: str
    kwh: Decimal | None
    amount: Decimal | None
    periods: tuple[str, ...] = ()
    basis: str = ""
    attributable: bool = False
    reason: str = ""
    raw_net_kwh: Decimal | None = None
    revisions: tuple[str, ...] = ()
    share_factor: Decimal = Decimal(1)

    @property
    def attributed_kwh(self) -> Decimal:
        return self.kwh if self.kwh is not None else Decimal(0)


@dataclass
class _PeriodLedger:
    period: str
    net_kwh: Decimal = Decimal(0)
    net_amount: Decimal = Decimal(0)
    has_amount: bool = False
    revisions: list[str] = field(default_factory=list)


def _is_void(bill: Bill) -> bool:
    return bill.statement_status == "void"


def _ledger_for_account(
    dataset: Dataset, account_token: str
) -> tuple[dict[str, _PeriodLedger], list[Issue]]:
    """Collapse a shared account's statements into one net figure per period.

    Only the most recent base statement in a period is counted.  Earlier base
    statements in the same period are re-issues and are reported, not added.
    Adjustment rows (``adjustment_of``) are signed deltas applied on top.
    """

    bills = dataset.bills_for_account(account_token)
    order = {bill.bill_id: index for index, bill in enumerate(dataset.bills)}
    bill_by_id = {b.bill_id: b for b in dataset.bills}
    grouped: dict[str, dict[str, list[Bill]]] = {}
    issues: list[Issue] = []

    for bill in bills:
        if _is_void(bill):
            issues.append(
                Issue(
                    code="void_bill_excluded",
                    severity="info",
                    message="void statement excluded from usage",
                    record=bill.bill_id,
                )
            )
            continue
        grouped.setdefault(bill.period, {"base": [], "adjustment": []})
        if bill.adjustment_of:
            grouped[bill.period]["adjustment"].append(bill)
        elif bill.statement_status == "correction":
            # Reference-less correction: already a loader error, never usage.
            continue
        else:
            grouped[bill.period]["base"].append(bill)

    ledgers: dict[str, _PeriodLedger] = {}
    for period, parts in grouped.items():
        bases = sorted(parts["base"], key=lambda b: (order.get(b.bill_id, 0), b.bill_id))
        current = bases[-1] if bases else None
        superseded = bases[:-1]
        ledger = _PeriodLedger(period=period)

        if current is None:
            issues.append(
                Issue(
                    code="adjustment_without_base",
                    severity="warning",
                    message="period has adjustments but no base statement to revise",
                    record=period,
                )
            )

        superseded_ids = {bill.bill_id for bill in superseded}
        for bill in superseded:
            ledger.revisions.append(bill.bill_id)
            issues.append(
                Issue(
                    code="reissue_not_new_usage",
                    severity="warning",
                    message=(
                        "a later statement in the same period supersedes this one; "
                        "its kWh is not counted again"
                    ),
                    record=bill.bill_id,
                )
            )

        if current is not None:
            ledger.net_kwh += current.electric_kwh
            if current.amount_due is not None:
                ledger.net_amount += current.amount_due
                ledger.has_amount = True

        for bill in sorted(parts["adjustment"], key=lambda b: (order.get(b.bill_id, 0), b.bill_id)):
            target = bill_by_id.get(bill.adjustment_of)
            if (
                target is None
                or target.account_token != bill.account_token
                or target.period != bill.period
                or bill.adjustment_of == bill.bill_id
            ):
                # The loader has already reported why; never count it as usage.
                continue
            if bill.adjustment_of in superseded_ids:
                # The delta may already be folded into the re-issued total.
                # Applying it again would double-count, so hold it for review.
                issues.append(
                    Issue(
                        code="adjustment_targets_superseded",
                        severity="warning",
                        message=(
                            "adjustment targets a superseded statement; it is not "
                            "applied until staff confirm it is not already included"
                        ),
                        record=bill.bill_id,
                    )
                )
                continue
            ledger.net_kwh += bill.electric_kwh
            if bill.amount_due is not None:
                ledger.net_amount += bill.amount_due
                ledger.has_amount = True
            ledger.revisions.append(bill.bill_id)

        ledgers[period] = ledger

    return ledgers, issues


def account_period_kwh(
    dataset: Dataset, account_token: str, period: str
) -> Decimal | None:
    """Net kWh for one account in one period, or ``None`` if absent."""

    ledgers, _ = _ledger_for_account(dataset, account_token)
    ledger = ledgers.get(period)
    if ledger is None:
        return None
    return ledger.net_kwh


def _allocation_index(
    allocations: tuple[SharedAllocation, ...],
) -> dict[str, list[SharedAllocation]]:
    index: dict[str, list[SharedAllocation]] = {}
    for allocation in allocations:
        index.setdefault(allocation.account_token, []).append(allocation)
    return index


def _allocation_for(
    index: dict[str, list[SharedAllocation]], account: str, household: str
) -> SharedAllocation | None:
    for allocation in index.get(account, []):
        if allocation.household_token == household:
            return allocation
    return None


def _totals(ledgers: dict[str, _PeriodLedger]) -> tuple[Decimal, Decimal, bool, tuple[str, ...], tuple[str, ...]]:
    kwh = sum((l.net_kwh for l in ledgers.values()), Decimal(0))
    amount = sum((l.net_amount for l in ledgers.values()), Decimal(0))
    has_amount = all(l.has_amount for l in ledgers.values()) if ledgers else False
    periods = tuple(sorted(ledgers, key=period_sort_key))
    revisions = tuple(
        bill_id for period in periods for bill_id in ledgers[period].revisions
    )
    return kwh, amount, has_amount, periods, revisions


def build_attributions(dataset: Dataset) -> tuple[dict[str, BillAttribution], list[Issue]]:
    """Return one attribution per household plus attribution-level issues."""

    allocations = _allocation_index(dataset.allocations)
    issues: list[Issue] = []
    results: dict[str, BillAttribution] = {}
    account_members: dict[str, list] = {}
    for household in dataset.households:
        if household.account_token:
            account_members.setdefault(household.account_token, []).append(household)

    for account, members in sorted(account_members.items()):
        ledgers, ledger_issues = _ledger_for_account(dataset, account)
        issues.extend(ledger_issues)
        kwh, amount, has_amount, periods, revisions = _totals(ledgers)
        shared = len(members) > 1

        if shared:
            rows = list(allocations.get(account, []))
            total_share = sum((row.share for row in rows), Decimal(0))
            addresses = {row.household_token for row in rows}
            complete = (
                bool(rows)
                and total_share <= Decimal(1)
                and addresses >= {m.household_token for m in members}
            )
            for member in members:
                allocation = _allocation_for(allocations, account, member.household_token)
                if not complete or allocation is None:
                    reason = (
                        "shared account has no per-household split; the whole bill is not "
                        "repeated for this household"
                    )
                    issues.append(
                        Issue(
                            code="shared_account_unattributed",
                            severity="warning",
                            message=reason,
                            record=member.household_token,
                        )
                    )
                    results[member.household_token] = BillAttribution(
                        household_token=member.household_token,
                        account_token=account,
                        kwh=None,
                        amount=None,
                        periods=periods,
                        basis="",
                        attributable=False,
                        reason=reason,
                        raw_net_kwh=kwh,
                        revisions=revisions,
                    )
                else:
                    results[member.household_token] = BillAttribution(
                        household_token=member.household_token,
                        account_token=account,
                        kwh=kwh * allocation.share,
                        amount=amount * allocation.share if has_amount else None,
                        periods=periods,
                        basis=allocation.basis,
                        attributable=True,
                        reason="",
                        raw_net_kwh=kwh,
                        revisions=revisions,
                        share_factor=allocation.share,
                    )
        else:
            member = members[0]
            results[member.household_token] = BillAttribution(
                household_token=member.household_token,
                account_token=account,
                kwh=kwh,
                amount=amount if has_amount else None,
                periods=periods,
                basis="sole household on the account",
                attributable=True,
                reason="",
                raw_net_kwh=kwh,
                revisions=revisions,
            )

    for household in dataset.households:
        if household.household_token in results:
            continue
        reason = "household has no supply account"
        results[household.household_token] = BillAttribution(
            household_token=household.household_token,
            account_token=household.account_token,
            kwh=None,
            amount=None,
            attributable=False,
            reason=reason,
        )

    return results, issues
