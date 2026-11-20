"""Shared helpers for building small synthetic datasets in tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from residential_aid.loader import build_dataset
from residential_aid.model import (
    Application,
    Bill,
    Household,
    ProgramLimits,
    SharedAllocation,
)


def household(
    token: str,
    *,
    account: str | None = None,
    band: str = "low",
    occupants: int = 3,
    shared: bool = False,
    consent: str = "active",
    area: str = "North",
    medical: bool | None = None,
) -> Household:
    return Household(
        household_token=token,
        area_id=area,
        income_band=band,
        occupants=occupants,
        account_token=account if account is not None else f"E-{token}",
        shared_account=shared,
        consent_status=consent,
        medical_need=medical,
    )


def bill(
    bill_id: str,
    account: str,
    period: str,
    kwh: str,
    amount: str = "0",
    *,
    adjustment_of: str = "",
    status: str = "final",
) -> Bill:
    return Bill(
        bill_id=bill_id,
        account_token=account,
        period=period,
        electric_kwh=Decimal(kwh),
        amount_due=Decimal(amount),
        adjustment_of=adjustment_of,
        statement_status=status,
    )


def application(
    application_id: str,
    household_token: str,
    *,
    quote: str | None = "5000",
    provider: str = "INST-A",
    start: str = "2026-07-01",
    end: str = "2026-07-31",
    status: str = "waiting",
    installed: str | None = None,
    post_kwh: str | None = None,
    measure: str = "heat_pump",
) -> Application:
    return Application(
        application_id=application_id,
        household_token=household_token,
        measure=measure,
        quote=Decimal(quote) if quote is not None else None,
        provider_id=provider,
        available_from=date.fromisoformat(start) if start else None,
        available_to=date.fromisoformat(end) if end else None,
        grant_status=status,
        installed_on=date.fromisoformat(installed) if installed else None,
        post_kwh=Decimal(post_kwh) if post_kwh is not None else None,
    )


def program(
    *,
    period: str = "2026-Q3",
    budget: str = "42000",
    cap: str = "9000",
    slots: int = 6,
    groups: tuple[str, ...] = ("low", "high_medical_need"),
) -> ProgramLimits:
    return ProgramLimits(
        period=period,
        total_budget=Decimal(budget),
        per_household_cap=Decimal(cap),
        max_active_installations=slots,
        priority_groups=groups,
    )


def allocation(account: str, token: str, share: str, basis: str = "occupancy") -> SharedAllocation:
    return SharedAllocation(
        account_token=account,
        household_token=token,
        share=Decimal(share),
        basis=basis,
    )


def dataset(households=(), bills=(), applications=(), programs=(), allocations=()):
    return build_dataset(
        households=tuple(households),
        bills=tuple(bills),
        applications=tuple(applications),
        programs=tuple(programs),
        allocations=tuple(allocations),
    )
