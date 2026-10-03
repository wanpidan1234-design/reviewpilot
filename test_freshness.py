"""Offline tests: stale-snapshot freshness guards and push-race declassification.

长审查窗口内 PR 会被外部合并或作者并发推 head——这些护栏保证：
不处理已终结 PR、不推撞车 fixup、不把竞态当熔断、不合并未审 head。
"""
import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("reviewpilot_fresh_engine", Path(__file__).with_name("engine.py"))
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

HEAD = "a" * 40
MOVED = "b" * 40


def rc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def ctx(**st):
    rcfg = {"repo": "owner/repo", "slug": "repo", "max_rounds": 2, "merge_method": "squash",
            "review_model": "m", "review_effort": "high"}
    state = {"head_sha": HEAD, "branch": "feat", "round": 0,
             "fixup_reason": {"kind": "k", "detail": "d"}}
    state.update(st)
    return engine.Ctx(rcfg, {"lifecycle_observer": True}, {"number": 7}, state)


class Freshness(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(engine, "log"))
        self.stack.enter_context(patch.object(
            engine.subprocess, "run", side_effect=AssertionError("external execution forbidden")))

    def test_push_race_classification(self):
        for s in ("! [rejected] non-fast-forward", "Updates were rejected. fetch first",
                  "a pushed branch tip is behind its remote counterpart",
                  "the remote contains work that you do not have"):
            self.assertTrue(engine.push_race(s), s)
        self.assertFalse(engine.push_race("Permission denied (publickey)"))
        self.assertFalse(engine.push_race(""))

    def test_fresh_pr_view_fail_open(self):
        with patch.object(engine, "gh", return_value=rc(1, "", "boom")):
            self.assertIsNone(engine.fresh_pr_view("owner/repo", 7))
        with patch.object(engine, "gh", return_value=rc(0, "not json")):
            self.assertIsNone(engine.fresh_pr_view("owner/repo", 7))
        with patch.object(engine, "gh", return_value=rc(0, json.dumps({"state": "OPEN", "headRefOid": HEAD}))):
            self.assertEqual(engine.fresh_pr_view("owner/repo", 7)["headRefOid"], HEAD)

    def test_fixup_abandons_when_head_moved_or_terminal(self):
        c = ctx()
        with patch.object(engine, "fresh_pr_view", return_value={"state": "OPEN", "headRefOid": MOVED}):
            nxt, reason = engine.node_fixup(c)
        self.assertEqual(nxt, engine.TERMINAL)
        self.assertIn("取消", reason)
        with patch.object(engine, "fresh_pr_view", return_value={"state": "MERGED", "headRefOid": HEAD}):
            nxt, reason = engine.node_fixup(ctx())
        self.assertEqual(nxt, engine.TERMINAL)
        self.assertNotIn("escalate", nxt)

    def test_merge_locks_reviewed_head_and_declassifies_race(self):
        calls = []

        def gh_stub(args, **kw):
            calls.append(args)
            if args[:1] == ["api"]:
                return rc(0, "a.py\n")
            return rc(0, "")

        c = ctx()
        with patch.object(engine, "gh", side_effect=gh_stub), \
             patch.object(engine, "fresh_pr_view", return_value={"state": "OPEN", "headRefOid": HEAD}):
            nxt, _ = engine.node_merge(c)
        self.assertEqual(nxt, engine.TERMINAL)
        merge_call = next(a for a in calls if a[:2] == ["pr", "merge"])
        self.assertIn("--match-head-commit", merge_call)
        self.assertEqual(merge_call[merge_call.index("--match-head-commit") + 1], HEAD)

        # 审查后作者又推了 head → 放弃合并而不是把旧 sha 合进去
        with patch.object(engine, "gh", side_effect=gh_stub), \
             patch.object(engine, "fresh_pr_view", return_value={"state": "OPEN", "headRefOid": MOVED}):
            nxt, reason = engine.node_merge(ctx())
        self.assertEqual(nxt, engine.TERMINAL)
        self.assertIn("放弃合并", reason)

    def test_merge_conflict_routes_to_resolver_not_escalate(self):
        def gh_stub(args, **kw):
            if args[:1] == ["api"]:
                return rc(0, "a.py\n")
            if args[:2] == ["pr", "merge"]:
                return rc(1, "", "GraphQL: Pull Request has merge conflicts (mergePullRequest)")
            return rc(0, "")
        with patch.object(engine, "gh", side_effect=gh_stub), \
             patch.object(engine, "fresh_pr_view", return_value={"state": "OPEN", "headRefOid": HEAD}):
            nxt, reason = engine.node_merge(ctx())
        self.assertEqual(nxt, "resolve_conflict")
        self.assertIn("冲突", reason)

    def _resolver_env(self, sh_router, codex=None):
        self.stack.enter_context(patch.object(engine, "sh", side_effect=sh_router))
        self.stack.enter_context(patch.object(engine, "ensure_head_available", return_value=True))
        self.stack.enter_context(patch.object(engine, "make_worktree", return_value=Path("/nonexistent-wt")))
        self.stack.enter_context(patch.object(engine, "remove_worktree"))
        self.stack.enter_context(patch.object(engine, "repo_git_lock", lambda slug: contextlib.nullcontext()))
        self.stack.enter_context(patch.object(engine, "post_comment"))
        self.stack.enter_context(patch.object(engine, "wait_for_head", return_value=True))
        self.codex = self.stack.enter_context(patch.object(engine, "run_codex", return_value=codex))
        self.stack.enter_context(patch.object(
            engine, "fresh_pr_view", return_value={"state": "OPEN", "headRefOid": HEAD}))

    def test_resolver_cap_and_dry_run_and_freshness(self):
        nxt, reason = engine.node_resolve_conflict(ctx(conflict_resolves=2))
        self.assertEqual(nxt, "escalate")
        self.assertIn("conflict_resolves=2", reason)

        c = ctx()
        c.cfg["dry_run"] = True
        nxt, _ = engine.node_resolve_conflict(c)
        self.assertEqual(nxt, engine.TERMINAL)
        self.assertEqual(c.st["phase"], "dry_run_would_resolve")

        with patch.object(engine, "fresh_pr_view", return_value={"state": "OPEN", "headRefOid": MOVED}):
            nxt, reason = engine.node_resolve_conflict(ctx())
        self.assertEqual(nxt, engine.TERMINAL)
        self.assertIn("取消解冲突", reason)

    def test_resolver_clean_automerge_pushes_without_codex(self):
        calls = []
        def router(args, cwd=None, **kw):
            calls.append(args)
            if args[:2] == ["git", "rev-parse"]:
                return rc(0, MOVED + "\n")
            return rc(0, "")
        self._resolver_env(router)
        c = ctx()
        nxt, reason = engine.node_resolve_conflict(c)
        self.assertEqual(nxt, "ci_gate")
        self.assertIn("无冲突", reason)
        self.codex.assert_not_called()
        self.assertTrue(any(a[:2] == ["git", "push"] for a in calls))
        self.assertEqual(c.st["head_sha"], MOVED)
        self.assertEqual(c.st["conflict_resolves"], 1)

    def test_resolver_codex_resolves_only_conflicted_files(self):
        calls = []
        def router(args, cwd=None, **kw):
            calls.append(args)
            if args[:2] == ["git", "merge"]:
                return rc(1, "CONFLICT (content): Merge conflict in VERSIONS.md")
            if args[:3] == ["git", "diff", "--name-only"]:
                return rc(0, "VERSIONS.md\n")
            if args[:1] == ["grep"]:
                return rc(1, "")  # 无残留标记
            if args[:3] == ["git", "status", "--porcelain"]:
                return rc(0, "M  staged_from_main.py\nUU VERSIONS.md\n")
            if args[:2] == ["git", "rev-parse"]:
                return rc(0, MOVED + "\n")
            return rc(0, "")
        self._resolver_env(router, codex="resolved ok")
        c = ctx()
        nxt, reason = engine.node_resolve_conflict(c)
        self.assertEqual(nxt, "ci_gate")
        self.assertIn("codex 解决 1 个冲突文件", reason)
        add = next(a for a in calls if a[:2] == ["git", "add"])
        self.assertEqual(add[-1], "VERSIONS.md")
        self.assertTrue(any(a[:2] == ["git", "push"] for a in calls))
        self.assertEqual(c.st["conflict_resolves"], 1)

    def test_resolver_escalates_on_scope_violation_or_leftover_markers(self):
        def router_factory(status_out, grep_rc):
            def router(args, cwd=None, **kw):
                if args[:2] == ["git", "merge"]:
                    return rc(1, "CONFLICT")
                if args[:3] == ["git", "diff", "--name-only"]:
                    return rc(0, "VERSIONS.md\n")
                if args[:1] == ["grep"]:
                    return rc(grep_rc, "VERSIONS.md\n" if grep_rc == 0 else "")
                if args[:3] == ["git", "status", "--porcelain"]:
                    return rc(0, status_out)
                if args[:2] in (["git", "push"], ["git", "add"], ["git", "commit"]):
                    raise AssertionError("越界/残留时不得 add/commit/push")
                return rc(0, "")
            return router
        # 越界：codex 动了非冲突文件（第二列非空格）
        self._resolver_env(router_factory(" M other.py\nUU VERSIONS.md\n", 1), codex="done")
        nxt, reason = engine.node_resolve_conflict(ctx())
        self.assertEqual(nxt, "escalate")
        self.assertIn("越界", reason)
        # 残留冲突标记
        self.stack.close(); self.setUp()
        self._resolver_env(router_factory("UU VERSIONS.md\n", 0), codex="done")
        nxt, reason = engine.node_resolve_conflict(ctx())
        self.assertEqual(nxt, "escalate")
        self.assertIn("残留", reason)

    def test_process_pr_conflicting_snapshot_starts_at_resolver(self):
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(engine, "STATE_DIR", Path(tmp)))
        rcfg = {"repo": "owner/repo", "slug": "repo", "trusted_authors": ["trusted"]}
        pr = {"number": 7, "headRefOid": HEAD, "headRefName": "feat", "baseRefName": "main",
              "title": "t", "labels": [], "author": {"login": "trusted"},
              "mergeable": "CONFLICTING"}
        hit = []
        with patch.object(engine, "fresh_pr_view", return_value={"state": "OPEN", "headRefOid": HEAD}), \
             patch.dict(engine.NODES,
                        resolve_conflict=lambda c: hit.append(1) or (engine.TERMINAL, "stub"),
                        ci_gate=lambda c: (_ for _ in ()).throw(AssertionError("冲突 PR 不得先进 ci_gate"))):
            engine.process_pr(rcfg, {"lifecycle_observer": True}, pr)
        self.assertEqual(hit, [1])
        st = json.loads((Path(tmp) / "repo" / "pr-7.json").read_text())
        self.assertIn("fetch_pr->resolve_conflict", [h["edge"] for h in st["history"]])

    def test_process_pr_skips_externally_terminal_pr(self):
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(engine, "STATE_DIR", Path(tmp)))
        rcfg = {"repo": "owner/repo", "slug": "repo", "trusted_authors": ["trusted"]}
        pr = {"number": 7, "headRefOid": MOVED, "headRefName": "feat", "baseRefName": "main",
              "title": "t", "labels": [], "author": {"login": "trusted"}}
        with patch.object(engine, "fresh_pr_view", return_value={"state": "MERGED", "headRefOid": MOVED}), \
             patch.dict(engine.NODES, ci_gate=lambda c: (_ for _ in ()).throw(AssertionError("must not process"))):
            engine.process_pr(rcfg, {"lifecycle_observer": True}, pr)
        st = json.loads((Path(tmp) / "repo" / "pr-7.json").read_text())
        self.assertEqual(st["phase"], "external_terminal")


if __name__ == "__main__":
    unittest.main()
