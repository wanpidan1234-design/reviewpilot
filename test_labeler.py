"""labeler.py 离线回归：外部边界（gh / jev API / 文件系统路径）全部 mock，不发网络请求。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import labeler


def _pr(num=1, author="trusted-user", draft=False, head="a" * 40,
        title="t", body="b", labels=None):
    return {
        "number": num,
        "title": title,
        "body": body,
        "author": {"login": author},
        "isDraft": draft,
        "headRefOid": head,
        "labels": [{"name": n} for n in (labels or [])],
    }


RCFG = {
    "repo": "example/demo",
    "slug": "demo",
    "trusted_authors": ["trusted-user"],
    "dry_run": False,
}

# Synthetic response fixture for the TypeSafe System One schema; no live call.
JEV_RESP = {
    "model": "test-labeler-model",
    "answers": {
        "pr_type": {"type": "choice", "choice": "fix", "confidence": 1.0,
                    "probabilities": {"refactor": 0.0, "fix": 1.0, "feat": 0.0,
                                      "chore": 0.0, "docs": 0.0}},
        "risk": {"type": "score", "score": 0.4, "confidence": 0.4,
                 "legend": {"0": "trivial", "1": "moderate", "2": "critical"},
                 "probabilities": {"0": 0.67, "1": 0.26, "2": 0.07}},
    },
    "usage": {"input_tokens": 487, "output_tokens": 69},
}


class CandidateGates(unittest.TestCase):
    def test_only_open_trusted_nondraft_allowlisted(self):
        prs = [
            _pr(1),                                  # 合格
            _pr(2, draft=True),                      # draft 跳过
            _pr(3, author="stranger"),               # 不可信作者跳过
            _pr(4),                                  # allowlist 外跳过
        ]
        rcfg = dict(RCFG, pr_allowlist=[1, 2, 3])
        got = [p["number"] for p in labeler.candidate_prs(rcfg, prs)]
        self.assertEqual(got, [1])

    def test_no_allowlist_means_unrestricted(self):
        got = [p["number"] for p in labeler.candidate_prs(RCFG, [_pr(7)])]
        self.assertEqual(got, [7])


class AnswerParsing(unittest.TestCase):
    def test_probe_schema_maps_to_labels(self):
        labels = labeler.labels_from_answers(JEV_RESP["answers"], min_confidence=0.6)
        self.assertEqual(sorted(labels), ["fix", "risk-low"])

    def test_low_confidence_choice_skipped(self):
        answers = json.loads(json.dumps(JEV_RESP["answers"]))
        answers["pr_type"]["confidence"] = 0.4
        labels = labeler.labels_from_answers(answers, min_confidence=0.6)
        self.assertEqual(labels, ["risk-low"])

    def test_risk_bucket_uses_probability_argmax_with_gate(self):
        answers = json.loads(json.dumps(JEV_RESP["answers"]))
        answers["risk"]["probabilities"] = {"0": 0.34, "1": 0.33, "2": 0.33}
        labels = labeler.labels_from_answers(answers, min_confidence=0.6)
        self.assertEqual(labels, ["fix"])  # 风险分布太平，不打风险标

    def test_risk_high(self):
        answers = json.loads(json.dumps(JEV_RESP["answers"]))
        answers["risk"]["probabilities"] = {"0": 0.05, "1": 0.15, "2": 0.80}
        labels = labeler.labels_from_answers(answers, min_confidence=0.6)
        self.assertIn("risk-high", labels)


class LabelOps(unittest.TestCase):
    def test_replaces_own_stale_labels_only(self):
        add, remove = labeler.label_ops(
            current=["bug", "feat", "risk-high"],
            desired=["fix", "risk-high"],
            prev_owned=["feat", "risk-high"],
        )
        self.assertEqual(sorted(add), ["fix"])
        self.assertEqual(sorted(remove), ["feat"])  # bug 非本工具所有，绝不动

    def test_never_removes_human_label_even_on_disagreement(self):
        # 人手打了 fix，机器从未打过 → jev 判 feat 也不许摘人的 fix
        add, remove = labeler.label_ops(
            current=["fix"], desired=["feat", "risk-low"], prev_owned=[])
        self.assertEqual(sorted(add), ["feat", "risk-low"])
        self.assertEqual(remove, [])

    def test_owned_set_guard(self):
        # 账本里混入非标签集条目（脏数据）也不会被删
        add, remove = labeler.label_ops(
            current=["bug"], desired=[], prev_owned=["bug"])
        self.assertEqual((add, remove), ([], []))

    def test_noop_when_labels_match(self):
        add, remove = labeler.label_ops(
            current=["fix", "risk-low", "other"],
            desired=["fix", "risk-low"], prev_owned=["fix", "risk-low"])
        self.assertEqual((add, remove), ([], []))


class StateText(unittest.TestCase):
    def test_contains_meta_and_truncates_diff(self):
        txt = labeler.build_state_text(
            RCFG, _pr(5, title="fix pay", body="body text"),
            files=[{"path": "a.py", "additions": 3, "deletions": 1}],
            diff="x" * 100, findings=[], max_state_bytes=200)
        self.assertIn("fix pay", txt)
        self.assertIn("a.py", txt)
        self.assertLessEqual(len(txt.encode()), 200 + 100)  # 允许截断标记的余量

    def test_findings_included(self):
        txt = labeler.build_state_text(
            RCFG, _pr(5), files=[], diff="",
            findings=["null deref in pay.py CONFIRMED"], max_state_bytes=5000)
        self.assertIn("null deref", txt)


class ConfirmedFindings(unittest.TestCase):
    def test_reads_confirmed_verify_results_for_matching_head(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "demo"
            d.mkdir()
            (d / "pr-5.json").write_text(json.dumps({
                "pr": 5, "head_sha": "a" * 40,
                "verify": {"sha": "a" * 40, "results": [
                    {"finding": "bad null check", "verdict": "CONFIRMED"},
                    {"finding": "style nit", "verdict": "REJECTED"},
                ]},
            }))
            with mock.patch.object(labeler, "STATE_DIR", Path(td)):
                got = labeler.confirmed_findings("demo", 5, "a" * 40)
        self.assertEqual(got, ["bad null check"])

    def test_stale_sha_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "demo"
            d.mkdir()
            (d / "pr-5.json").write_text(json.dumps({
                "pr": 5, "head_sha": "b" * 40,
                "verify": {"sha": "b" * 40,
                           "results": [{"finding": "x", "verdict": "CONFIRMED"}]},
            }))
            with mock.patch.object(labeler, "STATE_DIR", Path(td)):
                got = labeler.confirmed_findings("demo", 5, "a" * 40)
        self.assertEqual(got, [])


class LedgerAndBackoff(unittest.TestCase):
    def test_roundtrip_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.json"
            with mock.patch.object(labeler, "LEDGER_PATH", path):
                led = labeler.load_ledger()
                led["demo#5"] = {"head_sha": "a" * 40, "labels": ["jev:fix"]}
                labeler.save_ledger(led)
                self.assertEqual(labeler.load_ledger()["demo#5"]["labels"], ["jev:fix"])
                self.assertFalse(list(Path(td).glob("*.tmp")))

    def test_backoff_set_on_failure_and_skips(self):
        led = {}
        labeler.record_failure(led)
        self.assertTrue(labeler.in_backoff(led))
        labeler.record_success(led)
        self.assertFalse(labeler.in_backoff(led))

    def test_backoff_grows_but_caps(self):
        led = {}
        for _ in range(20):
            labeler.record_failure(led)
        self.assertLessEqual(led["_backoff"]["secs"], 3600)


class ProcessPr(unittest.TestCase):
    """端到端（gh/jev mock）：幂等、dry_run、写路径。"""

    LEDGER_PREV = {"demo#5": {"head_sha": "old" * 10 + "oldoldoldo", "labels": ["feat"]}}

    def _run(self, ledger, dry_run=False, jev_fail=False, pr=None, gh_fail_labels=False):
        pr = pr or _pr(5, labels=["feat", "human-tag"])
        calls = []

        def fake_gh(args, timeout=120):
            calls.append(args)
            out = ""
            if args[:2] == ["pr", "diff"]:
                out = "diff --git a/a.py b/a.py\n+fix"
            elif args[:2] == ["pr", "view"]:
                out = json.dumps({"files": [{"path": "a.py", "additions": 3, "deletions": 1}]})
            elif gh_fail_labels and args[0] == "api" and "labels" in str(args[1]):
                return mock.Mock(returncode=1, stdout="", stderr="HTTP 500")
            return mock.Mock(returncode=0, stdout=out, stderr="")

        def fake_jev(state_text, lab_cfg):
            if jev_fail:
                raise labeler.JevError("boom")
            return JEV_RESP

        rcfg = dict(RCFG, dry_run=dry_run)
        with mock.patch.object(labeler, "gh", fake_gh), \
             mock.patch.object(labeler, "jev_decide", fake_jev), \
             mock.patch.object(labeler, "confirmed_findings", lambda *a: []), \
             mock.patch.object(labeler, "log", lambda *a: None):
            ret = labeler.process_pr(rcfg, pr, {"min_confidence": 0.6}, ledger)
        return calls, ledger, ret

    def test_applies_labels_via_rest_and_records_ledger(self):
        # Use REST issues/N/labels rather than the unrelated gh pr edit route.
        ledger = json.loads(json.dumps(self.LEDGER_PREV))  # head 变了 → 重分类
        calls, ledger, ret = self._run(ledger)
        adds = [c for c in calls if c[0] == "api" and c[1].endswith("issues/5/labels")]
        self.assertEqual(len(adds), 1)
        joined = " ".join(adds[0])
        self.assertIn("labels[]=fix", joined)
        self.assertIn("labels[]=risk-low", joined)
        dels = [c for c in calls if c[0] == "api" and "-X" in c and "DELETE" in c]
        self.assertTrue(any(c[-1].endswith("labels/feat") for c in dels))  # 旧标是我打的，换掉
        self.assertFalse(any("human-tag" in c[-1] for c in dels))          # 人的标签不碰
        self.assertFalse([c for c in calls if c[:2] == ["pr", "edit"]])
        self.assertEqual(ledger["demo#5"]["head_sha"], "a" * 40)
        self.assertEqual(sorted(ledger["demo#5"]["labels"]), ["fix", "risk-low"])
        self.assertEqual(sorted(ret), ["fix", "risk-low"])  # 返回值供 engine 行内 deep_gate 用

    def test_ledger_owns_only_labels_we_added(self):
        # jev 想要的 risk-low 人已经打了 → 我们不加也不认领，将来不许删
        pr = _pr(5, labels=["risk-low"])
        calls, ledger, _ = self._run({}, pr=pr)
        adds = [c for c in calls if c[0] == "api" and c[1].endswith("issues/5/labels")]
        self.assertEqual(len(adds), 1)
        self.assertIn("labels[]=fix", " ".join(adds[0]))
        self.assertNotIn("labels[]=risk-low", " ".join(adds[0]))
        self.assertEqual(ledger["demo#5"]["labels"], ["fix"])

    def test_same_head_skips_all_network_and_returns_ledger_labels(self):
        # 幂等跳过也要把已认领标签还给调用方：engine 行内复用时 deep_gate 不能漏判
        ledger = {"demo#5": {"head_sha": "a" * 40, "labels": ["fix", "risk-low"]}}
        calls, _, ret = self._run(ledger)
        self.assertEqual(calls, [])
        self.assertEqual(ret, ["fix", "risk-low"])

    def test_dry_run_no_mutations(self):
        calls, ledger, ret = self._run({}, dry_run=True)
        writes = [c for c in calls if c[:2] == ["pr", "edit"] or c[:2] == ["label", "create"]
                  or (c[0] == "api" and ("-X" in c or any("labels" in str(x) for x in c[1:])))]
        self.assertFalse(writes)
        self.assertNotIn("demo#5", ledger)  # dry_run 不记账，放开后真跑
        self.assertEqual(ret, [])  # 什么都没落地 → 不给 engine 升档依据

    def test_jev_failure_sets_backoff_not_ledger(self):
        calls, ledger, ret = self._run({}, jev_fail=True)
        self.assertNotIn("demo#5", ledger)
        self.assertTrue(labeler.in_backoff(ledger))
        self.assertFalse([c for c in calls if c[:2] == ["pr", "edit"]])
        self.assertIsNone(ret)  # 无结论（≠ 空标签）

    def test_label_write_failure_returns_none_for_retry(self):
        # REST 写失败 → 不记账下轮重试，本轮也不给 engine 半截结论
        _, ledger, ret = self._run({}, gh_fail_labels=True)
        self.assertNotIn("demo#5", ledger)
        self.assertIsNone(ret)


class MergedWindow(unittest.TestCase):
    """merged_lookback_hours：只补窗口内已合并的 PR；0=关闭；closedAt 缺失/脏值不进。"""

    def test_keeps_only_recent_merged(self):
        import datetime
        now_dt = datetime.datetime(2026, 10, 1, 0, 0, tzinfo=datetime.timezone.utc)
        prs = [
            dict(_pr(num=1), closedAt="2026-09-30T23:00:00Z"),   # 1h 前 → 进
            dict(_pr(num=2), closedAt="2026-09-28T23:00:00Z"),   # 49h 前 → 不进
            dict(_pr(num=3), closedAt=""),                        # 脏值 → 不进
            dict(_pr(num=4)),                                     # 缺失 → 不进
        ]
        got = [p["number"] for p in labeler.recent_merged(prs, 48, now_dt=now_dt)]
        self.assertEqual(got, [1])

    def test_zero_hours_disables(self):
        prs = [dict(_pr(num=1), closedAt="2026-09-30T23:00:00Z")]
        self.assertEqual(labeler.recent_merged(prs, 0), [])

    def test_boundary_inclusive_within_window(self):
        import datetime
        now_dt = datetime.datetime(2026, 10, 1, 0, 0, tzinfo=datetime.timezone.utc)
        prs = [dict(_pr(num=7), closedAt="2026-09-29T00:00:01Z")]  # 刚好 48h 内
        got = [p["number"] for p in labeler.recent_merged(prs, 48, now_dt=now_dt)]
        self.assertEqual(got, [7])


if __name__ == "__main__":
    unittest.main()
