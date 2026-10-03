#!/usr/bin/env python3
"""ReviewPilot 旁路打标器：用 jev（TypeSafe System One）给 PR 分类并打类型和风险标签。

与主流程的关系：
主路径是 engine worker 审查前行内调用 process_pr（engine.jev_inline_labels 懒 import，
fail-open），让 risk-high 赶在首轮审查前触发 deep_gate 升档；本模块的独立 cron
（独立进程 + 独立 flock）退化为兜底/补标，与行内路径共享账本幂等。
不变量保持：jev/GitHub 任何故障只影响打标与升档，绝不影响审查与合并。

安全边界与 engine 同源：按非 draft、可信作者、allowlist 筛选 open 和近期已合并 PR
（不可信 diff 不外发第三方）；标签无前缀，只增删 OWNED_LABELS 集合内且账本记录
为自己打过的标签（人手打的同名标签不动）；JEV_API_KEY 走环境变量。
"""

import datetime
import fcntl
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from engine import gh, load_config, now, pr_allowlist

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"
LEDGER_PATH = STATE_DIR / "labeler-ledger.json"
LOCK_PATH = ROOT / "locks" / "labeler.lock"

JEV_URL = "https://api.typesafe.ai/v1/systemone"

TYPE_CRITERIA = {
    "feat": "adds new functionality or capability",
    "fix": "fixes a bug or defect",
    "refactor": "restructures code without changing behavior",
    "docs": "documentation or comments only",
    "chore": "build, CI, dependencies, config, assets",
}
RISK_BUCKETS = ["low", "medium", "high"]
RISK_CRITERIA = [
    "trivial, isolated change",
    "moderate change with some blast radius",
    "touches critical path or wide surface",
]
# gh label create 用色：类型一律灰蓝，风险绿/黄/红
LABEL_COLORS = {"feat": "1d76db", "fix": "1d76db", "refactor": "1d76db",
                "docs": "1d76db", "chore": "1d76db",
                "risk-low": "0e8a16", "risk-medium": "fbca04",
                "risk-high": "d93f0b"}
# 标签无前缀，所有权靠账本：只有自己打过的标签才允许摘，
# 人手打的同名标签一律不动。可触碰的名字空间限定在这张表内。
OWNED_LABELS = frozenset(LABEL_COLORS)

DEFAULTS = {"enabled": False, "model": "jev-latest", "min_confidence": 0.6,
            "max_state_bytes": 60000, "include_diff": True, "dry_run": False,
            "merged_lookback_hours": 48}


class JevError(Exception):
    pass


def log(msg: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    line = f"[{now()}] {msg}"
    with open(LOG_DIR / f"labeler-{day}.log", "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


# ------------------------------------------------------------------- 选 PR

def candidate_prs(rcfg: dict, prs: list) -> list:
    """与 engine 同一道门：open 列表内非 draft、可信作者、allowlist 内。"""
    allowed = pr_allowlist(rcfg)
    trusted = rcfg.get("trusted_authors") or []
    out = []
    for pr in prs:
        if pr.get("isDraft"):
            continue
        if (pr.get("author") or {}).get("login") not in trusted:
            continue
        if allowed is not None and pr["number"] not in allowed:
            continue
        out.append(pr)
    return out


def recent_merged(prs: list, hours: int, now_dt: datetime.datetime | None = None) -> list:
    """窗口内已合并的 PR（closedAt 距今 <= hours）。
    补扫用于覆盖在两次扫描之间已合并的 PR；账本按 (pr, head) 去重。
    hours <= 0 关闭；closedAt 缺失/解析失败一律不进（宁漏勿炸）。"""
    if hours <= 0:
        return []
    now_dt = now_dt or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now_dt - datetime.timedelta(hours=hours)
    out = []
    for pr in prs:
        raw = pr.get("closedAt") or ""
        try:
            closed = datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if closed >= cutoff:
            out.append(pr)
    return out


# ------------------------------------------------------------------ jev 调用

def jev_decide(state_text: str, lab_cfg: dict) -> dict:
    key = os.environ.get("JEV_API_KEY", "")
    if not key:
        raise JevError("JEV_API_KEY 未设置")
    body = json.dumps({
        "model": lab_cfg.get("model", DEFAULTS["model"]),
        "state": state_text,
        "questions": {
            "pr_type": {"type": "choice",
                        "instructions": "What kind of change is this PR?",
                        "criteria": TYPE_CRITERIA},
            "risk": {"type": "score",
                     "instructions": "How risky is merging this PR without human review?",
                     "criteria": RISK_CRITERIA},
        },
    }).encode()
    req = urllib.request.Request(
        JEV_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError, ValueError) as e:
        raise JevError(f"jev 调用失败: {e}") from e


def labels_from_answers(answers: dict, min_confidence: float) -> list:
    """置信度不够的维度直接不打标（宁缺毋滥），两维独立判定。"""
    labels = []
    t = answers.get("pr_type") or {}
    if t.get("choice") in TYPE_CRITERIA and float(t.get("confidence") or 0) >= min_confidence:
        labels.append(t["choice"])
    r = answers.get("risk") or {}
    probs = r.get("probabilities") or {}
    if probs:
        idx = max(probs, key=lambda k: float(probs[k]))
        if float(probs[idx]) >= min_confidence and idx in ("0", "1", "2"):
            labels.append(f"risk-{RISK_BUCKETS[int(idx)]}")
    return labels


def label_ops(current: list, desired: list, prev_owned: list) -> tuple[list, list]:
    """加=想要而没有的；摘=以前是我打的、还挂着、现在不想要的。
    人手打的标签（不在 prev_owned）即使与判定冲突也绝不摘。"""
    cur, want = set(current), set(desired)
    add = sorted(want - cur)
    remove = sorted((set(prev_owned) & OWNED_LABELS & cur) - want)
    return add, remove


# ------------------------------------------------------------- state 文本

def confirmed_findings(slug: str, num: int, head_sha: str) -> list:
    """engine state 里与当前 head 匹配的 CONFIRMED verify 结论（供风险判定参考）。"""
    p = STATE_DIR / slug / f"pr-{num}.json"
    if not p.exists():
        return []
    try:
        st = json.loads(p.read_text())
    except ValueError:
        return []  # engine 非原子写窗口，下轮再读
    v = st.get("verify") or {}
    if v.get("sha") != head_sha:
        return []
    return [r.get("finding", "") for r in (v.get("results") or [])
            if r.get("verdict") == "CONFIRMED"]


def build_state_text(rcfg: dict, pr: dict, files: list, diff: str,
                     findings: list, max_state_bytes: int) -> str:
    lines = [
        f"Repo: {rcfg['repo']}  PR #{pr['number']}",
        f"Title: {pr.get('title', '')}",
        f"Author: {(pr.get('author') or {}).get('login', '')}",
        f"Body: {(pr.get('body') or '')[:4000]}",
        f"Files changed ({len(files)}):",
    ]
    lines += [f"  {f.get('path')} (+{f.get('additions', 0)} -{f.get('deletions', 0)})"
              for f in files[:50]]
    if findings:
        lines.append("Confirmed review findings:")
        lines += [f"  - {x[:500]}" for x in findings[:10]]
    text = "\n".join(lines)
    if diff:
        budget = max_state_bytes - len(text.encode())
        if budget > 0:
            cut = diff.encode()[:budget].decode(errors="ignore")
            text += "\nDiff:\n" + cut
            if len(diff.encode()) > budget:
                text += "\n[diff truncated]"
    return text


# ---------------------------------------------------------------- ledger

def load_ledger() -> dict:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text())
        except ValueError:
            log("ledger 损坏，重建（旧文件保留 .bad）")
            LEDGER_PATH.rename(LEDGER_PATH.with_suffix(".bad"))
    return {}


def save_ledger(led: dict) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(led, indent=1, ensure_ascii=False))
    tmp.rename(LEDGER_PATH)


def in_backoff(led: dict) -> bool:
    b = led.get("_backoff") or {}
    return bool(b.get("until", "")) and b["until"] > now()


def record_failure(led: dict) -> None:
    b = led.get("_backoff") or {"fails": 0, "secs": 0}
    b["fails"] += 1
    b["secs"] = min(60 * 2 ** (b["fails"] - 1), 3600)
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=b["secs"])
    b["until"] = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    led["_backoff"] = b


def record_success(led: dict) -> None:
    led.pop("_backoff", None)


# ------------------------------------------------------------- 单 PR 处理

def ensure_labels(repo: str, names: list, ensured: set) -> None:
    for n in names:
        if (repo, n) in ensured:
            continue
        gh(["label", "create", n, "--repo", repo, "--force",
            "--color", LABEL_COLORS.get(n, "ededed"),
            "--description", "auto by ReviewPilot jev labeler"])
        ensured.add((repo, n))


def apply_labels(repo: str, num: int, add: list, remove: list) -> bool:
    """REST 加/删标签。不用 `gh pr edit`：其 GraphQL 仍查询已下线的
    Projects classic 字段（projectCards）时可能导致整个 mutation 被拒。"""
    ok = True
    if add:
        args = ["api", f"repos/{repo}/issues/{num}/labels"]
        for a in add:
            args += ["-f", f"labels[]={a}"]
        p = gh(args)
        if p.returncode != 0:
            log(f"{repo} pr#{num} REST add labels 失败: {p.stderr[-200:]}")
            ok = False
    for r in remove:
        p = gh(["api", "-X", "DELETE",
                f"repos/{repo}/issues/{num}/labels/{urllib.parse.quote(r, safe='')}"])
        # 404 = 标签已不在（并发被人摘了），视为成功
        if p.returncode != 0 and "404" not in (p.stderr or ""):
            log(f"{repo} pr#{num} REST remove label {r} 失败: {p.stderr[-200:]}")
            ok = False
    return ok


def process_pr(rcfg: dict, pr: dict, lab_cfg: dict, ledger: dict,
               ensured: set | None = None) -> list | None:
    """返回本 head 下我们认领的标签列表（engine 行内复用时喂 deep_gate）：
    已记账/本轮落账 → 认领标签；dry_run → []（没落地不给升档依据）；
    jev 失败或标签写失败 → None（无结论，下轮重试）。"""
    slug, num, head = rcfg["slug"], pr["number"], pr.get("headRefOid", "")
    key = f"{slug}#{num}"
    prev = ledger.get(key) or {}
    if prev.get("head_sha") == head:
        return list(prev.get("labels") or [])  # 同 head 已打过，幂等跳过（不发任何网络请求）
    dry = bool(rcfg.get("dry_run") or lab_cfg.get("dry_run"))

    files, diff = [], ""
    p = gh(["pr", "view", str(num), "--repo", rcfg["repo"], "--json", "files"])
    if p.returncode == 0:
        try:
            files = (json.loads(p.stdout or "{}").get("files") or [])
        except ValueError:
            files = []
    if lab_cfg.get("include_diff", DEFAULTS["include_diff"]):
        p = gh(["pr", "diff", str(num), "--repo", rcfg["repo"]], timeout=180)
        diff = p.stdout if p.returncode == 0 else ""

    text = build_state_text(rcfg, pr, files, diff,
                            confirmed_findings(slug, num, head),
                            int(lab_cfg.get("max_state_bytes", DEFAULTS["max_state_bytes"])))
    try:
        resp = jev_decide(text, lab_cfg)
    except JevError as e:
        record_failure(ledger)
        log(f"{slug} pr#{num} jev 失败（退避 {ledger['_backoff']['secs']}s）: {e}")
        return None
    record_success(ledger)

    desired = labels_from_answers(resp.get("answers") or {},
                                  float(lab_cfg.get("min_confidence", DEFAULTS["min_confidence"])))
    current = [x.get("name", "") for x in (pr.get("labels") or [])]
    prev_owned = prev.get("labels") or []
    add, remove = label_ops(current, desired, prev_owned)
    ours = sorted(set(desired) & (set(add) | set(prev_owned)))
    usage = resp.get("usage") or {}
    tag = " [dry_run]" if dry else ""
    if not add and not remove:
        log(f"{slug} pr#{num} 标签已最新 {desired}（jev {usage.get('input_tokens', '?')}tok）{tag}")
    elif dry:
        log(f"{slug} pr#{num} dry_run: +{add} -{remove}（不写 GitHub、不记账）")
        return []
    else:
        ensure_labels(rcfg["repo"], add, ensured if ensured is not None else set())
        if not apply_labels(rcfg["repo"], num, add, remove):
            log(f"{slug} pr#{num} 标签写入失败，下轮重试")
            return None  # 不记账，下轮重试
        log(f"{slug} pr#{num} labeled +{add} -{remove}（jev {usage.get('input_tokens', '?')}tok）")

    ledger[key] = {"head_sha": head, "labels": ours,
                   "model": resp.get("model", ""), "t": now()}
    return ours


# ------------------------------------------------------------------- main

def main() -> int:
    LOCK_PATH.parent.mkdir(exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # 上一轮还在跑
    cfg = load_config()
    lab_cfg = {**DEFAULTS, **(cfg.get("labeler") or {})}
    if not lab_cfg["enabled"]:
        return 0
    ledger = load_ledger()
    if in_backoff(ledger):
        log(f"退避中（至 {ledger['_backoff']['until']}），本轮跳过")
        return 0
    ensured: set = set()
    for rcfg in cfg["repos"]:
        p = gh(["pr", "list", "--repo", rcfg["repo"], "--state", "open", "--json",
                "number,title,body,author,isDraft,headRefOid,labels"])
        if p.returncode != 0:
            log(f"{rcfg['slug']} gh pr list 失败: {p.stderr[-200:]}")
            continue
        try:
            prs = json.loads(p.stdout or "[]")
        except ValueError:
            log(f"{rcfg['slug']} gh pr list 输出非 JSON")
            continue
        lookback = int(lab_cfg.get("merged_lookback_hours",
                                   DEFAULTS["merged_lookback_hours"]))
        if lookback > 0:
            p2 = gh(["pr", "list", "--repo", rcfg["repo"], "--state", "merged",
                     "--limit", "50", "--json",
                     "number,title,body,author,isDraft,headRefOid,labels,closedAt"])
            if p2.returncode != 0:
                log(f"{rcfg['slug']} gh pr list(merged) 失败: {p2.stderr[-200:]}")
            else:
                try:
                    merged = json.loads(p2.stdout or "[]")
                except ValueError:
                    merged = []
                    log(f"{rcfg['slug']} gh pr list(merged) 输出非 JSON")
                seen = {x["number"] for x in prs}
                prs += [x for x in recent_merged(merged, lookback)
                        if x["number"] not in seen]
        for pr in candidate_prs(rcfg, prs):
            process_pr(rcfg, pr, lab_cfg, ledger, ensured)
            if in_backoff(ledger):
                break  # jev 挂了，别把剩余 PR 也白跑一遍
        save_ledger(ledger)
        if in_backoff(ledger):
            break
    save_ledger(ledger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
