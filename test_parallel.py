"""Offline tests: per-PR parallel dispatch — tick 只派发、worker 抢锁、并发上限、冻结不派发。"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("reviewpilot_parallel_engine", Path(__file__).with_name("engine.py"))
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

HEAD = "a" * 40


def rc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def rcfg(**kw):
    d = dict(repo="owner/repo", slug="repo", trusted_authors=["trusted"], max_rounds=2)
    d.update(kw)
    return d


def pr(n, author="trusted", draft=False, head=HEAD):
    return dict(number=n, isDraft=draft, author={"login": author}, headRefOid=head,
                headRefName="feat", baseRefName="main", title="t", labels=[])


class Parallel(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        for attr in ("ROOT", "STATE_DIR", "LOG_DIR", "REPOS_DIR", "WT_DIR", "LOCK_DIR"):
            self.stack.enter_context(patch.object(engine, attr, Path(self.temp) / attr))
        self.stack.enter_context(patch.object(engine, "log"))
        self.stack.enter_context(patch.object(
            engine.subprocess, "run", side_effect=AssertionError("external execution forbidden")))
        self.stack.enter_context(patch.object(
            engine.subprocess, "Popen", side_effect=AssertionError("real spawn forbidden")))

    def tick(self, prs, defaults=None, repo_cfg=None):
        cfg = {"repos": [repo_cfg or rcfg()], "notify": {}}
        if defaults:
            cfg["defaults"] = defaults
        with patch.object(engine, "gh", return_value=rc(0, json.dumps(prs))), \
             patch.object(engine, "load_config", return_value=cfg), \
             patch.dict(os.environ, {"CRS_KEY": "offline-test-placeholder"}, clear=True), \
             patch.object(engine.sys, "argv", ["engine.py"]), \
             patch.object(engine, "dispatch_pr") as dispatch:
            self.assertEqual(engine.main(), 0)
        return [c.args[2]["number"] for c in dispatch.call_args_list]

    def test_tick_dispatches_each_open_trusted_pr(self):
        self.assertEqual(self.tick([pr(1), pr(2), pr(3, author="outsider"), pr(4, draft=True)]),
                         [1, 2])

    def test_tick_respects_parallel_cap_counting_running_workers(self):
        held = engine.try_lock(engine.pr_lock_path("repo", 1))  # PR1 已有 worker 在跑
        self.addCleanup(held.close)
        self.assertEqual(self.tick([pr(1), pr(2), pr(3)], defaults={"max_parallel_prs": 2}),
                         [2])  # running=1 + spawned=1 达上限，PR3 本 tick 不派发

    def test_tick_skips_frozen_and_idempotent_states(self):
        (Path(self.temp) / "STATE_DIR" / "repo").mkdir(parents=True)
        engine.save_state("repo", {"pr": 1, "escalated": True, "head_sha": HEAD})
        engine.save_state("repo", {"pr": 2, "escalated": False, "head_sha": HEAD, "phase": "merged"})
        self.assertEqual(self.tick([pr(1), pr(2), pr(3)]), [3])
        # head 变化后解冻派发
        self.assertEqual(self.tick([pr(1, head="b" * 40)]), [1])

    def test_worker_exits_quietly_when_lock_held(self):
        held = engine.try_lock(engine.pr_lock_path("repo", 7))
        self.addCleanup(held.close)
        with patch.object(engine, "gh", side_effect=AssertionError("must not fetch")), \
             patch.object(engine, "process_pr", side_effect=AssertionError("must not process")):
            self.assertEqual(engine.run_worker({"repos": [rcfg()], "notify": {}}, "repo", 7), 0)

    def test_worker_fetches_fresh_pr_and_processes_open_only(self):
        seen = []
        view = dict(pr(7), state="OPEN")
        with patch.object(engine, "gh", return_value=rc(0, json.dumps(view))), \
             patch.object(engine, "process_pr",
                          side_effect=lambda r, n, p, lab=None: seen.append(p["number"])):
            self.assertEqual(engine.run_worker({"repos": [rcfg()], "notify": {}}, "repo", 7), 0)
        self.assertEqual(seen, [7])
        for state in ("MERGED", "CLOSED"):
            with patch.object(engine, "gh", return_value=rc(0, json.dumps(dict(pr(8), state=state)))), \
                 patch.object(engine, "process_pr", side_effect=AssertionError("terminal PR must not process")):
                self.assertEqual(engine.run_worker({"repos": [rcfg()], "notify": {}}, "repo", 8), 0)

    def test_worker_passes_labeler_cfg_for_inline_jev(self):
        # Inline labeling receives cfg.labeler through the worker boundary.
        got = []
        view = dict(pr(7), state="OPEN")
        with patch.object(engine, "gh", return_value=rc(0, json.dumps(view))), \
             patch.object(engine, "process_pr",
                          side_effect=lambda r, n, p, lab: got.append(lab)):
            cfg = {"repos": [rcfg()], "notify": {}, "labeler": {"enabled": True}}
            self.assertEqual(engine.run_worker(cfg, "repo", 7), 0)
        self.assertEqual(got, [{"enabled": True}])

    def test_dispatch_spawns_detached_worker_process(self):
        calls = []
        with patch.object(engine.subprocess, "Popen", side_effect=lambda *a, **k: calls.append((a, k))):
            engine.dispatch_pr(rcfg(), {}, pr(5))
        (args,), kw = calls[0][0], calls[0][1]
        self.assertIn("--worker", args)
        self.assertEqual(args[args.index("--pr") + 1], "5")
        self.assertTrue(kw.get("start_new_session"))


if __name__ == "__main__":
    unittest.main()
