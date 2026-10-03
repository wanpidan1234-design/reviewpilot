"""deep_gate 升档：带 deep-review 标签的 PR，本 PR 所有 codex 调用 effort 升档。纯离线。"""

import unittest

import engine


RCFG = {"repo": "example/demo", "slug": "demo",
        "review_model": "test-model", "review_effort": "high",
        "deep_gate": {"label": "deep-review", "effort": "xhigh"}}


class DeepGate(unittest.TestCase):
    def test_hit_upgrades_effort_without_mutating_original(self):
        out, hit = engine.apply_deep_gate(RCFG, ["deep-review", "feat"])
        self.assertTrue(hit)
        self.assertEqual(out["review_effort"], "xhigh")
        self.assertEqual(RCFG["review_effort"], "high")  # 原配置不可变

    def test_no_label_on_pr_means_no_change(self):
        out, hit = engine.apply_deep_gate(RCFG, ["feat", "risk-high"])
        self.assertFalse(hit)
        self.assertIs(out, RCFG)

    def test_missing_deep_gate_config_is_inert(self):
        rcfg = {k: v for k, v in RCFG.items() if k != "deep_gate"}
        out, hit = engine.apply_deep_gate(rcfg, ["deep-review"])
        self.assertFalse(hit)
        self.assertIs(out, rcfg)

    def test_effort_defaults_to_xhigh_when_unset(self):
        rcfg = dict(RCFG, deep_gate={"label": "deep-review"})
        out, hit = engine.apply_deep_gate(rcfg, ["deep-review"])
        self.assertTrue(hit)
        self.assertEqual(out["review_effort"], "xhigh")

    def test_already_at_target_effort_still_reports_hit(self):
        rcfg = dict(RCFG, review_effort="xhigh")
        out, hit = engine.apply_deep_gate(rcfg, ["deep-review"])
        self.assertTrue(hit)
        self.assertEqual(out["review_effort"], "xhigh")


RCFG_MULTI = dict(RCFG, deep_gate={"labels": ["deep-review", "risk-high"], "effort": "xhigh"})


class DeepGateMultiLabel(unittest.TestCase):
    """Any configured label can upgrade effort and identify the matching rule."""

    def test_jev_risk_label_hits_and_reports_which(self):
        out, hit = engine.apply_deep_gate(RCFG_MULTI, ["feat", "risk-high"])
        self.assertEqual(hit, "risk-high")
        self.assertEqual(out["review_effort"], "xhigh")

    def test_manual_label_still_first_class(self):
        out, hit = engine.apply_deep_gate(RCFG_MULTI, ["deep-review"])
        self.assertEqual(hit, "deep-review")
        self.assertEqual(out["review_effort"], "xhigh")

    def test_no_match_returns_original_and_none(self):
        out, hit = engine.apply_deep_gate(RCFG_MULTI, ["feat", "risk-low"])
        self.assertIsNone(hit)
        self.assertIs(out, RCFG_MULTI)

    def test_legacy_single_label_key_reports_label_name(self):
        out, hit = engine.apply_deep_gate(RCFG, ["deep-review", "feat"])
        self.assertEqual(hit, "deep-review")
        self.assertEqual(out["review_effort"], "xhigh")
