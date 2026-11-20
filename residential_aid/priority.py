"""Band-level priority grouping.

Priority is deliberately coarse.  Income is only recorded as a band, so the
analysis never derives a precise income figure and never turns the bands into
a single ordering of households.  Households are placed in tiers; inside a
tier the listing is alphabetical and is *not* a ranking.  Bill level is a
separate need signal and never on its own lifts a household into the top
tier, because a high bill does not establish that a household benefits most
from an electrification measure.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .bills import BillAttribution
from .model import TIER_LABELS, TIER_VALUES, Dataset, Household

TIERS = ("high", "medium", "low", "unranked")

BURDEN_THRESHOLDS = (
    (Decimal(100), "low"),
    (Decimal(200), "medium"),
    (None, "high"),
)

ORDERING_NOTICE = (
    "Households are grouped into bands only. Ordering within a band is "
    "alphabetical and is not a ranking; no precise income is inferred."
)


@dataclass(frozen=True)
class PriorityMember:
    household_token: str
    tier: str
    tier_label: str
    drivers: tuple[str, ...]
    income_band: str
    medical_need: bool
    burden_band: str
    kwh_per_occupant_month: Decimal | None
    exclusion: str = ""

    @property
    def rankable(self) -> bool:
        return self.tier != "unranked"


@dataclass(frozen=True)
class PriorityGroup:
    tier: str
    label: str
    households: tuple[str, ...]
    drivers: tuple[str, ...] = ()


@dataclass
class PriorityReport:
    members: tuple[PriorityMember, ...]
    notice: str = ORDERING_NOTICE

    def tier_of(self, household_token: str) -> str:
        member = self._index().get(household_token)
        return member.tier if member else "unranked"

    def member(self, household_token: str) -> PriorityMember | None:
        return self._index().get(household_token)

    def groups(self) -> tuple[PriorityGroup, ...]:
        grouped: dict[str, list[str]] = {tier: [] for tier in TIERS}
        drivers: dict[str, set[str]] = {tier: set() for tier in TIERS}
        for member in self.members:
            grouped.setdefault(member.tier, []).append(member.household_token)
            drivers[member.tier].update(member.drivers)
        return tuple(
            PriorityGroup(
                tier=tier,
                label=TIER_LABELS[tier],
                households=tuple(sorted(grouped.get(tier, []))),
                drivers=tuple(sorted(drivers.get(tier, set()))),
            )
            for tier in TIERS
            if grouped.get(tier)
        )

    def tier_order(self) -> tuple[str, ...]:
        """Tiers strongest first, with unranked last."""

        present = {member.tier for member in self.members}
        return tuple(tier for tier in TIERS if tier in present)

    def high_burden(self) -> tuple[str, ...]:
        return tuple(
            sorted(m.household_token for m in self.members if m.burden_band == "high")
        )

    def unranked(self) -> tuple[str, ...]:
        return tuple(sorted(m.household_token for m in self.members if not m.rankable))

    def counts(self) -> dict[str, int]:
        return {tier: len(group.households) for group in self.groups() for tier in (group.tier,)}

    def _index(self) -> dict[str, PriorityMember]:
        return {member.household_token: member for member in self.members}


def burden_band(kwh_per_occupant_month: Decimal | None) -> str:
    if kwh_per_occupant_month is None:
        return "unknown"
    for threshold, label in BURDEN_THRESHOLDS:
        if threshold is None or kwh_per_occupant_month < threshold:
            return label
    return "unknown"


def _kwh_per_occupant(
    attribution: BillAttribution, household: Household
) -> Decimal | None:
    if not attribution.attributable or attribution.kwh is None:
        return None
    if not attribution.periods or not household.occupants:
        return None
    months = Decimal(len(attribution.periods))
    per_month = attribution.kwh / months
    return (per_month / Decimal(household.occupants)).quantize(Decimal("0.01"))


def build_priority(
    dataset: Dataset, attributions: dict[str, BillAttribution]
) -> PriorityReport:
    members: list[PriorityMember] = []
    for household in sorted(dataset.households, key=lambda h: h.household_token):
        attribution = attributions.get(
            household.household_token
        )
        per_occupant = (
            _kwh_per_occupant(attribution, household) if attribution else None
        )
        band = burden_band(per_occupant)
        drivers: list[str] = []
        exclusion = ""

        if household.consent_status != "active":
            tier = "unranked"
            exclusion = f"consent is {household.consent_status or 'blank'}"
            drivers.append("consent not active")
        elif household.income_band == "low":
            tier = "high"
            drivers.append("income band: low")
        elif household.income_band == "middle":
            tier = "medium"
            drivers.append("income band: middle")
        elif household.income_band == "high":
            tier = "low"
            drivers.append("income band: high")
        else:
            tier = "unranked"
            exclusion = "income band not supplied"
            drivers.append("income band unknown")

        if household.medical_need:
            drivers.append("high medical need")
            # Medical need is a recognized priority group, but it cannot
            # substitute for the income band: the household is grouped with
            # its band, or stays unranked when no band is recorded.
            if tier == "low":
                tier = "medium"

        # A high bill alone never moves a household up a tier.
        if band == "high":
            drivers.append("high consumption per occupant (need signal, not a rank)")
        elif band == "unknown" and attribution and not attribution.attributable:
            drivers.append("consumption not attributable")

        members.append(
            PriorityMember(
                household_token=household.household_token,
                tier=tier,
                tier_label=TIER_LABELS[tier],
                drivers=tuple(drivers),
                income_band=household.income_band,
                medical_need=bool(household.medical_need),
                burden_band=band,
                kwh_per_occupant_month=per_occupant,
                exclusion=exclusion,
            )
        )

    return PriorityReport(members=tuple(members))


def tier_rank(tier: str) -> int:
    return TIER_VALUES.get(tier, 0)
