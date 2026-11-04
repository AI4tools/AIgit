# aigit C 版

Python 参考实现在仓库根目录（`server.py` 等），这里是**同契约**的 C 重写。
目标不是「再写一个 git 前端」，而是：单文件静态 exe、无运行时依赖、JSON 逐字段与
Python 版相等，从而直接复用同一套黑盒判据。

## 许可

GPL-2.0（与 git 相同）。完整文本见仓库根目录 `LICENSE`。

## CI

`.github/workflows/ci.yml`（push/PR）：4 个 job —— 构建冒烟、16 片对拍、
质量（加固/Unicode/批量/演练）、压力（并发/长跑/畸形/变异）。
`.github/workflows/release.yml`：打 `v*` 标签 → 构建 + 打包 + `SHA256SUMS.txt`
+ 发布 Release。本地等价入口是 `c\tests\run-all-c.ps1`。

## 构建

```powershell
# 推荐：脚本会找 MinGW（可用 AIGIT_MINGW 覆盖），并可选跑判据
powershell -NoProfile -ExecutionPolicy Bypass -File c\build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File c\build.ps1 -Test

# 打包：c\dist\aigit-c\ 与 aigit-c.zip（exe + web + start.bat）
powershell -NoProfile -ExecutionPolicy Bypass -File c\package.ps1

# 手动
$env:PATH='C:\Environment\cpp\mingw64\bin;'+$env:PATH
mingw32-make -C c -j4          # 产出 c/build/aigit.exe（静态链接，单文件）
```

要求 MinGW-W64（UCRT）与 cmake 无关；makefile 只用了 `gcc` 与 `windres` 级别的工具。

## 运行

```powershell
c\build\aigit.exe --root D:\path\to\repo --port 8765
```

`--root` 必须是仓库工作区根目录。服务只读 + 少量写端点（M2 范围内），默认监听
`127.0.0.1`。

## 当前覆盖（2026-11-11：GET 21 组 + POST 27 个 + 5 个静态页，全部接上）

Python 版 `do_GET` 里的 21 组路由已经**全部**接上：

| 组 | 端点 |
|---|---|
| 基础 | `/api/health`、`/api/status`、`/api/branches`、`/api/log`、`/api/file`、`/api/diff` |
| 历史 | `/api/history/file`、`/api/history/blame`、`/api/history/tags`、`/api/history/reflog`、`/api/history/stashes`、`/api/history/stash`、`/api/history/plan/<op>` |
| 冲突/远程 | `/api/conflict/state`、`/api/conflict/list`、`/api/conflict/inspect`、`/api/remote/capability`、`/api/remote/list`、`/api/remote/heads`、`/api/remote/ahead-behind`、`/api/remote/plan/<op>` |
| 预演 | `/api/reset/plan`、`/api/discard/plan` |

写面（M2/M3）已经接上 27 个 POST 路由里的 **全部 27 个**：

| 组 | 端点 |
|---|---|
| 基础写 | `/api/stage`、`/api/unstage`、`/api/discard`、`/api/commit` |
| 分支/合并 | `/api/checkout`、`/api/branch`、`/api/merge`、`/api/merge-abort`、`/api/reset` |
| 冲突 | `/api/conflict/resolve`、`/api/conflict/choose`、`/api/conflict/delete`、`/api/conflict/commit`、`/api/conflict/abort` |
| 历史写 | `/api/history/amend`、`/api/history/undo-last-commit`、`/api/history/reflog/restore` |
| 引用写 | `/api/history/revert`、`/api/history/revert/abort`、`/api/history/branch/rename`、`/api/history/branch/delete`、`/api/history/tag/create`、`/api/history/tag/delete` |
| stash 写 | `/api/history/stash`、`/api/history/stash/apply`、`/api/history/stash/pop`、`/api/history/stash/drop` |
| 生成器 | `/api/gen`（mergeable / unrelated / adddel / conflict / all） |
| 远程写 | `/api/remote/fetch`、`/api/remote/pull`、`/api/remote/push` |

**这就是 Python `server.py` 的全部路由**（21 组 GET + 31 个 POST + 5 个静态页），
没有「v1 不做」的端点。`/api/gen` 的随机内容与 Python 不逐字相同（C 用
splitmix64，Python 用 Mersenne Twister），但分支名/文件名/键集/语义一致，同一
seed 在 C 内可复现；`count > 100` 是 C 加的 DoS 护栏（Python 无上限，1000 会卡
300 秒）。

静态资源按 Python 版的**白名单**发：`/`、`/index.html`、`/conflicts-demo.html`、
`/virtuallist.js`、`/history-panel.js`、`/conflicts-panel.js`；其余 404。
web 目录查找顺序：`--web <dir>` → exe 所在目录 `../../web` → 当前目录 `web`。

启动参数：`--root <repo> [--port <n>] [--sandbox] [--web <dir>] [--open] [--version]`。

- `--open`：监听成功后自动打开浏览器（打包的 `start.bat` 默认带上；
  `AIGIT_NO_BROWSER=1` 可压掉，给无人值守/测试用）。
- `--port` 被占用时**自动往后试 20 个**并打印真实端口 —— 双击启动不会因为
  8899 被别的程序占着就整个失败。
- `--version` 打印 `aigit 1.0.0`。

## 安全（上架前已加固）

| 面 | 现状 |
|---|---|
| 监听范围 | 只 `bind 127.0.0.1`，局域网/外网连不上 |
| 本机同源闸 | 校验 `Host`（必须 `127.0.0.1` / `localhost` / `[::1]`）与 `Origin`（必须同回环源）。挡 DNS rebinding 与网页 CSRF —— 否则任意网页都能用 `text/plain` 的 POST 直接触发 `discard` / `reset --hard`（`confirmed` 攻击者自己填）。没有 `Host` 头一律 403 |
| CORS | 不发送 `Access-Control-Allow-Origin`，浏览器读不到跨源响应 |
| 破坏性操作 | `discard` / `reset` / 删除 / 回退都要求 `confirmed:true`，且都有只读 plan 预演；`reset` 的 `mode` 没有默认值 |
| 路径 | 越界、`.git`、目录、从不存在的路径全部 400；批量接口整批失败，不做部分成功 |
| 响应头 | `X-Content-Type-Options: nosniff` |
| 静态资源 | 清单驱动（`web/.aigit-static`），URL 与文件名都校验，`..` 直接拒 |

**未做**：exe 没有代码签名（SmartScreen 会提示）；没有自动更新。
`--sandbox` 与 Python 同义：清空 root、`git init -b main`、写 README 并提交
`init: sandbox`；非仓 `--root` 也会自动引导（仓级身份 `aigit`，无环境身份也能提交）。

## 判据

```powershell
python -X utf8 c\tests\run_conformance.py          # 16 片
python -X utf8 c\tests\concurrency_c.py 12 25     # 并发冒烟，300 请求
python -X utf8 c\tests\soak_c.py 8 300            # 长跑，2,400 请求（看句柄/工作集）
python -X utf8 c\tests\fuzz_http_c.py 200         # 畸形请求，服务不能死
python -X utf8 c\tests\guard_adv_c.py            # 仓库配置里的 filter/diff 不能被执行
python -X utf8 c\tests\unicode_int_c.py          # Unicode 数字参数与 Python 同结果
python -X utf8 c\tests\unicode_name_c.py         # \w / 空白 / repr 文案与 Python 同结果
python -X utf8 c\tests\bigint_c.py               # 超 2^126 的回显与 Python 同结果
python -X utf8 c\tests\scenario_session.py       # 端到端演练：5 个节点 + 3 种回退
python -X utf8 c\tests\scenario_conflict.py      # 端到端演练：合并冲突 + abort/choose
python -X utf8 c\tests\scenario_reset.py         # 端到端演练：diff 审错后的 unstage/discard/reset
python -X utf8 c\tests\scenario_batch.py         # 端到端演练：300/1000 文件的批量审核与批量重置
python -X utf8 c\tests\batch_diff_c.py           # 批量 diff 端点（与逐文件结果逐字节一致）
python -X utf8 c\tests\launch_guard_c.py         # 上架加固：Host/Origin 闸、端口顺延、--version
python -X utf8 c\tests\mutation_c.py             # 3 个变异靶，必须全部 KILLED
```

长跑实测：2,400 请求 / 84.6s，0 异常，句柄 63→66（采样 63–91），工作集
4.4→5.2MB（峰值 5.8MB）。畸形请求实测：200 条（坏请求行、负/超大
Content-Length、chunked、70KB 请求行、2000 个重复头、NUL、截断 body），
0 连接异常，之后 `/api/health` 仍 200。chunked POST 实测能把 body 解开并落到
`/api/stage`；慢速半包连接 10.0s 后被服务端关闭。

**端到端演练**（`c/tests/scenario_session.py`，52 项断言，全部实测）：
造一个真项目 `calc`（stdlib Python + 真 unittest），只用 HTTP API 操作，走 5 个节点：
`N0 骨架 → N1 加 div → N2 CLI/README → N3「简化」div（// 而非 /，引入回归）→
N4 revert N3`。每一步改文件后取 `/api/diff` 的 diff 块（断言有 `@@` hunk、
新增行、暂存前后一致），提交后用**项目自己的 unittest** 验收。实测：
N3 后 `Ran 5 tests ... FAILED (failures=1)`，revert 后 `OK`。

回退三种都验了：
- `revert`：历史只追加（`log` 顶部是 Revert 提交，N3 仍在），测试复绿；
- `discard`（先 `discard/plan` 看清要丢什么）：未提交的坏改动被丢弃，文件与 N4 逐字节相同；
- `reflog restore --hard`：HEAD 回到 N2、文件内容等于 N2 版本、reflog 追加一条
  （6→7）、被抛下的 N4 仍可 `cat-file -e` 到，再用 reflog 退回 N4，测试复绿；
- 另有 `stash` 往返：暂存后工作区干净、`pop` 后 WIP 内容逐字节回来。

**端到端演练 4**（`c/tests/scenario_batch.py`，15 项断言）：批量审核 / 批量重置。
300 个改动 + 100 个未跟踪、一次 1,000 个文件，全部逐字节校验（sha256）。实测（3 轮 p50）：

| 操作 | 耗时 | 说明 |
|---|---:|---|
| `stage` 300 个（审核入队） | 229.1ms | 走 `--pathspec-from-file=-`，**没有命令行长度上限**；首次调用 2.5s（冷） |
| `unstage` 300 个 | 188.9ms | |
| `discard` 300 个 | 316.7ms | 300/300 逐字节回到提交内容，未跟踪文件不动 |
| `reset --hard`（300 脏文件） | 350.8ms | 一次干净，300/300 逐字节回滚 |
| `stage` / `discard` 1,000 个 | 495.4 / 895.7ms | 同上，逐字节 1000/1000 |
| **单文件 diff** | 42.6ms | 300 个 ≈ 12.8s（逐文件）；见下面的批量端点，143ms |

边界（都与 Python 对拍过）：
- 重复路径不报错（git 去重）；不存在的路径被**静默忽略**（200）；
- 批里夹带 `.git/config` → 400「不允许操作 .git」；夹带 `../outside.txt` → 400「路径越界」；
- **`discard` 对已暂存文件是 no-op**（Python 也是 `git checkout -- path`，从索引恢复）——
  批量重置已暂存内容要 `unstage` → `discard`，或用 `reset --hard`。这条已写成断言钉住。

**批量审核的慢点已解**（C 的超集）：`POST /api/diff {"files":[…],"staged":bool}`
一次返回多文件的 hunk。实现是 2 次 spawn（`-z --name-only` 拿精确路径顺序 +
`-p` 拿补丁），按顺序一一配对；条数不一致时退回逐文件（正确优先）。
实测 **300 个文件 142.8ms**（逐个 42.6ms × 300 ≈ 13.4s，快 **94×**），
每个文件的 `diff` 与单文件 `GET /api/diff` **逐字节相同**（含空格名、中文名、
子目录、二进制、改名）。护栏与单文件版一致（目录 / `.git` / 越界 / 从不存在的
路径 → 400，整批失败）。`--no-renames` 保证「一个路径一块」。

**端到端演练 3**（`c/tests/scenario_reset.py`，20 项断言）：diff 审错后的重置阶梯。
- 已 stage → `POST /api/unstage`（±94.7ms）撤回审核队列，`staged diff` 变空；
- 未提交 → `GET /api/discard/plan?files=…` 预演 → `POST /api/discard {confirmed:true}`
  （±101.0ms），文件逐字节回到提交内容；不带 `confirmed` 一律 400 `needs_confirmation`；
- 已提交 → `GET /api/reset/plan?mode=…&ref=…` 预演 → `POST /api/reset {mode, ref,
  confirmed:true}`（`--hard` ±143.8ms）；**缺 `mode` 或 `confirmed` 都 400**，绝不替用户猜；
- 想先留改动再决定 → `--mixed`（改动落在 unstaged）然后再 `discard`；
- 不想改历史 → `revert`（§12.25 测过 160.9ms）或 `reflog restore`。
产品里**没有**独立的 review 状态机（`doc/aigit-review.md` 是设计评审文档），
「审核」就是 staged 这一层；重置就是把改动往下/往上退一级。

**端到端演练 2**（`c/tests/scenario_conflict.py`，23 项断言）：两侧改同一个文件 →
`merge` 明确报冲突（不是假 `ok`）→ `conflict/state`（`inMerge`）与
`conflict/list` 都对 → `merge-abort` 让 HEAD / 文件内容 / 工作区三方逐字节复原 →
再合并一次用 `conflict/abort` 回退 → 第三次 `conflict/choose` 选 theirs +
`conflict/commit`，合并提交确有 2 个父提交、工作区干净。

**大仓对比**（5,000 文件、每文件 40 行，15 次 p50，C/Python）：
`status` **0.85×**、`branches` **0.56×**、`log` **0.95×**、`file` **0.78×**、
`diff` **0.73×**、`history/tags` **0.73×** —— C 在所有测项上不慢于 Python。

**静态分析**：`gcc -fanalyzer` 全量跑过，9 条告警里修掉 6 条（`strip_dup(NULL)`
空指针、bind/listen 失败未关 socket）；剩 3 条是 SOCKET 无符号哨兵导致的误报。
ASan 本机不可用（MinGW-W64 未提供 `libasan`），这是仍未补的一环。

**变异基线**：3 个靶（status 路径按空格切分 / 放行 `.git` / commit 丢 `output`）
全部 **KILLED**；未变异基线 `conformance_basic` + `conformance_m0` 均 PASS。
写这组时确实发现过一个判据缺口（`status` 的未跟踪空格文件名），已补 fixture。

一条命令跑全部门禁：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File c\tests\run-all-c.ps1
# -Fast 只跑构建 + 16 片 + guard，跳过并发/长跑/畸形/变异
```

静态页白名单在 `web/.aigit-static`（`URL <TAB> Content-Type <TAB> 文件名`），
加静态资源不用改 C；清单缺失时回退内置表。

每片都是「同一仓、同一时刻，起 Python 参考服务与 C 服务，逐端点比较
HTTP 码 + JSON 深比较」（读面分片还比 `X-aigit-git-calls`/`cmds`）。分片：

- `conformance_m0.py` —— health/status/404/405/CRLF（9 项）
- `conformance_basic.py` —— branches/log/file/diff（98 项）
- `conformance_histfile.py` —— history/file、history/blame（96 项）
- `conformance_histlist.py` —— history/tags、reflog、stashes（35 项）
- `conformance_conflict.py` —— conflict/*、remote/{capability,list,ahead-behind}（107 项）
- `conformance_plan.py` —— reset/plan、discard/plan（92 项）
- `conformance_histplan.py` —— history/stash、history/plan/<op>（185 项）
- `conformance_remoteplan.py` —— remote/heads、remote/plan/<op>（1,820 项，含 5 档能力闸）
- `conformance_write_basic.py` —— stage/unstage/discard/commit（122 项，含与真 git 的对照）
- `conformance_write_more.py` —— checkout/branch/merge/merge-abort/reset（93 个 POST 步骤 + 28 个最终状态）
- `conformance_write_conflict.py` —— conflict/{resolve,choose,delete,commit,abort}（221 个 POST 步骤 + 44 个最终状态）
- `conformance_write_history.py` —— amend/undo-last-commit/reflog restore（93 次 POST + 32 个最终状态）
- `conformance_write_refs.py` —— revert/revert-abort/branch rename|delete/tag create|delete（154 步 + 466 条 fuzz）
- `conformance_write_stash.py` —— stash/stash apply|pop|drop（103 次 POST + 32 个最终状态）
- `conformance_write_remote.py` —— remote fetch/pull/push（127 例，含 6 档能力闸、本地 bare remote）
- `conformance_gen.py` —— `/api/gen`（262 项：语义复核、确定性、count、护栏、键集）

写面退出判据是 12 个「与真 git 同操作」的场景（`cmp_write_vs_git_c.py`，在
`D:\aigit_mut_tmp`，不在仓库内）：

```powershell
$env:AIGIT_SERVER_CMD='D:\project\aigit\c\build\aigit.exe'
python -X utf8 D:\aigit_mut_tmp\cmp_write_vs_git_c.py
# 失败 0 / 12 场景（stage/unstage/commit/discard/checkout/commit_crlf/merge_ff/
# conflict_resolve/commit_large/commit_binary/commit_submodule/commit_mode_bit）
```

性能预算 28 项也用同一套阈值跑 C（`perf_budget_c.py`）：spawn 预算 **28/28**，
ms 预算 27–28/28（`stage` 擦线），明细见 `doc/aigit-perf.md` §12.24。

## 现成 Python 套件对 C 的结果（独立判据，不在本仓内提交）

把仓库根目录的现成套件里「起 `server.py` 子进程」那一行换成 C exe（克隆到
`D:\aigit_mut_tmp\py_suite_*`，不改本仓测试），实测：

| 套件 | 结果 | 说明 |
|---|---|---|
| `test_aigit_contract.py` | **18/18 PASS** | 8 个端点的响应形状（键集写死，非从源码推导） |
| `test_aigit_extra.py` | **20 PASS / 0 FAIL / 1 SKIP** | SKIP 是本机造不出文件符号链接（WinError 1314） |
| `test_aigit_gate.py` | **10/10 PASS** | reset/discard 的确认闸与两种 plan 预演 |
| `test_aigit_web.py` | **43 PASS / 0 FAIL / 1 WARN**（109 条） | 静态层 + 前端接线；WARN 是「服务端原样回显 path」，Python 版同样存在 |
| `test_aigit.py` | **48 PASS / 8 FAIL / 3 ERROR / 1 SKIP** | 8 FAIL 全是 `/api/gen`（v1 不做）；3 ERROR 是测试进程自己 `import server`（C 没有这个模块） |

这些套件跑出来的真缺陷已修：端口占用退出码、并发 `index.lock`（锁升级为全请求）、
带空格路径被截断（porcelain v2 路径字段取到记录末尾）、静态页缺失、
`cat-file --batch` 对带空格对象名回 `<原样输入> missing` 时永久阻塞。

读面退出判据是 275 项「与 git 直接比对」的差分（`cmp_vs_git_c.py`，在
`D:\aigit_mut_tmp`，不在仓库内）：

```powershell
$env:AIGIT_SERVER_CMD='D:\project\aigit\c\build\aigit.exe'
python -X utf8 D:\aigit_mut_tmp\cmp_vs_git_c.py
# 合计 275 项检查，失败 0（symlink 场景因本机无符号链接权限 SKIP）
```

## 与 Python 版仍存在的已知差异

只列会影响结果的；都是边角：

1. 整数参数与 Python 同语义：共享的 `py_int128_parse` 带 Unicode Nd 表
   （62 段 / 660 码点）与 Unicode 空白；`limit=٥`、全角 `５`、`۱_۲` 实测同码同结果。
2. **超过 2^126 / int64 的数**：内部运算仍饱和（`__int128` / `int64`），但**所有
   回显都精确** —— `requested_first`、`没有第 N 个 stash`、`end_line(...)`、
   `?index=` 都走 `py_int_canon`（原文本的规范十进制），JSON body 的整数字面量
   在解析时保留（`jint_lit`）。实测 88 例（8 个超长值 × 5 个 GET 端点 +
   3 个 POST 端点 × 13 个 body）与 Python 逐字节一致。
   剩下的只有内部比较，而比较对象都 < 2^126，行为等价。
3. 工作区文件读失败（被独占/无权限）时，Python 抛 500，C 按「不许 500」返回空内容。
4. 请求头/参数上限：C 的 query 值超过 64 KiB 会回 400（Python 无此上限）；
   请求体在响应前会先读走（避免 RST）。C **支持 `Transfer-Encoding: chunked`**
   （Python 的 `http.server` 不支持，这是有意超集）；慢速/半包连接 10s 超时后关闭。
   没有 keep-alive（响应后关闭，与 Python 一致）。
5. Unicode 细节**已对齐**：`\w` 用 733 段区间表（与 `str.isalnum()` 逐码点一致，
   133,548 个码点）、`isspace` 用 29 码点集合、`str.strip()` / `repr()` 用共享实现
   （不可打印字符按 `\xNN` / `\uNNNN` / `\UNNNNNNNN` 转义）。
   此前 C 用 Windows `CT_CTYPE1` 判 `\w`，**漏认 85,535 个码点**（CJK 分支名会被
   误拒），已修。剩余：git 输出里的非法 UTF-8，Python 转 U+FFFD，C 原样透传。
6. 符号链接语义未验证：本机建链接需要开发者模式/管理员权限（WinError 1314）。
7. 配置中和已与 Python 对齐：按真实 gitdir（含 worktree 的 `gitdir:` 文件）读
   `.git/config` / `config.worktree` / 用户配置（含 `include`/`includeIf`）枚举
   filter/diff/merge/alias 子段并逐个置空；每次 git 调用前用配置指纹
   （mtime+size）检测变化并**重新枚举**（运行中改配置也绕不过）；
   `diff/show/log/whatchanged/format-patch` 在子命令后追加
   `--no-ext-diff --no-textconv`；远程动词（fetch/pull/push/ls-remote 等）保留
   用户的 `GIT_CONFIG_GLOBAL` 与 `credential.helper/core.sshCommand`，本地动词仍隔离。
   实测：直接配置、include、includeIf、转义子段名、worktree 公共配置、
   运行中追加 filter，六种情况都没有执行标记命令。
8. 性能预算：spawn 预算 **28/28**；ms 预算 **27–28/28**。`POST /api/stage`
   在 164–180ms 之间抖动（预算 170），**同机 Python 参考版同样会 179.9ms 超线** ——
   这是环境/磁盘敏感项，不能宣称稳定全过；其余 27 项有余量。
9. Python 在文件系统异常（unlink/rmdir/listdir 失败）或 body 非法 UTF-8 时可能抛
   500；C 按「不许 500」一律回 400。未构造稳定 fixture。
10. commit 失败文案 Python 按**字符**截 400，C 按**字节**截（非 ASCII git 输出时可见）。
11. 仓锁：进程内用一把全局 CRITICAL_SECTION 把**每个请求**串行化（Python 是
    per-repo RLock、锁在方法粒度）；另外 C 会在 `%TEMP%\aigit-lock-<hash>.lock`
    （按真实 gitdir 哈希）独占打开锁文件，**第二个 aigit 进程服务同一仓会明确
    拒绝**（等 2s 后退出 1）。锁不放在 `.git` 里，所以运行中重命名/删除 `.git`
    不会被占用拒绝。这比 Python 多了一道跨进程保护；worktree 解析不到 gitdir 时
    跳过该锁。
    实测并发 12 请求、8 次并发 commit、2 个进程抢同一仓均无 index.lock 泄漏。
12. 仓内「符号链接→目录」C 一律跳过（Python 当文件链接收下）；目录联接已验证一致，
    真 symlink 因本机权限（WinError 1314）未验证。
13. `--root` 指向**不存在/非仓库目录**时，C 与 Python 一样会自动 `git init -b main`
    + 仓级身份 + README + `init: sandbox` 提交（`--sandbox` 额外先清空目录）。
    已有仓不受影响。
14. 历史写（amend/revert/branch/tag/stash）的 spawn 数可能比 Python 多：Python 有
    refs/reflog 文件快路径，C 走 git 命令；响应与副作用已逐项对齐。
15. `/api/gen` 的**随机内容**不逐字相同（C 用 splitmix64，Python 用 Mersenne
    Twister）：文件正文、行号、base/提交 sha 会不同；分支名、文件名、`files`、
    `id`/`expect`/`verified`、键集与语义一致。同一 seed 在 C 内可复现。
16. `/api/gen` 的 `count > 100` C 回 400（DoS 护栏）；Python 无上限，实测
    `count=1000` 会卡 300 秒不返回。1–100 与 Python 一致。
17. **CI / 代码签名**：本机没有 CI runner、也没有签名证书；提供的是本地一键门禁
    `c\tests\run-all-c.ps1`（已实测），exe 未签名。这两项需要外部环境/证书。
18. **ASan 不可用**：MinGW-W64 不带 `libasan`（`-lasan` 不存在）；已用
    `gcc -fanalyzer` 全量替代并把告警修到只剩 SOCKET 误报。真符号链接仍受
    WinError 1314 限制（需开发者模式/管理员），目录联接与硬链接已覆盖。

## 工程债（仍未完全消掉的部分）

- **helper 副本已清空**（2026-11-11）：`c/src` 里不再有 `strip` / `repr` / `\w` /
  UTF-8 解码 / 整数解析的本地实现，全部收进 `params.c`：
  `q_get_n` / `q_get_nonempty`（query）、`py_int128_parse` / `py_int_parse` /
  `py_int_canon`（整数）、`py_utf8_one` / `py_utf8_step`（解码）、
  `py_is_space_cp` / `py_is_ascii_space` / `py_is_word` / `py_is_printable`、
  `py_strip` / `py_strip_dup` / `py_strip_dup_n`、`py_repr_str` / `py_repr_n`、
  `py_has_space_or_ctrl`；路径规约在 `paths.c::path_canon_rel`。
  每个都做过全码点 / 随机对拍（`\w` 133,548 码点、strip 4,087 例、
  repr 4,125 例，0 不一致）。
- **全局请求锁只覆盖碰 git 的路由**（2026-11-11 改）：`serve_static` 与
  `/api/health` 不进锁（它们只读启动时确定的 `root` 与内存里的白名单）。
  改前实测：16 路并发 `git status` 期间，`/index.html` 的 p50 是 **718.4ms**
  （3 秒只完成 5 次），单发 0.5ms —— 短请求被长请求队头阻塞。
  改后同一场景 p50 **1.1ms**、p95 11.1ms、3 秒完成 **540** 次；静态页吞吐
  4.1–5.2k req/s、`/api/health` 5.5k req/s。
  仍有取舍：**碰 git 的路由之间**依然整体串行（Python 是方法级 RLock），
  因为 `git status` 会写索引扩展，并发读 + 写一样会撞 `index.lock`。
- **CI / 签名**：没有可用 CI runner 与签名证书；替代物是本地 `run-all-c.ps1`。
- **ASan**：MinGW-W64 无 `libasan`；已用 `-fanalyzer` 替代。
- **真符号链接**：本机 `WinError 1314`，无法构造；目录联接/硬链接已覆盖。

## 目录

```
c/src/
  main.c       参数解析 / 启动
  http.c       WinSock2 HTTP + 路由分派
  json.c       JSON 渲染与转义
  util.c       buf / UTF-8 转换 / 字符串工具
  git.c        git 调用 + 环境隔离 + 语义配置透传 + 调用计数
  params.c     query 解析（percent-decode，NUL 可见）
  paths.c      请求路径 → 仓内相对路径（安全关键）
  status.c     porcelain v2 解析与 JSON
  catfile.c    常驻 git cat-file --batch（不计数，带状态查询）
  bodyjson.c   请求体 JSON 解析（Python json.loads 口径）
  writeutil.c  写端点共用：body 取值 / 统一成功响应（含 status opt-out、E3 兜底）
  indexfile.c  `.git/index` 直读（tracked 集合、未合并条目判定）
  read_basic.c       branches/log/file/diff
  read_history.c     history/file、history/blame
  read_history_list.c history/tags、reflog、stashes（直读 .git）
  read_conflict.c    conflict/*、remote/{capability,list,ahead-behind}
  read_plan.c        reset/plan、discard/plan
  read_histplan.c    history/stash、history/plan/<op>
  read_remoteplan.c  remote/heads、remote/plan/<op>
  write_basic.c      stage/unstage/discard/commit
  write_more.c       checkout/branch/merge/merge-abort/reset
  write_conflict.c   conflict/{resolve,choose,delete,commit,abort}
  write_history.c    amend/undo-last-commit/reflog restore
  write_refs.c       revert/revert-abort/branch rename|delete/tag create|delete
  write_stash.c      stash/stash apply|pop|drop
  write_remote.c     remote/fetch|pull|push
  gen.c              /api/gen 场景生成器
  guardcfg.c         按配置枚举 filter/diff/merge/alias（guard 对位）
  routes.h     各模块的分派约定
```
