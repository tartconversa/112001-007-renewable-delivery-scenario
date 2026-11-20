"""Revisions and delays must flag what to revisit, without rewriting history."""

import unittest
from decimal import Decimal

from residential_aid.analysis import Analysis
from support import application, bill, dataset, household, program


def baseline():
    return dataset(
        households=[household("H1", account="E1"), household("H2", account="E2")],
        bills=[
            bill("B1", "E1", "2026-01", "1000"),
            bill("B2", "E1", "2026-02", "800"),
            bill("B3", "E2", "2026-01", "700"),
        ],
        applications=[
            application(
                "A1",
                "H1",
                status="approved",
                start="2026-01-01",
                end="2026-02-28",
                installed="2026-01-15",
                post_kwh="800",
            ),
            application(
                "A2",
                "H2",
                status="waiting",
                start="2026-03-01",
                end="2026-03-31",
            ),
        ],
        programs=[program()],
    )


class PublicationTests(unittest.TestCase):
    def test_publication_preserves_the_basis_it_was_issued_with(self):
        analysis = Analysis(baseline())
        publication = analysis.publish("q1-approvals")
        analysis.delay_install("A2", "2026-04-15", reason="provider rescheduled")
        self.assertEqual(
            [row["available_to"] for row in publication.plan if row["application_id"] == "A2"],
            ["2026-03-31"],
        )
        self.assertEqual(analysis.publications[0].published_at, publication.published_at)

    def test_delay_flags_the_published_slot_for_review(self):
        analysis = Analysis(baseline())
        analysis.publish("q1-approvals")
        analysis.delay_install("A2", "2026-04-15", reason="provider rescheduled")
        impact = analysis.impact()[0]
        self.assertIn("A2", impact.plan_slots_to_release)
        self.assertTrue(impact.records_changed)
        self.assertTrue(any("window moved" in reason for reason in impact.reasons))

    def test_unrelated_revision_marks_records_changed_without_false_alarms(self):
        analysis = Analysis(baseline())
        analysis.publish("q1-approvals")
        analysis.revise_bill(
            account_token="E2",
            period="2026-01",
            electric_kwh=Decimal("-50"),
            correction_of="B3",
            reason="meter re-read",
        )
        impact = analysis.impact()[0]
        self.assertTrue(impact.records_changed)
        self.assertEqual(impact.approvals_to_review, ())
        self.assertEqual(impact.plan_slots_to_release, ())

    def test_bill_revision_changes_attribution_but_not_the_old_publication(self):
        analysis = Analysis(baseline())
        analysis.publish("q1-approvals")
        before = analysis.attributions["H2"].kwh
        analysis.revise_bill(
            account_token="E2",
            period="2026-01",
            electric_kwh=Decimal("-50"),
            correction_of="B3",
        )
        self.assertEqual(before, Decimal("700"))
        self.assertEqual(analysis.attributions["H2"].kwh, Decimal("650"))
        self.assertEqual(analysis.publications[0].fingerprint != analysis.dataset.fingerprint(), True)

    def test_revised_bill_does_not_become_new_usage(self):
        analysis = Analysis(baseline())
        total_before = analysis.attributions["H1"].kwh
        analysis.revise_bill(
            account_token="E1", period="2026-01", electric_kwh=Decimal("30")
        )
        self.assertEqual(
            analysis.attributions["H1"].kwh, total_before + Decimal("30")
        )
        self.assertEqual(
            len([b for b in analysis.dataset.bills if b.account_token == "E1"]), 3
        )

    def test_revised_outcome_requires_correction_of_published_effect(self):
        analysis = Analysis(baseline())
        self.assertEqual(
            analysis.statuses["A1"].measurement_status, "measured improvement"
        )
        analysis.publish("q1-approvals")
        # A later full-period reading reverses the apparent saving.
        analysis.revise_bill(
            account_token="E1",
            period="2026-02",
            electric_kwh=Decimal("1500"),
            correction_of="B2",
            reason="estimated then actual",
        )
        impact = analysis.impact()[0]
        self.assertIn("A1", impact.published_effect_to_retract)
        self.assertTrue(
            any("needs correcting" in reason for reason in impact.reasons)
        )


if __name__ == "__main__":
    unittest.main()
