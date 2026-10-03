# ReviewPilot

ReviewPilot 将 GitHub PR 的 CI、AI 审查、问题复核、修复和合并组织成显式状态机。Python 引擎负责调度，`codex exec` 负责审查与代码修改；可选 TypeSafe 打标器和 OpenClaw 通知观察器提供分类与进展通知。

这是源码分发，附带可安装的 Codex skill。它尚不是经过安全加固的无人值守服务，也不是已发布的 Python 包或平台插件。公开仓库不等于开源许可；再分发边界见 [RIGHTS.md](RIGHTS.md)。

## 安装

需要已有 Python 3.10+ 和可用的官方 PyPI 二进制 wheels。在源码目录运行：

```sh
./install.sh
# 或指定已有解释器：
PYTHON=/absolute/path/to/python3 ./install.sh
./reviewpilot --help
./reviewpilot --version
```

安装器只创建专用 `.venv`，从官方 PyPI 安装 `requirements.txt` 中固定版本的二进制依赖，并在 `config.yml` 不存在时复制 `config.example.yml`。它不安装系统组件、不克隆仓库、不运行 PR 流程、不配置凭据、不创建 cron 或服务。已有配置不会被覆盖；缺少兼容 Python 或 wheel 时停止。

```sh
./install.sh --verify-only
```

此检查只读取安装文件和依赖元数据，不导入应用或测试，不检查认证。默认配置仓库为空，标签与通知观察器关闭；安装完成不代表运行条件已满足。

## 配置与单次运行

运行前阅读 [SECURITY.md](SECURITY.md)。准备 `config.yml`、`repos/<slug>/` 中的专用 Git clone，以及当前 shell 可见的 `git`、`gh`、`codex`。GitHub 连接器权限不会自动转为本机 `gh` 或 Git 权限。为 CI 准备所需工具，不使用空 `ci` 列表代替验证：空列表会被引擎判为成功。

在 `repos` 中登记目标 `owner/repository` 和唯一 `slug`；用 `pr_allowlist: [编号]` 限定本次 PR，用 `trusted_authors` 限定已授权作者。默认的空白名单拒绝全部 PR。还需选择实际可用的审查模型、推理力度、超时、修复次数上限及 CI 命令。示例文件中的说明不代表已验证的项目配置。

完成访问、数据外发和 GitHub 写入授权后，使用同步入口：

```sh
./reviewpilot run --repo <slug> --pr <number>
# 等价的 Python 入口：
.venv/bin/python reviewpilot.py run --repo <slug> --pr <number>
```

此命令将一个 PR 交给 `engine.py --worker`，等待本次 worker 结束。一次调用最多经历 20 次阶段跳转，可能多轮修复、推送，最后合并并删除分支；它不是“只生成审查报告”命令。它仍可能因状态缓存跳过已完成阶段。不要以退出码 0 单独判断成功，应核对该 PR 的状态、日志和 GitHub 结果。

`dry_run: true` 会抑制主要 GitHub 写入和通知，并跳过实际修复、解冲突与合并，但仍读取 GitHub、运行真实 CI、调用 Codex，并可能调用 TypeSafe。它不是离线预演，也不是阻止代码外发的开关。

## 工作流

| 阶段 | 行为及可见副作用 |
| --- | --- |
| 获取与筛选 | 读取 PR，检查 draft、编号白名单和可信作者，保存本地状态。 |
| 可选分类 | TypeSafe 接收 PR 内容，打标器创建或修改仓库标签并增删 PR 标签。 |
| CI | 在专用 worktree 中执行配置的 shell 命令，回写 `reviewpilot/ci` commit status。 |
| 审查与复核 | Codex 审查代码、逐条复核问题，发布评论和 `reviewpilot/review` commit status。 |
| 修复与解冲突 | Codex 修改文件，引擎 commit、push、评论，再进入 CI 和审查。 |
| 合并 | 在合并前校验已审 head，按配置合并并请求删除分支。 |
| 熔断 | 保存状态、发布评论并尝试通知，等待人工处理或新 head。 |

结论通过评论和 commit status 表达；当前没有提交 GitHub 正式 review approval 的步骤。流程没有专门部署节点，但 CI、push 或 merge 可能触发目标仓库的部署工作流。

常用配置：

| 字段 | 作用 |
| --- | --- |
| `repo` / `slug` | GitHub 标识与本地目录标识。 |
| `pr_allowlist` | 正整数列表；`[]` 拒绝全部，缺失表示不按编号限制。非法值拒绝运行；CLI 的 `--pr` 不绕过白名单。 |
| `trusted_authors` | 允许引擎处理的 GitHub 作者。 |
| `ci` | worktree 中顺序执行的 shell 命令。 |
| `review_model` / `review_effort` / `codex_timeout` | Codex 模型、推理力度与超时。 |
| `max_rounds` / `max_conflict_resolves` | 修复和自动解冲突的次数上限。 |
| `merge_method` | `squash`、`merge` 或 `rebase`。 |
| `protected_paths` | 触碰指定路径前缀时拒绝自动合并；不限制模型或 CI 对文件的访问。 |
| `diff_max_bytes` / `diff_exclude` | 审查 diff 的大小上限与过滤；不隔离 checkout 中的文件。 |
| `deep_gate.labels` / `deep_gate.effort` | 命中标签后提高当前 PR 的 Codex 推理力度。 |
| `dry_run` | 抑制部分写入，仍有本地执行、网络与 AI 调用。 |
| `max_parallel_prs` | 原始扫描入口派发后台 worker 的并发上限。 |

## 可选模块与平台前提

`labeler` 子命令运行分类扫描：它使用 `JEV_API_KEY` 请求 `https://api.typesafe.ai/v1/systemone`，可扫描开放 PR 和配置窗口内的已合并 PR，不限于上一条 `run` 命令指定的编号。保持 `labeler.enabled: false` 可停用该模块。

`observer` 子命令运行通知观察器：它读取配置仓库的 PR 与本地状态，并通过 OpenClaw 投递通知。保持 `notify.lifecycle_observer: false` 可停用观察器，**但不会关闭引擎自身的直接通知路径**。不要用虚假的通知目标代替停用；需要无通知的实际写入运行时，必须先实现明确的禁用机制。

引擎目前检查环境中是否存在 `CRS_KEY`，这只是遗留前置检查，不确定 Codex 的服务提供商或认证方式。实际模型接收方取决于现有 Codex 设置、认证及代理。不要为了通过检查而创建或复制凭据。

核心使用 POSIX 文件锁；macOS 的 Python 可以提供该接口，但端到端生产兼容性尚未验证。CI 工具链取决于目标仓库。`run.sh`、`labeler-run.sh`、`observer-run.sh` 只作为遗留兼容入口保留：部分脚本重设 PATH、依赖 GNU 工具或 Linux 路径，并可能从 shell 配置读取和 `eval` 密钥赋值。新使用者应使用 `reviewpilot` 入口，不使用这些脚本配置运行环境。

## 安装 Codex skill

将本仓库的 `skills/reviewpilot/` 目录复制到用户的 `${CODEX_HOME:-$HOME/.codex}/skills/reviewpilot/`，保留 `SKILL.md` 和 `agents/openai.yaml`。目标目录已有 skill 时先比较内容，避免直接覆盖。此步骤仅安装工作流说明，不安装程序、不配置认证，也不授予外发或写入权限。

调用示例：

> 使用 $reviewpilot 检查我选定的 ReviewPilot 安装和 PR 运行前提，先说明数据接收方和可能的写入。

skill 会使用用户选定的源码目录，读取该副本的 README、SECURITY 和配置。复制 skill 后仍须保留程序源码；它不嵌入引擎，也不声明不存在的平台插件接口。

## 状态、验证与发布

- `state/<slug>/pr-<number>.json`：阶段、head、审查结论和历史。
- `logs/`：引擎及模块日志；可能包含私人代码或输出。
- `notifications/`：通知队列与投递账本；删除可能丢失去重信息。
- `repos/`、`worktrees/`、`locks/`：本地运行数据。

以上目录、活动配置与认证材料均不得提交。不要从旧安装迁移状态来验证新的公开副本，也不要删除状态来强制重跑正在处理的 PR。

发布版附带隔离测试入口。审查测试文件后，从已安装依赖的源码目录运行：

```sh
.venv/bin/python -I -S -B tests/run_offline.py
```

它只复制明确列出的源码和测试到临时目录，清空继承环境，并用 Python 审计钩子阻断网络与子进程；不运行应用入口或测试发现。审计钩子是对已审查测试的补充防护，不是运行恶意 Python 或原生扩展的操作系统沙箱。静态检查、安装元数据检查和模拟测试不能证明真实 CI、AI、GitHub 合并或 macOS 生产运行可用。

`./pack.sh` 只通过 `git archive` 打包当前 HEAD 中已跟踪的文件，生成 `reviewpilot-v0.1.0.tar.gz`；未提交的修改不会进入归档。打包前仍须审查已跟踪文件，`.gitignore` 不会清除已经进入 Git 的秘密。
