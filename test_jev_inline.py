"""Offline tests for inline risk labels before the ReviewPilot review gate.

All labeler network boundaries are mocked.
"""

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import engine
import labeler

HEAD = "a" * 40
PR = {"number": 7, "headRefOid": HEAD, "headRefName": "feat", "baseRefName": "main",
      "title": "t", "labels": [], "author": {"login": "trusted"}}
RCFG = {"repo": "owner/repo", "slug": "repo", "trusted_authors": ["trusted"],
        "review_model": "m", "review_effort": "high",
        "deep_gate": {"labels": ["deep-review", "risk-high"], "effort": "xhigh"}}
LAB = {"enabled": True}


class JevInlineWrapper(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(engine, "log"))
        self.stack.enter_context(mock.patch.dict(os.environ, {"JEV_API_KEY": "offline-test-placeholder"}))

    def test_disabled_returns_empty_without_touching_labeler(self):
        with mock.patch.object(labeler, "load_ledger", side_effect=AssertionError("no touch")):
            self.assertEqual(engine.jev_inline_labels(RCFG, PR, None), [])
            self.assertEqual(engine.jev_inline_labels(RCFG, PR, {"enabled": False}), [])

    def test_missing_key_skips_and_leaves_ledger_alone(self):
        # key 缺失（run.sh 注入断了）→ 不碰账本：绝不给旁路 labeler cron 投毒退避
        os.environ.pop("JEV_API_KEY", None)
        with mock.patch.object(labeler, "load_ledger", side_effect=AssertionError("no touch")):
            self.assertEqual(engine.jev_inline_labels(RCFG, PR, LAB), [])

    def test_backoff_respected_no_jev_call_no_save(self):
        led = {"_backoff": {"until": "9999-01-01T00:00:00Z", "fails": 1, "secs": 60}}
        with mock.patch.object(labeler, "load_ledger", return_value=led), \
             mock.patch.object(labeler, "process_pr",
                               side_effect=AssertionError("backoff 中不得调 jev")), \
             mock.patch.object(labeler, "save_ledger",
                               side_effect=AssertionError("backoff 跳过不得写账本")):
            self.assertEqual(engine.jev_inline_labels(RCFG, PR, LAB), [])

    def test_success_returns_labels_and_persists_ledger(self):
        saved = []
        with mock.patch.object(labeler, "load_ledger", return_value={}), \
             mock.patch.object(labeler, "process_pr", return_value=["fix", "risk-high"]), \
             mock.patch.object(labeler, "save_ledger", side_effect=saved.append):
            got = engine.jev_inline_labels(RCFG, PR, LAB)
        self.assertEqual(got, ["fix", "risk-high"])
        self.assertEqual(len(saved), 1)

    def test_jev_failure_persists_backoff_and_returns_empty(self):
        # process_pr 返回 None（jev 失败/写标失败）→ 账本可能带上退避，必须落盘
        saved = []
        with mock.patch.object(labeler, "load_ledger", return_value={}), \
             mock.patch.object(labeler, "process_pr", return_value=None), \
             mock.patch.object(labeler, "save_ledger", side_effect=saved.append):
            self.assertEqual(engine.jev_inline_labels(RCFG, PR, LAB), [])
        self.assertEqual(len(saved), 1)

    def test_fail_open_on_unexpected_exception(self):
        with mock.patch.object(labeler, "load_ledger", side_effect=RuntimeError("boom")):
            self.assertEqual(engine.jev_inline_labels(RCFG, PR, LAB), [])


class ProcessPrIntegration(unittest.TestCase):
    """行内 jev 判出 risk-high → 首轮审查即 xhigh（不等旁路 cron）。"""

    def test_inline_risk_high_escalates_before_first_review(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(engine, "STATE_DIR", Path(tmp)))
            stack.enter_context(mock.patch.object(engine, "log"))
            stack.enter_context(mock.patch.object(
                engine, "fresh_pr_view", return_value={"state": "OPEN", "headRefOid": HEAD}))
            stack.enter_context(mock.patch.object(
                engine, "jev_inline_labels", return_value=["fix", "risk-high"]))
            efforts = []

            def ci(ctx):
                efforts.append(ctx.cfg["review_effort"])
                return engine.TERMINAL, "stub"

            stack.enter_context(mock.patch.dict(engine.NODES, ci_gate=ci))
            engine.process_pr(RCFG, {"lifecycle_observer": True}, dict(PR), LAB)
            st = json.loads((Path(tmp) / "repo" / "pr-7.json").read_text())
        self.assertEqual(efforts, ["xhigh"])
        self.assertEqual(st["deep_gate"], {"label": "risk-high", "effort": "xhigh"})

    def test_no_risk_label_stays_at_normal_effort(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(engine, "STATE_DIR", Path(tmp)))
            stack.enter_context(mock.patch.object(engine, "log"))
            stack.enter_context(mock.patch.object(
                engine, "fresh_pr_view", return_value={"state": "OPEN", "headRefOid": HEAD}))
            stack.enter_context(mock.patch.object(
                engine, "jev_inline_labels", return_value=["fix", "risk-low"]))
            efforts = []

            def ci(ctx):
                efforts.append(ctx.cfg["review_effort"])
                return engine.TERMINAL, "stub"

            stack.enter_context(mock.patch.dict(engine.NODES, ci_gate=ci))
            engine.process_pr(RCFG, {"lifecycle_observer": True}, dict(PR), LAB)
            st = json.loads((Path(tmp) / "repo" / "pr-7.json").read_text())
        self.assertEqual(efforts, ["high"])
        self.assertNotIn("deep_gate", st)


if __name__ == "__main__":
    unittest.main()
