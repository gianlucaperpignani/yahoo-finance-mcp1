"""Boundary and deployment contract checks; no market calls."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import policy_contract as policy


class ContractTests(unittest.TestCase):
    def test_policy_and_discovery_files_pinned(self):
        self.assertEqual(policy.load_policy("risk_policy.json")["authority"], "technical_translation_only")
        import level_b
        for name, value in policy.load_policy("screening_config.json")["constants"].items():
            self.assertEqual(getattr(level_b, name), value)

    def test_mandate_change_needs_explicit_revalidation(self):
        policy.validate_mandate("CONS-2026-10-17-r6", policy.POLICY["mandate_fingerprints"]["CONS-2026-10-17-r6"])
        with self.assertRaisesRegex(ValueError, "MANDATE_POLICY_MISMATCH"):
            policy.validate_mandate("CONS-2026-10-17-r6", "0" * 64)

    def test_policy_edit_without_pin_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("release_contract.json", "risk_policy.json"):
                (root / name).write_bytes((ROOT / name).read_bytes())
            data = json.loads((root / "risk_policy.json").read_text())
            data["thresholds"]["var99_daily_critical_pct"] = 8
            (root / "risk_policy.json").write_text(json.dumps(data))
            with patch.object(policy, "ROOT", root), self.assertRaisesRegex(ValueError, "POLICY_HASH_MISMATCH"):
                policy.load_policy("risk_policy.json")

    def test_package_change_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = json.loads((ROOT / "release_contract.json").read_text())["risk_engine"]["files"]
            for name in files:
                (root / name).write_text("unapproved replacement")
            with self.assertRaisesRegex(ValueError, "RISK_ENGINE_PACKAGE_MISMATCH"):
                policy.verify_engine(root)

    def test_no_threshold_change_since_r6_baseline(self):
        # Independent approved baseline, deliberately not read from config.
        expected = dict(reconciliation_equity_fraction=.0025, reconciliation_floor_eur=1.,
            var99_daily_alert_pct=3., var99_daily_critical_pct=4., var99_weekly_alert_pct=7.5,
            var99_weekly_critical_pct=10., correlation_abs_alert=.70, correlation_positive_critical=.80,
            correlated_group_purchase_limit_pct=40., issuer_alert_pct=20., issuer_purchase_limit_pct=25.,
            sector_theme_alert_pct=35., sector_theme_purchase_limit_pct=45., cash_min_pct=15.,
            cash_max_pct=30., invested_min_pct=70., invested_max_pct=85., history_min_returns=200,
            history_ordinary_returns=250, rvol_cause_required=3., rvol_extreme=5.)
        self.assertEqual(policy.T, expected)


if __name__ == "__main__":
    unittest.main()
