"""Offline regression tests: no config/credentials or live automation are used."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import yaml

spec = importlib.util.spec_from_file_location("reviewpilot_allowlist_engine", Path(__file__).with_name("engine.py"))
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
ABSENT = object()
INVALID = [None, "14", 14, True, False, {}, (), [True], [False], [0], [-1], [1.0], ["14"], [14, None]]


def config(allowlist=ABSENT, repo="example/demo-repo", slug="demo-repo"):
    result = dict(repo=repo, slug=slug, trusted_authors=["trusted"], protected_paths=["secret/"],
                  diff_max_bytes=10, dry_run=True, max_rounds=2, merge_method="squash")
    if allowlist is not ABSENT:
        result["pr_allowlist"] = allowlist
    return result


def pr(number=14, draft=False, author="trusted"):
    return dict(number=number, isDraft=draft, author={"login": author}, headRefOid="abc123",
                headRefName="feature", baseRefName="main", title="test", labels=[])


class AllowlistTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        for attr in ("ROOT", "STATE_DIR", "LOG_DIR", "REPOS_DIR", "WT_DIR", "LOCK_DIR"):
            self.stack.enter_context(patch.object(engine, attr, Path(self.temp) / attr))
        self.stack.enter_context(patch.object(engine, "log"))
        self.stack.enter_context(patch.object(engine.subprocess, "run", side_effect=AssertionError("external execution forbidden")))
        self.sh = self.stack.enter_context(patch.object(engine, "sh", side_effect=AssertionError("shell forbidden")))
        self.gh = self.stack.enter_context(patch.object(engine, "gh", side_effect=AssertionError("GitHub forbidden")))
        # 新鲜度校验是 allowed PR 的合法外呼；此处 stub 为"取不到数据"退回快照行为，
        # 保持本套件"被过滤 PR 零外呼"的原始断言语义。
        self.stack.enter_context(patch.object(engine, "fresh_pr_view", return_value=None))
        self.stack.enter_context(patch.object(engine, "run_codex", side_effect=AssertionError("codex forbidden")))
        self.node = self.stack.enter_context(patch.dict(engine.NODES, ci_gate=lambda ctx: (engine.TERMINAL, "test stop")))

    def scan(self, rcfg, prs, args=()):
        self.gh.side_effect = None
        self.gh.return_value = types.SimpleNamespace(returncode=0, stdout=json.dumps(prs), stderr="")
        with patch.object(engine, "load_config", return_value={"repos": [rcfg], "notify": {}}), \
             patch.dict(os.environ, {"CRS_KEY": "offline-test-placeholder"}, clear=True), \
             patch.object(engine.sys, "argv", ["engine.py", *args]):
            return engine.main()

    def test_direct_absent_empty_and_only_14_before_state(self):
        for allowlist, number, allowed in [(ABSENT, 11, True), ([], 14, False), ([14], 14, True), ([14], 11, False)]:
            with self.subTest(allowlist=allowlist, number=number), patch.object(engine, "load_state", return_value={}) as load, patch.object(engine, "save_state"):
                engine.process_pr(config(allowlist), {}, pr(number))
                self.assertEqual(load.called, allowed)
        self.gh.assert_not_called()
        self.sh.assert_not_called()

    def test_scan_filters_allowlist_and_drafts_before_dispatch(self):
        for allowlist, expected in [(ABSENT, [14, 11]), ([], []), ([14], [14])]:
            with self.subTest(allowlist=allowlist), patch.object(engine, "dispatch_pr") as process:
                self.assertEqual(self.scan(config(allowlist), [pr(14), pr(11), pr(10, draft=True), pr(14, draft=True)]), 0)
                self.assertEqual([c.args[2]["number"] for c in process.call_args_list], expected)

    def test_cli_pr_cannot_override_allowlist(self):
        for number, expected in [(11, []), (14, [14]), (10, [])]:
            with self.subTest(number=number), patch.object(engine, "dispatch_pr") as process:
                self.scan(config([14]), [pr(14), pr(11), pr(10, draft=True)], ["--repo", "demo-repo", "--pr", str(number)])
                self.assertEqual([c.args[2]["number"] for c in process.call_args_list], expected)

    def test_denied_cli_creates_no_state(self):
        self.scan(config([14]), [pr(11)], ["--pr", "11"])
        self.assertFalse(engine.STATE_DIR.exists())

    def test_invalid_direct_fails_before_state(self):
        for value in INVALID:
            with self.subTest(value=value), patch.object(engine, "load_state", return_value={}) as load, patch.object(engine, "save_state"):
                with self.assertRaisesRegex(ValueError, "pr_allowlist"):
                    engine.process_pr(config(value), {}, pr())
                load.assert_not_called()
        self.gh.assert_not_called()

    def test_invalid_scan_fails_before_github_or_dispatch(self):
        for value in INVALID:
            with self.subTest(value=value), patch.object(engine, "dispatch_pr") as process:
                self.gh.reset_mock()
                with self.assertRaisesRegex(ValueError, "pr_allowlist"):
                    self.scan(config(value), [pr(14)])
                self.gh.assert_not_called()
                process.assert_not_called()
        self.assertFalse(engine.STATE_DIR.exists())

    def test_load_config_validates_merged_values(self):
        engine.ROOT.mkdir()
        for value in INVALID:
            # Tuples become YAML lists; test that Python-only type at direct boundaries instead.
            if isinstance(value, tuple):
                continue
            for inherited in (False, True):
                with self.subTest(value=value, inherited=inherited):
                    cfg = {"defaults": {"pr_allowlist": value} if inherited else {},
                           "repos": [config() if inherited else config(value)]}
                    (engine.ROOT / "config.yml").write_text(yaml.safe_dump(cfg))
                    with self.assertRaisesRegex(ValueError, "pr_allowlist"):
                        engine.load_config()
        for value in (ABSENT, [], [14]):
            (engine.ROOT / "config.yml").write_text(yaml.safe_dump({"repos": [config(value)]}))
            self.assertEqual(engine.load_config()["repos"][0], config(value))

    def test_existing_two_repos_keep_unrestricted_scan_and_config(self):
        engine.ROOT.mkdir()
        repos = [config(repo="example/service", slug="service"),
                 config(repo="example/worker", slug="worker")]
        (engine.ROOT / "config.yml").write_text(yaml.safe_dump({"defaults": {"review_effort": "medium"}, "repos": repos}))
        self.assertEqual(engine.load_config()["repos"], [dict(r, review_effort="medium") for r in repos])
        for rcfg in repos:
            with self.subTest(repo=rcfg["repo"]), patch.object(engine, "dispatch_pr") as process:
                self.scan(rcfg, [pr(11), pr(14), pr(10, draft=True)])
                self.assertEqual([c.args[2]["number"] for c in process.call_args_list], [11, 14])

    def test_allowed_untrusted_author_still_skipped(self):
        with patch.object(engine, "load_state", return_value={}), patch.object(engine, "save_state") as save:
            engine.process_pr(config([14]), {}, pr(author="outsider"))
            self.assertEqual(save.call_args.args[1]["phase"], "skipped_untrusted")
        self.gh.assert_not_called()

    def test_allowed_pr_preserves_context_safety_settings(self):
        rcfg = config([14])
        seen = []
        def node(ctx):
            seen.append(ctx.cfg)
            return engine.TERMINAL, "test stop"
        with patch.dict(engine.NODES, ci_gate=node), patch.object(engine, "load_state", return_value={}), patch.object(engine, "save_state"):
            engine.process_pr(rcfg, {}, pr())
        self.assertEqual(seen, [rcfg])
        self.assertIs(seen[0], rcfg)

    def test_protected_paths_and_dry_run_merge_still_apply(self):
        for filename, expected in [("secret/key.txt", "escalate"), ("src/app.py", engine.TERMINAL)]:
            with self.subTest(filename=filename):
                ctx = engine.Ctx(config([14]), {}, pr(), {"head_sha": "abc123"})
                self.gh.side_effect = None
                self.gh.return_value = types.SimpleNamespace(returncode=0, stdout=filename, stderr="")
                self.gh.reset_mock()
                self.assertEqual(engine.node_merge(ctx)[0], expected)
                self.assertEqual(self.gh.call_count, 1)
                self.assertEqual(self.gh.call_args.args[0][0], "api")
        self.sh.assert_not_called()

    def test_diff_limit_and_dry_run_still_apply(self):
        ctx = engine.Ctx(config([14]), {}, pr(), {"head_sha": "abc123"})
        ctx.diff_text = "x" * 11
        with patch.object(engine, "ensure_head_available") as ensure:
            self.assertEqual(engine.node_ai_review(ctx)[0], "escalate")
            ensure.assert_not_called()
        engine.post_status(ctx, "test", "success", "test")
        engine.post_comment(ctx, "test")
        engine.notify(ctx, "test")
        self.gh.assert_not_called()
        self.sh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
