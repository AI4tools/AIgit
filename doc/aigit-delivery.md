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
python -X utf8 test_aigit.py                    # 60 条主套件
python -X utf8 test_aigit_concurrency.py        # 并发 15 组（含注入自检）
python -X utf8 tools\perf-budget.py             # 22 操作性能预算
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

## 6. 文档索引

| 文件 | 内容 |
|---|---|
| `doc/aigit-perf.md` | 性能路线全账：成本模型、逐轮优化、判据、边界巡检、差分矩阵 |
| `doc/aigit-conflicts.md` | 冲突面板的接口与 `Merged:` trailer 契约 |
| `doc/aigit-gitguard.md` | 配置中和（哪些能执行、哪些被屏蔽） |
| `doc/aigit-capabilities.md` | 能力清单与远程闸 |
| `tools/run-all.ps1` | 全量门禁 |
| `tools/perf-budget.py` | 性能预算判定 |
| `tools/check-encoding.ps1` | 编码门禁（BOM / UTF-16 / 行尾） |
