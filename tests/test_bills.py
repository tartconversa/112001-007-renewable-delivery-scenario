"""Bill normalisation rules: revisions, corrections and shared accounts."""

import unittest
from decimal import Decimal

from residential_aid.bills import build_attributions
from support import allocation, bill, dataset, household


class RevisionTests(unittest.TestCase):
    def test_same_period_reissue_is_not_additional_usage(self):
        data = dataset(
            households=[household("H1", account="E1")],
            bills=[
                bill("B1", "E1", "2026-01", "500"),
                bill("B2", "E1", "2026-01", "520"),  # re-issued total
            ],
        )
        attributions, issues = build_attributions(data)
        self.assertEqual(attributions["H1"].kwh, Decimal("520"))
        codes = {i.code for i in issues}
        self.assertIn("reissue_not_new_usage", codes)

    def test_correction_delta_adjusts_the_period(self):
        data = dataset(
            households=[household("H1", account="E1")],
            bills=[
                bill("B1", "E1", "2026-01", "840"),
                bill("B2", "E1", "2026-01", "-120", adjustment_of="B1", status="correction"),
            ],
        )
        attributions, _ = build_attributions(data)
        self.assertEqual(attributions["H1"].kwh, Decimal("720"))
        self.assertIn("B2", attributions["H1"].revisions)

    def test_correction_in_another_period_is_not_usage(self):
        data = dataset(
            households=[household("H1", account="E1")],
            bills=[
                bill("B1", "E1", "2026-01", "840"),
                bill("B2", "E1", "2026-02", "-120", adjustment_of="B1", status="correction"),
            ],
        )
        attributions, _ = build_attributions(data)
        self.assertEqual(attributions["H1"].kwh, Decimal("840"))
        self.assertEqual(
            len([p for p in attributions["H1"].periods]), 2
        )
        codes = {i.code for i in data.issues}
        self.assertIn("cross_period_adjustment", codes)

    def test_adjustment_to_superseded_statement_is_held(self):
        data = dataset(
            households=[household("H1", account="E1")],
            bills=[
                bill("B1", "E1", "2026-01", "500"),
                bill("B2", "E1", "2026-01", "520"),
                bill(
                    "B3",
                    "E1",
                    "2026-01",
                    "-20",
                    adjustment_of="B1",
                    status="correction",
                ),
            ],
        )
        attributions, issues = build_attributions(data)
        self.assertEqual(attributions["H1"].kwh, Decimal("520"))
        self.assertIn("adjustment_targets_superseded", {i.code for i in issues})

    def test_void_statement_is_excluded(self):
        data = dataset(
            households=[household("H1", account="E1")],
            bills=[
                bill("B1", "E1", "2026-01", "500", status="void"),
                bill("B2", "E1", "2026-02", "300"),
            ],
        )
        attributions, _ = build_attributions(data)
        self.assertEqual(attributions["H1"].kwh, Decimal("300"))


class SharedAccountTests(unittest.TestCase):
    def test_shared_account_without_allocation_is_not_repeated(self):
        data = dataset(
            households=[
                household("H1", account="E1", shared=True),
                household("H2", account="E1", shared=True),
            ],
            bills=[bill("B1", "E1", "2026-01", "1000")],
        )
        attributions, issues = build_attributions(data)
        for token in ("H1", "H2"):
            self.assertFalse(attributions[token].attributable)
            self.assertIsNone(attributions[token].kwh)
        self.assertEqual(
            sum(1 for i in issues if i.code == "shared_account_unattributed"), 2
        )

    def test_allocation_splits_the_bill_once(self):
        data = dataset(
            households=[
                household("H1", account="E1", shared=True),
                household("H2", account="E1", shared=True),
            ],
            bills=[bill("B1", "E1", "2026-01", "1000")],
            allocations=[
                allocation("E1", "H1", "0.6"),
                allocation("E1", "H2", "0.4"),
            ],
        )
        attributions, issues = build_attributions(data)
        self.assertEqual(attributions["H1"].kwh, Decimal("600.0"))
        self.assertEqual(attributions["H2"].kwh, Decimal("400.0"))
        self.assertFalse([i for i in issues if i.code == "shared_account_unattributed"])

    def test_partial_allocation_leaves_remainder_unassigned(self):
        data = dataset(
            households=[
                household("H1", account="E1", shared=True),
                household("H2", account="E1", shared=True),
            ],
            bills=[bill("B1", "E1", "2026-01", "1000")],
            allocations=[allocation("E1", "H1", "0.5")],
        )
        attributions, _ = build_attributions(data)
        self.assertFalse(attributions["H1"].attributable)
        self.assertFalse(attributions["H2"].attributable)
        codes = {i.code for i in data.issues}
        self.assertIn("allocation_remainder", codes)

    def test_allocation_above_one_is_rejected(self):
        data = dataset(
            households=[
                household("H1", account="E1", shared=True),
                household("H2", account="E1", shared=True),
            ],
            bills=[bill("B1", "E1", "2026-01", "1000")],
            allocations=[
                allocation("E1", "H1", "0.8"),
                allocation("E1", "H2", "0.5"),
            ],
        )
        codes = {i.code for i in data.issues}
        self.assertIn("allocation_exceeds_one", codes)


if __name__ == "__main__":
    unittest.main()
