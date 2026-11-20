"""Slot and budget arrangement comparison.

The planner compares *arrangements* of the same application pool: how many
installation slots to open and to which installers, how to order the pool,
and what each arrangement does to the highest-need households, budget use and
installer capacity.  A scenario never mutates recorded grants; existing
approvals are treated as commitments and reported separately when they no
longer fit.

Rules that hold in every arrangement:

* The per-household cap applies to the total aid for one household, not to
  each measure separately.  Excess is trimmed and noted, never quietly paid.
* Slots are bounded per installer by the scenario's slot plan, because a
  provider cannot install more than it has capacity for.
* A household whose consent is not active, or that has no priceable
  application, does not occupy a slot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .model import Dataset, ProgramLimits, TIER_VALUES
from .priority import PriorityReport

ZERO = Decimal(0)


@dataclass(frozen=True)
class Scenario:
    name: str
    capacity: int
    budget: Decimal
    per_household_cap: Decimal
    slot_plan: dict[str, int] = field(default_factory=dict)
    reserve_high_slots: int = 0

    def with_slots(self, **changes) -> "Scenario":
        values = {
            "name": self.name,
            "capacity": self.capacity,
            "budget": self.budget,
            "per_household_cap": self.per_household_cap,
            "slot_plan": dict(self.slot_plan),
            "reserve_high_slots": self.reserve_high_slots,
        }
        values.update(changes)
        return Scenario(**values)


@dataclass(frozen=True)
class Candidate:
    application_id: str
    household_token: str
    provider_id: str
    measure: str
    quote: Decimal | None
    tier: str
    burden_band: str
    committed: bool
    reason: str = ""


@dataclass(frozen=True)
class AllocationLine:
    application_id: str
    household_token: str
    provider_id: str
    tier: str
    grant: Decimal
    action: str  # "existing commitment" | "new"
    note: str = ""


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    capacity: int
    budget: Decimal
    awarded: tuple[AllocationLine, ...]
    deferred: tuple[Candidate, ...]
    not_funded: tuple[Candidate, ...]
    excluded: tuple[Candidate, ...]
    budget_used: Decimal
    budget_remaining: Decimal
    slots_used: int
    slots_remaining: int
    provider_use: dict[str, int]
    tier_counts: dict[str, int]
    high_burden_served: int
    high_burden_waiting: int
    committed_served: int
    committed_at_risk: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def awarded_ids(self) -> tuple[str, ...]:
        return tuple(line.application_id for line in self.awarded)

    @property
    def deferred_ids(self) -> tuple[str, ...]:
        return tuple(c.application_id for c in self.deferred)

    def summary(self) -> dict:
        return {
            "name": self.name,
            "capacity": self.capacity,
            "budget": str(self.budget),
            "budget_used": str(self.budget_used),
            "budget_remaining": str(self.budget_remaining),
            "slots_used": self.slots_used,
            "slots_remaining": self.slots_remaining,
            "provider_use": dict(self.provider_use),
            "tier_counts": dict(self.tier_counts),
            "high_burden_served": self.high_burden_served,
            "high_burden_waiting": self.high_burden_waiting,
            "committed_at_risk": list(self.committed_at_risk),
            "awarded": [
                {
                    "application_id": line.application_id,
                    "household_token": line.household_token,
                    "provider_id": line.provider_id,
                    "tier": line.tier,
                    "grant": str(line.grant),
                    "action": line.action,
                    "note": line.note,
                }
                for line in self.awarded
            ],
            "deferred": [c.application_id for c in self.deferred],
            "not_funded": [c.application_id for c in self.not_funded],
            "excluded": [c.application_id for c in self.excluded],
            "notes": list(self.notes),
        }


def _candidates(
    dataset: Dataset, priority: PriorityReport
) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    new: list[Candidate] = []
    committed: list[Candidate] = []
    excluded: list[Candidate] = []

    for application in dataset.applications:
        if application.grant_status in {"rejected", "withdrawn"}:
            continue
        member = priority.member(application.household_token)
        household = dataset.household(application.household_token)
        candidate = Candidate(
            application_id=application.application_id,
            household_token=application.household_token,
            provider_id=application.provider_id,
            measure=application.measure,
            quote=application.quote,
            tier=member.tier if member else "unranked",
            burden_band=member.burden_band if member else "unknown",
            committed=application.grant_status == "approved",
        )
        if household is None:
            excluded.append(
                Candidate(**{**candidate.__dict__, "reason": "unknown household"})
            )
        elif household.consent_status != "active":
            excluded.append(
                Candidate(
                    **{
                        **candidate.__dict__,
                        "reason": f"consent is {household.consent_status or 'blank'}",
                    }
                )
            )
        elif application.quote is None:
            excluded.append(
                Candidate(
                    **{
                        **candidate.__dict__,
                        "reason": "no priced quote",
                    }
                )
            )
        elif application.grant_status == "approved":
            committed.append(candidate)
        else:
            new.append(candidate)
    return new, committed, excluded


def _order(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(
        candidates,
        key=lambda c: (
            -TIER_VALUES.get(c.tier, 0),
            c.household_token,
            c.application_id,
        ),
    )


def _even_slots(providers: list[str], capacity: int) -> dict[str, int]:
    if not providers:
        return {}
    base, extra = divmod(capacity, len(providers))
    return {
        provider: base + (1 if index < extra else 0)
        for index, provider in enumerate(providers)
    }


def default_slots(dataset: Dataset, program: ProgramLimits) -> dict[str, int]:
    providers = sorted(
        {
            a.provider_id
            for a in dataset.applications
            if a.provider_id and a.grant_status not in {"rejected", "withdrawn"}
        }
    )
    return _even_slots(providers, program.max_active_installations)


def run_scenario(
    dataset: Dataset,
    priority: PriorityReport,
    program: ProgramLimits,
    scenario: Scenario,
) -> ScenarioResult:
    new, committed, excluded = _candidates(dataset, priority)
    notes: list[str] = []

    slot_plan = dict(scenario.slot_plan) or _even_slots(
        sorted({c.provider_id for c in new + committed if c.provider_id}),
        scenario.capacity,
    )
    provider_use: dict[str, int] = {provider: 0 for provider in slot_plan}
    household_spend: dict[str, Decimal] = {}
    awarded: list[AllocationLine] = []
    deferred: list[Candidate] = []
    not_funded: list[Candidate] = []
    at_risk: list[str] = []
    used = ZERO

    def provider_room(candidate: Candidate) -> int:
        limit = slot_plan.get(candidate.provider_id, 0)
        return limit - provider_use.get(candidate.provider_id, 0)

    def grant_for(candidate: Candidate) -> tuple[Decimal, str]:
        """Grant after the household-level cap; returns (grant, note)."""

        if candidate.quote is None:
            return ZERO, "no priced quote"
        already = household_spend.get(candidate.household_token, ZERO)
        room = scenario.per_household_cap - already
        if room <= 0:
            return ZERO, (
                f"household already at the {scenario.per_household_cap} cap; "
                "a further measure cannot be subsidised"
            )
        if candidate.quote > room:
            return room, (
                f"grant trimmed from {candidate.quote} to {room}: household cap "
                f"{scenario.per_household_cap} across all measures"
            )
        return candidate.quote, ""

    # 1. Existing approvals are commitments; honour them before new awards.
    # They are not silently dropped when a scenario runs short: the household
    # keeps its priority place when the list of at-risk commitments is reported.
    for candidate in sorted(committed, key=lambda c: (c.provider_id, c.application_id)):
        grant, note = grant_for(candidate)
        if grant <= 0:
            not_funded.append(candidate)
            at_risk.append(candidate.application_id)
            notes.append(
                f"{candidate.application_id}: approved grant cannot be funded — {note}"
            )
            continue
        if used + grant > scenario.budget:
            not_funded.append(candidate)
            at_risk.append(candidate.application_id)
            notes.append(
                f"{candidate.application_id}: approved grant no longer fits the budget"
            )
            continue
        if provider_room(candidate) <= 0:
            not_funded.append(candidate)
            at_risk.append(candidate.application_id)
            notes.append(
                f"{candidate.application_id}: no {candidate.provider_id} slot for an "
                "existing approval"
            )
            continue
        used += grant
        household_spend[candidate.household_token] = (
            household_spend.get(candidate.household_token, ZERO) + grant
        )
        provider_use[candidate.provider_id] = provider_use.get(candidate.provider_id, 0) + 1
        awarded.append(
            AllocationLine(
                application_id=candidate.application_id,
                household_token=candidate.household_token,
                provider_id=candidate.provider_id,
                tier=candidate.tier,
                grant=grant,
                action="existing commitment",
                note=note,
            )
        )

    # 2. New awards, strongest priority tier first.
    committed_slots = len(awarded)
    capacity = max(scenario.capacity, committed_slots)
    committed_tiers = {
        line.tier for line in awarded if line.action == "existing commitment"
    }
    # Slots held back for the high tier only make sense if no high-tier
    # commitment already occupies one.
    reserved = 0
    if scenario.reserve_high_slots and "high" not in committed_tiers:
        high_new = [c for c in new if c.tier == "high"]
        reserved = min(
            scenario.reserve_high_slots,
            max(0, capacity - committed_slots),
            len(high_new),
        )
        if reserved:
            notes.append(
                f"holding {reserved} slot(s) open for the high-priority tier; "
                "lower tiers cannot fill them"
            )

    for candidate in _order(new):
        allowance = capacity if candidate.tier == "high" else capacity - reserved
        if len(awarded) >= allowance:
            deferred.append(candidate)
            if reserved and candidate.tier != "high":
                notes.append(
                    f"{candidate.application_id}: slot held for the high-priority "
                    "tier, so this lower-tier application waits"
                )
            else:
                notes.append(
                    f"{candidate.application_id}: no slot left in this arrangement"
                )
            continue
        grant, note = grant_for(candidate)
        if grant <= 0:
            not_funded.append(candidate)
            notes.append(f"{candidate.application_id}: {note}")
            continue
        if used + grant > scenario.budget:
            not_funded.append(candidate)
            notes.append(
                f"{candidate.application_id}: budget for this arrangement is exhausted"
            )
            continue
        if provider_room(candidate) <= 0:
            deferred.append(candidate)
            notes.append(
                f"{candidate.application_id}: {candidate.provider_id} has no free slot"
            )
            continue
        used += grant
        household_spend[candidate.household_token] = (
            household_spend.get(candidate.household_token, ZERO) + grant
        )
        provider_use[candidate.provider_id] = provider_use.get(candidate.provider_id, 0) + 1
        awarded.append(
            AllocationLine(
                application_id=candidate.application_id,
                household_token=candidate.household_token,
                provider_id=candidate.provider_id,
                tier=candidate.tier,
                grant=grant,
                action="new",
                note=note,
            )
        )

    tier_counts: dict[str, int] = {}
    for line in awarded:
        tier_counts[line.tier] = tier_counts.get(line.tier, 0) + 1

    awarded_households = {line.household_token for line in awarded}
    high_burden = set(priority.high_burden())
    committed_served = sum(1 for line in awarded if line.action == "existing commitment")

    if excluded:
        notes.append(
            f"{len(excluded)} application(s) excluded: consent not active, unknown "
            "household, or no priced quote"
        )
    if len(awarded) > scenario.capacity:
        notes.append(
            "existing commitments exceed the planned slot count; the arrangement "
            "reports them rather than dropping an approval"
        )

    return ScenarioResult(
        name=scenario.name,
        capacity=scenario.capacity,
        budget=scenario.budget,
        awarded=tuple(awarded),
        deferred=tuple(deferred),
        not_funded=tuple(not_funded),
        excluded=tuple(excluded),
        budget_used=used,
        budget_remaining=scenario.budget - used,
        slots_used=len(awarded),
        slots_remaining=max(0, scenario.capacity - len(awarded)),
        provider_use=provider_use,
        tier_counts=tier_counts,
        high_burden_served=len(high_burden & awarded_households),
        high_burden_waiting=len(high_burden - awarded_households),
        committed_served=committed_served,
        committed_at_risk=tuple(dict.fromkeys(at_risk)),
        notes=tuple(dict.fromkeys(notes)),
    )


def default_scenarios(dataset: Dataset, program: ProgramLimits) -> list[Scenario]:
    capacity = program.max_active_installations
    base_slots = default_slots(dataset, program)
    providers = sorted(base_slots)
    scenarios = [
        Scenario(
            name="current capacity",
            capacity=capacity,
            budget=program.total_budget,
            per_household_cap=program.per_household_cap,
            slot_plan=dict(base_slots),
        )
    ]

    if providers:
        more = dict(base_slots)
        more[providers[0]] += 1
        scenarios.append(
            Scenario(
                name="one extra slot",
                capacity=capacity + 1,
                budget=program.total_budget,
                per_household_cap=program.per_household_cap,
                slot_plan=more,
            )
        )

        narrow = {providers[0]: capacity}
        scenarios.append(
            Scenario(
                name=f"all slots at {providers[0]}",
                capacity=capacity,
                budget=program.total_budget,
                per_household_cap=program.per_household_cap,
                slot_plan=narrow,
                reserve_high_slots=1,
            )
        )

    fewer = dict(base_slots)
    if providers and capacity > 0:
        # Remove the reduced slot from the installer that currently holds the
        # most, so the total really is one fewer.
        shrink = max(providers, key=lambda provider: (fewer[provider], provider))
        if fewer[shrink] > 0:
            fewer[shrink] -= 1
    scenarios.append(
        Scenario(
            name="one fewer slot",
            capacity=max(0, capacity - 1),
            budget=program.total_budget,
            per_household_cap=program.per_household_cap,
            slot_plan=fewer,
        )
    )

    scenarios.append(
        Scenario(
            name="half budget",
            capacity=capacity,
            budget=(program.total_budget * Decimal("0.5")).quantize(Decimal("1")),
            per_household_cap=program.per_household_cap,
            slot_plan=dict(base_slots),
        )
    )
    return scenarios


def compare_scenarios(
    dataset: Dataset,
    priority: PriorityReport,
    program: ProgramLimits,
    scenarios: list[Scenario] | None = None,
) -> tuple[ScenarioResult, ...]:
    if scenarios is None:
        scenarios = default_scenarios(dataset, program)
    return tuple(
        run_scenario(dataset, priority, program, scenario) for scenario in scenarios
    )


def differences(
    left: ScenarioResult, right: ScenarioResult
) -> dict[str, list[str]]:
    """Which applications move between arrangements."""

    left_awarded = set(left.awarded_ids)
    right_awarded = set(right.awarded_ids)
    return {
        "awarded_only_in_left": sorted(left_awarded - right_awarded),
        "awarded_only_in_right": sorted(right_awarded - left_awarded),
        "both": sorted(left_awarded & right_awarded),
    }
