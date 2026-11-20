"""Approval, installation and measured result are independent states."""

import unittest
from decimal import Decimal

from residential_aid.analysis import Analysis
from residential_aid.registry import RegistrationError
from support import allocation, application, bill, dataset, household, program


def build(**overrides):
    defaults = dict(
        households=[household("H1", account="E1")],
        bills=[bill("B1", "E1", "2026-01", "1000")],
        applications=[
            application(
                "A1", "H1", status="waiting", start="2026-01-01", end="2026-03-31"
            )
        ],
        programs=[program()],
    )
    defaults.update(overrides)
    return dataset(**defaults)


class StatusTests(unittest.TestCase):
    def test_reported_post_kwh_is_not_a_measured_outcome(self):
        data = build(
            applications=[
                application(
                    "A1",
                    "H1",
                    status="approved",
                    installed="2026-01-15",
                    post_kwh="400",
                )
            ]
        )
        status = Analysis(data).statuses["A1"]
        self.assertEqual(status.grant_status, "approved")
        self.assertEqual(status.install_status, "installed")
        self.assertEqual(status.measurement_status, "projected only")
        self.assertEqual(status.reported_post_kwh, Decimal("400"))
        self.assertIsNone(status.change_kwh)

    def test_post_install_bill_verifies_the_claimed_saving(self):
        data = build(
            bills=[
                bill("B1", "E1", "2026-01", "1000"),
                bill("B2", "E1", "2026-02", "700"),
            ],
            applications=[
                application(
                    "A1",
                    "H1",
                    status="approved",
                    installed="2026-01-20",
                    post_kwh="700",
                )
            ],
        )
        status = Analysis(data).statuses["A1"]
        self.assertEqual(status.measurement_status, "measured improvement")
        self.assertEqual(status.baseline_kwh, Decimal("1000.00"))
        self.assertEqual(status.post_bill_kwh, Decimal("700.00"))
        self.assertEqual(status.change_kwh, Decimal("-300.00"))

    def test_installation_month_is_not_counted_as_post_install(self):
        data = build(
            bills=[
                bill("B1", "E1", "2026-01", "1000"),
                bill("B2", "E1", "2026-01", "900"),
            ],
            applications=[
                application("A1", "H1", status="approved", installed="2026-01-20")
            ],
        )
        status = Analysis(data).statuses["A1"]
        self.assertEqual(status.measurement_status, "not yet measurable")

    def test_unattributed_shared_account_cannot_produce_an_outcome(self):
        data = dataset(
            households=[
                household("H1", account="E1", shared=True),
                household("H2", account="E1", shared=True),
            ],
            bills=[bill("B1", "E1", "2026-02", "900")],
            applications=[
                application("A1", "H1", status="approved", installed="2026-01-10")
            ],
            programs=[program()],
        )
        status = Analysis(data).statuses["A1"]
        self.assertEqual(status.measurement_status, "not attributable")

    def test_approved_household_uninstalled_is_scheduled_not_measured(self):
        data = build()
        analysis = Analysis(data)
        analysis.approve("A1")
        status = analysis.statuses["A1"]
        self.assertEqual(status.grant_status, "approved")
        self.assertEqual(status.install_status, "scheduled")
        self.assertEqual(status.measurement_status, "not yet measurable")

    def test_install_does_not_require_a_measured_result(self):
        data = build()
        analysis = Analysis(data)
        analysis.approve("A1")
        analysis.register_install("A1", "2026-01-10")
        status = analysis.statuses["A1"]
        self.assertEqual(status.install_status, "installed")
        self.assertEqual(status.measurement_status, "not yet measurable")


class RegistryTests(unittest.TestCase):
    def test_cannot_install_without_approval(self):
        analysis = Analysis(build())
        with self.assertRaises(RegistrationError):
            analysis.register_install("A1", "2026-01-10")

    def test_cannot_approve_without_active_consent(self):
        data = build(households=[household("H1", account="E1", consent="pending")])
        analysis = Analysis(data)
        with self.assertRaises(RegistrationError):
            analysis.approve("A1")

    def test_cannot_approve_without_a_quote(self):
        data = build(applications=[application("A1", "H1", quote=None)])
        analysis = Analysis(data)
        with self.assertRaises(RegistrationError):
            analysis.approve("A1")

    def test_install_outside_provider_window_is_refused(self):
        analysis = Analysis(build())
        analysis.approve("A1")
        with self.assertRaises(RegistrationError):
            analysis.register_install("A1", "2026-09-01")

    def test_each_decision_keeps_its_basis(self):
        analysis = Analysis(build())
        analysis.approve("A1", approved_by="staff-7")
        analysis.register_install("A1", "2026-01-10", recorded_by="crew-2")
        actions = [row["action"] for row in analysis.decisions]
        self.assertEqual(actions, ["grant approved", "installation recorded"])
        self.assertEqual(analysis.decisions[0]["approved_by"], "staff-7")
        self.assertEqual(analysis.decisions[1]["recorded_by"], "crew-2")


if __name__ == "__main__":
    unittest.main()
