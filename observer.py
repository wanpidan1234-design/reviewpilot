#!/usr/bin/env python3
"""ReviewPilot PR lifecycle observer. Writes notifications/ and sends configured notifications.

At-least-attempt delivery, NOT exactly-once: timeout/crash after delivery but
before durable acknowledgement can duplicate a message. No raw remote text
is copied into notifications — card text comes from engine state (written only
behind the trusted-author gate); the GitHub snapshot contributes timestamps
(createdAt/mergedAt) only. Run independently of the engine flock.
"""
import argparse
import email.utils
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import yaml

ROOT = Path(__file__).resolve().parent
STAGES = {
    'fetch_pr->ci_gate': '开始处理', 'ci_gate->ai_review': 'CI通过',
    'ci_gate->fixup': 'CI失败', 'ai_review->verify': 'AI审查：进入验证',
    'ai_review->merge': 'AI审查通过', 'verify->merge': '验证通过',
    'verify->fixup': '验证：需要修复', 'fixup->ci_gate': 'fixup已推送',
    'fetch_pr->skip': '待授权/仅观察',
    'fetch_pr->resolve_conflict': '合并冲突：自动解决中',
    'merge->resolve_conflict': '合并冲突：自动解决中',
    'resolve_conflict->ci_gate': '冲突已解决，重进门禁',
}
PHASES = {'ci_gate': 'CI检查', 'ai_review': 'AI审查', 'verify': '验证',
          'fixup': '修复', 'merge': '等待合并', 'escalated': '熔断待人工',
          'skipped_untrusted': '待授权/仅观察', 'merged': '等待GitHub确认',
          'resolve_conflict': '解决合并冲突'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def atomic(path, data):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as out:
        json.dump(data, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def valid_repo(rc):
    return (isinstance(rc.get('repo'), str) and
            re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', rc['repo']) and
            isinstance(rc.get('slug'), str) and
            re.fullmatch(r'[A-Za-z0-9_-]+', rc['slug']))


def dry(cfg, rc=None):
    return bool(cfg.get('dry_run') or cfg.get('defaults', {}).get('dry_run') or
                cfg.get('notify', {}).get('dry_run') or (rc or {}).get('dry_run'))


def access(rc, pr):
    allowed = rc.get('pr_allowlist')
    authorized = ('pr_allowlist' not in rc or
                  isinstance(allowed, list) and all(type(n) is int and n > 0 for n in allowed)
                  and pr['number'] in allowed)
    trusted = (pr.get('author') or {}).get('login') in (rc.get('trusted_authors') or [])
    if pr.get('isDraft'):
        return '草稿；待授权/仅观察'
    if not authorized or not trusted:
        return '待授权/仅观察'
    return '已在配置处理范围（仍须现有安全门禁）'


def parse_ts(value):
    from datetime import datetime
    try:
        return datetime.strptime(str(value), '%Y-%m-%dT%H:%M:%SZ')
    except (ValueError, TypeError):
        return None


def fmt_secs(secs):
    if secs < 0:
        return ''
    if secs < 60:
        return f'{secs}s'
    if secs < 3600:
        return f'{secs // 60}m{secs % 60:02d}s'
    if secs < 86400:
        return f'{secs // 3600}h{secs % 3600 // 60:02d}m'
    return f'{secs // 86400}d{secs % 86400 // 3600:02d}h'


def elapsed(st):
    # 首末事件间隔；时间戳缺失/格式异常一律回退为空，不影响卡片主体。
    hist = [h.get('t') for h in (st.get('history') or []) if isinstance(h, dict) and h.get('t')]
    if len(hist) < 2:
        return ''
    a, b = parse_ts(hist[0]), parse_ts(hist[-1])
    if a is None or b is None:
        return ''
    return fmt_secs(int((b - a).total_seconds()))


CARD_ICONS = (('熔断待人工', '⚖️'), ('卡点', '⏳'), ('发现新PR', '👀'), ('PR重新打开', '🔁'),
              ('CI失败', '🔧'), ('冲突', '🔀'))


def card(repo, num, labels, stage, scope, nxt, st):
    """单条通知卡片：首行定性+超链接（与 merged_card 同款），中段速览（标题/分支/审查/轮次/耗时）。"""
    icon = ('✅' if stage == '已合并' else '🚫' if stage == '已关闭' else
            next((i for key, i in CARD_ICONS if any(key in l for l in labels)), '🔄'))
    name = repo.split('/')[-1]
    lines = [f'{icon} {stage} · [{name} #{num}](https://github.com/{repo}/pull/{num})']
    title = str(st.get('title') or '').strip()
    if title:
        lines.append(f'「{title[:80]}」')
    if st.get('branch'):
        lines.append(f'⎇ {st["branch"]}' + (f' → {st["base"]}' if st.get('base') else ''))
    review = st.get('review') or {}
    facts = []
    verdict = review.get('final_verdict') or review.get('verdict')
    if verdict:
        facts.append(f'审查 {verdict}（{len(review.get("findings") or [])} findings）')
    if isinstance(st.get('round'), int) and st['round'] > 0:
        facts.append(f'fixup {st["round"]} 轮')
    dur = elapsed(st)
    if dur:
        facts.append(f'全程 {dur}')
    if facts:
        lines.append(' · '.join(facts))
    lines.append('事件：' + '；'.join(labels))
    # 熔断原因是 engine 模板文案（在 scope 内 PR 上生成），可进卡片；
    # fixup 细节仅放审查 findings（已公开于 PR 评论），CI 原始日志只报类别。
    if st.get('escalated') and st.get('escalated_reason'):
        reason = ' '.join(str(st['escalated_reason']).split())
        lines.append(f'原因：{reason[:300]}')
        fx = st.get('fixup_reason') or {}
        if fx and any(k in reason for k in ('round', '回派', '无产出')):
            kind = ' '.join(str(fx.get('kind') or '').split())
            detail = ' '.join(str(fx.get('detail') or '').split()) if '审查' in kind else ''
            blocker = kind + ('：' + detail if detail else '')
            if blocker:
                lines.append(f'待解决：{blocker[:400]}')
    lines.append(f'范围：{scope}')
    lines.append(f'下一步：{nxt}')
    return '\n'.join(lines)


def merged_card(repo, num, st, pr):
    """已合并终局报告。Telegram markdown（粗体/行内代码/链接/引用块）由 openclaw 转实体，
    不用表格（Telegram 不渲染）。详情（标题/分支/描述/diff 统计/审查）只取引擎 state——
    引擎只在可信作者门后落库；GitHub 快照仅贡献 created/merged 时间戳，维持
    「不复制远端原文」约束。所有字段缺失均整行降级，不影响卡片主体。"""
    pr = pr or {}
    name = repo.split('/')[-1]
    lines = [f'✅ **已合并** · [{name} #{num}](https://github.com/{repo}/pull/{num})']
    title = str(st.get('title') or '').strip()
    if title:
        lines.append(f'**「{title[:100]}」**')
    created = parse_ts(st.get('created_at') or pr.get('createdAt'))
    merged_at = parse_ts(st.get('merged_at') or pr.get('mergedAt'))
    if created and merged_at:
        span = fmt_secs(int((merged_at - created).total_seconds()))
        if span:
            lines += ['', f'⏱ 提出 → 合并 **{span}**'
                          f'（{created:%m-%d %H:%M} → {merged_at:%m-%d %H:%M} UTC）']
    how = ([f'ReviewPilot 引擎 {st.get("merge_via") or "自动"} 合入'] if st.get('merged')
           else ['GitHub已合并（外部或他途；未归因于引擎）'])
    dur = elapsed(st)
    if dur:
        how.append(f'引擎处理 {dur}')
    rnd = st.get('round')
    if st.get('merged') or isinstance(rnd, int):
        r = int(rnd or 0)
        how.append(f'fixup {r} 轮' if r else '0 轮 fixup，一把过')
    lines.append('🤖 ' + ' · '.join(how))
    review = st.get('review') or {}
    verdict = review.get('final_verdict') or review.get('verdict')
    if verdict:
        n = len(review.get('findings') or [])
        lines.append(f'🔍 AI 审查 {verdict}' + (f' · {n} findings 已处置' if n else ' · 无 findings'))
    if st.get('branch'):
        seg = f'⎇ `{st["branch"]}`' + (f' → `{st["base"]}`' if st.get('base') else '')
        stats = st.get('stats') or {}
        if isinstance(stats.get('files'), int):
            seg += f' · {stats["files"]} 个文件 +{stats.get("additions", 0)}/−{stats.get("deletions", 0)}'
        lines.append(seg)
    body = str(st.get('body') or '').strip()
    if body:
        cut = body[:700] + ('…' if len(body) > 700 else '')
        lines += ['', '📋 **变更内容**'] + ['> ' + l for l in cut.splitlines()]
    top = [f for f in (st.get('stats') or {}).get('top_files') or []
           if isinstance(f, dict) and f.get('path')]
    if top:
        lines += ['', '主要文件：'] + [
            f'`{f["path"]}` +{f.get("additions", 0)}/−{f.get("deletions", 0)}' for f in top[:4]]
    return '\n'.join(lines)


def history_tokens(st):
    # Repeated cached edges on a head are not progress. Successful fixup pushes
    # delimit rounds; hashes/round ordinal distinguish genuine re-entry.
    result, pushed, seen = [], set(), set()
    for h in st.get('history') or []:
        if not isinstance(h, dict):
            continue
        e, sha = h.get('edge', ''), h.get('sha')
        if e == 'fixup->ci_gate':
            pushed.add(sha)
        token = digest([e, sha, len(pushed)])
        if token not in seen:
            result.append((token, e))
            seen.add(token)
    return result


class Observer:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / 'notifications'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'ledger.json'
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            'version': 1, 'repos': [], 'prs': {}, 'outbox': {}, 'sent': {}, 'route_retry_at': 0}

    def save(self):
        atomic(self.path, self.data)

    def enqueue(self, key, repo, text, now, is_dry=False):
        if is_dry or key in self.data['sent'] or key in self.data['outbox']:
            return
        self.data['outbox'][key] = dict(repo=repo, text=text, attempts=0, retry_at=now)

    def observe(self, cfg, snapshots, states, now, selftest=False):
        if selftest:
            self.enqueue('service-enabled', None,
                         'ReviewPilot 生命周期通知已启用；历史已建立基线，不回灌。下一步：只读观察配置仓库，不扩大自动处理权限。',
                         now, dry(cfg) or all(dry(cfg, r) for r in cfg['repos']))
        for rc in cfg['repos']:
            repo = rc['repo']
            if not valid_repo(rc) or repo not in snapshots:
                continue  # failed collection must never establish a baseline
            baseline = repo not in self.data['repos']
            for pr in snapshots[repo]:
                num = pr.get('number')
                if type(num) is not int or num <= 0 or pr.get('state') not in ('OPEN', 'CLOSED', 'MERGED'):
                    continue
                if (pr.get('author') or {}).get('login') not in (rc.get('trusted_authors') or []):
                    continue  # 非可信作者的 PR 不汇报也不处理
                key = f'{repo}#{num}'
                st = states.get(key, {})
                old = self.data['prs'].get(key)
                tokens = history_tokens(st)
                seen = set(old.get('history', [])) if old else set()
                current = [t for t, _ in tokens]
                head = digest(pr.get('headRefOid'))
                phase = st.get('phase')
                phase = phase if phase in PHASES else 'observing'
                status = pr['state']
                scope = access(rc, pr)
                signature = digest([head, status, phase, st.get('round', 0), current, scope, bool(st.get('escalated'))])
                changed = old is None or signature != old['signature']
                revision = (old or {}).get('revision', 0) + int(changed)
                entry = dict(repo=repo, number=num, state=status, head=head,
                             signature=signature, history=current, phase=phase, scope=scope,
                             escalated=bool(st.get('escalated')), revision=revision,
                             since=now if changed else old['since'],
                             stalled=False if changed else old['stalled'])
                labels = []
                terminal = status in ('CLOSED', 'MERGED')
                if not baseline:
                    if terminal:
                        if old is None or old['state'] != status:
                            labels = ['GitHub已合并（可能为外部合并；未归因于引擎）' if status == 'MERGED' else 'GitHub已关闭（未合并）']
                            # Old unsent progress is obsolete after terminal observation.
                            for event in list(self.data['outbox']):
                                if event.startswith(key + ':'):
                                    del self.data['outbox'][event]
                    else:
                        if old is None:
                            labels.append('发现新PR')
                        elif old['state'] != 'OPEN':
                            labels.append('PR重新打开')
                        if old and old.get('scope', scope) != scope:
                            labels.append('处理资格变化')
                        if old and old['head'] != head:
                            labels.append('新head；解除熔断' if old['escalated'] and not st.get('escalated') else '新head，等待门禁复审')
                        elif old and old['escalated'] and not st.get('escalated'):
                            labels.append('解除熔断，等待门禁复审')
                        for token, e in tokens:
                            if token in seen:
                                continue
                            label = '熔断待人工' if e.endswith('->escalate') or e == 'escalate->END' else STAGES.get(e)
                            if label and label not in labels:
                                labels.append(label)
                        if st.get('escalated') and (not old or not old['escalated']) and '熔断待人工' not in labels:
                            labels.append('熔断待人工')
                        if (not labels and not entry['stalled'] and
                                phase in ('ci_gate', 'ai_review', 'verify', 'fixup', 'merge',
                                          'resolve_conflict') and
                                not st.get('escalated') and '仅观察' not in scope and
                                now - entry['since'] >= 35 * 60):
                            labels.append('卡点：同阶段35分钟无新进展')
                            entry['stalled'] = True
                    if labels:
                        stage = ('已合并' if status == 'MERGED' else '已关闭' if status == 'CLOSED'
                                 else PHASES.get(phase, '只读观察'))
                        nxt = ('生命周期结束，仅保留观察记录' if terminal else
                               '等待人工裁决，不自动扩大权限' if st.get('escalated') or '仅观察' in scope else
                               '等待现有引擎推进；持续无进展请检查引擎/门禁')
                        text = (merged_card(repo, num, st, pr) if status == 'MERGED'
                                else card(repo, num, labels, stage, scope, nxt, st))
                        event = f'{key}:{revision}:{"stall" if entry["stalled"] else "progress"}'
                        self.enqueue(event, repo, text, now, dry(cfg, rc))
                self.data['prs'][key] = entry
            if baseline:
                self.data['repos'].append(repo)
        self.save()

    def flush(self, cfg, now, sender=None, limit=3):
        sender = sender or send_cli
        if dry(cfg) or not cfg.get('notify', {}).get('lifecycle_observer') or now < self.data['route_retry_at']:
            return
        repos = {r['repo']: r for r in cfg['repos']}
        count = 0
        for key, item in list(self.data['outbox'].items()):
            rc = repos.get(item['repo']) if item['repo'] else None
            if item['repo'] and (rc is None or dry(cfg, rc)):
                continue
            if item['retry_at'] > now:
                continue
            if count >= limit:
                break
            count += 1
            # Persist pre-attempt delay: crash recovery never immediately hot-loops.
            item['attempts'] += 1
            delay = min(3600, 60 * 2 ** min(item['attempts'] - 1, 6))
            item['retry_at'] = now + delay
            item['reason'] = 'delivery_unconfirmed'
            self.save()
            try:
                result = sender(cfg['notify'], item['text'])
            except Exception:
                result = {'ok': False, 'ambiguous': True}
            mid = result.get('messageId')
            if result.get('ok') is True and isinstance(mid, (str, int)) and not isinstance(mid, bool) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', str(mid)):
                self.data['sent'][key] = {'messageId': str(mid), 'at': now}
                del self.data['outbox'][key]
            else:
                retry = result.get('retry_after', 0)
                retry = retry if isinstance(retry, (int, float)) and retry >= 0 else 0
                item['retry_at'] = now + max(delay, retry)
                item['ambiguous'] = bool(result.get('ambiguous'))
                if retry or result.get('reason') == 'rate_limited':
                    self.data['route_retry_at'] = item['retry_at']
                    self.save()
                    break
            self.save()


def retry_after(text, now=None):
    values = []
    for m in re.finditer(r'(?:retry[-_ ]after)[\"\s]*[:=][\"\s]*([^\r\n]+)', text, re.I):
        value = m.group(1)
        numeric = re.match(r'\d+(?:\.\d+)?', value)
        if numeric:
            values.append(float(numeric.group()))
        else:
            try:
                values.append(max(0, email.utils.parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)))
            except (ValueError, TypeError, OverflowError):
                pass
    return max(values, default=0)


def send_cli(notify, text):
    args = ['openclaw', 'message', 'send', '--channel', notify['channel'],
            '--target', str(notify['target']), '--message', text, '--json']
    if notify.get('account'):
        args += ['--account', str(notify['account'])]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'ambiguous': True, 'reason': 'timeout'}
    except OSError:
        return {'ok': False, 'reason': 'cli_unavailable'}
    try:
        body = json.loads(p.stdout)
        if not isinstance(body, dict):
            body = {}
    except (ValueError, TypeError):
        body = {}
    payload = body.get('payload') if isinstance(body.get('payload'), dict) else {}
    nested = payload.get('result') if isinstance(payload.get('result'), dict) else {}
    records = [body, payload, nested]
    mid = body.get('messageId') or payload.get('messageId') or nested.get('messageId')
    success = (p.returncode == 0 and bool(mid) and
               all(r.get('ok') is not False and not r.get('dryRun') and
                   r.get('deliveryStatus', 'sent') == 'sent' for r in records))
    if success:
        return {'ok': True, 'messageId': mid}
    retry = retry_after((p.stdout or '') + '\n' + (p.stderr or ''))
    return {'ok': False, 'retry_after': retry, 'ambiguous': bool(mid) or p.returncode == 0,
            'reason': 'rate_limited' if retry or re.search(r'\b429\b', (p.stdout or '') + (p.stderr or '')) else 'delivery_unconfirmed'}


def github(args):
    p = subprocess.run(['gh', 'api', *args], capture_output=True, text=True, timeout=90)
    if p.returncode:
        raise RuntimeError('github_read_failed')
    if '--paginate' in args:
        # Older installed gh emits consecutive JSON documents, without --slurp.
        decoder, remaining, pages = json.JSONDecoder(), p.stdout.lstrip(), []
        while remaining:
            page, end = decoder.raw_decode(remaining)
            if not isinstance(page, list):
                raise ValueError('github_invalid_page')
            pages.append(page)
            remaining = remaining[end:].lstrip()
        return pages
    return json.loads(p.stdout)


def normalized(p):
    # createdAt/mergedAt 是纯时间戳（供已合并卡片算「提出→合并」时长），不属远端原文。
    return dict(number=p['number'], state='MERGED' if p.get('merged') or p.get('merged_at') else str(p['state']).upper(),
                isDraft=bool(p.get('draft')), headRefOid=(p.get('head') or {}).get('sha'),
                author={'login': (p.get('user') or {}).get('login')},
                createdAt=p.get('created_at'), mergedAt=p.get('merged_at'))


def collect(root, cfg, ledger):
    snapshots, states = {}, {}
    for rc in cfg['repos']:
        if not valid_repo(rc):
            continue
        repo = rc['repo']
        try:
            pages = github(['--paginate', f'repos/{repo}/pulls?state=all&sort=created&direction=desc&per_page=100'])
            prs = {p['number']: normalized(p) for page in pages for p in page}
            for previous in ledger['prs'].values():
                if previous['repo'] == repo and previous['state'] == 'OPEN' and previous['number'] not in prs:
                    n = previous['number']
                    prs[n] = normalized(github([f'repos/{repo}/pulls/{n}']))
            snapshots[repo] = list(prs.values())
        except Exception:
            continue  # no false close / baseline on API failure, no raw error logging
        for p in prs.values():
            path = Path(root) / 'state' / rc['slug'] / f'pr-{p["number"]}.json'
            try:
                st = json.loads(path.read_text())
                if isinstance(st, dict):
                    states[f'{repo}#{p["number"]}'] = st
            except (OSError, ValueError):
                # Engine writes non-atomically. Preserve previous snapshot on transient reads.
                previous = ledger['prs'].get(f'{repo}#{p["number"]}')
                if path.exists() and previous and p['state'] == 'OPEN':
                    snapshots[repo] = [x for x in snapshots[repo] if x['number'] != p['number']]
    return snapshots, states


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true', help='queue a once-only enabled notice')
    args = parser.parse_args()
    cfg = yaml.safe_load((ROOT / 'config.yml').read_text())
    cfg['repos'] = [dict(cfg.get('defaults') or {}, **r) for r in cfg['repos']]
    if cfg.get('notify', {}).get('lifecycle_observer') is not True:
        return 0
    directory = ROOT / 'notifications'
    directory.mkdir(exist_ok=True)
    with (directory / 'observer.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        obj = Observer(ROOT)
        snapshots, states = collect(ROOT, cfg, obj.data)
        now = time.time()
        obj.observe(cfg, snapshots, states, now, selftest=args.self_test and len(snapshots) == len(cfg['repos']))
        obj.flush(cfg, now)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('ReviewPilot observer: safe failure; inspect local configuration/availability')
        raise SystemExit(1)
