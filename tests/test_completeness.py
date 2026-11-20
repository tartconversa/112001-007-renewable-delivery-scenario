"""Completeness, supportable scope and loader validation."""

import unittest
from decimal import Decimal

from residential_aid.analysis import Analysis
from residential_aid.loader import DatasetError, load_dataset
from support import allocation, application, bill, dataset, household, program


class CompletenessTests(unittest.TestCase):
    def test_missing_income_band_blocks_a_ranked_result(self):
        data = dataset(
            households=[household("H1", band="unknown")],
            bills=[bill("B1", "E-H1", "2026-01", "500")],
            applications=[application("A1", "H1")],
            programs=[program()],
        )
        row = Analysis(data).completeness_report()[0]
        self.assertIn("income_band", row["missing"])
        self.assertEqual(row["priority_tier"], "unranked")
        self.assertEqual(row["items"][1]["status"], "missing")

    def test_scope_states_what_can_be_supported(self):
        data = dataset(
            households=[household("H1")],
            bills=[bill("B1", "E-H1", "2026-01", "500")],
            applications=[
                application(
                    "A1",
                    "H1",
                    quote="12000",
                    start="2026-07-01",
                    end="2026-07-31",
                )
            ],
            programs=[program(cap="9000")],
        )
        row = Analysis(data).completeness_report()[0]
        self.assertEqual(row["scope"]["quote"], "12000")
        self.assertEqual(row["scope"]["grant_ceiling"], "9000")
        self.assertEqual(row["scope"]["grant_requested"], "9000")
        self.assertTrue(row["scope"]["capped_by_household_cap"])
        self.assertTrue(row["scope"]["supportable"])

    def test_pending_consent_is_a_blocker_not_a_ready_file(self):
        data = dataset(
            households=[household("H1", consent="pending")],
            bills=[bill("B1", "E-H1", "2026-01", "500")],
            applications=[application("A1", "H1")],
            programs=[program()],
        )
        row = Analysis(data).completeness_report()[0]
        self.assertEqual(row["status"], "blocked")
        self.assertIn("consent", row["missing"])
        self.assertIn("consent not active", row["scope"]["blockers"])

    def test_no_quote_blocks_pricing_but_not_the_record(self):
        data = dataset(
            households=[household("H1")],
            bills=[bill("B1", "E-H1", "2026-01", "500")],
            applications=[application("A1", "H1", quote=None)],
            programs=[program()],
        )
        row = Analysis(data).completeness_report()[0]
        self.assertFalse(row["scope"]["can_price_support"])
        self.assertEqual(row["status"], "blocked")


class LoaderTests(unittest.TestCase):
    def test_unknown_household_reference_is_an_error(self):
        data = dataset(
            households=[household("H1")],
            applications=[application("A1", "GHOST")],
            programs=[program()],
        )
        codes = {issue.code for issue in data.issues}
        self.assertIn("unknown_application_household", codes)
        self.assertIn("error", {i.severity for i in data.issues if i.code == "unknown_application_household"})

    def test_approval_without_consent_is_flagged(self):
        data = dataset(
            households=[household("H1", consent="pending")],
            applications=[application("A1", "H1", status="approved")],
            programs=[program()],
        )
        codes = {issue.code for issue in data.issues}
        self.assertIn("approval_without_consent", codes)

    def test_allocation_to_another_account_is_rejected(self):
        data = dataset(
            households=[household("H1", account="E1"), household("H2", account="E2")],
            allocations=[allocation("E1", "H2", "1.0")],
        )
        codes = {issue.code for issue in data.issues}
        self.assertIn("allocation_account_mismatch", codes)

    def test_shared_flag_conflict_is_flagged(self):
        data = dataset(
            households=[
                household("H1", account="E1", shared=True),
                household("H2", account="E1", shared=False),
            ]
        )
        codes = {issue.code for issue in data.issues}
        self.assertIn("shared_flag_conflict", codes)

    def test_fixture_manifest_records_load(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "data"
        data = load_dataset(root)
        self.assertEqual(len(data.households), 3)
        self.assertEqual(len(data.bills), 3)
        self.assertEqual(len(data.applications), 3)

    def test_missing_directory_is_a_clear_error(self):
        with self.assertRaises(DatasetError):
            load_dataset("/nonexistent/fixtures")


if __name__ == "__main__":
    unittest.main()
