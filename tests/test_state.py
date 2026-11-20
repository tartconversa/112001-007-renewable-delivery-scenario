"""Session state round-trips and CLI behaviour."""

import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from residential_aid.analysis import Analysis
from residential_aid.state import load_state, save_state
from support import application, bill, dataset, household, program


ROOT = Path(__file__).resolve().parents[1]


def session():
    return dataset(
        households=[household("H1", account="E1")],
        bills=[bill("B1", "E1", "2026-01", "1000")],
        applications=[
            application(
                "A1", "H1", status="waiting", start="2026-01-01", end="2026-03-31"
            )
        ],
        programs=[program()],
    )


class StateTests(unittest.TestCase):
    def test_decisions_and_publications_survive_a_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis = Analysis(session())
            analysis.approve("A1", approved_by="staff-1")
            analysis.publish("q1")
            path = Path(tmp) / "session.json"
            save_state(analysis, ROOT / "data", path)

            restored, _ = load_state(ROOT / "data", path)
            self.assertEqual(restored.statuses["A1"].grant_status, "approved")
            self.assertEqual(restored.decisions[0]["approved_by"], "staff-1")
            self.assertEqual(restored.publications[0].label, "q1")
            self.assertEqual(
                restored.publications[0].fingerprint,
                analysis.publications[0].fingerprint,
            )

    def test_state_from_another_fixture_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            path.write_text(
                json.dumps({"source": "/somewhere/else", "applications": []}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_state(ROOT / "data", path)

    def test_bill_revisions_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis = Analysis(session())
            analysis.revise_bill(
                account_token="E1", period="2026-01", electric_kwh=Decimal("-100")
            )
            path = Path(tmp) / "session.json"
            save_state(analysis, ROOT / "data", path)
            restored, _ = load_state(ROOT / "data", path)
            self.assertEqual(restored.attributions["H1"].kwh, Decimal("900"))


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        command = [sys.executable, "-m", "residential_aid", "--data", str(ROOT / "data"), *args]
        return subprocess.run(
            command, capture_output=True, text=True, cwd=ROOT, check=False
        )

    def test_read_only_command_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "session.json"
            result = self.run_cli("--state", str(state), "status")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state.exists())

    def test_mutating_command_writes_state_and_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "session.json"
            ok = self.run_cli("--state", str(state), "approve", "A02", "--by", "s1")
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertTrue(state.exists())
            self.assertEqual(json.loads(ok.stdout)["grant_status"], "approved")

            bad = self.run_cli("--state", str(state), "install", "A01", "2029-01-01")
            self.assertEqual(bad.returncode, 2)
            self.assertIn("after the provider window closes", bad.stderr)

    def test_all_subcommand_is_valid_json(self):
        result = self.run_cli("all")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("plans", payload)
        self.assertIn("status", payload)


if __name__ == "__main__":
    unittest.main()
