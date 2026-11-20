"""Priority grouping: bands, not rankings."""

import unittest
from decimal import Decimal

from residential_aid.analysis import Analysis
from residential_aid.priority import build_priority
from support import application, bill, dataset, household, program


class PriorityTests(unittest.TestCase):
    def test_missing_income_band_is_unranked_not_estimated(self):
        data = dataset(
            households=[household("H1", band="unknown")],
            bills=[bill("B1", "E-H1", "2026-01", "900")],
        )
        analysis = Analysis(data)
        member = analysis.priority.member("H1")
        self.assertFalse(member.rankable)
        self.assertEqual(member.tier, "unranked")
        self.assertIn("income band", member.exclusion)

    def test_band_drives_tier_and_bill_alone_does_not_promote(self):
        data = dataset(
            households=[
                household("H1", band="high", occupants=1),
                household("H2", band="low", occupants=4),
            ],
            bills=[
                bill("B1", "E-H1", "2026-01", "4000"),
                bill("B2", "E-H2", "2026-01", "200"),
            ],
        )
        analysis = Analysis(data)
        self.assertEqual(analysis.priority.tier_of("H1"), "low")
        self.assertEqual(analysis.priority.tier_of("H2"), "high")
        # The high bill is recorded as a need signal but is not a rank.
        self.assertEqual(analysis.priority.high_burden(), ("H1",))
        self.assertEqual(
            [g.tier for g in analysis.priority.groups()], ["high", "low"]
        )

    def test_medical_need_does_not_replace_missing_income_band(self):
        data = dataset(
            households=[household("H1", band="unknown", medical=True)],
            bills=[bill("B1", "E-H1", "2026-01", "500")],
        )
        analysis = Analysis(data)
        self.assertEqual(analysis.priority.tier_of("H1"), "unranked")

    def test_consent_withdrawn_household_is_unranked(self):
        data = dataset(
            households=[household("H1", band="low", consent="pending")],
            bills=[bill("B1", "E-H1", "2026-01", "500")],
        )
        analysis = Analysis(data)
        self.assertEqual(analysis.priority.tier_of("H1"), "unranked")
        self.assertIn("consent", analysis.priority.member("H1").exclusion)

    def test_order_within_tier_is_not_income(self):
        data = dataset(
            households=[
                household("H2", band="low", occupants=1),
                household("H1", band="low", occupants=1),
            ],
            bills=[
                bill("B1", "E-H1", "2026-01", "100"),
                bill("B2", "E-H2", "2026-01", "400"),
            ],
        )
        report = analysis_priority(data)
        high = report.groups()[0]
        self.assertEqual(high.households, ("H1", "H2"))
        self.assertIn("not a ranking", report.notice)


def analysis_priority(data):
    return build_priority(data, Analysis(data).attributions)


if __name__ == "__main__":
    unittest.main()
