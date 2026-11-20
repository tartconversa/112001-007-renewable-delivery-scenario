"""Slot and budget arrangement comparisons."""

import unittest
from decimal import Decimal

from residential_aid.analysis import Analysis
from residential_aid.planning import Scenario, differences
from support import application, bill, dataset, household, program


def pool(slots=2, budget="42000", cap="9000"):
    return dataset(
        households=[
            household("H1", band="low"),
            household("H2", band="low"),
            household("H3", band="middle"),
            household("H4", band="high"),
        ],
        bills=[
            bill("B1", "E-H1", "2026-07", "900"),
            bill("B2", "E-H2", "2026-07", "800"),
            bill("B3", "E-H3", "2026-07", "700"),
            bill("B4", "E-H4", "2026-07", "600"),
        ],
        applications=[
            application("A1", "H1", quote="5000", provider="INST-A"),
            application("A2", "H2", quote="5000", provider="INST-A"),
            application("A3", "H3", quote="5000", provider="INST-B"),
            application("A4", "H4", quote="5000", provider="INST-B", status="approved"),
        ],
        programs=[program(slots=slots, budget=budget, cap=cap)],
    )


class PlanningTests(unittest.TestCase):
    def test_tighter_slot_count_changes_who_is_served(self):
        analysis = Analysis(pool())
        program = analysis.dataset.programs[0]
        wide = analysis.plan(
            Scenario(
                name="wide",
                capacity=3,
                budget=program.total_budget,
                per_household_cap=program.per_household_cap,
                slot_plan={"INST-A": 2, "INST-B": 1},
            )
        )
        narrow = analysis.plan(
            Scenario(
                name="narrow",
                capacity=2,
                budget=program.total_budget,
                per_household_cap=program.per_household_cap,
                slot_plan={"INST-A": 1, "INST-B": 1},
            )
        )
        change = differences(wide, narrow)
        self.assertIn("A2", change["awarded_only_in_left"])
        self.assertIn("A1", change["both"])
        self.assertLessEqual(narrow.slots_used, 2)

    def test_existing_approval_is_a_commitment_not_a_choice(self):
        analysis = Analysis(pool(slots=1))
        program = analysis.dataset.programs[0]
        result = analysis.plan(
            Scenario(
                name="one slot",
                capacity=1,
                budget=program.total_budget,
                per_household_cap=program.per_household_cap,
                slot_plan={"INST-A": 0, "INST-B": 1},
            )
        )
        self.assertIn("A4", result.awarded_ids)
        self.assertEqual(result.committed_served, 1)

    def test_budget_shortfall_reports_commitments_at_risk(self):
        analysis = Analysis(pool())
        program = analysis.dataset.programs[0]
        result = analysis.plan(
            Scenario(
                name="tiny budget",
                capacity=4,
                budget=Decimal("1000"),
                per_household_cap=program.per_household_cap,
                slot_plan={"INST-A": 2, "INST-B": 2},
            )
        )
        self.assertIn("A4", result.committed_at_risk)
        self.assertLessEqual(result.budget_used, Decimal("1000"))

    def test_household_cap_applies_across_measures(self):
        data = dataset(
            households=[household("H1", band="low")],
            bills=[bill("B1", "E-H1", "2026-07", "900")],
            applications=[
                application("A1", "H1", quote="7000", measure="heat_pump"),
                application("A2", "H1", quote="7000", measure="solar", provider="INST-B"),
            ],
            programs=[program(cap="9000", slots=2)],
        )
        analysis = Analysis(data)
        limits = data.programs[0]
        result = analysis.plan(
            Scenario(
                name="two measures",
                capacity=2,
                budget=limits.total_budget,
                per_household_cap=limits.per_household_cap,
                slot_plan={"INST-A": 1, "INST-B": 1},
            )
        )
        self.assertEqual(result.budget_used, Decimal("9000"))

    def test_consent_withdrawn_application_does_not_take_a_slot(self):
        data = dataset(
            households=[household("H1", band="low", consent="withdrawn")],
            bills=[bill("B1", "E-H1", "2026-07", "900")],
            applications=[application("A1", "H1")],
            programs=[program(slots=3)],
        )
        analysis = Analysis(data)
        limits = data.programs[0]
        result = analysis.plan(
            Scenario(
                name="test",
                capacity=3,
                budget=limits.total_budget,
                per_household_cap=limits.per_household_cap,
                slot_plan={"INST-A": 3},
            )
        )
        self.assertNotIn("A1", result.awarded_ids)
        self.assertIn("A1", [c.application_id for c in result.excluded])
        self.assertEqual(result.provider_use["INST-A"], 0)

    def test_reservation_holds_a_slot_the_lower_tier_cannot_fill(self):
        data = dataset(
            households=[household("H1", band="low"), household("H2", band="middle")],
            bills=[
                bill("B1", "E-H1", "2026-07", "900"),
                bill("B2", "E-H2", "2026-07", "600"),
            ],
            applications=[
                application("A1", "H1", quote="5000", provider="INST-A"),
                application("A2", "H2", quote="5000", provider="INST-B"),
            ],
            programs=[program(slots=1)],
        )
        analysis = Analysis(data)
        limits = data.programs[0]
        scenario = Scenario(
            name="reserved",
            capacity=1,
            budget=limits.total_budget,
            per_household_cap=limits.per_household_cap,
            slot_plan={"INST-A": 0, "INST-B": 1},
            reserve_high_slots=1,
        )
        with_reservation = analysis.plan(scenario)
        without = analysis.plan(scenario.with_slots(reserve_high_slots=0))

        # High tier cannot use the free INST-B slot; the reservation keeps it
        # open rather than handing it to the middle tier.
        self.assertNotIn("A1", with_reservation.awarded_ids)
        self.assertNotIn("A2", with_reservation.awarded_ids)
        self.assertEqual(with_reservation.slots_remaining, 1)
        self.assertTrue(
            any("holding 1 slot" in note for note in with_reservation.notes)
        )

        self.assertIn("A2", without.awarded_ids)

    def test_reservation_is_not_added_on_top_of_a_high_commitment(self):
        data = pool(slots=3)
        analysis = Analysis(data)
        limits = data.programs[0]
        result = analysis.plan(
            Scenario(
                name="reserved",
                capacity=3,
                budget=limits.total_budget,
                per_household_cap=limits.per_household_cap,
                slot_plan={"INST-A": 2, "INST-B": 1},
                reserve_high_slots=5,
            )
        )
        # The high tier already has a commitment; clamping stops the
        # reservation from shrinking the pool below what is real.
        self.assertIn("A4", result.awarded_ids)
        self.assertEqual(result.slots_used, 3)

    def test_scenario_does_not_mutate_recorded_grants(self):
        analysis = Analysis(pool(slots=4))
        before = {a.application_id: a.grant_status for a in analysis.dataset.applications}
        analysis.plans()
        after = {a.application_id: a.grant_status for a in analysis.dataset.applications}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
