# aigit 交付说明（一页版）

> 这份是**交付视图**：怎么跑、现在什么水平、验过什么、没验什么。
> 性能细节与逐轮账在 `doc/aigit-perf.md`；冲突面板契约在 `doc/aigit-conflicts.md`。

## 1. 它是什么

本机 git 前端：一个标准库 HTTP 服务 + 三个网页标签页（改动 / 合并 / 提交历史）。
零第三方依赖，不开窗口，只 bind `127.0.0.1`。

## 2. 怎么验（一条命令）

```powershell
# 日常：编码门禁 + 20 个套件（约 10 分钟）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run-all.ps1 -Fast

# 完整：再加并发套件 + 性能预算（约 15 分钟）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run-all.ps1

# 发布前：再加 50 靶变异基线（约 35 分钟）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run-all.ps1 -WithMutation
```

退出码 0 = 全绿，1 = 有红项（会打印失败项与日志目录）。
**变异基线在一次性快照仓里跑**：`--base-ref HEAD` 是 `git archive HEAD`，未提交改动
不在里面，直接在工作区跑会静默测到旧代码。

单独跑某一项也可以：

```powershell
python -X utf8 python-ref\test_aigit.py                    # 60 条主套件
python -X utf8 python-ref\test_aigit_concurrency.py        # 并发 15 组（含注入自检）
python -X utf8 python-ref\tools\perf-budget.py             # 22 操作性能预算
python -X utf8 mutation_runner.py --all         # 变异（需快照仓）
```

## 3. 性能现状（本机，22 操作）

| 层 | 现状 | 预算（门禁硬指标） |
|---|---|---|
| 读（13 项） | 0.6–79.5ms，合计 **4 spawn**；7 项已 **0 spawn** | ≤1 spawn/项，≤100ms |
| 轻写（默认契约） | 99–136ms，2 spawn | ≤2 spawn，≤170ms |
| 轻写（`status:false`） | **59–97ms，1 spawn** | ≤1 spawn，≤140ms |
| discard | 103ms，2–3 spawn | ≤3 spawn（例外），≤170ms |
| commit（默认） | 515–528ms，8 spawn | ≤8 spawn，≤650ms |
| commit（`status:false`） | **406ms，7 spawn** | ≤7 spawn，≤600ms |

口径说明：**spawn 是硬指标**（确定性），墙钟只是带宽（同代码两轮之间单项抖动 ±35ms）。
分层判据与地板论证见 `doc/aigit-perf.md` §8.1 / §12.10。

## 4. 验过什么（都是实测，不是声称）

| 面 | 证据 |
|---|---|
| 与真 git 一致（读） | 13 种仓 × 23 项 × 快/回退两遍 = **275 项 0 失败** |
| 与真 git 一致（写） | 12 个场景（stage/unstage/commit/discard/checkout/merge/冲突/CRLF/大文件/二进制/子模块/权限位），**同一次提交的 sha 逐字相同** |
| 回归套件 | 20 个自跑套件 + 并发 15 组，全绿 |
| 变异覆盖 | **50/50 KILLED、0 VOID** |
| 安全面 | F-06（仓外链接）9 条；NUL 路径走私、消息注入、越界路径等边界 119 例 |

## 5. 已知差异与未覆盖（明确登记，不藏）

1. **合并提交的 trailer**：自带消息时会补 `Merged: <分支>`（文档化契约，
   见 `doc/aigit-conflicts.md:147`）。因此冲突提交的 sha 与 plain git 不同 —— 这是
   **有意差异**，差分测试显式钉住它。
2. **符号链接的磁盘语义**：本机建不了 symlink（`WinError 1314`，需开发者模式）。
   已验的是「symlink 以对象形式在 HEAD 里」的读路径；`core.symlinks` 的
   存 blob / 存 symlink 选择**未验**。
3. **文件权限位**：只验了索引层的 mode 变化（`update-index --chmod`），
   Windows 上 `core.fileMode` 的实际语义未验。
4. **子模块写操作**：只验了「提交子模块指针」；`submodule update` 等未接入。
5. **平台**：Windows 专用（`mklink`、`WinError`、句柄语义）。非 Windows 未验。
6. **`status:false` 是 opt-in 语义**：请求体显式给 `"status": false` 时，写响应
   省略刷新后的状态（UI 随后自己 `refresh()`）。缺省行为不变，老客户端不受影响。
7. **P4 常驻进程**：默认空闲 60s 回收；回收前删仓会 `WinError 32`。
   可用 `AIGIT_CATFILE_IDLE=0` 关掉常驻（回到每请求一次 spawn）。

## 6. C 版（`c/build/aigit.exe`）

同一套 HTTP 契约的 C 重写：单个静态 exe、无运行时依赖。和 Python 版的关系是
**对等实现**，不是替代分支——两边的 JSON 响应、副作用、状态码都按同一套判据对齐。

### 构建与运行

```powershell
$env:PATH='C:\Environment\cpp\mingw64\bin;'+$env:PATH
mingw32-make -C c -j4

c\build\aigit.exe --root D:\path\to\repo --port 8899
# 可选：--sandbox（清空 root 后建演练仓）、--web <dir>（静态页目录）
```

`--sandbox` 与非仓 `--root` 的自动 init 与 Python 的 `main()` 同义：不是仓就
`git init` + README + `init: sandbox` 提交；没有提交身份时也能启动（只跳过提交）。

### 覆盖

- GET：21 组路由（health/status/branches/log/file/diff、history/*、conflict/*、
  remote 读面、reset/discard plan）
- POST：27 个写路由（stage/unstage/discard/commit、checkout/branch/merge/
  merge-abort/reset、conflict/*、history 的 amend/revert/stash/branch/tag/reflog）
- 静态：`/`、`/index.html`、`/conflicts-demo.html` 与三个 js（白名单，其余 404）
- 远程写：`/api/remote/{fetch,pull,push}`（NetGate 默认 off）
- 生成器：`/api/gen`（mergeable/unrelated/adddel/conflict/all）
- 与 Python 版路由**全部对齐**，没有 v1 不做的端点

### 怎么验

```powershell
# 一条命令的本地门禁（构建 + 16 片 + guard + 并发/长跑/畸形 + 变异）
powershell -NoProfile -ExecutionPolicy Bypass -File c\tests\run-all-c.ps1

python -X utf8 c\tests\run_conformance.py        # 16 片，逐端点 C vs Python
python -X utf8 c\tests\concurrency_c.py 12 25    # 并发 300 请求
python -X utf8 c\tests\soak_c.py 8 300           # 长跑 2,400 请求（句柄/工作集）
python -X utf8 c\tests\fuzz_http_c.py 200        # 畸形请求
python -X utf8 c\tests\guard_adv_c.py            # 仓库配置里的 filter/diff 不能被执行
python -X utf8 c\tests\unicode_name_c.py         # \w / 空白 / repr 文案与 Python 同结果
python -X utf8 c\tests\unicode_int_c.py          # Unicode 数字参数与 Python 同结果
python -X utf8 c\tests\bigint_c.py               # 超 2^126 的整数回显与 Python 同结果
python -X utf8 c\tests\scenario_session.py       # 端到端：5 个节点 + 3 种回退（真 unittest）
python -X utf8 c\tests\scenario_conflict.py      # 端到端：合并冲突 + 回退 + 解决提交
python -X utf8 c\tests\mutation_c.py             # 3 个变异靶，必须全部 KILLED
```

读/写差分（不在仓库内，`D:\aigit_mut_tmp`）：

```powershell
$env:AIGIT_SERVER_CMD='D:\project\aigit\c\build\aigit.exe'
python -X utf8 D:\aigit_mut_tmp\cmp_vs_git_c.py         # 275 项读面 vs git
python -X utf8 D:\aigit_mut_tmp\cmp_write_vs_git_c.py   # 12 场景写面 vs git
```

实测：16 片全过、275/275、12/12、并发 0 不一致、长跑 2,400 请求 0 异常、
200 条畸形请求 0 崩溃；性能预算 spawn 28/28、ms 连续 7 次中 6 次全过。
现成 Python 套件指向 C：contract 18/18、extra 20/0、gate 10/10、web 43/0、
main 48 PASS（余 8 全是 `/api/gen`）。

### C 版边界（详见 `c/README.md`）

`/api/gen` 与 remote 写不做；非 ASCII 数字、超 int64 饱和、Unicode `strip`/`\w`
是边角差异；真符号链接本机无法构造（WinError 1314）；接真 https/ssh 远程时
guard 会屏蔽用户凭据。写面用一把全局锁按请求串行（Python 是 per-repo RLock）。

## 7. 文档索引

| 文件 | 内容 |
|---|---|
| `doc/aigit-perf.md` | 性能路线全账：成本模型、逐轮优化、判据、边界巡检、差分矩阵 |
| `doc/aigit-conflicts.md` | 冲突面板的接口与 `Merged:` trailer 契约 |
| `doc/aigit-gitguard.md` | 配置中和（哪些能执行、哪些被屏蔽） |
| `doc/aigit-capabilities.md` | 能力清单与远程闸 |
| `tools/run-all.ps1` | 全量门禁 |
| `tools/perf-budget.py` | 性能预算判定 |
| `tools/check-encoding.ps1` | 编码门禁（BOM / UTF-16 / 行尾） |
| `c/README.md` | C 版：构建/覆盖/判据/已知差异 |
| `c/tests/run_conformance.py` | C 版的 16 片一致性判据 |
| `c/tests/mutation_c.py` | 3 个变异靶（改坏源码后判据必须红） |
| `c/tests/unicode_name_c.py` | `\w` / Unicode 空白 / `repr` 文案 |
| `c/tests/unicode_int_c.py` | Unicode 数字参数（Nd 表） |
| `c/tests/bigint_c.py` | 超 2^126 / int64 的回显 |
| `c/tests/scenario_session.py` | 端到端演练：真项目 5 个节点 + revert/discard/reflog-restore/stash |
| `c/tests/scenario_conflict.py` | 端到端演练：合并冲突 + merge-abort/conflict-abort/choose/commit |
| `c/tests/scenario_reset.py` | 端到端演练：审错后的 unstage / discard / reset（含护栏） |
| `c/tests/scenario_batch.py` | 端到端演练：300/1000 文件的批量审核与批量重置 |
| `c/tests/guard_adv_c.py` | 仓库配置执行面（filter/diff/textconv） |
