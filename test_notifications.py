"""Offline only: subprocess blocked except injected mock CLI responses."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('observer', HERE / 'observer.py')
observer = importlib.util.module_from_spec(spec) if spec and (HERE / 'observer.py').exists() else None
if observer:
    spec.loader.exec_module(observer)
import engine


def pr(n=1, state='OPEN', **kw):
    return dict(number=n, state=state, headRefOid='a'*40, isDraft=False, author={'login': 'trusted'}, **kw)


def edge(name, sha='a'*40, t='2026-09-11T10:00:00Z'):
    return dict(edge=name, sha=sha, t=t, reason='SECRET raw stdout never send')


class Notifications(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(observer, 'lifecycle observer implementation missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {'notify': {'channel': 'telegram', 'target': '123', 'lifecycle_observer': True},
                    'repos': [{'repo': 'owner/repo', 'slug': 'repo', 'trusted_authors': ['trusted'], 'pr_allowlist': [1]}]}
        self.external = patch('subprocess.run', side_effect=AssertionError('real external operation forbidden'))
        self.external.start()
        self.addCleanup(self.external.stop)
        self.states = {}
        self.o = observer.Observer(self.root)

    def tick(self, prs, now=0, repo='owner/repo', **kw):
        return self.o.observe(self.cfg, {repo: prs}, self.states, now, **kw)

    def pending(self):
        return list(self.o.data['outbox'].values())

    def texts(self):
        return '\n'.join(x['text'] for x in self.pending())

    def baseline(self):
        self.tick([pr()])
        self.assertFalse(self.pending())

    def test_first_baseline_and_new_repo_do_not_backfill(self):
        self.states['owner/repo#1'] = {'history': [edge('ci_gate->ai_review')], 'phase': 'ai_review'}
        self.baseline()
        self.cfg['repos'].append({'repo': 'owner/new', 'slug': 'new'})
        self.o.observe(self.cfg, {'owner/repo': [pr()], 'owner/new': [pr(90, 'MERGED')]}, self.states, 5)
        self.assertFalse(self.pending())

    def test_selftest_once_explicit_and_safe(self):
        self.tick([], selftest=True)
        self.assertIn('生命周期通知已启用', self.texts())
        self.tick([], 1, selftest=True)
        self.assertEqual(len(self.pending()), 1)

    def test_untrusted_author_pr_is_silent(self):
        self.baseline()
        p = pr(3); p['author'] = {'login': 'stranger'}
        self.tick([pr(), p], 1)
        self.assertNotIn('pull/3', self.texts())
        self.tick([pr(), dict(p, state='MERGED')], 2)
        self.assertNotIn('pull/3', self.texts())

    def test_discovery_draft_outside_allowlist_observe_only(self):
        self.baseline()
        p = pr(2); p['isDraft'] = True; p['title'] = 'SECRET'
        self.tick([pr(), p], 1)
        self.assertIn('待授权/仅观察', self.texts())
        self.assertIn('草稿', self.texts())
        self.assertIn('https://github.com/owner/repo/pull/2', self.texts())
        self.assertNotIn('SECRET', self.texts())
        self.tick([pr(), p], 2)
        self.assertEqual(len(self.pending()), 1)

    def test_progress_coalesces_history_and_dedupes_head_round(self):
        self.baseline()
        edges = ['fetch_pr->ci_gate', 'ci_gate->ai_review', 'ai_review->verify', 'verify->fixup', 'fixup->ci_gate']
        self.states['owner/repo#1'] = {'history': [edge(e) for e in edges], 'phase': 'ci_gate', 'round': 1}
        self.tick([pr()], 1)
        self.assertEqual(len(self.pending()), 1)
        for label in ['开始', 'CI通过', 'AI审查', '验证', 'fixup已推送']:
            self.assertIn(label, self.texts())
        self.tick([pr()], 2)
        self.assertEqual(len(self.pending()), 1)
        self.states['owner/repo#1']['history'].append(edge('ci_gate->fixup', sha='b'*40))
        self.states['owner/repo#1']['round'] = 2
        p = pr(); p['headRefOid'] = 'b'*40
        self.tick([p], 3)
        self.assertEqual(len(self.pending()), 2)
        self.assertIn('CI失败', self.texts())
        self.assertNotIn('SECRET', self.texts())

    def test_terminal_suppresses_same_tick_progress_and_pending(self):
        self.baseline()
        self.states['owner/repo#1'] = {'history': [edge('ci_gate->ai_review')]}
        self.tick([pr()], 1)
        self.tick([pr(state='MERGED')], 2)
        self.assertEqual(len(self.pending()), 1)
        self.assertIn('GitHub已合并', self.texts())
        self.assertNotIn('engine完成', self.texts())
        self.assertNotIn('CI通过', self.texts())
        self.tick([pr(state='MERGED')], 3)
        self.assertEqual(len(self.pending()), 1)

    def test_closed_and_reopened(self):
        self.baseline()
        self.tick([pr(state='CLOSED')], 1)
        self.assertIn('已关闭', self.texts())
        self.tick([pr()], 2)
        self.assertIn('重新打开', self.texts())

    def test_stall_once_until_progress_resets(self):
        self.states['owner/repo#1'] = {'phase': 'verify', 'history': []}
        self.baseline()
        self.tick([pr()], 2100)
        self.assertIn('卡点', self.texts())
        self.assertIn('下一步', self.texts())
        self.tick([pr()], 9000)
        self.assertEqual(len(self.pending()), 1)
        self.states['owner/repo#1']['history'].append(edge('verify->fixup'))
        self.tick([pr()], 9100)
        self.tick([pr()], 11200)
        self.assertEqual(len(self.pending()), 3)

    def test_escalation_and_new_head_resume(self):
        self.baseline()
        self.states['owner/repo#1'] = {'phase': 'escalated', 'escalated': True, 'history': [edge('fixup->escalate')]}
        self.tick([pr()], 1)
        self.assertIn('熔断待人工', self.texts())
        self.states['owner/repo#1']['escalated'] = False
        self.states['owner/repo#1']['phase'] = 'ci_gate'
        p = pr(); p['headRefOid'] = 'b'*40
        self.tick([p], 2)
        self.assertIn('解除熔断', self.texts())

    def test_escalation_card_carries_detailed_reason(self):
        self.baseline()
        self.states['owner/repo#1'] = {
            'phase': 'escalated', 'escalated': True,
            'escalated_reason': 'round=2 >= max_rounds=2 熔断',
            'fixup_reason': {'kind': 'AI 审查 BLOCK（对抗验证确认）',
                             'detail': '确认成立的 findings：\n- 越界读取'},
            'history': [edge('fixup->escalate')]}
        self.tick([pr()], 1)
        self.assertIn('原因：round=2 >= max_rounds=2 熔断', self.texts())
        self.assertIn('待解决：AI 审查 BLOCK（对抗验证确认）：确认成立的 findings： - 越界读取',
                      self.texts())

    def test_merge_failure_reason_shown_without_stale_fixup_detail(self):
        self.baseline()
        self.states['owner/repo#1'] = {
            'phase': 'escalated', 'escalated': True,
            'escalated_reason': 'merge 失败: X Pull request not mergeable',
            'fixup_reason': {'kind': 'AI 审查 BLOCK（对抗验证确认）', 'detail': 'STALE finding'},
            'history': [edge('merge->escalate')]}
        self.tick([pr()], 1)
        self.assertIn('原因：merge 失败', self.texts())
        self.assertNotIn('STALE', self.texts(), '合并失败的熔断不得回放早轮 fixup 细节')

    def test_ci_round_limit_shows_kind_but_not_raw_logs(self):
        self.baseline()
        self.states['owner/repo#1'] = {
            'phase': 'escalated', 'escalated': True,
            'escalated_reason': 'round=2 >= max_rounds=2 熔断',
            'fixup_reason': {'kind': 'CI 红（lint/build 失败）', 'detail': 'SECRET raw build stdout'},
            'history': [edge('fixup->escalate')]}
        self.tick([pr()], 1)
        self.assertIn('待解决：CI 红（lint/build 失败）', self.texts())
        self.assertNotIn('SECRET', self.texts())

    def test_observed_or_escalated_prs_are_not_processing_stalls(self):
        self.states['owner/repo#1'] = {'phase': 'escalated', 'escalated': True}
        p = pr(2); p['isDraft'] = True
        self.tick([pr(), p, pr(3)])
        self.tick([pr(), p, pr(3)], 2200)
        self.assertFalse(self.pending(), 'waiting for authorization is not an engine stall')

    def test_ready_and_scope_changes_are_reported(self):
        p = pr(); p['isDraft'] = True
        self.tick([p])
        self.tick([pr()], 1)
        self.assertIn('处理资格变化', self.texts())
        self.tick([pr()], 2)
        self.assertEqual(len(self.pending()), 1)

    def test_terminal_status_does_not_say_waiting_for_github(self):
        self.states['owner/repo#1'] = {'phase': 'merged'}
        self.baseline()
        self.tick([pr(state='MERGED')], 1)
        self.assertIn('✅ **已合并**', self.texts())
        self.assertNotIn('等待GitHub确认', self.texts())

    def test_merged_card_reports_span_and_content_from_engine_state(self):
        self.baseline()
        self.states['owner/repo#1'] = {
            'phase': 'merged', 'merged': True, 'merge_via': 'squash', 'round': 1,
            'title': '请求重试', 'branch': 'feature/retry', 'base': 'main',
            'created_at': '2026-09-26T18:02:00Z', 'merged_at': '2026-09-26T21:44:00Z',
            'body': '为示例请求增加超时重试。\n- 指数退避\n- 降级路径',
            'stats': {'files': 3, 'additions': 120, 'deletions': 45,
                      'top_files': [{'path': 'a.py', 'additions': 80, 'deletions': 20}]},
            'review': {'verdict': 'BLOCK', 'final_verdict': 'APPROVE',
                       'findings': [{'t': 'x'}, {'t': 'y'}]},
            'history': [edge('fetch_pr->ci_gate', t='2026-09-26T20:39:00Z'),
                        edge('verify->merge', t='2026-09-26T21:44:00Z')]}
        self.tick([pr(state='MERGED')], 1)
        text = self.texts()
        self.assertIn('✅ **已合并** · [repo #1](https://github.com/owner/repo/pull/1)', text)
        self.assertIn('**「请求重试」**', text)
        self.assertIn('提出 → 合并 **3h42m**（09-26 18:02 → 09-26 21:44 UTC）', text)
        self.assertIn('ReviewPilot 引擎 squash 合入', text)
        self.assertIn('引擎处理 1h05m', text)
        self.assertIn('fixup 1 轮', text)
        self.assertIn('AI 审查 APPROVE · 2 findings 已处置', text)
        self.assertIn('⎇ `feature/retry` → `main` · 3 个文件 +120/−45', text)
        self.assertIn('📋 **变更内容**', text)
        self.assertIn('> - 指数退避', text)
        self.assertIn('`a.py` +80/−20', text)
        self.assertNotIn('GitHub已合并', text, '引擎已归因时不得再挂外部合并的对冲话术')
        self.assertNotIn('SECRET', text, 'history 原始 reason 不得进入已合并卡片')

    def test_merged_card_external_falls_back_to_snapshot_timestamps(self):
        self.baseline()
        p = pr(state='MERGED')
        p['createdAt'] = '2026-09-24T08:00:00Z'
        p['mergedAt'] = '2026-09-26T11:22:00Z'
        self.tick([p], 1)
        text = self.texts()
        self.assertIn('GitHub已合并（外部或他途；未归因于引擎）', text)
        self.assertIn('提出 → 合并 **2d03h**', text)
        self.assertNotIn('变更内容', text, '无引擎 state 时不得凭空编造内容详情')

    def test_dry_run_no_send_or_formal_sent(self):
        self.cfg['repos'][0]['dry_run'] = True
        self.baseline()
        self.tick([pr(), pr(2)], 1)
        send = unittest.mock.Mock()
        self.o.flush(self.cfg, now=2, sender=send)
        send.assert_not_called()
        self.assertFalse(self.o.data['sent'])
        self.cfg['repos'][0]['dry_run'] = False
        self.o.flush(self.cfg, now=3, sender=lambda *_: {'ok': True, 'messageId': '7'})
        self.assertFalse(self.o.data['sent'], 'dry run events must not become live backlog')

    def test_root_dry_run_blocks_existing_outbox(self):
        self.baseline(); self.tick([pr(), pr(2)], 1)
        self.cfg['dry_run'] = True
        send = unittest.mock.Mock()
        self.o.flush(self.cfg, now=2, sender=send)
        send.assert_not_called()

    def test_success_failure_timeout_429_restart_and_limit(self):
        self.baseline(); self.tick([pr(), pr(2), pr(3), pr(4)], 1)
        calls = []
        def fail(*args):
            calls.append(args)
            return {'ok': False, 'retry_after': 500, 'reason': 'rate_limited'}
        self.o.flush(self.cfg, now=2, sender=fail, limit=2)
        self.assertEqual(len(calls), 1, 'rate limit pauses whole route')
        self.assertEqual(len(self.pending()), 3)
        self.o = observer.Observer(self.root)
        self.o.flush(self.cfg, now=501, sender=fail)
        self.assertEqual(len(calls), 1)
        self.o.flush(self.cfg, now=502, sender=lambda *_: {'ok': True, 'messageId': '42'}, limit=1)
        self.assertEqual(len(self.o.data['sent']), 1)
        self.assertEqual(len(self.pending()), 2)
        self.o.flush(self.cfg, now=503, sender=lambda *_: {'ok': True}, limit=1)
        self.assertEqual(len(self.o.data['sent']), 1)

    def test_cli_contract_and_safe_failures(self):
        cfg = self.cfg['notify']
        for payload in [{'messageId': '42'}, {'result': {'messageId': '42'}}]:
            result = types.SimpleNamespace(returncode=0, stdout=json.dumps({'action': 'send', 'channel': 'telegram', 'dryRun': False, 'payload': payload}), stderr='')
            with patch('subprocess.run', return_value=result) as run:
                out = observer.send_cli(cfg, 'safe')
                self.assertTrue(out['ok']); self.assertEqual(out['messageId'], '42')
                self.assertEqual(run.call_args.args[0][:3], ['openclaw', 'message', 'send'])
                self.assertIn('--json', run.call_args.args[0])
        for code, body in [(0, {'ok': False, 'messageId': '42'}), (1, {'messageId': '42'}), (0, {}), (0, {'dryRun': True, 'messageId': '42'}), (0, {'deliveryStatus': 'partial', 'messageId': '42'})]:
            with patch('subprocess.run', return_value=types.SimpleNamespace(returncode=code, stdout=json.dumps(body), stderr='SECRET')):
                self.assertFalse(observer.send_cli(cfg, 'safe')['ok'])
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('openclaw', 60, output='SECRET')):
            out = observer.send_cli(cfg, 'safe')
            self.assertFalse(out['ok']); self.assertTrue(out['ambiguous']); self.assertNotIn('SECRET', str(out))
        with patch('subprocess.run', return_value=types.SimpleNamespace(returncode=1, stdout='', stderr='429 Retry-After: 123 SECRET')):
            out = observer.send_cli(cfg, 'safe')
            self.assertEqual(out['retry_after'], 123); self.assertNotIn('SECRET', str(out))

    def test_collect_paginated_and_tracked_terminal_read_only(self):
        self.baseline()
        state = self.root / 'state/repo/pr-1.json'
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'history': [edge('ci_gate->ai_review')]}))
        before = state.read_bytes()
        commands = []
        def run(args, **kw):
            commands.append(args)
            if '/pulls?' in args[-1]:
                return types.SimpleNamespace(returncode=0, stdout=json.dumps([{'number': 2, 'state': 'open', 'draft': True, 'head': {'sha': 'b'*40}, 'user': {'login': 'outsider'}}]), stderr='')
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({'number': 1, 'state': 'closed', 'merged': True, 'head': {'sha': 'a'*40}}), stderr='')
        with patch('subprocess.run', side_effect=run):
            prs, states = observer.collect(self.root, self.cfg, self.o.data)
        self.assertEqual(prs['owner/repo'][1]['state'], 'MERGED')
        self.assertEqual(state.read_bytes(), before)
        self.assertIn('--paginate', commands[0]); self.assertNotIn('--slurp', commands[0])
        self.assertTrue(all(cmd[:2] == ['gh', 'api'] for cmd in commands))
        self.assertIn('owner/repo#1', states)

    def test_github_pagination_works_without_new_slurp_flag(self):
        result = types.SimpleNamespace(returncode=0, stdout='[{"number":1}]\n[{"number":2}]\n[]\n', stderr='')
        with patch('subprocess.run', return_value=result):
            pages = observer.github(['--paginate', 'repos/owner/repo/pulls'])
        self.assertEqual(pages, [[{'number': 1}], [{'number': 2}], []])

    def test_timeout_persists_backoff_across_restart(self):
        self.baseline(); self.tick([pr(), pr(2)], 1)
        def timeout(*_):
            raise subprocess.TimeoutExpired('mock', 60)
        self.o.flush(self.cfg, now=2, sender=timeout)
        self.o = observer.Observer(self.root)
        item = self.pending()[0]
        self.assertTrue(item['ambiguous'])
        self.assertEqual(item['retry_at'], 62)
        send = unittest.mock.Mock(return_value={'ok': True, 'messageId': '8'})
        self.o.flush(self.cfg, now=61, sender=send)
        send.assert_not_called()
        self.o.flush(self.cfg, now=62, sender=send)
        self.assertEqual(len(self.o.data['sent']), 1)

    def test_invalid_state_does_not_fake_resume(self):
        self.states['owner/repo#1'] = {'phase': 'escalated', 'escalated': True}
        self.baseline()
        path = self.root / 'state/repo/pr-1.json'
        path.parent.mkdir(parents=True)
        path.write_text('{partial')
        remote = [[{'number': 1, 'state': 'open', 'head': {'sha': 'a'*40}}]]
        with patch.object(observer, 'github', return_value=remote):
            prs, states = observer.collect(self.root, self.cfg, self.o.data)
        self.o.observe(self.cfg, prs, states, 1)
        self.assertFalse(self.pending())
        self.assertTrue(self.o.data['prs']['owner/repo#1']['escalated'])

    def test_retry_after_http_date_and_nested_json(self):
        self.assertEqual(observer.retry_after('Retry-After: Fri, 11 Sep 2026 12:00:00 GMT', now=1789127400), 600)
        self.assertEqual(observer.retry_after('{"parameters":{"retry_after":321}}'), 321)

    def test_baseline_history_is_not_new_discovery_progress(self):
        self.tick([])
        self.states['owner/repo#2'] = {'history': [edge('ci_gate->ai_review')]}
        self.tick([pr(2)], 1)
        self.assertIn('发现新PR', self.texts())
        self.assertEqual(len(self.pending()), 1)

    def test_repeated_same_head_stage_does_not_spam(self):
        self.baseline()
        self.states['owner/repo#1'] = {'phase': 'ai_review', 'history': [edge('fetch_pr->ci_gate'), edge('ci_gate->ai_review')]}
        self.tick([pr()], 1)
        self.states['owner/repo#1']['history'] += [edge('fetch_pr->ci_gate', t='later'), edge('ci_gate->ai_review', t='later')]
        self.tick([pr()], 2)
        self.assertEqual(len(self.pending()), 1)

    def test_terminal_survives_partial_engine_state(self):
        self.baseline()
        path = self.root / 'state/repo/pr-1.json'
        path.parent.mkdir(parents=True)
        path.write_text('{partial')
        with patch.object(observer, 'github', return_value=[[{'number': 1, 'state': 'closed', 'merged': True, 'user': {'login': 'trusted'}}]]):
            prs, states = observer.collect(self.root, self.cfg, self.o.data)
        self.o.observe(self.cfg, prs, states, 1)
        self.assertIn('GitHub已合并', self.texts())

    def test_wrapper_no_secrets_and_independent_lock(self):
        path = HERE / 'observer-run.sh'
        self.assertTrue(path.exists(), 'safe observer wrapper missing')
        script = path.read_text()
        for forbidden in ['CRS_KEY', '.bashrc', 'source ', 'engine.py', 'reviewpilot.lock']:
            self.assertNotIn(forbidden, script)
        self.assertIn('NVM_NODE_BIN', script)
        self.assertIn('observer.py', script)

    def test_legacy_notify_and_switch(self):
        ctx = types.SimpleNamespace(cfg={}, notify_cfg=self.cfg['notify'], slug='repo', num=1)
        with patch.object(engine, 'sh', return_value=types.SimpleNamespace(returncode=0)) as sh:
            engine.notify(ctx, 'safe'); sh.assert_not_called()
            del ctx.notify_cfg['lifecycle_observer']
            engine.notify(ctx, 'safe'); sh.assert_called_once()


if __name__ == '__main__':
    unittest.main()
