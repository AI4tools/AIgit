# aigit 性能优化路线 P0–P5（正式版）

> 状态：**路线正本**。P0 载体已落、首份 quick 实测已出；
> **P1 计数器已在代码里但只罩 23/91 处调用点（§12.2 活证据）**；
> 端点解剖已出第一批真账（§12），P2 的肉量已由实测重划（§12.4）。
> 口径：`[实测]` = 有命令、有退出码、可复现（附产出路径或出处）；
> `[预估]` = 由实测外推、**尚未验证**；`[待测]` = 连外推都没有，前面阶段出数才能定。
> 量尺一句话：**Windows 上每次 git 子进程 ~40ms 地板价，砍一次 spawn ≈ 省 40ms。**
> 倍数神话不进本文。

---

## 0. 文档关系与工作区锚定

### 0.1 与同日并行稿的关系

2026-10-21 有并行线同时落了初稿 `doc/aigit-perf-roadmap-20261021.md`（276 行），
并实现了 P0 载体 `bench_aigit.py`（23,845 B）且跑出了首份 quick 实测
（`D:\aigit_mut_tmp\bench_quick.json`，6,265 B）。

**本文吸收并行稿全部实测项与两处自我审查更正，作为正本。约定：**

- 两个文件**内容职责不重复使用**：路线的后续更新一律只写本文；
  20261021 初稿按历史快照**保留不删**，不再向它写新内容；
- `bench_aigit.py` 及其产出 JSON 是两份文档共同引用的唯一测量载体。

### 0.2 工作区状态（2026-10-21 快照，[实测]）

`git status --porcelain` 共 **23 条 = 已跟踪改动 13（含 1 处删除 `server_base.py`）+ 未跟踪 9**，
其中与性能直接相关的未跟踪项：`.gitattributes`、`bench_aigit.py`、
`doc/aigit-capabilities.md`、`doc/aigit-perf-roadmap-20261021.md`、
`doc/aigit-tests-20261006.md`、`find_test_gaps.py`、`test_aigit_contract.py`、`tools/`。
**本路线在脏工作区上设计，不假设任何"干净基线"；提交与否由用户决定，本文不依赖提交动作。**

规模锚点：当前 `server.py` = 122,013 B；A5 报告撰写时快照为 25,384 B，
**差 4.8 倍** —— 这就是"引用旧报告之前必须重读代码"的量化理由。

---

## 1. 已知条件

### 1.1 成本结构（[实测]，引自 A5 / E13 两份在盘报告）

| 项 | 数字 | 出处 |
|---|---|---|
| 裸 `git --version`（什么都不干） | **39.71ms** median（36.30–44.10） | A5 §2.2 |
| `rev-parse --abbrev-ref` 全程 | **42.42ms** median（40.17–54.35） | 同上 |
| 老 `status()` = 2 次子进程 | **96.0ms**（v1 扫描 51.6 + `symbolic-ref` 44.0，账闭合） | E13 §3.1 |
| v2 快路径 = 1 次子进程 | **57.5ms**，**省 38.5ms = 40.1%** | 同上 |
| 模型校验 | `status() ≈ git status + rev-parse`，3/4 档吻合 ±2.5ms | A5 §2.2 |

**推论（全文量尺）**：`端点延迟 ≈ Σ(每次 git 调用真实耗时) + 解析开销`。
地板价恒定 ⇒ **spawn 计数**是可排序的稳定量，墙钟随机器漂。

### 1.2 响应面（[实测]，同上出处）

| 项 | 数字 | 出处 |
|---|---|---|
| HTTP 全量 status（5k 文件） | 204.8ms / 454,420 B | A5 §2.2 |
| 同接口分页（perf 层挂上时） | 103.6ms / 17,519 B（体 −96%） | 同上 |
| 10 万文件 status 干净 / 全 untracked | 135ms / 505ms，内存峰值 69.7MB | A5 结论 |
| 真实悬崖（stage 命令行 32K 上限，`WinError 206`） | **已修**，见 1.3 | E3 §5 |

### 1.3 自我审查：两处过期结论（写进路线防止再犯）

**① A5 把 206 悬崖当"最大未修缺口" —— 已修、且已有测试钉住。**
`server.py` 的 `_run_paths()` 已改用
`--literal-pathspecs + --pathspec-from-file=- --pathspec-file-nul`（路径走 stdin、
一条命令、与长度无关；行号以 2026-10-21 快照为 L526-547，**会漂移，不作判据**，
定位锚是函数名与 docstring "从 stdin 读, 不拼命令行(E8)"）。
语义等价两条实测见 `aigit-E3-server-fixes.md` §5；回归钉在
`test_aigit_extra.py` 一条穷举 206 场景的用例上（其强度是否真越过 32K，由 P0 的
stage 批量档持续量着，复核前不宣称"已覆盖"）。

**② 本路线当日第一版曾把 `pathspec-from-file` 列为"最大未修缺口" —— 错。**
错因：引用 A5 结论而没先读 `server.py`。教训固化进 P0 判据：
**任何阶段第一步是重测，不是重读旧报告；不可复现的数字不进本文档。**

### 1.4 静态账（[实测]，2026-10-21 用 `Select-String` 数调用点）

| 文件 | `_run`/`_out`/`git.run` 类调用点 |
|---|---|
| `server.py` | 23 |
| `history_ops.py` | 35 |
| `conflicts.py` | 17 |
| `gitops.py` | 16 |
| **合计** | **91** |

91 处 ≠ 91 次/请求。"每请求实际几次"是 P1 回答的问题；
**P1 出表之前，任何"先合并哪两个调用"的具体建议都算猜的。**

---

## 2. P0 · 可复现基准台 —— 进度与首份实测

### 2.1 已完成（[实测]）

- 载体 `D:\project\aigit\bench_aigit.py`（23,845 B，未跟踪）：
  stdlib-only，`%TEMP%` 下一次性造仓，量 median/min/max + 字节数，JSON `--out` 落盘；
  **自身不判断快慢、不提优化建议**（修掉 `aigit-dist.md` §5 "脚本丢失导致数字不可复现"的病根）。
- **首份 quick 实测**：`python bench_aigit.py --quick --out D:\aigit_mut_tmp\bench_quick.json`
  → 产出 6,265 B，用时 34.0s，`git 2.50.1.windows.1`，tier=**1000** 文件（quick 档）。

| 端点（tier 1000） | median | min / max | 响应体 |
|---|---:|---|---:|
| `/api/status` | **112.7ms** | 111.2 / 117.2 | 13,021 B |
| `/api/status?offset=0&limit=200` | 126.8ms | 112.8 / 137.6 | 13,021 B（同上 ⇒ **分页被忽略**） |
| `/api/log?limit=30` | **54.2ms** | 46.9 / 54.7 | 129 B |
| `/api/history/blame?file=big.txt` | **113.1ms** | 108.5 / 114.9 | **178,489 B** |
| `/api/diff?file=big.txt` | **49.6ms** | 48.5 / 50.5 | **182,956 B** |
| `POST /api/stage`（3000 路径） | **2,773.6ms** | 2,464.5 / **12,899.8** | 193,018 B |

校准段（尺子自证）：模型地板 = 裸 git status 46.98 + 额外 spawn 预算 40.0 = 86.98ms，
实测 112.73ms，偏差 +25.75 ≤ 容差 60 → **PASS**。

### 2.2 quick 数据当时就能下的三个判断（[实测]推论，非假设）

1. **`aigit_perf.py` 三件套当前确实一个都没生效**：普通 `server.py` 启动路径不调
   `install()`，所以分页档与全量档**字节数逐字节相同**（13,021 = 13,021）。
   E13 §4 那个"默认不挂载"决策是实测可复核的事实，不是记忆。
2. **blame 是当前最大的「响应体炸弹」，但它**有** `-L` 截断**（默认 500 行，问 git 之前就
   卡范围，见 §12.5 的构成解剖）——178 KB 是"每行一个 10 键 dict"的结构性开销
   （154KB porcelain + 1,311B JSON 封装，**封装开销 <1%** [实测]），
   不是无界回吐。追加轮已把它拆到键级，见 §12.5。
3. **stage 3000 路径 = 2.77s（中位）**，且 max 高达 12.9s（方差未解释，可能冷启动，
   `[待测]` 后续 full 档多 run 分辨）。这是**修完 206 之后第一次测到 stage 的真实成本**，
   收工线据此重设（见 §8）。

### 2.3 P0 剩余待办（[待测]）

- **full 档**（1k/5k/10k 全跑，不少于 9 run）产出正式 before 基线；
- JSON 落 `doc\` 证据目录并**带 commit sha 前 8 位**命名（E13 教训：锚定不清的数字会腐坏）；
- stage 三档（100 / 3000 / 5000）分离"spawn 成本 vs git 真实 hash 成本"的账
  ——2.77s 里有多少是 40ms×N 的 spawn、多少是 git 本职，P1 计数器直接回答。

---

## 3. P1 · 每请求子进程计数与排行（状态：**计数器已存在，覆盖有缺口**）

**缺口当轮已按方案 a 补齐（[实测]，见 §12.2.1）**——补齐后的剩余待办：用罩满的
计数器对 30 条 `PROBE_PATHS` + 写端点全量排行（§12.1 的 7 端点先行账已把方向定死：
**解析不是瓶颈、多数端点已在 1-spawn 地板，排行的主要产出是给 P4 定靶**）；
conflicts 路径的头部计数待真冲突仓验证。

**已有（[实测] 本轮核实）**：`server.py` 内置 env 门控计数器
（`AIGIT_GIT_COUNT=1`；`_git_count_reset/_git_count_hit`，定位锚 L472-502、L532-533，
行号会漂不作判据），并经响应头 `X-aigit-git-calls` / `X-aigit-git-cmds` 吐出
（`send_header` L1655/1661 锚）。**缺口**：它只挂在 `Repo._run_full` 一条路上，
91 处调用点只罩 23 处 —— history_ops(35)/conflicts(17)/gitops(16) 各有自己的
subprocess.run，实测活证据见 §12.2（blame 真 spawn 2 次、头部报 0）。

**剩余待办**：① 补齐覆盖率（修法选项与判据在 §12.2）；② 用补齐后的计数器对
30 条 `PROBE_PATHS` + 写端点全量排行——§12.1 的 7 端点先行账已把方向定死：
**解析不是瓶颈、多数端点已在 1-spawn 地板，排行的主要产出是给 P4 定靶**。

**停机条件**（按 §12.1 实测修订）：排行最大 spawn ≤2：**2 不等于无肉**——
status/blame 的第二个 spawn 各 ≈40ms 是真肉（§12.4）；真正的停机线是
「spawn 已到 1 或第二个 spawn 无法安全摘除」。

---

## 4. P2 · 逐端点合并/去重 git 调用（[预估]：每砍一次 spawn ≈ −40ms/请求，依据 = §1.1 闭合账 ±2.5ms）

按 P1 排行从高到低：

1. **请求级 memo**：同请求内同一 `(子命令, 参数)` 只 spawn 一次（请求结束即丢，无失效问题）；
2. **一次出多份答案**：`status --porcelain=v2 --branch` 已带 `# branch.head`，
   查还有谁在单独 spawn 问分支；
3. **互不依赖的查询合并**：`rev-parse --git-dir --is-bare-repository --show-toplevel` 一类。

**判据**：基准台上该端点 median 降 ≥30ms 才算赢；六套件 + 28/28 变异 + 33/33 探针全绿；
响应键集与值类型逐对不变（时间戳类除外）；before/after 两份 JSON 留档。

**停机条件**：端点 spawn 已到 1 且降幅 <15% → 记"已到地板"，转 P3。

---

## 5. P3 · sha 键记忆化（"算过的永不重算"的单机版，对标 Scion 内容寻址缓存）

**允许缓存的**：`subject_of(sha)` 等**输入只有 sha** 的派生值；
blame 按 `(epoch, HEAD_sha, rel)` 键。

**红线（不设开关、不设例外）**：`status` / `dirty` / 工作区 diff —— 它们是 fail-closed
守卫的输入，缓存 = 给守卫喂过期数据。

**失效规则（唯一一条）**：任何写端点（stage/unstage/discard/reset/commit/amend/merge…）
成功返回时 `epoch += 1`。不做精细失效。

**判据**：① 值先量后写的语义测试：commit 后立刻查必须反映新 HEAD；
② 新变异体 `epoch-bump-off` 必须被该测试杀掉（进 catalog，全量基线 28→29 维持 100%）；
③ 基准台 blame 端点第 2 次起 median 降 ≥30ms；④ 全套守卫不红。

**停机条件**：blame 本来 <60ms → 缓存无肉，降级为只缓存 subject_of 类零风险项。

---

## 6. P4 · 常驻 git 进程（`cat-file --batch`）—— 单机版 scion-daemon

**性质：这不是提速补丁，是故障形态的更换。**

- 落地沿用 `aigit_perf.py` 已验证的灰度模式：**默认关、`--fast-objects` 显式开启 →
  稳定期收数据 → 默认翻转需用户明确授权**（E13 §4 理由先重审）；
- 范围：文件/diff/冲突 resolver 读 blob 的热路径，其余保持 `_run`；
- **判据必须先于实现写好**：
  1. 旧形态探针：先录下"不存在 blob / 损坏 blob / 仓不存在"三类故障的
     错误归类签名（`code=` 与 HTTP 码）——判据形状是"哪条闸拒的"，不是"有没有拒"；
  2. 新实现下三类签名**逐字一致**，否则是改了对外契约、不是提速；
  3. `kill` 常驻进程后下一请求必须自愈（重 spawn）且不回吐半截数据；
  4. 该路径延迟降 ≥40ms（至少省一次 spawn）。

**停机条件**：任一签名不一致 → 该路径回退 `_run`，在 P2/P3 层面收工。

---

## 7. P5 · 分页补齐与传输收尾

- `/api/log` 现只有 `limit`（默认 30，**[实测]** 定位锚：`def log(self, limit=30)`；
  行号会漂不作判据），补 `offset`，复用现成 `paginate()`；
- blame 的 178 KB 单文件响应（§2.2）是 P0 实测出来的新证据：第二维分页
  （按行窗）是否做，**等 P1 排行定**（它也牵出"blame 是否值得 C1 缓存"）；
  前提问题先答：大文件的 blame 该不该截断少内容——属契约改动，**需用户点头再动**，
  本文只把它登记为候选；
- gzip（stdlib）：**仅当** P0 显示分页后响应体仍是瓶颈才做，否则不做；
- 每 adds 一个键跑一次 `find_test_gaps`，维持零提及 0。

---

## 8. 收工判据（整条路线）

| 指标 | 现值 | 收工线 | 状态 |
|---|---|---|---|
| status（本机，1k 仓） | **54.8ms** `[实测]`（当轮四，v2 单 spawn） | **≤60ms 不得回退** | **已达标（主线快路径已落，不再依赖 perf 层挂载）** |
| 10 万文件 status 干净 / 全 untracked | 135 / 505ms `[实测]` A5 | ≤700ms | 已达标，钉住 |
| stage 3000 路径 | **2,773.6ms** `[实测]` bench_quick | ≤2,000ms | **未达标**，分解账 `[待测]`（P1 出） |
| blame（big.txt，1k 仓） | 113.1ms / 178KB `[实测]` | 待 P1 定（分页/缓存二选一或都做） | `[待测]` |
| spawn ≥4 的端点 | commit 成功路径 **10 → 8**（当轮九）`[实测]` | 见 §8.1 分层判据（硬指标 = spawn 预算） | **已出具「已到地板」结论**：commit 剩 7 个真干活 + 1 个 status 刷新，其中两遍 `diff --cached` 是哨兵，压到 ≤3 必须拆防线 |
| 万能兜底 | — | 每阶段收尾：六套件 + 28/28 变异 + 33/33 探针 + 零提及 0 + 编码门禁 0/0 | 任一格红即停 |

> stage 收工线说明：≤2s 是按 quick 实测 2.77s 定的**改进目标**，不是拍脑袋的宇宙常数。
> 2.77s 里 spawn/git 本职/scope 校验各占多少未拆账（`[待测]`），若拆完发现 git add 3000 文件
> 本来就要 2.5s，则判据按"spawn 省尽后其实测值"重定，并写明原判据为何不成立。

### 8.1 分层判据（当轮九定，取代「全线 ≤100ms」）

**原判据为何不成立**（[实测] §12.10）：`git commit` 本体 **172.9ms**、切已有分支
**107.4ms**，每次 spawn 固定启动税 **36.4ms**。这两条命令的地板都压在 100ms 以上，
所以「全线 ≤100ms」不是目标，是一条永远红的线；而一条永远红的判据最后只会被改掉。

**新判据：硬指标 = spawn 预算，软指标 = 墙钟软带。**

| 层 | 硬：spawn 预算 | 软：本机墙钟 | 现状（[实测] §12.11–§12.15） | 状态 |
|---|---|---|---|---|
| 读（13 项） | **≤1 / 请求**（ref/对象类 ≤0） | ≤90ms | **4 spawn**；7 项已 0 spawn，单项 0.9–76.6ms | 达标 |
| 轻写（stage×3 / unstage / checkout / branch / merge） | **≤2** | ≤140ms | 85–112ms | 达标 |
| discard | ≤3（**例外**） | ≤140ms | 126–176ms | 达标（见下） |
| commit | ≤8 | ≤600ms | 8 spawn / 496–525ms | 达标 |
| 地板论证 | spawn 36.4ms | — | §12.10 | 已钉住 |

**为什么 `discard` 单独留 3**：它的 `ls-files` 不是在过滤，而是在**分叉**——
「已跟踪 → `checkout --` 还原」还是「未跟踪 → unlink / rmtree 删除」。判错一个，
就是把用户的已跟踪文件当未跟踪**删掉**（F-02 那类数据丢失）。`stage/unstage` 的
同一份 `tracked` 只用来过滤「已删除但已跟踪」的路径，判错的方向是少暂存 / 多传一个
不存在的路径（都不丢数据），所以只有那两个允许走索引直读（§12.12）。

**为什么硬指标用 spawn 而不是 ms**：同一份代码在同一台机器上，未改动的读端点
两轮之间抖动 ±25ms（§12.11 的对账表里 `/api/log` +18.3ms、`/api/status` +15.0ms，
代码没碰）。spawn 次数是确定性量，能当判据；毫秒只能当软带 —— 用毫秒当硬判据，
噪声会伪装成回退或改进。

---

## 9. 风险登记与红线

**红线（不可谈判）**：

1. 不缓存工作区状态（status/dirty/diff）—— fail-closed 守卫的输入必须现算；
2. 不为提速跳过任何守卫（越界闸、add 作用域、确认协议、脏区拒绝）；
3. `--root` 不指向真实仓做基准/探针（bench 只碰 `%TEMP%` 生成仓）；
4. 不改判据让数字变好；判据变更必须写明原判据为何失效；
5. 所有数字带命令 + 退出码；不可复现的数字不进任何文档。

**风险登记**：

| 风险 | 触发 | 缓解 |
|---|---|---|
| 旧报告数字腐坏 | 引用任何文档结论前 | 先读代码/重测（1.3 两例是活证据） |
| 基准台失真 | P0 | 校准段打印 PASS/FAIL，quick 版已经过一次 PASS |
| 缓存喂过期数据 | P3 | 红线 §9.1 + `epoch-bump-off` 变异体钉失效 |
| 常驻进程改错误契约 | P4 | 故障签名先录后改，不一致即回退 |
| 性能改造动摇安全语义 | P2–P4 | 每阶段跑 28 变异体；新守卫逻辑同步进 catalog |
| 默认开启未经评审 | P4 | opt-in + 稳定期 + 用户授权 |
| 多线并行写同一文件 | 全程 | bench/计数器改动只进 bench_aigit.py/新文件；server.py 行为改动单线串行 |

---

## 10. 依赖与并行

| 阶段 | 前置 | 可并行 |
|---|---|---|
| P0 基准台 | 无（载体已落，full 档待跑） | 与 P1 并行 |
| P1 计数排行 | 无（server 能起即可） | 与 P0 full 档并行 |
| P2 合并调用 | P1 排行 | 按端点拆互不重叠小线 |
| P3 记忆化 | P0 基线 + P2 后排行 | — |
| P4 常驻进程 | P0 + 故障签名探针 | — |
| P5 分页 | 无 | 与 P2/P3 并行（文件域不重叠） |

---

## 11. 证据与参考

| 文件 | 用途 |
|---|---|
| `litematica-preview\doc\aigit-A5-scale-perf.md` | 规模曲线、40ms 地板价（206 结论已过期，引时标注） |
| `litematica-preview\doc\aigit-E13-perf-layer.md` §3.1 | status 38.5ms/40.1% 复测与闭合账 |
| `litematica-preview\doc\aigit-E3-server-fixes.md` §5 | `_run_paths` 语义等价实测 |
| `aigit\bench_aigit.py` | **P0 载体**（23,845 B，未跟踪） |
| `D:\aigit_mut_tmp\bench_quick.json` | **首份实测**（6,265 B，tier 1000，校准 PASS） |
| `D:\aigit_mut_tmp\anatomy1.py` / `anatomy2.py` | 追加轮解剖脚本（HTTP 电池 / 进程内三路 wrapper，rc 均 0） |
| `anatomy1_out.json` / `anatomy2_out.json` | 7 端点 spawn 真账 + 逐次耗时（§12 表格来源） |
| `D:\aigit_mut_tmp\task_P0_bench.md` | 并行线 P0 派单记要 |
| `doc\aigit-perf-roadmap-20261021.md` | 同日初稿（历史快照，更新一律进本文） |
| `doc\aigit-tests-20261006.md` | §16–§20 六轮测试全记录（判据出处） |
| 台账 `cross-project-status-20261002.md` §22 | 跨仓证据账 |

---

## 12. 端点解剖实测（2026-10-21 追加轮——把「待测」换成账）

**方法（[实测]，两条都有命令与 rc）**：scratch 仓 `C:\Users\Public\tmp\opencode\aigit_anatomy\repo`
（1,000 追踪文件 + `big.txt` 1,000 行 + 弄脏 25 条，只读端点）。

- `D:\aigit_mut_tmp\anatomy1.py` → 起真 server（`AIGIT_GIT_COUNT=1 --root <scratch>`），
  HTTP 电池 7 端点 × 7 run（热身丢 1）+ 裸 git 对照 + blame/diff 响应体解剖
  → `C:\Users\Public\tmp\opencode\anatomy1_out.json`，rc=0；
  副本与 `[实测]` 记要在 `D:\aigit_mut_tmp\`（anatomy1.py / anatomy2.py / *_out.json）。
- `D:\aigit_mut_tmp\anatomy2.py` → 进程内把**三路**底层各包一层计数+计时 wrapper
  （`Repo._run_full` / `HistoryOps._run` / `conflicts.Git.run`），拿每端点**真实**
  spawn 序列与逐次耗时，3 run 取最优 → `D:\aigit_mut_tmp\anatomy2_out.json`，rc=0。

环境坑（当轮踩到，记下）：`C:\Users\Public\tmp\opencode` 目录里有**别的线留下的
`bisect.py`**，会把标准库 bisect 遮蔽掉（statistics 一 import 就炸）——
脚本别放那个目录跑；照改不动其它线的文件。

### 12.1 每端点真实账（in-process，最优 run）

| 端点 | wall | 真 spawn | Σgit | 解析 | 命令逐次 |
|---|---:|---:|---:|---:|---|
| `/api/status` | 99.8ms | **2** | 99.7 | 0.07ms | status 58.2 + symbolic-ref 41.5 |
| `/api/history/blame` | 112.5ms | **2** | 104.3 | 8.2ms | rev-parse 40.2(`_require_commits`) + blame 64.1 |
| `/api/log?limit=30` | 41.4ms | 1 | 41.4 | 0.03ms | log |
| `/api/branches` | 42.0ms | 1 | 42.0 | 0.03ms | for-each-ref |
| `/api/diff?file=big.txt` | 44.5ms | 1 | 44.5 | 0.02ms | diff |
| `/api/file?file=big.txt` | 45.5ms | 1 | 45.5 | 0.02ms | show HEAD |
| reflog/stash list | 39.9ms | 1 | 39.8 | 0.03ms | stash |

三重闭合验证（[实测]）：in-process 侧 wall−Σgit = 解析项全部 <9ms；
HTTP 侧 `/api/health`（0 spawn）median **0.66ms** = HTTP 栈+JSON 封装的地板；
status 两次独立测量（HTTP 96.1 / in-process 99.8）与 raw 双命令之和
（49.2+40.6=89.8）在 ±10% 抖动内一致。**每端点成本 ≈ spawn 数 × ~40ms + 解析**
这条量尺在 7 端点上全部成立。

### 12.2 计数器覆盖缺口 —— 本轮最大结构发现

P1 计数器只挂 `Repo._run_full` 一条路 = **91 处调用点只罩 23 处**
（history_ops 35 / conflicts 17 / gitops 16 自带 subprocess.run）。
活证据（[实测]）：blame 真 spawn **2** 次（anatomy2 逐次账），HTTP 头却报
`X-aigit-git-calls: 0`（anatomy1）。

**重要边界（先说清，防止误伤）**：**gitguard 中和闸不受影响**。四模块全部各自
过闸——history_ops `_run` 内自带 `gitguard.for_root+safe_env`（L162-183 锚）、
conflicts 由 `_argv()` 统一拼（L106 锚）、gitops 由 server 启动注入
`guard=gitguard`（L120 锚）。缺的是**账**，不是**闸**：
「仓库配置指向的程序一个都执行不到」这条安全不变式仍然全覆盖。

**修法选项**（动之前需要你点头；判据=「每一路都计数」）：
a) **回调注入**（推荐）：server 把计数回调塞给三个模块；conflicts **不能顶层
   import server**（其模块注释 L197 明写这是既有约束），必须走注入不能走 import；
b) 抽公共 `_run` 基类：改动大、收益不直接，不做首选。
判据：补齐后 blame 头部必须报 `2`；变异体候选 `counter-off` 进 catalog。

### 12.2.1 补齐记录（当轮已做，方案 a，[实测]）

四处改动：`server.py` 加 `_count_hook()`（门关时零额外开销）+ 三个懒加载工厂
各注入一行；`history_ops.py` / `conflicts.py` / `gitops.py` 各加模块级
`COUNT_HOOK = None` 并在自家 spawn 咽喉点（history_ops `_run`、conflicts
`Git.run` + `Git.cat_batch` 两点、gitops `_run`）判 `is not None` 后回调。
conflicts 侧**没有** import server（设计约束保持），gitguard 闸零改动。

验证（[实测]）：
- 真 server + `AIGIT_GIT_COUNT=1`：`/api/history/blame` 头 = **`2`**
  （cmds=`rev-parse,blame`，与进程内逐次账逐一对上）；
  `/api/history/reflog` 从瞎账变 `2`（`rev-parse,reflog`）；
  `/api/status` 不受扰动仍 `2`；
- `py_compile` 四文件 rc=0；编码门禁 5 文件 0/0；
- 回归：锚点 `test_blame_guard_signatures` **2/2**（改造前后各跑一次都绿）；
  `test_history_ops` **37/37**、`test_conflicts` **20/20**、`test_aigit_net`
  **56/56**（29.3s）。

**待验证**（改了但没逐字实测）：conflicts 路径的 HTTP 头计数——需要一个真
处于合并态的 scratch 仓才碰得到 `Git.run`；静态双挂点已插，但「头部能看到
ls-files 等 cmds」这步没跑出来（当轮造冲突仓失败，属环境问题非逻辑问题）。

**补记（当轮三）：已由 §12.7 全操作表实测关闭**——`/api/conflict/state`
头部报 `6`（cmds 全为 rev-parse）、`/api/conflict/list` 报 `2`（diff,rev-parse），
conflicts 路径的计数链（注入 → `Git.run` → 响应头）**实测通达**。

### 12.3 解析不是瓶颈（这条结论足以砍掉一类想象中的优化）

- porcelain→dict、JSON 序列化的解析成本：**全部端点 ≤ 0.07ms，最重的 blame 也只有 8.2ms**
  （152,680B porcelain → 500 个 10 键 dict → 153,991B JSON）；
- JSON 封装开销 = 153,991 − 152,680 = **1,311B（0.85%）**；
- HTTP 栈地板 0.66ms/请求（0 spawn 的 `/api/health`）。

**结论**：在这个规模下，任何「换更快 JSON 库 / 手写序列化 / 流式输出」的努力
都砍不动 ≥40ms 的 spawn 地板，方向性错误，不做。

### 12.4 P2 的真实肉量（用实测替换 §4 里的猜测表）

7 个实测端点里 **5 个已经踩在 1-spawn 地板上**（wall≈单个 git 调用），
P2 可摘的肉只有两个第二 spawn：

| 靶 | 第二 spawn | 怎么摘 | 前置判据 |
|---|---|---|---|
| `/api/status` | symbolic-ref **41.5ms** | `--porcelain=v2 --branch` 一条出全 | 快路径**已存在**于 aigit_perf.py（默认关、E13 实测 57.5ms）——**要不要挂进主线**是 E13 §4 决策重审，需你拍板，不是写代码问题 |
| `/api/history/blame` | rev-parse **40.2ms**（`_require_commits` 守卫） | 惰性化：blame 失败时再兜底补守卫 | **先录旧形态探针签名**：空仓库时今天的错误 code 是 `no_commits`，惰性化后 blame 会先炸 `blame_failed`——错误形态变了，必须先有旧签名再改 |

**P2 结论**：总肉 ≈ **80ms**（两个端点各一刀）。摘完之后 spawn 全部 ≥1 即到地板，
**地板本身（每请求 ~40ms）只有 P4 的常驻进程能打**——P4 从「可选」升级为
「status/blame 收割后唯一的主菜」。

### 12.4.1 补正（当轮三：全操作表出来后，上一条的排序被取代）

上述结论只覆盖**7 个读端点**。补测 13 读 + 9 写共 22 个操作后（§12.7），
P2 的真实排序改写成：

| 新排序 | 操作 | spawn | 可预期收获 | 风险 |
|---|---:|---:|---:|---|
| 1 | `/api/conflict/state` | **6**（全是 rev-parse） | ✅**已做**：**260.5→91.9ms（6→2）**，实际 −168.6ms | 已落地（gitdir 缓存 + ORIG_HEAD 单读） |
| 2 | `POST /api/commit` | **9**（`diff` 重复 3 次） | −120ms[预估]（diff 3→1、ls-files 2→1） | 高（写操作，fail-closed 守卫在里头；须逐个确认后动） |
| 3 | 6 个写操作（stage×3/unstage/discard/checkout/branch） | 4 | ✅**已做**：整排 **4→3 spawn**，实测 −21~−53ms/项（写面白得的 symbolic-ref 账单已删） | 已落地（status v2 进主线） |
| 4 | `/api/history/blame` | 2 | ✅**已做**：**114.3→80.8ms（2→1）**；`_require_commits` 移到失败路径 | 已落地（文案签名逐字不变，锚点 4/4） |
| 5 | `/api/history/reflog`、`/api/conflict/list` | 2 | ✅**已做**：reflog **90.4→42.5ms**（2→1，空仓语义不变）；conflict/list 99.8→54.6（gitdir 缓存白捡） | 已落地 |

读端点里 **tags/stashes/file/branches/log/diff 6 个已在 1-spawn 地板**（44.9–53.6ms），
不动。**全表合计**：22 操作 ≈ 2,868ms，其中 spawn 计费 ≈ 2,320ms = **81%**——
即「约四分之三的等待是 CreateProcess，不是 git 本体、更不是 Python」。

### 12.5 blame 响应体构成（同时修正我准版文档的一处错）

`DEFAULT_BLAME_LINES=500`、截断**在问 git 之前**就做（`-L 1,500`）——
追加轮之前我把 178KB 说成「全量回吐」，**不准**，收回。实际构成（[实测]）：

| 键 | 占比 | 备注 |
|---|---:|---|
| content | 31.8% | 500 行 × ~31B/行，这里没有肉 |
| time | 14.3% | 逐行重复（本语料 500 行同一提交） |
| summary | 12.1% | 逐行重复 |
| filename | 5.0% | 逐行重复 |
| author_email | 3.9% | 逐行重复 |

entries=500，`unique_sha=1` / `unique_summary=1` —— **本语料的去重收益被高估**：
语料只有一次提交+弄脏了最后一行，前 500 行全是同一 sha 撑的。真实 reduce 幅度
随文件的 commit 多样性变化，**换多提交语料重测过才算数**【待测】。
现有事实：单行 dict 里 `time/summary/filename/email/sha/short` 六键对重复值逐行
携带——公共提交块（commit table + 行→sha 索引）能否省 >30% 体，排队
P1 全量排行之后，**契约改动，需你点头再进 P5**。

### 12.6 `/api/file` 双份回吐

`old + new` 是同一文件的两份（`show HEAD:big.txt` raw 56,893B → 响应体 115,888B
= **2.04×**）[实测]。若前端只展示其中一栏就是纯浪费；截断或懒加载属契约改动，
**登记候选，需你点头**。

### 12.7 全操作时间表（当轮三，[实测]）

方法：scratch 仓（201 追踪文件 + big.txt 1000 行 + 31 条脏区），起真 server
（`AIGIT_GIT_COUNT=1`），读端点 6 run 去头尾取中位，写操作真改仓。
脚本 `D:\aigit_mut_tmp\ops_timings.py` → `ops_timings.json`，rc=0。

`spawn 计费` 一列 = calls × 40ms（A5 地板价），括号为该操作墙钟里被 spawn
吃掉的比例。全部按墙钟降序：

| 操作 | ms | calls | cmds | spawn 计费 |
|---|---:|---:|---|---:|
| POST /api/commit | 507.2 | **9** | ls-files,diff,ls-files,rev-parse,diff,add,rev-parse,**diff**,commit | 360 (71%) |
| GET /api/conflict/state | 260.5 | **6** | rev-parse ×6（`_gd` 每状态文件一次 + ORIG_HEAD 读两遍） | 240 (**92%**) |
| POST /api/stage（10 文件，第二轮） | 203.8 | 4 | ls-files,add,status,symbolic-ref | 160 (79%) |
| POST /api/checkout（回 master） | 203.5 | 4 | show-ref,checkout,status,symbolic-ref | 160 (79%) |
| POST /api/unstage（10 文件） | 195.4 | 4 | ls-files,reset,status,symbolic-ref | 160 (82%) |
| POST /api/stage（10 文件） | 181.9 | 4 | ls-files,add,status,symbolic-ref | 160 (88%) |
| POST /api/stage（脏 big.txt） | 173.2 | 4 | ls-files,add,status,symbolic-ref | 160 (92%) |
| POST /api/discard（1 untracked） | 167.8 | 4 | ls-files,checkout,status,symbolic-ref | 160 (95%) |
| POST /api/branch（新建） | 160.5 | 3 | checkout,status,symbolic-ref | 120 (75%) |
| GET /api/history/blame | 114.3 | 2 | rev-parse,blame | 80 (70%) |
| GET /api/status | 101.1 | 2 | status,symbolic-ref | 80 (79%) |
| POST /api/merge（已同步） | 100.8 | 2 | merge,diff | 80 (79%) |
| GET /api/conflict/list | 99.8 | 2 | diff,rev-parse | 80 (80%) |
| GET /api/history/reflog | 90.4 | 2 | rev-parse,reflog | 80 (88%) |
| GET /api/history/tags | 53.6 | 1 | for-each-ref | 40 (75%) |
| GET /api/file | 52.5 | 1 | show | 40 (76%) |
| GET /api/branches | 51.2 | 1 | for-each-ref | 40 (78%) |
| GET /api/history/stashes | 50.5 | 1 | stash | 40 (79%) |
| GET /api/diff | 44.9 | 1 | diff | 40 (89%) |
| GET /api/log | 41.6 | 1 | log | 40 (96%) |
| GET /api/remote/capability | 10.9 | 0 | — | 0 |
| GET /api/health | 1.0 | 0 | — | 0 |

> ⚠ **口径更正（当轮九，[实测]）**：本表 `POST /api/commit` 那一行记的是**失败路径**。
> 电池仓的初始 commit 用的是 `-c user.name=…` 临时参数，没写进仓级配置，而 aigit 只在
> 自建仓（`Repo.init`）里写身份 —— 服务端 `git commit` 因此返回 rc=128
> （`Author identity unknown`），整个请求 400。9 次 spawn 是真的（cmds 逐条对得上），
> 但 507.2ms 不是「提交成功」的价格。**成功路径实测 10 spawn / 558.4–611.3ms**（§12.11）。
> 本表其余 21 行不受影响：只有 `commit` 需要身份，`add/reset/checkout/merge(ff)` 都不需要。
> 这一格的教训是「判据必须先证明会响」的反面案例：电池一直在测 400 分支，而它
> 从不抱怨，数字看着还挺合理。

**三个直接读出来的事实**：

1. **22 操作合计 ≈ 2,868ms，spawn 计费合计 2,320ms = 81%**——五分之四的等待
   是 `CreateProcess`，不是 git 本体、更不是 Python（§12.3 已证解析 ≤8.2ms）；
2. **写操作没有一个是「贵在写」**：stage/unstage/discard/checkout 全都是
   4 spawn 套餐 = 作用域守卫(ls-files/checkout/show-ref) + 动词 + status 刷新
   （status+symbolic-ref）——**同一张「每请求多付一次 symbolic-ref」的账单
   在读面上也收，在写面上再收一次**；
3. `/api/commit` 9 spawn 里 `diff` 三遍、`ls-files` 两遍、`rev-parse` 两遍。
   当轮九把这 9 次逐个对到代码行后，**原「可减 3 spawn ≈ −120ms」的预估不成立**：
   只有 2 次能安全摘（`ls-files` 两类合一次、`rev-parse` 两次合一次）；
   三遍 `diff` 里两遍 `--cached` 是 `_assert_index_scope` **刻意冗余的哨兵**，摘掉=拆防线；
   `diff`（工作区 vs 索引）摘掉会丢「工作区已删文件」的删除记录。实测 `10→8 spawn`（§12.11）；
   `/api/conflict/state` 更极端：6 次 rev-parse 全是同一个 `_gd()` 在逐个
   状态文件重复解析 gitdir（并且 `origHead` 那个 if-else 把 `_read_gd("ORIG_HEAD")`
   在同一表达式里**读了两遍**，纯重复）——6→2 约 −160ms，且是只读端点。

**可摘总量**（按 §12.4.1 的五条，保守合计）≈ **−720ms / −25%**，摘完后
残余仍是 spawn 地板 → P4 常驻进程才有下一层。

### 12.7.1 改后复测（当轮四，[实测]：两刀已落地）

同一脚本重跑（before 存在 `ops_timings_before.json`）：

| 操作 | 改前 | 改后 | Δ | calls |
|---|---:|---:|---:|---|
| GET /api/status | 101.1 | **54.8** | **−46.3** | 2→**1** |
| GET /api/conflict/state | 260.5 | **91.9** | **−168.6** | 6→**2** |
| GET /api/conflict/list | 99.8 | 45.1 | −54.7 | 2→**1**（gitdir 缓存白捡） |
| POST stage（3 行） | 173.2–203.8 | 125.1–160.4 | −21.5 ~ −48.1 | 4→**3** |
| POST unstage | 195.4 | 145.5 | −49.9 | 4→**3** |
| POST discard | 167.8 | 158.9 | −8.9 | 4→**3** |
| POST checkout | 203.5 | 150.5 | −53.0 | 4→**3** |
| POST branch | 160.5 | 112.6 | −47.9 | 3→**2** |
| POST commit | 507.2 | 519.1 | +11.9（噪声内） | 9→9（**最大剩余靶**） |
| GET blame / reflog | 114.3 / 90.4 | 116.2 / 90.8 | 噪声内 | 2→2（守卫未动，符合预期） |
| **合计 22 操作** | **2,868** | **2,296** | **−572（−19.9%）** | **58→45** |

写面那列 −8.9~−53 的离散以 ±10% 机器抖动为主，**稳定信号是 calls 4→3**；
spawn 计费占改后总时的 45×40=1,800/2,296 = **78%**——地板占比反而更高了，
因为可摘的 spawn 已经被摘掉。

**语义判据（改前先量，改后比对）**：
- status 新旧解析 **10/10 SAME**（`compare_parsers.py`，同仓同时刻双解析；
  fixture 覆盖 rename/含空格路径/unborn HEAD/detached/合并冲突/嵌套 untracked）；
- ⚠ **跨重建比较无效**：第一版「快照→重建→比对」报了 2 处假差异，根因是重建
  出来的脏状态本身会漂（同一 clean 仓两次一 0 条、一 1 条），比较必须同仓同时刻；
- 套件：`test_aigit.py` **60/60**（164.7s）、并发 rc=0（C3/C8/C9/C10 全过）、
  contract **18/18**、`test_conflicts` **20/20**、锚点 **2/2**；
- **未跑**（如实标注）：28 变异体全量基线 + 33 探针——本轮改了 `status()` 的
  解析路径与 `conflicts.merge_state()`，若变异目录里有 status/冲突态相关靶，
  下一轮全量基线必须复核，不能拿本轮结论替代。

### 12.7.2 惰性守卫落地（当轮五，[实测]）

- **blame**：`_require_commits` 从「最前面无条件跑」挪到**失败路径补跑**。
  成功路径 **2→1 spawn**；错误路径 spawn **顺序**变为 blame→rev-parse
  （空仓）/ cat-file→rev-parse（无文件），计数不变；
  错误**文案签名逐字未改**（HTTP 四例全过，锚点 `test_blame_guard_signatures.py`）；
- **reflog**：`list_reflog` 去掉 `_has_commits()` 守卫——`git reflog` 在空仓
  rc=128、失败即返回 `[]`，与今天「空仓 200 + []」的可观察行为**逐字相同**；
  成功路径 **2→1 spawn**；
- 新性能契约（进锚点）：blame 成功路径 spawn 恰为 `["blame"]`、
  reflog 恰为 `["reflog"]`（空仓也仅 1 次 spawn 且返回 []）。
- 回归：`test_history_ops` **37/37**、`test_aigit_contract` **18/18**、
  锚点 **4/4**。
- **干净墙钟（基线结束后复测，无后台负载）**：blame **80.8ms**（原 114.3）、
  reflog **42.5ms**（原 90.4），均 **1 spawn**；22 操作合计
  **2,239ms / 43 spawns**（原始 2,868ms / 58 → −629ms = **−21.9%**）；
  spawn 计费 43×40=1,720ms = 仍占 **77%**。
- 负载噪声对照（同批踩到的）：带负载时 `commit` 519→1,616ms、`merge` 87→163ms，
  故当轮先记结构信号、后补干净墙钟——**报数字必须说明当时机器在干什么**。

### 12.7.3 汇总（当轮七，[实测]：全部改动后的最终时间表）—— **已被 §12.12 取代**

口径：scratch 仓（201 追踪文件 + big.txt 1000 行 + 31 条脏区），真 server +
`AIGIT_GIT_COUNT=1`，读端点 6 run 去头尾取中位、写操作真改仓。
「首次」= 优化前（同一脚本、同一机器）；「现状」= 今天**两次独立复跑取中位**
（2129 / 2239ms 两次，合计抖动来自单请求 ±10~15ms 的地板抖动）；「抖动」=
两次复跑之间的差值，用作读数可信度的自证。按现状降序：

| 操作 | 首次 ms | 现状 ms | Δ | spawn | 两次抖动 |
|---|---:|---:|---:|---|---:|
| POST /api/commit | 507.2 | 494.2 | −13.0 | 9→9 | 39.2 |
| POST stage（脏 big.txt） | 173.2 | 146.9 | −26.3 | 4→3 | 0.8 |
| POST unstage（10 文件） | 195.4 | 145.8 | −49.6 | 4→3 | 3.6 |
| POST checkout | 203.5 | 144.9 | −58.6 | 4→3 | 6.3 |
| POST stage（10 文件，二次） | 203.8 | 142.8 | −61.0 | 4→3 | 29.3 |
| POST stage（10 文件） | 181.9 | 141.1 | −40.8 | 4→3 | 34.1 |
| POST discard（1 文件） | 167.8 | 126.3 | −41.5 | 4→3 | 4.3 |
| POST branch（新建） | 160.5 | 118.8 | −41.8 | 3→2 | 11.1 |
| POST merge | 100.8 | 91.5 | −9.3 | 2→2 | 12.2 |
| GET conflict/state | 260.5 | **88.0** | **−172.5** | 6→2 | 13.6 |
| GET history/blame | 114.3 | 77.0 | −37.3 | 2→1 | 7.6 |
| GET file | 52.5 | 57.1 | +4.6 | 1→1 | 10.3 |
| GET log | 41.6 | 54.2 | +12.6 | 1→1 | 14.5 |
| GET conflict/list | 99.8 | 54.0 | −45.8 | 2→1 | 1.2 |
| GET diff | 44.9 | 50.3 | +5.4 | 1→1 | 5.2 |
| GET status | 101.1 | **47.8** | **−53.3** | 2→1 | 2.2 |
| GET history/tags | 53.6 | 47.1 | −6.5 | 1→1 | 0.2 |
| GET branches | 51.2 | 46.2 | −5.0 | 1→1 | 11.6 |
| GET history/stashes | 50.5 | 46.0 | −4.5 | 1→1 | 2.5 |
| GET history/reflog | 90.4 | 44.9 | −45.6 | 2→1 | 4.7 |
| GET remote/capability | 10.9 | 11.2 | +0.2 | 0→0 | 2.5 |
| GET health | 1.0 | 7.9 | +6.9 | 0→0 | 13.8 |

| 分组 | 首次 | 现状 | 变化 |
|---|---:|---:|---:|
| 读端点 13 个 | 972.3ms / 20 spawn | **631.7ms / 12 spawn** | −341ms（−35%） |
| 写端点 9 个 | 1,894.1ms / 38 spawn | **1,552.4ms / 31 spawn** | −342ms（−18%） |
| **合计 22 个** | **2,866ms / 58 spawn** | **2,184ms / 43 spawn** | **−682ms（−23.8%）** |

**读数注意**（不粉饰）：

- `commit` 那 −13ms 在 39.2ms 抖动里，**等于没变**：它仍是 9 spawn / ~494ms，
  是当前最大单项（§12.4.1 第 2 条未做）；
- `file` / `log` / `diff` / `health` 的 **+Δ 不是变慢**：spawn 数没变（1/1/1/0），
  纯地板抖动，同一请求两次能差 10~15ms；
- spawn 计费 43×40=1,720ms = 现状总时的 **79%**——可摘的 spawn 已摘完，
  剩下的是每请求一次的地板价；
- **修正一处口算错误**：此前 §12.7 写「spawn 合计 53 次 / 2,120ms / 74%」是错的，
  按 JSON 逐项加是 **58 次 / 2,320ms / 81%**（漏加了 5 个 4-spawn 写操作里的
  一个口径差）。本文所有据此推导的百分比已同步更正；逐条 counts 本来是对的，
  错的只是我的手算合计。

### 12.8 写回 §8 收工判据的三行

- **status ≤60ms**：默认路径实测 96–100ms（2 spawn）——**当前不达标**；
  达标实现已存在（aigit_perf 快路径 1 spawn、57.5ms [实测] E13），
  卡在「挂不挂载」的决策而不是写代码；
- **blame**：已从「待 P1 定」升级为具体候选（§12.4 第二行）；
- **stage 3000 路径 2,773.6ms**：本端点只有 **1 次 spawn**（`_run_paths` 走 stdin
  [实测] E3 §5），肉几乎全在 git add 本职（refresh + hash 3,000 文件）——
  **拆账待测**，大概率 spawn 优化在它身上无肉。

## 12.9 边界巡检（当轮六，[实测]：40 例畸形输入）

方法：`D:\aigit_mut_tmp\fuzz_edges.py` —— 一个 scratch 仓 + 真 server，覆盖
畸形 limit/start/end/index、目录当文件、缺文件、越界路径、含空格与中文路径、
空字段、类型错误、错误 HTTP 方法。**结果：0 个 500**。

**已修的横向不一致（同一件事在不同端点三种行为）**：

| 请求 | 修前 | 修后 |
|---|---|---|
| `diff?file=adir`（目录） | 200 + `diff:""`（装作无改动） | 400 `不是文件(是目录),已拒绝:adir` |
| `history/file?file=adir` | 200 + `commits:[]` | 同上 400 |
| `history/blame?file=adir` | 400 + git 原文 `fatal: no such path …`，**169ms** | 同上 400，**1.4ms**（不再白烧一次 spawn） |
| `log?limit=abc` / `stash?index=abc` / `gen count:"abc"` | 400 + Python 原文 `invalid literal for int() …` | 400 `参数 X 必须是整数,收到 'abc'` |

做法：`Repo.reject_dir()` 统一 E23 文案并接到 diff/blame/history-file 三条路由
（`/api/file` 的 E23 原样保留）；`_qi`/`_qopt` 内联 `int()` 收成干净文案，
另修两处**绕过 `_qi` 自己内联解析**的点（`log` 的 limit、`/api/gen` 的 count）
并新增 `_bi`。回归：`test_aigit` **60/60**（159.4s）、contract **18/18**、巡检 0 个 500。

**登记但未改（需决定，不擅自改契约）** —— **2026-11-02 已全部收口，见 §12.19**：

1. `log?limit=0/-1` → 200 + `log:[]`（静默空）；同族的 blame 对 limit 是**夹取**
   （clamp）——参数处理口径不一致；→ **已统一为夹取 [1,1000]**
2. `diff?file=<从未存在的文件>` → 200 + 空 diff。**不能一刀切 400**：工作区删掉
   但 HEAD 里存在的文件，diff 本来就该给出删除差异；→ **已按「磁盘/索引/HEAD 三处都
   没有」判 400，删除态仍 200**
3. `/api/history/reflog`、`/api/history/stashes` 返回**裸数组**，其余端点返回
   `{ok:…}` 对象——形状不统一（前端目前按现状消费）；→ **已统一成
   `{ok:true, ...:[...]}`（连 tags 一起），前端 `asArray` 两种形状都认**
4. `PUT/DELETE` → 501（`BaseHTTPRequestHandler` 默认），不是 405。→ **已改 405 +
   `Allow: GET, POST`（PATCH 同）**

## 12.10 成本模型与「100ms 全线」的可达性边界（当轮八，[实测]+[预估]）

**单条命令实测**（同仓；状态改变类每次重建状态各 3 次取中位，其余 5 次）：

| 命令 | ms | 备注 |
|---|---:|---|
| `git --version` | **36.4** | 纯进程启动 = 每次 spawn 的固定税 |
| `rev-parse` / `ls-files` / `symbolic-ref` / `diff` / `show` / `log` / `status v2` | 37.0–40.7 | 轻命令，几乎只有启动税 |
| `add big.txt`（单文件） | 43.7 | 内容哈希 |
| `checkout --`（单文件还原） | 42.3 | |
| `checkout -b`（真新建） | 54.8 | |
| `add 10 个小文件` | **84.5** | 新内容哈希 + 写 index |
| `checkout` 切已有分支 | **107.4** | |
| `commit` 真提交（10 文件） | **172.9** | fsync + index/ref 落盘 |

模型：`墙钟 ≈ 36.4 × spawn 次数 + 命令本体工本 + 8ms HTTP/Python`。
实测校验：1-spawn 端点 45–57 / 2-spawn 81–125 / 3-spawn 124–165，全部落在模型内。

**100ms 可达性分档**：

| 档 | 操作 | 依据 |
|---|---|---|
| 已 ≤100（14 个） | 13 个读端点（40–88）+ merge 91.5 | 已到 spawn 地板 |
| 可达【预估】 | discard 126→~91；stage×3 / unstage / branch → 93–107（边界） | 3→2 spawn 省 37ms，但 add/reset/checkout 本体仍在 |
| **不可达（2 个）** | checkout（本体 107.4ms，0 额外 spawn 也超）；commit（本体 172.9ms，最省 commit+status ≈ 222ms） | 只能换语义（异步提交 / 不返回刷新后状态） |

**这条模型的用处**：它解释了为什么写面的边际收益有限——3 spawn 里至少有 1 个是
git 本体昂贵动作（add 55–85 / commit 173 / checkout 107），不是可摘的守卫税；
也说明「全线 ≤100ms」作为判据不成立（判据本身会永远红）。当轮九据此改成分层判据
（见 §8.1），并且**硬指标换成 spawn 预算**（确定性、不随机器噪声漂）：
读 ≤2 / 轻写 ≤3 / commit ≤8；墙钟只作软带：读 ≤90 / 轻写 ≤170 / commit ≤600。
**§12.10 原先设想的「commit ≤300」也一并撤回**：那不是优化空间不足，是算错了 ——
9 次里只有 2 次可安全摘（两遍 `diff --cached` 是哨兵），实测成功路径地板
**8 spawn / 463–525ms**（§12.11）。

## 12.11 commit 两刀与一处判据修正（当轮九，[实测]）

### 12.11.1 先认账：电池一直在测失败路径

`ops_timings.py` 的 scratch 仓用 `git -c user.name=…` 做初始提交，身份**没有写进仓配置**；
而 aigit 只在自建仓（`Repo.init`）里写 `user.name/user.email`。于是服务端
`POST /api/commit` 每次都在 `git commit` 上拿到 rc=128（`Author identity unknown`），
整个请求 400。电池不校验 `ok`，把 400 分支的耗时当成了提交成本：

| 口径 | ms | spawn | 响应 |
|---|---:|---:|---:|
| 电池旧格（失败路径，400） | 474.6 | 9 | 76 B |
| 成功路径（仓内写身份后） | 558.4–611.3 | **10** | 839 B |

修法：电池 `build()` 里补 `git config user.name/user.email`（只影响脚手架，不改产品）。
第 10 次 spawn 是成功后才走到的 `status` 刷新 —— 失败路径在 `commit` 就断了，
所以旧格永远看不到它。

### 12.11.2 9 次 spawn 逐个定性

| # | 命令 | 出处 | 处置 |
|---|---|---|---|
| 1 | `ls-files` | `_add_scope` 已跟踪集合 | **合并进 #3**（`-t` 一次给两类） |
| 2 | `diff`（工作区 vs 索引） | `_add_scope` 的 wanted | 保留：摘掉会丢「工作区已删除文件」的删除记录 |
| 3 | `ls-files --others` | `_add_scope` 未跟踪 | 合并进 #1 |
| 4 | `rev-parse --verify HEAD` | `_index_delta` | **缓存**（持锁段内 HEAD 不变） |
| 5 | `diff --cached HEAD`（before） | `_index_delta` | 保留 |
| 6 | `add` | 暂存 | 保留 |
| 7 | `rev-parse --verify HEAD` | `_index_delta`（after，哨兵） | 用 #4 的答案 |
| 8 | `diff --cached HEAD`（after，哨兵） | `_assert_index_scope` | **保留**：代码里写明是刻意冗余的哨兵 |
| 9 | `commit` | 本体 172.9ms | 保留 |
| 10 | `status` | `commit_and_status` 刷新 | 保留（契约：返回新状态） |

原「可减 3 spawn」的预估作废：能摘的只有 #1/#3 合并与 #4/#7 合并。

### 12.11.3 两刀的等价性证据（不是读码推断）

**刀一：`ls-files -t -z --cached --others --exclude-standard` 一次拿两类**
（`?` = 未跟踪，其余 = 已跟踪）。差分脚本 `cmp_ls_scope.py`：10 种状态
（clean / dirty / untracked / staged_new / rename_staged / deleted_worktree /
空格+中文 / ignored_present / merge_conflict / skip_worktree）下，与「两次 ls-files」
**集合 10/10 完全一致**，rc=0。

**刀二：`head_exists` 在持锁段内只问一次**
（`git add` 不移动 HEAD）；未出生 HEAD 时 `before` 直接复用 `_add_scope` 的 `tracked`
（同一把锁内、同一时刻的整个索引），`after` **仍取新鲜的**（哨兵不许复用）。
反向风险：若外部进程在这两次询问之间完成「首次提交」，缓存版会把 `after` 判成
「整个索引」→ 结果是**更严格**（多出来的路径触发拒绝），不会放宽。

**刀三（判据）：提交改动集 == 旧算法 wanted**。`commit_semantic_diff.py` 三态
（常规脏仓 / 未出生 HEAD 首次提交 / 空格+中文名），比对
`git diff --name-only --no-renames <base>..HEAD` 与旧算法
（`diff` ∪ `ls-files --others`）：

```
PASS dirty   http=200 ok=True calls=8  wanted=7 本次改动=7 树=6
PASS unborn  http=200 ok=True calls=7  wanted=3 本次改动=3 树=3
PASS spaces  http=200 ok=True calls=8  wanted=2 本次改动=2 树=3
失败 0 / 3   （rc=0）
```

未出生 HEAD 首次提交是 **10 → 7**（多省一次：`before` 复用 `tracked`）。

### 12.11.4 结果（同脚手架、同口径、仓内已写身份）

`commit_bench.py`（5 轮，每轮重建脏状态，校验 HEAD 真的前进）：

| | ms（中位） | spawn | HEAD 前进 | 树内文件数（5 轮） |
|---|---:|---:|---|---|
| 改前（`before12` 副本） | **611.3** | **10** | 5/5 +1 | 221/241/261/281/301 |
| 改后（主线） | **524.9** | **8** | 5/5 +1 | 221/241/261/281/301 |

全操作电池（`ops_timings.py`，两轮同脚本，改前跑副本 `before12`）：

| 组 | 改前 | 改后 | delta |
|---|---:|---:|---:|
| 22 操作合计 | 2,168.7ms / 44 spawn | **2,032.0ms / 42 spawn** | −136.7ms（−6.3%） |
| 写 9 项 | 1,598.1ms / 32 spawn | 1,432.7ms / 30 spawn | **−165.4ms（−10.3%）** |
| 读 13 项 | 570.6ms / 12 spawn | 599.3ms / 12 spawn | +28.7ms（**噪声**：代码未动） |
| `POST /api/commit` | 558.4ms / 10 | **463.3ms / 8** | **−95.1ms（−17.0%）** |

**噪声带实测**：8 个本轮未改代码的端点两轮合计 +57.2ms，单项 −7.9…+21.3ms
（`/api/log` +18.3、`/api/status` +15.0、`/api/merge` +21.3、`/api/conflict/state` +6.7）。
所以读面那 +28.7ms 不能记成回退 —— 也不能记成改进；**只有 spawn 数的变化是本轮的账**。
这也是 §8.1 把硬指标定成 spawn 预算的直接原因。

### 12.11.5 回归

- `test_f06_guard.py`：**9/9 PASS**，rc=0（34.2s；T2 判「`escaping_links` 只被调 1 次」、
  T3 判「`commit_merge` 只有一道防线」——本轮没碰这两处，判据原样绿）；
- `test_aigit.py`：**60/60 PASS**，rc=0（156.0s）；
- `cmp_ls_scope.py` 10/10、`commit_semantic_diff.py` 3/3，均 rc=0；
- 变异基线（v12 快照 `ee2d574`，全量 28 靶，workers=4）：**28/28 KILLED、0 VOID、rc=0**
  （`baseline_v12_20261022.json` + `mutlogs_v12/` 54 个日志）；快照 29/29 个 `.py`
  与工作区**逐字节一致**、`status` 干净 —— 否则基线测的是另一份代码；
- 编码门禁 0/0。

## 12.12 轻写 3→2：索引直读与 ref 直读（当轮十，[实测]）

### 12.12.1 先否掉一个我自己提过的方案

上一轮选项里写的是「给 `ls-files`/`show-ref` 守卫加 `.git/index` 的 mtime+size 缓存」。
**这个方案无效**：写操作自己就会改索引，下一次请求必然 miss —— 缓存键在写路径上
永远是过期的。要真省掉那次 spawn，只能在 Python 里**直接读索引文件**。

### 12.12.2 只有 stage/unstage 可以省，discard 不行

| 调用点 | `tracked` 用来干什么 | 判错的后果 | 处置 |
|---|---|---|---|
| `stage` / `unstage` (`_live_paths`) | 保留「已跟踪但工作区已删」的路径 | 少暂存一个路径 / 多传一个不存在的路径（rc=128） | **允许索引直读** |
| `discard` / `discard_plan` | **分叉**：`checkout --` 还原 vs `unlink`/`rmtree` 删除 | 把已跟踪文件当未跟踪**删掉**（F-02 数据丢失） | **必须 spawn `ls-files`** |

`discard` 因此保留 3 spawn，并在 §8.1 里作为唯一例外写明 —— 省 37ms 不值得拿
数据丢失换。

### 12.12.3 索引直读的安全边界（`Repo._index_tracked_fast`）

只在**能确定**的形态上返回集合，其余一律返回 `None` 让调用方回退到 `git ls-files`：
`GIT_INDEX_FILE` 被设置 / `.git` 不是目录（worktree、submodule）/ 索引版本不是 2、3
（v4 是路径前缀压缩）/ 出现 `link`（split-index）或 `sdir`（sparse-index）扩展 /
条目长度越界 / 路径不是合法 UTF-8 / 文件截断 / 任何异常。

`git` 写索引是「临时文件 + rename」的原子替换，所以读到的一定是完整的一份旧或新索引，
不会读到半截。

**等价性实测**（`cmp_index_tracked.py`，8 种状态）：

| 状态 | 索引版本 | 快路径 | 结果 | 预期 |
|---|---|---|---|---|
| v2 默认 / v3 skip-worktree / 未出生 HEAD / 冲突未合并 / 增删改混合 | v2/v3 | 走 | 集合**逐名相等** | 走 |
| v4 前缀压缩 / split-index / sparse-index | v2/v4 | 回退 | — | 回退 |

**8/8**，rc=0。注意这张表刻意同时验「该走的走了」和「该退的退了」——
只验前者的话，一个永远返回 `None` 的实现也能全绿。

### 12.12.4 ref 直读（`Repo._local_branch_exists`）

`checkout` 的 `show-ref --verify refs/heads/<name>` 换成先看 loose ref
（`.git/refs/heads/<name>`）和 `packed-refs`。三态设计：

* 看见且内容像一条 ref（40/64 位十六进制，或 `ref: …` 符号引用）→ `True`，**省掉 spawn**；
* 没看见或内容不像 → `None` → **回退 `show-ref --verify`**（它才是权威）；
* 永不返回 `False`：仓可能用 reftable 后端，那时文件层面「没看见」不等于「不存在」，
  说 `False` 就是错杀。

所以拒绝文案 `分支不存在,已拒绝 checkout:<name>` 一个字都没变（实测逐字比对）。

### 12.12.5 结果

| 操作 | 改前 | 改后 | spawn |
|---|---:|---:|---|
| stage（10 文件，第二轮） | 125.5 | **89.1** | 3→2 |
| stage（10 文件） | 140.5 | **108.9** | 3→2 |
| stage（脏 big.txt） | 123.0 | **104.1** | 3→2 |
| unstage（10 文件） | 128.7 | **95.4** | 3→2 |
| checkout（回 master） | 132.3 | **97.0** | 3→2 |
| discard（1 untracked） | 124.3 | 133.4 | 3→3（按设计不动） |

全表：**2,032.0ms / 42 spawn → 1,965.3ms / 37 spawn**；写 9 项
**1,432.7ms / 30 spawn → 1,329.6ms / 25 spawn（−103.1ms）**。

**这一轮的噪声更大，值得记下来**：未改代码的 `commit` 8 spawn 不变却 +32.7ms、
`branch` +20.0ms、读面合计 +36.4ms（`tags` +18.7、`health` +13.0）。
±35ms 的抖动里，**只有 spawn 数的变化能归因**。这正是 §8.1 把硬指标定成
spawn 预算、把毫秒降级为软带的实证理由。

### 12.12.6 回归

- `check_light_ops.py` **17/17 PASS**，rc=0：stage 混合路径（存在 + 不存在 +
  已删除但跟踪）只暂存该暂存的、不存在的被忽略而不是 500；unstage 同理；
  checkout 不存在分支 → 400 且文案逐字；**packed-refs 里的分支**（loose ref 不存在）
  快路径认得；v4 / split-index 仓**正确回退**到 `ls-files`；
  `discard` 反向对照（已跟踪=还原、未跟踪=删除）仍走真 `ls-files`；
- `test_aigit` **60/60** rc=0（155.8s）、`test_f06_guard` **9/9** rc=0（36.0s）、
  `test_conflicts` rc=0、`test_history_ops` **37/37**、`test_aigit_contract` **18/18**；
- 新增套件 `test_index_fastpath.py` **20/20**（10.1s）：验「该走快路径的逐名相等」
  与「该回退的必须回退」两半，外加 5 条 HTTP 契约（stage 混合路径、checkout 拒绝文案、
  packed-refs 分支、v4 仓回退、discard 反向对照）。套件自跑式输出与 `test_aigit.py`
  同形，已在 `SUITE_PARSERS` 里登记（否则引擎会 `KeyError`）；
- 新增变异靶 3 条（`S-idx-fast-extgate-off` / `S-idx-fast-firstentry-only` /
  `S-branch-ref-loose-off`）：单跑 **3/3 KILLED**，0 VOID；
- 全量变异基线（v13 快照 `006586e`，31 靶，workers=4）：**31/31 KILLED、0 VOID、
  1,121.6s、rc=0**（`baseline_v13_20261023.json` + `mutlogs_v13/` 57 个日志）；
- 编码门禁 0/0。

### 12.12.7 一条被证伪的变异（记下来，免得下次重犯）

`S-idx-fast-extgate-off` 第一版探针用 **split-index** 造 fixture，引擎判
`VOID/PROBE-NOOP`（改前改后签名一样）。查下来不是引擎坏了、也不是变异写错：

> split-index 的主索引里，**「从共享索引里删掉的条目」是用空路径编码的**。
> `_index_tracked_fast` 里那句 `if not name: return None` 已经先一步回退了，
> 所以「关掉 `sdir`/`link` 扩展闸」在这个 fixture 上是**等价变异**。

换成 sparse-index 造 fixture 后签名立刻变化，3/3 KILLED。**`PROBE-NOOP` 这条判定
本身就值钱**：它把「我以为在测某道闸，其实另一道闸先拦住了」这种假覆盖当场标了出来。

## 12.13 引用 / reflog / 索引直读：读面 4 项归零（当轮十一，[实测]）

### 12.13.1 先划掉两个「看起来能省、其实不能」

上一轮我给的三项里，`tags` 和 `reflog` **做不到纯 refs 直读**，因为它们的返回带
对象数据：

| 端点 | 返回里要的字段 | 数据在哪 |
|---|---|---|
| `history/tags` | `%(objecttype)` `%(contents:subject)` `%(creatordate)` | **tag/commit 对象**（annotated tag 还要先解 tag 对象） |
| `history/reflog` | `%an`（**commit 作者**）`%ad` | **commit 对象头** |

读对象就得解 pack（idx + 偏移 + delta）——那是重写 git，不是优化。所以这两项
维持 spawn。真正能做的是四项，判据只有一条：**返回里的每个字段都能从
`.git` 的文件布局直接读出**。

### 12.13.2 做了什么

新模块 `gitrefs.py`（`conflicts.py` 不能 import `server`，所以逻辑必须住在下层）：

| 端点 | 原来 | 现在 | 依据 |
|---|---:|---:|---|
| `conflict/state` | 80.1ms / 2 | **2.5ms / 0** | `HEAD` + ref（原来两条 `rev-parse`） |
| `conflict/list`（无冲突） | 52.5ms / 1 | **1.4ms / 0** | 索引里有没有 stage≠0 |
| `history/stashes` | 52.8ms / 1 | **1.9ms / 0** | `.git/logs/refs/stash` |
| `branches`（+`list_branches`/`default_branch`） | 40.8ms / 1 | **1.5ms / 0** | `refs/heads/**` + `packed-refs` |

读 13 项：**635.7ms / 12 spawn → 364.8ms / 7 spawn（−270.9ms，−42.6%）**；
全表 **1,965.3 → 1,717.7ms（37 → 32 spawn，−12.6%）**。

三处顺带修掉的真问题：

1. **`stashes` 的行就是 reflog 消息**——`git stash list` 的每一行不过是
   `"stash@{n}: " + reflog 消息`，所以直接读文件拼行，**后面那段解析一个字没改**
   （同一份解析器就不会漂）；
2. **`_gitdir()` 冷缓存时白付一次 spawn**——`.git` 是目录时它按定义就是 gitdir，
   原来每个冷缓存的冲突端点都要 `rev-parse --git-dir`。电池里看不出来，是因为
   同进程里 `conflict/state` 先把缓存热了 —— **这是「同一进程内顺序依赖」造成的
   假 0**，单端点冷启动实测 1 spawn；
3. **`conflict/list` 的空清单**：`diff --diff-filter=U` 只有在索引存在 stage≠0
   时才可能非空；没有就 1.4ms 返回，有就走原来的批量路径（正确性一点没动）。

### 12.13.3 回退边界（`None` = 交给 git）

`.git` 不是目录（worktree/子模块）、`GIT_DIR`/`GIT_COMMON_DIR`/`GIT_INDEX_FILE`
被设置、`.git/reftable` 存在、版本不是 2/3、`link`/`sdir` 扩展、空路径条目、
非 UTF-8、结构越界 —— 任一命中就回退。**索引文件不存在算「空索引」而不是
「不认识」**（git 的语义就是这样，判成回退等于白付一次 spawn）。

**差分实测**（`cmp_refstore.py`，11 个状态，rc=0）：

| 状态 | 分支集合 | HEAD 三态 | stash | 未合并 |
|---|---|---|---|---|
| 松散(含斜杠/中文) / packed / 混合 / 200 个 | 一致 | 一致 | 一致 | 一致 |
| 游离 HEAD / 未出生 HEAD | 一致 | 一致 | 一致 | 一致 |
| stash 两条 / 无 stash | 一致 | 一致 | 一致 | 一致 |
| 冲突态 / 冲突已解决 | 一致 | 一致 | 一致 | 一致 |
| **worktree（`.git` 是文件）** | 回退 | 回退 | 回退 | 回退 |

### 12.13.4 过程中被实测纠正的三处（都进了判据）

1. **未出生 HEAD 的 `currentBranch` 是 `"HEAD"`**：`git rev-parse --abbrev-ref HEAD`
   在空仓上 **rc=128，但 stdout 是 `HEAD`**；而生产代码是 `stdout.strip() or None`，
   **不看 returncode**。我的第一版快路径返回 `None` —— 那是**契约变化**。现在
   未出生与游离都映射成 `"HEAD"`，与原来逐字一致。
2. **`stash_entries` 的 `reversed(...)[:n]`**：`reversed()` 返回迭代器，不可切片，
   直接 `TypeError`，被 `test_history_ops` 6 条用例抓住。
3. **`test_no_index_falls_back` 这条判据本身不成立**：索引文件缺失时
   `git ls-files` 本来就输出空，所以「必须回退」等于要求实现比 git 更保守。
   已改写为「缺失 = 空索引」并写明原判据为何不成立。

### 12.13.5 判据与覆盖

- 新增套件 `test_refstore.py` **19/19**（14.0s，含 2 条 `default_branch` 远端用例）：四项差分 + 6 条 HTTP 契约
  （四处 `calls=0`、有冲突时 `conflict/list` 仍走批量路径、worktree 回退后仍能服务、
  远端 HEAD 短名逐字）；
- 新增变异靶 4 条（`S-refs-packed-ignored` / `S-refs-headmark-off` /
  `S-stash-order-off` / `C-unmerged-always-empty`），并把原 3 条索引靶从
  `server.py` 迁到 `gitrefs.py`：**7/7 KILLED**，0 VOID；
- 回归：`test_aigit` 60/60、`test_conflicts` rc=0、`test_history_ops` 37/37、
  `test_index_fastpath` 20/20、contract 18/18、f06 9/9、编码门禁 0/0。

### 12.13.6 `default_branch()` 的远端那一支：缺陷与修复

实测（git 2.50.1）：`for-each-ref --format=%(refname:short) refs/remotes/*/HEAD`
对 `refs/remotes/origin/HEAD` 输出的是 **`origin`**，不是 `origin/main` ——
远端 HEAD 有专门的短名规则（剥掉 `refs/remotes/` 与结尾 `/HEAD`）。

于是 `HistoryOps.default_branch()` 里那句注释 `# "origin/main" -> "main"` 与事实不符：
`lines[0]` 是 `"origin"`，`split("/", 1)` 根本不触发，函数返回 **`"origin"`**。
调用点随后用 `_branch_exists("origin")` 挡掉，表现为「远端 HEAD 这一支等于没生效，
退回到 main/master 猜测」。

**2026-10-27 已修（用户批准，属对外行为变化）**：`gitrefs.remote_head_branch()`
解析 `refs/remotes/<r>/HEAD` 的内容（`ref: refs/remotes/<r>/<branch>`）取最后一段，
返回真正的分支名；远端 HEAD 指向裸 sha（无反推依据）时返回 None，退回 main/master
猜测。回退路径改用 `%(symref)`（真正的目标引用），不再用会给出 `origin` 的
`%(refname:short)`。

影响面（都在预期内，且都是**变正确**）：`is_default` 判断、删除分支时的
default 提示、push 的默认分支。判据：`test_refstore.py` 三条用例
（远端指向 main / 指向 trunk / 无远端 HEAD / 裸 sha）。

## 12.14 写端点巡检：NUL 路径走私与「拒绝带副作用」（当轮十二，[实测]）

方法：`D:\aigit_mut_tmp\fuzz_write.py` —— 一个干净仓 + 一个冲突仓、真 server，
**31 个 POST 端点 × 119 组畸形载荷**。三条断言：不 500 / 不泄漏 Python 原文 /
**被拒的请求不许有副作用**（HEAD、`status --porcelain`、stash/分支/tag 计数前后一致）。

第一轮 **6 条命中**，其中 2 条是我的用例本身合法（冲突仓里的 `abort`/`commit`，
已移出），4 条是真问题：

### 12.14.1 最严重：路径里的 NUL 是**路径走私**（数据丢失）

```
POST /api/unstage {"files": ["a.txt\0.png"]}                    → 200，实际取消了 a.txt 的暂存
POST /api/discard {"files": ["a.txt\0.png"], "confirmed": true} → 200，实际**丢弃了 a.txt 的改动**
```

两个机制叠在一起：

1. **Windows 上 `Path("a.txt\0.png").resolve()` 静默截断成 `a.txt`**（实测：不抛
   `ValueError`，`exists()` 直接 False）。所以 `resolve_in_root` 的越界/.git/root
   三道闸看到的都是 `a.txt` —— 它们全都「通过」了；
2. `_run_paths` 用 `--pathspec-from-file=-` + NUL 分隔，`a.txt\0.png` 到 git 那边
   变成**两个** pathspec（`a.txt` 与 `.png`）。

**修法**：在 `resolve_in_root` 里、**碰 `Path` 之前**拒 NUL（唯一入口，所有带路径的
端点都经过它）。这不是「路径不合法」，是「请求的那个文件在解析后换了人」。

### 12.14.2 commit 的 NUL 消息：400 但索引已被改（拒绝带前半截副作用）

`commit()` 先 `stage_everything_safely()` 再 `git commit -m`，而 NUL 到 subprocess
层才炸 → 请求 400，文件却已经进了索引。**修法**：消息校验前移到暂存之前。

### 12.14.3 消息参数静默接受非字符串

`{"message": ["x"]}` 曾被 `%s` 拼成 `"['x']"`，真的打出一个内容莫名的 stash。
**修法**：`_need_msg()` 收口（只收 str/None、不许 NUL），用在 stash / amend / tag。

### 12.14.4 `reflog/restore` 的参数校验顺序错了

`_need(isinstance(index,int) and 0<=index<len(rows), "...%d..." % (index,...))`——
`%` 格式化在 `_need` **之前**求值，`index=None` 时先抛 `TypeError`，于是响应里泄漏
`"%d format: a real number is required, not NoneType"`。**修法**：类型先单独判。

### 12.14.5 结果与判据

| 项 | 结果 |
|---|---|
| 复跑 `fuzz_write.py`（119 例） | **0 失败**，rc=0 |
| 新增套件 `test_write_inputs.py` | **10/10**（14.9s） |
| 新增变异靶 4 条 | **4/4 KILLED**，0 VOID |
| 回归 | test_aigit 60/60、conflicts rc=0、history_ops 37/37、index_fastpath 20/20、refstore 19/19、contract 18/18、f06 9/9 |

**一条探针教训**：`C-commit-nul-msg-off` 第一次跑是 `ENGINE-ERROR`——去掉闸之后
`commit()` 抛的是 **Python 原生 `ValueError`**，探针只 `except GitError` 就自己崩了。
把「非 GitError」单独记成 `raised=2` 之后才正常 KILLED。**变异体被误判成引擎错误，
和「变异体存活」一样会掩盖覆盖缺口。**

## 12.15 P4 常驻 `cat-file`：三个「只读对象」的端点归零（当轮十三，[实测]）

### 12.15.1 为什么只做三个端点

| 端点 | 一次请求里 git 到底在干什么 | P4 有用吗 |
|---|---|---|
| `/api/file`（旧版内容） | `git show <ref>:<path>` = 取一个 blob | **有用**（本体 2–3ms） |
| `history/tags` | 取 tag/commit 对象读 type/subject/日期 | **有用** |
| `history/reflog` | 取 commit 对象读作者/时间 + reflog 文件 | **有用** |
| `status` / `diff` / `blame` | git 自己要算索引/工作区/行归属 | 没用（不是「取一个对象」） |
| `log` | 要遍历提交图（rev-list） | 没用（图形遍历仍是 1 spawn） |

所以 P4 的收益上限不是「所有读端点」，是**只读对象的那一类**。

### 12.15.2 结果（干净机器上测的）

| 操作 | 改前 | 改后 | spawn |
|---|---:|---:|---|
| `/api/file?file=big.txt` | 42.8ms | **14.6ms** | 1→0 |
| `/api/history/tags` | 45.5ms | **0.9ms** | 1→0 |
| `/api/history/reflog` | 41.9ms | **8.8ms** | 1→0 |
| 读 13 项合计 | 364.8ms / 7 | **282.3ms / 4** | −82.5ms |
| 全表 22 项 | 1,717.7ms / 32 | **1,621.8ms / 29** | −95.9ms |

⚠ **上一轮那组「改后全线 3 倍慢」的数字（blame 79→215、status 50→178）是在后台
跑全量变异基线时测的，作废。** 测量必须独占机器 —— 这次重测才得到上面的数。

### 12.15.3 复现的是 git 的格式化语义（实测钉住的规则）

`tags`/`reflog` 不是「取字节」，是「按 git 的规则排版」。四条规则都是实测出来的：

1. **subject = 第一段**：取到第一个空行为止，行间**一个空格**连接。
   `"L1\nL2\n\nbody"` → `"L1 L2"`（`log --pretty=%s` 与 `%(contents:subject)` 一致）；
2. **creatordate**：注解 tag 取 **tagger** 时间，轻量 tag 取 **committer** 时间；
   `%(creatordate:short)` = 记录里那个时区下的 `YYYY-MM-DD`；
3. **`%h` 宽度** = `max(7, 最短唯一前缀)`，`core.abbrev` 可覆盖（=N 或 `no`）。
   唯一性判定**复用 git 自己**：`cat-file --batch` 对歧义前缀答 `ambiguous`，
   对目标对象答 `ok` —— 自己扫对象库既慢又可能与 git 规则漂移；
4. **打平顺序**：同秒创建的多个 tag，git 是「按 refname 排好序后**稳定**排序」，
   打平保持字典序（实测 ann / light / multi 三条同秒，输出就是字典序）。

### 12.15.4 回退矩阵（每一格都是「交回 git」，不是猜）

`.git` 不是目录（worktree）｜消息非 UTF-8｜commit/tag 带 `encoding` 头｜注解 tag
缺 `tagger`｜tag 指向 blob/tree｜标签数 > 500（要全读才能排序）｜`core.abbrev`
写在 `include` 里（不解析 include）｜`cat-file` 进程起不来/中途死掉（重建一次，
再失败就回退）｜单个对象 > 128MB。

### 12.15.5 代价（P4 不是白拿的）

**常驻进程按仓持有目录/包文件句柄**。在 Windows 上直接后果是：删这个仓的目录会
`WinError 32` 失败。这不是理论问题 —— `test_history_ops` 跑出 **37/37 全过、rc=1**，
原因就是它自己的清场删不掉测试仓。

修法：`gitcat.close_all()` 显式关闭，并给会删临时仓的套件在 `tearDownClass` /
清场前调用。**记在这里的原因是**：以后任何「删仓/搬仓」的路径都要先想到它；
反过来，如果哪天发现删除莫名失败，先查是不是常驻进程还握着。

### 12.15.6 判据

| 项 | 结果 |
|---|---|
| `test_object_read.py`（新增） | **5/5**：文本/空/二进制/CRLF/中文空格/分支 ref/缺失路径逐字节一致；tree 仍回退；worktree 回退 |
| `test_history_fast.py`（新增） | **9/9**：tags 与 `for-each-ref` 逐字段相等（含 limit/时区/多行/同秒），reflog 与 `git reflog` 逐字段相等（含 `core.abbrev=10`、作者名带空格、`-0700` 时区、空 reflog） |
| 新增变异靶 5 条 | **5/5 KILLED**（类型谎报 / subject 连接 / reflog 顺序 / 最小缩写长度 / 标签排序），0 VOID |
| 全量变异基线（v17 快照 `377c287`，44 靶，workers=4） | **44/44 KILLED、0 VOID、1,189.1s、rc=0**（`baseline_v17_20261027.json` + `mutlogs_v17/` 74 个日志） |
| 回归 | test_aigit 60/60、conflicts rc=0、history_ops 37/37、index_fastpath 20/20、refstore 19/19、write_inputs 10/10、contract 18/18、f06 9/9、编码门禁 0/0 |

## 12.16 拿真 git 当裁判：160 项差分抓出的 phantom 改动（当轮十四，[实测]）

### 12.16.1 方法

`D:\aigit_mut_tmp\cmp_vs_git.py`：**7 种仓状态 × 23 项检查 × 2 种路径**（快路径 /
用 `GIT_DIR` 强制回退）= **160 项**，每一项的期望值都用**另一条 git 命令**独立算出来
（不是拿 aigit 自己的解析器再算一遍）。

覆盖：status（staged/unstaged/conflicted/branch）、branches、log、tags、reflog、
stashes、file（old/new）、diff（工作区/暂存）、conflict（state/list）、blame
（行号+sha+作者）。仓状态：clean / dirty（暂存+未暂存+未跟踪+忽略+删除）/
branches（松散+中文）/ detached / unborn / conflict / stash。

**结果：160/160 通过**（修复后）。

### 12.16.2 它抓到的真缺陷：CRLF 仓里的 phantom 改动

第一次跑，`stash` 那组报：

```
status.unstaged   aigit: ['a.txt']    plain git: []
diff.a.txt.0      aigit: 整个文件重写  plain git: 空 diff
```

根因链（三段，缺一段都复现不出来）：

1. 这台机器的 **system** gitconfig（`D:/cangku/Git/etc/gitconfig`）默认
   `core.autocrlf=true`（Git for Windows 的默认值）；
2. plain git 因此把工作区检出成 **CRLF**、索引里存 **LF**；
3. aigit 的 `gitguard.safe_env()` 用 `GIT_CONFIG_NOSYSTEM=1` +
   `GIT_CONFIG_GLOBAL=<不存在>` **把 system/global 配置整个屏蔽**（安全需要），
   于是 git 看到的 `core.autocrlf` 是未设置 → 原始字节比较 → **CRLF ≠ LF → 报已修改**。

这不是显示小毛病：**通过 aigit 提交会把 CRLF 写进对象库**，与用户 git 的历史不一致。

### 12.16.3 修法：只透传「影响结果、不能执行程序」的 core.*

`gitguard.semantic_overrides(root)`：一次 `git config --get-regexp`（用**用户环境**，
per-root 缓存）+ `apply_semantic(env, root)` 通过 `GIT_CONFIG_COUNT/KEY/VALUE` 注入
（优先级最高、位置无关，且不占 argv）。

透传（只改比较/输出）：`core.autocrlf`、`eol`、`safecrlf`、`symlinks`、
`ignorecase`、`filemode`、`quotepath`、`precomposeunicode`、`excludesfile`、
`trustctime`、`checkstat`。

**继续屏蔽**（能执行程序）：`hooksPath`、`filter.*`、`gpg`、`sshCommand`、`pager`、
`editor`、`fsmonitor`、`attributesFile`、`uploadpack.packObjectsHook`、`gitProxy`。

三处都接了线：`server._run_full`、`history_ops._run`、`conflicts._run`（后两处
`git diff` 同样比较工作区与索引，不接就会显示「整个文件都改了」）。

### 12.16.4 为什么以前没发现（诚实记录）

之前的性能电池与套件仓里**没有 CRLF 文件**（测试数据一律 `newline="\n"`），
所以这条缺陷一直没被触发。**是「换一个裁判」——用真 git 对同一份仓做差分——
才让它现形**，而不是又加了几条自证式的断言。

这也解释了一个方法论问题：同一批断言反复跑，只能证明「没改坏」；
要发现「一开始就错」，必须换一个**独立的裁判**。

### 12.16.5 判据

新增 `test_vs_git.py`（4 条）：

| 用例 | 作用 |
|---|---|
| `test_crlf_worktree_is_clean_for_both` | 阳性：CRLF 工作区 + `autocrlf=true` → 两边都必须干净 |
| `test_negative_control_autocrlf_false_reports_modified` | **阴性对照**：同样的 CRLF 工作区 + `autocrlf=false` → 两边都必须报已修改（否则「永远报干净」也能让阳性全绿） |
| `test_fast_path_matches_git` | status/branches/log/diff/file 与 git 逐项相等（快路径） |
| `test_fallback_path_matches_git` | 同上，`GIT_DIR` 强制回退 |

临时 global 配置用 `GIT_CONFIG_GLOBAL` 指向临时文件模拟，**不依赖本机 system 配置**，
换机器也能跑。变异靶 `S-semantic-config-dropped`（去掉透传）**KILLED**；
全量基线（v18 快照 `c369ab0`，45 靶）：**45/45 KILLED、0 VOID、1,222.4s、rc=0**
（`baseline_v18_20261028.json` + `mutlogs_v18/` 75 个日志）。

### 12.16.6 修后复测

同一份电池：**spawn 数完全不变**（29），墙钟在噪声内（1,621.8 → 1,484.3ms，
主要是 commit 512→469 的抖动）。语义配置只在每个 Repo **首次**调用时问一次，
之后命中缓存；那次调用不经过 `_run_full`，**不进 spawn 计数**（判据数的是业务调用）。

## 12.17 写操作也对齐真 git：同一次提交的 sha 必须逐字相同（当轮十五，[实测]）

### 12.17.1 为什么读面差分不够

§12.16 证明了「**看到的**」一致，但那个 CRLF 缺陷最严重的后果在**写**：
读错了只是显示问题，写错了是**历史污染**（CRLF 进了对象库，之后任何 git 客户端
检出都会多一层转换）。所以必须有第二份裁判：**同一份 fixture 复制两份，
一份走 aigit API、一份走 plain git，然后比最终仓库状态。**

### 12.17.2 方法

`D:\aigit_mut_tmp\cmp_write_vs_git.py` / 仓内 `test_write_vs_git.py`：

* 把 `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` / 身份**固定**（两边同一份 env），
  于是**同一次提交的 commit sha 可以直接比** —— 这比「树一样」严格得多：
  消息、作者、时间任一不同，sha 就不一样；
* 比较项：`HEAD` sha、tree sha、`ls-files -s`（mode/blob/stage/path 全列）、
  每个工作区文件的 sha256、`status --porcelain`（排序后逐行）、分支表；
* 场景：**stage / unstage / commit / discard / checkout / CRLF 提交 / 中文空格路径提交 / 建分支**。

### 12.17.3 结果

| 场景 | 结论 |
|---|---|
| stage（修改 + 新文件） | 状态**逐项相同** |
| unstage（两个已暂存撤一个） | 逐项相同 |
| **commit**（改 + 增 + 删） | 逐项相同，且 **commit sha 逐字相同** |
| discard（已跟踪还原 + 未跟踪删除） | 逐项相同 |
| checkout（切分支） | 逐项相同 |
| **CRLF 提交**（工作区 CRLF + autocrlf=true） | 逐项相同 + **对象库 blob 是纯 LF**（`'a2\nsecond\n'`）+ commit sha 相同 |
| **中文 + 空格路径提交** | 逐项相同 + commit sha 相同（quotePath / pathspec 编码都在这一条上） |
| 建分支（`feat/x`） | 逐项相同 |

**8/8 场景通过**。CRLF 那条同时是 §12.16 修复的**写面证据**：没有 `apply_semantic`
透传时，aigit 提交的 blob 会带 `\r`，这条断言当场红。

### 12.17.4 判据

| 项 | 结果 |
|---|---|
| `test_write_vs_git.py`（新增 8 条） | **8/8**（26.2s） |
| `S-semantic-config-dropped` 挂上写面套件后 | 仍 **KILLED**（读面 + 写面两条都会咬） |
| 回归（12 个套件） | 全绿：aigit 60 / conflicts / history_ops 37 / contract 18 / f06 9 / index_fastpath 20 / refstore 19 / write_inputs 10 / object_read 5 / history_fast 9 / vs_git 4 / write_vs_git 8 |

### 12.17.5 两条方法论（这轮最值得留下的）

1. **「同一次操作的 sha 相同」是最强的一类等价判据**：它把树、消息、身份、时间
   全部包进一个 40 位十六进制串里，任何一处漂移都藏不住；比逐字段断言更难被
   「碰巧通过」。
2. **读面差分不能替代写面差分**。同一个缺陷在两个面上是两种性质：读面是
   「显示错」，写面是「历史被写坏」。只测一边，等于只买了一半的保险。

## 12.18 并发面复核：P4 的共享子进程没引入竞争（当轮十五附录，[实测]）

### 12.18.1 为什么单独跑

P4 引入了一个**跨线程共享的对象**：每个仓一个常驻 `cat-file --batch` 进程 + 一把
会话锁。它不在仓锁（`Repo._lock`）之下，所以串行套件一个都测不到它。

### 12.18.2 结果

`test_aigit_concurrency.py`：**15/15 PASS、0 FAIL、0 SKIP，259.1s，rc=0**。

| 组 | 关键数字 |
|---|---|
| C0 校准 | 产品原样：同域临界区重叠 **0**；注入空锁：**440**（探针非空转） |
| C1 stage × conflict/resolve | 72 ops，0 个 5xx / 0 traceback / 0 跨域重叠 |
| C2 / C2B discard × reset --hard | 60 / 60 ops，0 个 5xx、0 个 WinError 共享冲突 |
| C3 checkout/branch × commit | 72 ops，`commit_200=24`，0 个 5xx |
| C4 / C4B / C4C 历史危险操作 | 54 / 48 / 36 ops，0 个 5xx；归属判据 `subject_mismatch=0`、`undo_bad=0` |
| C5 同一文件两请求 | 72 ops，`sha256_mismatch=0` |
| C7 三域混合 | 跨域重叠 **0**（repo 72 / history 216 / conflicts 69 次 git 调用） |
| C10 注入对照 | 不注入 36/36 返回 200；注入空锁+60ms 窗口 → 26 非 200，其中 **20 次 `index.lock` 冲突** |

### 12.18.3 锁扫描的变化

`C9-lock-scan` 的嵌套边与上一版一致（server 3 条、conflicts 1 条、history 0 条）。
新增的语义配置读取（`gitguard.semantic_overrides`）**在 `Repo._lock` 之外**执行
（它问的是「用户的 git 会怎么读这个仓」，不碰仓库状态），所以既没有新增嵌套边，
也不会在持锁期间多一次 spawn。

### 12.18.4 限定（这份结论不能被读大）

这是「在 `<上述组合> × <上述并发度> × <上述轮数>` 下**没找到**」，不是「并发没问题」。
套件自己的 C0/C10 注入对照证明：**判据在真出问题时是会红的**。

## 12.19 契约收口 + 一条命令的门禁 + P4 句柄回收（当轮十六，[实测]）

### 12.19.1 四条契约：口径怎么定、改了什么

§12.9 登记的「登记但未改」四条，这次全部收口。**每一条都先定口径再改**：

| # | 原状 | 口径决定 | 改法 |
|---|---|---|---|
| 1 | `log?limit=0/-1` → 200 + 空数组 | 与同族 `blame` 一致：**夹取** `[1,1000]` | `Repo.log()` 里夹取；`0/-1` 返回 1 条，非整数仍 400 |
| 2 | `diff?file=<从未存在>` → 200 + 空 diff | **三处都没有**才 400（磁盘/索引/HEAD）；删除态必须仍 200 | 新增 `Repo.path_known()`，只在这个条件上拒绝 |
| 3 | `reflog`/`stashes`（以及同样裸的 `tags`）返回数组 | 与其余端点统一成 `{ok:true, …:[…]}` | 服务端包一层；前端 `asArray()` **两种形状都认**，旧服务端也不破 |
| 4 | `PUT/DELETE` → 501（基类默认） | 方法认识、资源不允许 → **405 + `Allow`** | `do_PUT/do_DELETE/do_PATCH` 统一走 `_method_not_allowed()` |

第 2 条的**阴性对照**是关键：把「工作区已删但已跟踪」的情况也判 400，就会毁掉
diff 最有价值的场景之一。`test_api_contract.py::test_diff_deleted_tracked_file_still_200`
专门钉住它（断言 200 且 diff 里有 `deleted file`）。

第 3 条改了三个测试文件的断言（`test_aigit` / `test_refstore` / `test_history_fast`）
和 `web/history-panel.js` 的 `asArray()` —— **前端不需要跟着服务端升级节奏走**，
两种形状都能渲染。

### 12.19.2 `tools/run-all.ps1`：一条命令出红/绿

之前 13 个入口靠人记，漏跑是必然的。现在：

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run-all.ps1              # 编码门禁 + 12 套件 + 并发
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run-all.ps1 -Fast        # 只跑编码门禁 + 12 套件
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run-all.ps1 -WithMutation # 再加变异基线
```

三条设计要点：

* **变异基线不在工作区跑**。`mutation_runner --base-ref HEAD` 的语义是
  `git archive HEAD`，未提交改动**不在里面** —— 在工作区直接跑会静默测到旧代码。
  门禁脚本先把工作区复制成一次性快照仓、本地提交，再在快照里跑；
* **ASCII-only**。PowerShell 5.1 按 ANSI 读无 BOM 脚本，中文会毁掉解析
  （与 `tools/check-encoding.ps1` 同一条规矩，那次我自己踩了）；
* **摘要行有优先级**。取「最后一行」会取到 `清场完成 … 已删除` 这种噪声，所以按
  `PASS/FAIL → passed N of M → killed → encoding issues` 的顺序挑，兜底才取最后一行。

### 12.19.3 P4 的句柄生命周期：空闲回收

§12.15.5 记过：常驻 `cat-file` 按仓持有句柄，Windows 上删仓会 `WinError 32`。
现在 `gitcat` 有：

* `IDLE_SECONDS`（默认 60，可用 `AIGIT_CATFILE_IDLE` 覆盖）；
* 后台 daemon 线程每 `REAP_INTERVAL`（15s）扫一次；
* **只在拿到会话锁时才关** —— 绝不允许 terminate 一个正在读响应的进程；
* `reap(now=None)` 是单次扫描，测试直接调它，不用等 60s；
* `close_for(root)` 给「删仓/搬仓前主动关」用。

代价：冷启动的下一次请求要多起一次进程（~40ms）。这是有意的取舍 —— 句柄泄漏
会让「删仓」这个基本操作失败，比偶发 40ms 严重。

### 12.19.4 判据

| 项 | 结果 |
|---|---|
| `test_api_contract.py`（新增 5 条） | **5/5**：log 夹取（0/-1/2/999999 + 非整数 400）、diff 不存在 400、**删除态仍 200**、三个 history 端点形状、405 + Allow |
| 变异靶（`S-log-limit-clamp-off` / `S-diff-path-known-off` / `S-tags-shape-key-off`） | **3/3 KILLED** |
| `test_object_read.py` 新增 | 空闲回收后**仓目录可删**（句柄已释放） |
| 门禁自跑 `-Fast`（**20 套件**） | **21 步全过，约 610s** |
| 门禁全量（含并发 + 性能预算） | **23 步全过，893s**；变异基线单跑 **48/48 KILLED、0 VOID** |

### 12.19.5 自查发现的三个洞（都补了）

1. **门禁清单漏了 6 个套件**：第一版只列了我这轮常跑的 12 个，
   漏掉 `test_aigit_extra` / `web` / `invariants` / `net` / `gate` /
   `blame_guard_signatures`。补进去之后立刻发现第 2 条。
2. **`test_blame_guard_signatures` 的判据已过期**：它断言 reflog 成功路径
   `spawn == ["reflog"]`，而 P4 之后是 **0**（`.git/logs/HEAD` + 对象解析）。
   这条套件不在门禁清单里，所以 P4 落地两周没人发现。
   **已改判据并写明理由**：原判据守的是「守卫不许多余 spawn」，现在连业务 spawn
   都没了，是更强形态；错误路径的 HTTP 签名与 spawn 顺序由同文件的其它用例继续钉。
3. **`.ps1` 的两条硬规矩**（ASCII-only + CRLF）我自己各踩了一次：中文让 PS 5.1
   按 ANSI 读出语法错，LF 让编码门禁报 advisory。都已修正。

## 12.20 差分矩阵扩展：仓的形态（当轮十七，[实测]）

### 12.20.1 读面：13 种仓 × 23 项 × 快/回退两遍

在 §12.16 的 7 种状态之外，补了**仓的形态**（这类形态最容易让「快路径」与 git 分叉）：

| 形态 | 构造 | 结果 |
|---|---|---|
| 大文件 | 6 万行文本，提交后再全量改 | 一致（file/diff/status） |
| 二进制 | 含 NUL 的 blob，修改后再比 | 一致（diff 走 `Binary files differ`） |
| 暂存改名 | `git mv a.txt a2.txt`（未提交） | 一致（status 的 R 条目 + file 回退到索引版本） |
| 子模块 | 本地仓 `submodule add`，再让子模块工作区变脏 | 一致（status 的 `S.M.` 形态） |
| 稀疏检出 | `sparse-checkout set sub2`（**不开** sparse-index） | 一致（不在 cone 里的文件：file 只给 HEAD 版、diff 空） |
| 符号链接 | `os.symlink` | **SKIP**：本机 `WinError 1314`（需要开发者模式/管理员） |

**合计 275 项检查，0 失败**（12 种形态跑满；符号链接明确 SKIP，不是静默跳过）。

### 12.20.2 写面：10 个场景

新增：**快进合并**、**冲突解决 + 提交**、**大文件提交**、**二进制提交**。

前两个各有值得记的地方：

* 快进合并：固定身份/日期后 **sha 逐字相同**；
* **冲突提交**：aigit 会在「自带消息」后补一行 `Merged: <分支>` ——
  这是**文档化契约**（`doc/aigit-conflicts.md:147`、`:365` 的 `messageSource`），
  不是 bug。所以差分**不比 sha**，而是显式断言：

  ```
  aigit 消息 == git 消息 + "\nMerged: side\n"
  ```

  这样做的两个好处：① 不再把「有意差异」误报成失败；② **这行 trailer 要是哪天
  被悄悄删掉，这条会红** —— 差异从「没人知道」变成「被钉住」。

### 12.20.3 这轮被迫修正的四处「裁判口径」

扩矩阵时红了几次，全是**裁判没跟上契约**，不是产品缺陷 —— 但每一处都值得记，
因为它们说明「差分测试本身也会漂」：

1. **未知路径 400**（§12.19 的新契约）：`diff` 对三处都没有的路径现在 400，
   而老裁判还在比「200 + 空」；
2. **稀疏检出下 blame 400**：文件不在工作区时 `git blame` 自己也失败，
   端点 400 与之一致 —— 裁判必须先看 git 的退出码；
3. **`/api/file` 的索引回退**：HEAD 里没有时端点会退而读暂存区版本（改名/未出生
   场景都会命中），裁判必须模拟这条规则；
4. **门禁摘要行**：取「最后一行」会拿到 `清场完成 … 已删除` 这种噪声，改成按
   `PASS/FAIL → passed N of M → killed → encoding` 的优先级挑。

### 12.20.4 判据

| 项 | 结果 |
|---|---|
| 读面矩阵（scratch `cmp_vs_git.py`） | **275 项 / 0 失败**（12 形态 × 23 项 × 2 路径） |
| `test_matrix_vs_git.py`（新增 6 条） | **6/6**（15.3s，含 symlink 对象读） |
| `test_write_vs_git.py`（扩到 12 条） | **12/12**（25.9s） |
| 门禁 `-Fast`（**20 套件**） | **21 步全过，613s** |

### 12.20.5 覆盖边界（当轮十八更新）

* **符号链接**：磁盘上的链接本机建不了（`WinError 1314`）；已覆盖的是
  **「symlink 以对象形式在 HEAD 里」的读路径**（用 `update-index --cacheinfo 120000`
  造条目）。`core.symlinks` 的「存 blob 还是存 symlink 对象」选择**未验**；
* **文件权限位**：已验**索引层的 mode 变化**（`update-index --chmod=+x` → 提交后
  tree entry 与 plain git 逐字一致）；Windows 上 `core.fileMode` 对工作区的影响未验；
* **子模块写操作**：已验**提交子模块指针**（推进子模块后提交，tree/sha 与 plain git
  相同）；`submodule update` / `sync` 未接入；
* **合并提交的 `Merged:` trailer**：有意差异，已在 §12.20.2 显式钉住。

## 12.21 提交失败必须红 + 性能预算进门禁 + 交付说明（当轮十八，[实测]）

### 12.21.1 又一条「命令返回成功、事情没发生」

`Repo.commit()` 以前这样判断成功：

```python
out = self._run(["commit", "-m", message], check=False)   # 只拿 stdout
if not out.strip():
    raise GitError("没有可提交的改动(或提交被 hook 拒绝)")
```

**没看 returncode**。而 `git commit` 失败时会把 `On branch main / nothing to commit`
打到 **stdout** —— 于是「提交没发生」被当成成功，接口回 **200 + ok:true**。

是扩充 symlink 场景时撞出来的：索引里有一条「工作区没有」的条目，aigit 的提交契约
是「先全量暂存」，`git add` 把它当删除 → 无可提交 → `git commit` rc=1 → 旧代码 200。

修法：改成 `_run_full(..., check=False)` 并**判 rc**；`nothing to commit` 映射成
`没有可提交的改动(工作区与索引都没有变化)`，其它失败透出 git 原文（截断 400 字）。
判据 `test_api_contract.py::test_commit_with_nothing_to_commit_is_400` 同时断言
**HEAD 不动**。

副作用（好事）：并发套件 C10 的基线从「36/36 全 200」变成「6/36 200」——竞态里
那些根本没提交成功的请求，以前被谎报成 200，现在是 400。套件判据（损坏计数）不变。

### 12.21.2 性能预算进门禁

新增 `tools/perf-budget.py`：22 个操作，**spawn 是硬指标**、墙钟是带宽
（读 ≤1/100ms、轻写 ≤2/170ms、discard ≤3、commit ≤8/650ms），超了就让门禁红。

脚本自己踩过一个坑并修掉：写操作重复发同一个请求，第二次会变成「分支已存在 /
没有可提交的改动」的**失败路径**，测出的 spawn 数是错的（branch 1、commit 6）。
现在写操作**只跑一次**并且**断言 200**，非成功路径直接判 FAIL 并打印 HTTP 码。

实测（本机）：读 0.6–79.5ms / 4 spawn；轻写 99–136ms / 2；commit 528ms / 8。

### 12.21.3 交付说明

新增 `doc/aigit-delivery.md`（一页）：怎么一条命令验、性能现状、验过什么、
**已知差异与未覆盖**（`Merged:` trailer、symlink 磁盘语义、权限位、子模块写、
Windows-only、P4 句柄）。它取代「口头交接」。

### 12.21.4 判据与门禁现状

| 项 | 结果 |
|---|---|
| 门禁 `-Fast`（**20 套件**） | **21 步全过，613s** |
| 门禁全量（含并发 + 性能预算） | **23 步全过，893s** |
| 变异基线（v21 快照 `fdd8ab1`，**49 靶**） | **49/49 KILLED、0 VOID、1,440.3s、rc=0** |
| 读/写差分 | 275 项 0 失败 / 12 场景 0 失败 |
| 并发 | 15 组全过（C10 基线因 rc 修复从 36/36 变 6/36，判据不变） |

## 12.22 换语义的第一刀：写响应可以不带 status（当轮十九，[实测]）

### 12.22.1 为什么这一刀是「真省」而不是「搬家」

写操作响应里的 `status` 看起来是刚需 —— 但 UI 的六个写入口
（stage / unstage / discard / commit / checkout / branch）**后面都紧跟一次
`refresh()`**，而 `refresh()` 自己就发 `GET /api/status`。所以响应里那份 status 是
**同一个请求内的重复计算**：一次 status = 1 spawn ≈ 40ms。

### 12.22.2 设计：opt-in，默认契约不变

* 请求体加 `"status": false` → 响应里**没有** status 键，改为 `"statusOmitted": true`；
* 缺省/其它值一律照旧（老客户端不受影响）；
* `commit` / `checkout` / `branch` 在 opt-out 时走**原方法**（`commit()` /
  `checkout()` / `new_branch()`），不调用 `*_and_status` —— 这样那条 status spawn
  是真的不发生了，不是「取了再丢」；
* UI 的五处（`index.html`）已改为带 `status: false`；`applyStatus()` 对缺 status 的
  响应本来就是 no-op，所以不用改它。

### 12.22.3 实测（同一台机器，28 项预算里同轮相对比）

| 操作 | 默认（带 status） | opt-out | 省 |
|---|---:|---:|---:|
| stage | ~136ms / 2 spawn | **97.5ms / 1** | −39ms |
| unstage | ~112ms / 2 | **67.3ms / 1** | −45ms |
| checkout | 99.6ms / 2 | **73.2ms / 1** | −26ms |
| branch | 102.4ms / 2 | **59.3ms / 1** | −43ms |
| discard | 93.7ms / 2 | **90.3ms / 2** | 小（本来就只多一次） |
| **commit** | 514.9ms / 8 | **406.4ms / 7** | **−108ms（−21%）** |

`tools/perf-budget.py` 现在**同时**量两组：默认契约与 opt-out，并各自有预算
（opt-out 写：stage/unstage/checkout/branch 1 spawn、discard 2、commit 7）。

### 12.22.4 过程中踩的两个坑（都是「测量/实现顺序」类）

1. **早退放晚了**：`_ok_with_status()` 里我把 `if not _want_status: return ...` 写在
   「先取 status」之后 —— 等于「取完再省略」，spawn 一个没省。挪到函数最前面才对。
2. **预算脚本里的 checkout 400 是产品正确行为**：切换分支会覆盖本地改动时 git 本来
   就会拒绝（`Your local changes ... would be overwritten`）。修的是脚本（改用同树
   分支），不是产品。

### 12.22.5 判据

| 项 | 结果 |
|---|---|
| `test_api_contract.py::test_write_status_opt_out` | 7/7（新增 1 条：opt-out 无 status 键 + **操作仍然生效** + 默认仍带） |
| 变异靶 `S-status-optout-ignored` | **KILLED**（探针走 HTTP：`omitted=1|has_staged=0`） |
| 门禁全量（含并发 + 性能预算 28 项） | **23 步全过，911s** |
| 变异基线（v22 快照 `751ecf9`，**50 靶**） | **50/50 KILLED、0 VOID、1,426.5s、rc=0** |

---

## 12.23 C 版 M1 读面的实测账（当轮二十，[实测]）

**被测对象**：`D:\project\aigit\c\build\aigit.exe`（MinGW-W64 16.2.0 UCRT，静态链接，
741,715 字节）。参考对象是同一仓、同一请求序列下的 `server.py`。

**方法**（`D:\aigit_mut_tmp\bench_read.py`，不在仓库内）：
同一 fixture 仓（400 个文件、2 个提交、5 个注解标签、1 个 stash、1 个脏文件）分别起
Python / C 服务，每个端点 3 轮热身 + 30 次采样，取 **p50**；同时读
`X-aigit-git-calls` 确认 spawn 次数。窗口内无其它负载。

| 端点 | Python p50 | C p50 | C/Python | spawn（Py → C） |
|---|---:|---:|---:|---:|
| `/api/status` | 54.9ms | 63.1ms | 1.15× | 1 → 1 |
| `/api/branches` | 6.7ms | 1.0ms | **0.15×** | 0 → 0 |
| `/api/log?limit=50` | 51.5ms | 57.5ms | 1.12× | 1 → 1 |
| `/api/file?file=f100.txt` | 2.3ms | 1.3ms | **0.59×** | 0 → 0 |
| `/api/diff` | 1.0ms | 0.5ms | **0.53×** | 0 → 0 |
| `/api/history/file?file=f100.txt&limit=20` | 135.5ms | 172.6ms | 1.27× | 3 → 3 |
| `/api/history/blame?file=f100.txt&start=1&end=40` | 48.4ms | 62.2ms | 1.29× | 1 → 1 |
| `/api/history/tags?limit=50` | 1.4ms | 1.0ms | **0.69×** | 0 → 0 |
| `/api/history/reflog?limit=20` | 2.9ms | 2.1ms | **0.73×** | 0 → 0 |
| `/api/history/stashes?limit=20` | 1.7ms | 5.4ms | **3.11×** | 0 → 0 |
| `/api/conflict/state` | 2.9ms | 1.0ms | **0.33×** | 0 → 0 |
| `/api/remote/list` | 50.0ms | 55.8ms | 1.11× | 1 → 1 |

**直读快路径的第一版对照**（同一脚本，改快路径前）：

| 端点 | 改前 C p50 | 改后 C p50 | 改前/改后 |
|---|---:|---:|---:|
| `/api/history/tags` | 63.4ms | 1.0ms | **63×** |
| `/api/history/reflog` | 57.5ms | 2.1ms | **27×** |
| `/api/history/stashes` | 57.6ms | 5.4ms | **11×** |

改前这三个端点各 spawn 1 次 git；改后 0 次（直读 `.git/logs/*`、`.git/refs/tags*`、
`.git/packed-refs`、`.git/config`，对象内容走常驻 `git cat-file --batch`，与 Python
`gitcat.py` 一样不计入调用数）。

**结论与边界**：

- 读面里所有**已经 0 spawn** 的端点，C 都不慢于 Python（0.15×–0.73×）。
- 仍然 1–3 spawn 的端点（status / log / history-file / blame / remote-list），C 比 Python
  慢 11%–29%；差异全部落在 git 子进程的启动/管道开销上，不是解析。这一档属于 M3
  性能线，尚未开刀。
- `/api/history/stashes` 虽然也是 0 spawn，仍比 Python 慢 3.1×（5.4ms vs 1.7ms）；
  绝对差 3.7ms，未定位（待测：怀疑是每条 stash 多一次文件打开）。
- **本表只代表这一台机器、这一个 fixture**；没有做冷启动/大仓/并发下的对比，
  这些数字不能外推。C 版并发面尚未测。

本次判据（同一构建）：

| 判据 | 结果 |
|---|---|
| `c/tests/run_conformance.py`（5 片） | **5/5 通过，395 项全过，100.7s** |
| `cmp_vs_git_c.py` 275 项读面差分（服务端切 C） | **275 项，失败 0**（symlink 场景 SKIP：本机无符号链接权限） |

---

## 12.24 C 版 M2/M3 写面：28 项性能预算实测（当轮二十一，[实测]）

**被测对象**：同一 `aigit.exe`（含 M2/M3 写面）。**方法**：`tools/perf-budget.py`
的同一 28 项、同一 fixture，只把服务端换成 C（`D:\aigit_mut_tmp\perf_budget_c.py`，
`AIGIT_SERVER_CMD`）。阈值用的就是本文 §8.1 的既有预算，没有为 C 放宽。

| 操作 | ms | spawn | spawn 预算 | ms 预算 | 结果 |
|---|---:|---:|---:|---:|---|
| GET /api/status | 62.2 | 1 | 1 | 100 | ok |
| GET /api/log | 61.3 | 1 | 1 | 100 | ok |
| GET /api/branches | 1.2 | 0 | 0 | 100 | ok |
| GET /api/history/tags | 0.9 | 0 | 0 | 100 | ok |
| GET /api/history/stashes | 0.7 | 0 | 0 | 100 | ok |
| GET /api/history/reflog | 11.3 | 0 | 0 | 100 | ok |
| GET /api/diff | 58.3 | 1 | 1 | 100 | ok |
| GET /api/file | 2.9 | 0 | 0 | 100 | ok |
| GET /api/history/blame | 87.0 | 1 | 1 | 100 | ok |
| GET /api/conflict/state | 0.8 | 0 | 0 | 100 | ok |
| GET /api/conflict/list | 10.3 | 0 | 0 | 100 | ok |
| GET /api/remote/capability | 0.5 | 0 | 0 | 100 | ok |
| GET /api/health | 0.6 | 0 | 0 | 100 | ok |
| POST /api/stage | 166.9 | 2 | 2 | 170 | ok |
| POST /api/stage (dirty big) | 135.9 | 2 | 2 | 170 | ok |
| POST /api/stage (second round) | 122.5 | 2 | 2 | 170 | ok |
| POST /api/unstage | 143.2 | 2 | 2 | 170 | ok |
| POST /api/checkout | 143.5 | 2 | 2 | 170 | ok |
| POST /api/branch | 138.9 | 2 | 2 | 170 | ok |
| POST /api/merge | 133.3 | 2 | 2 | 170 | ok |
| POST /api/discard | 68.2 | 1 | 3 | 170 | ok |
| POST /api/commit | 644.5 | 8 | 8 | 650 | ok |
| POST /api/stage (no-status) | 105.2 | 1 | 1 | 140 | ok |
| POST /api/unstage (no-status) | 68.0 | 1 | 1 | 140 | ok |
| POST /api/checkout (no-status) | 74.3 | 1 | 2 | 140 | ok |
| POST /api/branch (no-status) | 77.6 | 1 | 1 | 140 | ok |
| POST /api/discard (no-status) | 70.7 | 1 | 2 | 140 | ok |
| POST /api/commit (no-status) | 496.1 | 7 | 7 | 600 | ok |

**结果：28/28 在预算内。**

**补快路径前**是 **7 项超**，全部是 spawn 数：

| 项 | 补前 | 补后 |
|---|---|---|
| `GET /api/conflict/list`（空清单） | 1 spawn（>0） | **0** |
| `POST /api/stage` | 3 spawn（>2） | **2** |
| `POST /api/unstage` | 3 spawn（>2） | **2** |
| `POST /api/stage (no-status)` | 2 spawn（>1） | **1** |
| `POST /api/unstage (no-status)` | 2 spawn（>1） | **1** |

做法：新增 `c/src/indexfile.c` 直读 `.git/index`（v2/v3，含 stage 位；v4 /
split-index / reftable 一律回退），`stage/unstage` 的 tracked 集合不再跑
`git ls-files`，`conflict/list` 在「索引里没有 stage≠0」时直接返回空清单。
**补的过程中真踩到一个双重释放**：`ss_add()` 接管字符串所有权，最初把 index
缓冲区里的指针直接传进去、随后又 `idx_free()` —— 150 个写面用例里 63 个
`ConnectionReset`。改成传副本后 122/122 恢复。

**边界**：`commit` 644.5ms 距 650ms 预算只剩 5.5ms（8 spawn 的固定成本）；
预算本身是 Python 版定的，C 版「过预算」只说明没有回退，不代表比 Python 快。
这些数字同样只代表这台机器与这套 fixture。

---

## 13. 变更记录

- **v24（2026-11-03 当轮二十一，C 版写面性能预算）**：新增 §12.24。28 项预算对 C 全过；
  补 `.git/index` 直读后 7 项 spawn 超支归零（stage/unstage 3→2、conflict/list 1→0）。
  登记补快路径时引入并修复的双重释放。

- **v23（2026-11-03 当轮二十，C 版 M1 读面性能实测）**：新增 §12.23。C 版 17 个读端点
  与 Python 版逐项相等（conformance 395 项 + 275 项 git 差分）；直读快路径让
  tags/reflog/stashes 从 57–63ms 降到 1–5ms。剩余 1–3 spawn 的端点慢 11%–29%，
  归 M3。数字只代表本机 + 400 文件 fixture。

- **v22（2026-11-02 当轮十九，写响应可选省略 status）**：新增 §12.22。
  ① UI 的六个写入口后面都紧跟 `refresh()`（它自己发 `GET /api/status`），所以写响应
  里的 status 是**同一请求内的重复计算**（1 spawn ≈40ms）。新增 opt-in：
  body 带 `"status": false` → 响应省略 status、标 `statusOmitted:true`；默认契约不变。
  ② `commit`/`checkout`/`branch` 的 opt-out 走原方法（不调用 `*_and_status`），
  spawn 是真的不发生。UI 五处已改。
  ③ 实测：stage 136→97.5、unstage 112→67.3、checkout 99.6→73.2、branch 102→59.3、
  **commit 514.9→406.4（−21%）**。`perf-budget.py` 现在同时量两组（28 项）。
  ④ 坑两处：早退写在「取 status」之后等于没省（已挪到最前）；预算脚本里的
  checkout 400 是产品正确行为（脏树切分支被拒），修脚本不是修产品。
  ⑤ 判据：`test_write_status_opt_out`（opt-out 无 status 键 + 操作仍生效 + 默认仍带）、
  变异靶 `S-status-optout-ignored` KILLED；门禁全量 23 步 911s；
  变异基线（v22，50 靶）**50/50 KILLED、0 VOID**。
- **v21（2026-11-02 当轮十八，提交 rc 修复 + 性能预算门禁 + 交付说明）**：新增 §12.21。
  ① **真缺陷**：`commit()` 只看 stdout、不看 rc —— `git commit` 失败时把
  "nothing to commit" 打到 stdout，于是「没提交」被回 200 + ok:true。
  已改为判 rc；`nothing to commit` → 400 且 HEAD 不动（有断言）。
  并发套件 C10 基线因此从 36/36 变 6/36（以前是谎报）。
  ② `tools/perf-budget.py` 进门禁：22 操作、spawn 硬指标 + 墙钟带宽；
  写操作单跑且断言 200（否则会测到「分支已存在/无可提交」的失败路径）。
  ③ `doc/aigit-delivery.md` 一页交付说明（怎么验、性能现状、验过什么、已知未覆盖）。
  ④ 补覆盖：子模块指针提交、索引层 mode 变化、symlink 对象读（磁盘 symlink 仍不可验）。
  ⑤ 变异基线（v21，49 靶）**49/49 KILLED、0 VOID**（新增 `C-commit-rc-ignored`）。
  ⑤ 门禁现在 **20 套件**：`-Fast` 21 步 613s；全量（含并发+性能预算）23 步 893s。
- **v20（2026-11-02 当轮十七，差分矩阵扩到仓的形态）**：新增 §12.20。
  ① 读面补 5 种形态（大文件 / 二进制 / 暂存改名 / 子模块 / 稀疏检出），
  合计 **275 项 / 0 失败**；符号链接因本机 `WinError 1314` **明确 SKIP**。
  ② 写面补 4 个场景（快进合并 / 冲突解决+提交 / 大文件提交 / 二进制提交），
  `test_write_vs_git.py` 扩到 **12/12**。
  ③ **发现一处「有意差异」**：冲突提交在自带消息后会补 `Merged: <分支>`
  （文档化契约，doc/aigit-conflicts.md:147）。差分不比 sha，而是**显式钉住**
  这行 trailer —— 它要是被删掉，测试会红。
  ④ 扩矩阵时修正了四处**裁判口径**（未知路径 400、稀疏下 blame 400、
  `/api/file` 的索引回退、门禁摘要行），全部记在 §12.20.3。
  ⑤ 判据：`test_matrix_vs_git.py` 6/6、门禁 `-Fast`（20 套件）**21 步全过 613s**。
- **v19（2026-11-02 当轮十六，契约收口 + 门禁 + 句柄回收）**：新增 §12.19。
  ① §12.9 登记的四条全部收口：`log` limit 夹取到 `[1,1000]`；`diff` 只在
  「磁盘/索引/HEAD 三处都没有」时 400（**删除态仍 200**，有阴性对照）；`reflog`/
  `stashes`/`tags` 统一成 `{ok:true,…:[…]}`（前端 `asArray` 两种形状都认）；
  `PUT/DELETE/PATCH` 从 501 改 **405 + `Allow`**。
  ② 新增 `tools/run-all.ps1`：编码门禁 + 12 套件 + 并发（`-Fast` 跳过并发/变异，
  `-WithMutation` 加变异）。**变异基线在一次性快照仓里跑**——`--base-ref HEAD` 是
  `git archive HEAD`，在工作区跑会静默测到旧代码。脚本 ASCII-only（PS 5.1 读无 BOM
  脚本按 ANSI）。
  ③ `gitcat` 加空闲回收（默认 60s，`reap()` 单次扫描，**只在拿到会话锁时关**），
  解决删仓/搬仓 `WinError 32`；代价是冷启动多一次 ~40ms。
  ④ 判据：`test_api_contract.py` 5/5、新变异靶 3/3 KILLED、空闲回收后目录可删；
  门禁自跑 `-Fast`（18 套件）**20 步全过（591s）**、`-WithMutation` **16 步全过
  （1,899s，变异 48/48）**。
  ⑤ 自查补洞：门禁清单原先漏了 6 个套件；补进去后发现
  `test_blame_guard_signatures` 的判据已过期（P4 让 reflog 从 1 spawn 变 0，
  而该套件两周没被跑过）——已改判据并写明原判据为何不成立（§12.19.5）。
- **v18（2026-10-30 当轮十五，写操作差分）**：新增 §12.17 + `test_write_vs_git.py`。
  ① 同一份 fixture 复制两份（aigit API / plain git），固定身份与日期后比较
  HEAD sha、tree、`ls-files -s`、每个工作区文件的 sha256、status、分支表。
  ② **stage / unstage / commit / discard / checkout 全部逐项相同**，
  且 commit 的 **sha 逐字相同**（比「树一样」严格：消息/作者/时间任一不同就不成立）。
  ③ **CRLF 提交**专项：工作区 CRLF + autocrlf=true 时，aigit 写进对象库的 blob
  是纯 LF —— 这是 §12.16 修复的**写面证据**，也说明「读错=显示问题、写错=历史污染」
  必须分开测。
  ④ 判据：`test_write_vs_git.py` **8/8**（含中文/空格路径提交与建分支）；`S-semantic-config-dropped` 挂上写面套件后
  仍 **KILLED**；全量基线（v19 快照 `ce03432`，45 靶）**45/45 KILLED、0 VOID、1,274.6s**；
  12 个套件全绿；并发面复核 `test_aigit_concurrency` **15/15 PASS**（259.1s，
  注入自检成立：产品 0 重叠 / 注入 440）—— 见 §12.18。
- **v17（2026-10-28 当轮十四，拿真 git 当裁判）**：新增 §12.16 + `test_vs_git.py`。
  ① 建 160 项差分（7 种仓 × 23 项 × 快路径/回退两遍），期望值全部由**另一条 git
  命令**独立算出。
  ② 抓到真缺陷：system gitconfig 的 `core.autocrlf=true` 被 `gitguard` 的
  `GIT_CONFIG_NOSYSTEM=1` 屏蔽 → CRLF 工作区被报成「已修改」（diff 却为空），
  且通过 aigit 提交会把 CRLF 写进对象库。修法：透传「影响结果、不能执行程序」的
  `core.*`（autocrlf/eol/symlinks/ignorecase/quotepath/excludesfile …），
  executable 面继续屏蔽；`server` / `history_ops` / `conflicts` 三处接线。
  ③ 判据：`test_vs_git.py` 4/4（含**阴性对照**：autocrlf=false 时两边都必须报已修改），
  变异靶 `S-semantic-config-dropped` **KILLED**；全量基线（v18，45 靶）
  **45/45 KILLED、0 VOID**；复测 spawn 数不变（29）。
  ④ 诚实记录：这条缺陷以前没被发现，是因为所有测试仓都用 LF 写入 ——
  **换一个独立裁判**才让它现形，而不是再加自证式断言。
- **v16（2026-10-27 当轮十三，P4 常驻 cat-file）**：新增 §12.15 + `gitcat.py` /
  `gitobjects.py`，新增套件 `test_object_read.py`(5) 与 `test_history_fast.py`(9)。
  ① `/api/file` **42.8→14.6ms**、`history/tags` **45.5→0.9ms**、
  `history/reflog` **41.9→8.8ms**，三项全部 1→**0 spawn**；读 13 项
  **364.8→282.3ms（7→4 spawn）**；全表 **1,717.7→1,621.8ms（32→29 spawn）**。
  ② 只做「取一个对象」的端点：`status/diff/blame` 要 git 自己算，`log` 要图遍历，
  P4 对它们没有收益（已写进 §12.15.1）。
  ③ tags/reflog 复现的是**格式化语义**：subject 段落折叠、tagger/committer 的
  creatordate、`%h` = max(7, 唯一前缀)（用 cat-file 的 ambiguous 判定复用 git 自己）、
  同秒按 refname 稳定排序。回退矩阵见 §12.15.4。
  ④ **代价**：常驻进程按仓持有句柄，Windows 上删仓会 WinError 32（`test_history_ops`
  曾 37/37 全过却 rc=1）；加 `gitcat.close_all()` 并在清场前调用。
  ⑤ 作废一组数据：上一轮「改后全线 3 倍慢」是在变异基线抢 CPU 时测的，已在
  §12.15.2 标注作废并重测。
  回归：aigit 60/60、conflicts rc=0、history_ops 37/37、index_fastpath 20/20、
  refstore 19/19、write_inputs 10/10、object_read 5/5、history_fast 9/9、
  contract 18/18、f06 9/9；新增变异靶 5 条 **5/5 KILLED**；全量基线（v17，44 靶）
  **44/44 KILLED、0 VOID**；编码门禁 0/0。
- **v15（2026-10-27 当轮十二，写端点巡检）**：新增 §12.14。
  ① `fuzz_write.py`：31 个 POST 端点 × 119 组畸形载荷，断言「不 500 / 不泄漏
  Python 原文 / **被拒请求无副作用**」，首轮 4 条命中。
  ② **最严重一条**：路径里的 NUL 在 Windows 上被 `Path.resolve()` **静默截断**，
  于是 `unstage`/`discard` 会去动**另一个文件**（discard 即数据丢失）；修在唯一入口
  `resolve_in_root`，碰 Path 之前就拒。
  ③ commit 的 NUL 消息原来「先暂存再报错」→ 校验前移；stash/amend/tag 的消息参数
  收口成 `_need_msg()`（只收 str/None、不许 NUL）；reflog/restore 的类型校验顺序修正
  （原来泄漏 `%d format: ... not NoneType`）。
  ④ 新增 `test_write_inputs.py` **10/10**、变异靶 4 条 **4/4 KILLED**；
  一条探针教训：去掉闸后抛的是原生 ValueError，探针只抓 GitError 会把变异误判成
  ENGINE-ERROR。
  回归：aigit 60/60、conflicts rc=0、history_ops 37/37、index_fastpath 20/20、
  refstore 19/19、contract 18/18、f06 9/9、编码门禁 0/0。
- **v14（2026-10-27 当轮十一，读面 4 项归零）**：新增 §12.13 + `gitrefs.py`。
  ① `conflict/state`（HEAD+ref）、`conflict/list`（索引 stage 位）、
  `history/stashes`（`.git/logs/refs/stash`）、`branches`（`refs/heads`+`packed-refs`）
  改为直读文件：**80.1→2.5 / 52.5→1.4 / 52.8→1.9 / 40.8→1.5ms，全部 0 spawn**。
  ② 顺带修掉 `_gitdir()` 冷缓存时的白付 spawn（`.git` 是目录就不必问 git）；
  电池里那个「0 spawn」是**同进程顺序依赖造成的假 0**，已写进文档。
  ③ **划掉 tags / reflog**：它们的返回要 `%(contents:subject)`、`%an` 等**对象数据**，
  纯 refs 直读做不到（要解 pack = 重写 git）。
  ④ 读 13 项 **635.7→364.8ms（12→7 spawn，−42.6%）**；全表 **1,965.3→1,717.7ms
  （37→32 spawn，−12.6%）**。
  ⑤ 被实测纠正三处：未出生 HEAD 的 `currentBranch` 必须是 `"HEAD"`（rc=128 但
  stdout 有值）；`reversed()[:n]` 不可切片；`test_no_index_falls_back` 这条判据
  本身不成立（已改写并说明理由）。
  判据：新套件 `test_refstore.py` 17/17、新变异靶 4 条 + 迁移 3 条 **7/7 KILLED**；
  回归 test_aigit 60/60、conflicts rc=0、history_ops 37/37、index_fastpath 20/20、
  contract 18/18、f06 9/9、编码门禁 0/0。
- **v13（2026-10-22 当轮十，轻写 3→2）**：新增 §12.12。
  ① `.git/index` 直读取已跟踪集合（`_index_tracked_fast`），**只给 stage/unstage 的
  过滤用**；discard 判错=删掉已跟踪文件（F-02），保留 spawn 并写进 §8.1 例外。
  该退的形态全部回退：v4 / split-index / sparse-index / worktree / `GIT_INDEX_FILE`
  （`cmp_index_tracked.py` 8/8）。
  ② checkout 的 `show-ref` 换成 loose ref + packed-refs 直读，三态设计（看见=True /
  看不见=None 回退 / 永不 False，防 reftable 错杀），拒绝文案逐字不变。
  ③ spawn：stage×3、unstage、checkout **3→2**；全表 **42→37**，写面
  **1,432.7→1,329.6ms（30→25 spawn）**。
  ④ **否掉**「index mtime+size 缓存」这个我上一轮提过的方案：写操作自己改索引，
  缓存键在写路径上必然过期。
⑤ **新增判据**：`test_index_fastpath.py` 20/20（快路径逐名相等 + 回退必须退 +
  5 条 HTTP 契约），已在 `SUITE_PARSERS` 登记；新增变异靶 3 条单跑 **3/3 KILLED**，
  其中 `S-idx-fast-extgate-off` 第一版探针是**等价变异**（split-index 的空路径
  条目先被另一道闸拦住），换 sparse-index fixture 后才咬住 —— 见 §12.12.7。
  ⑥ 全量变异基线（快照 `006586e`，31 靶）：**31/31 KILLED、0 VOID、1,121.6s、rc=0**。
  回归：check_light_ops 17/17、test_aigit 60/60、f06 9/9、conflicts rc=0、
  history_ops 37/37、contract 18/18、test_index_fastpath 20/20、编码门禁 0/0。
- **v12（2026-10-21 当轮九，commit 两刀 + 判据修正）**：新增 §12.11 与 §8.1。
  ① **认账**：电池仓无身份，`POST /api/commit` 一直在测 400 失败路径
  （474.6ms/9 spawn），成功路径实为 **10 spawn / 558.4–611.3ms**；电池与
  语义差分脚本已补仓级身份。
  ② **两刀**：`ls-files -t` 一次拿「已跟踪+未跟踪」（差分 10/10 集合一致）、
  `rev-parse` 的 HEAD 存在性在持锁段只问一次（未出生 HEAD 时 before 复用 tracked）。
  commit **10→8 spawn，611.3→524.9ms（5 轮中位）**；全表 **2,168.7/44 →
  2,032.0/42（−136.7ms）**。
  ③ **判据**：§8 的「spawn ≥4 端点逐个 ≤2」改为 §8.1 分层判据 ——
  硬指标 = spawn 预算（读 ≤2 / 轻写 ≤3 / commit ≤8），墙钟只作软带
  （读 ≤90 / 轻写 ≤170 / commit ≤600）；原「commit ≤300」撤回（哨兵不可拆）。
  回归：test_aigit 60/60、test_f06_guard 9/9、语义差分 3/3、编码门禁 0/0。
- **v11（2026-10-21 当轮八，成本模型）**：新增 §12.10——单条 git 命令实测成本
  （启动税 36.4ms；`commit` 真提交 172.9ms、切分支 107.4ms、add 10 文件 84.5ms）
  与「墙钟 ≈ 36.4×spawn + 本体工本 + 8ms」模型（1/2/3-spawn 端点全部落在模型内）。
  据此判定「全线 ≤100ms」不成立：14 个操作已达标，commit/checkout 的 git 本体就
  超过 100ms，只能换语义；建议改分层判据（读 ≤80 / 轻写 ≤110 / commit ≤300）。**（v12 已按实测重定：读 ≤90 / 轻写 ≤170 / commit ≤600，见 §8.1；commit ≤300 不成立。）**
  数据：`raw_cost.py`、`raw_cost_stateful.py`，rc=0。
- **v10（2026-10-21 当轮七，最终汇总表）**：新增 §12.7.3——全部改动后的 22 操作
  时间表：**2,866ms / 58 spawn → 2,184ms / 43 spawn（−682ms = −23.8%）**，
  读端点 −35%（972→632ms）、写端点 −18%（1,894→1,552ms）；最大改善
  conflict/state −172.5ms、status −53.3ms、reflog −45.6ms。**修正口算错误**：
  §12.7 的「53 次 / 2,120ms / 74%」应为「58 次 / 2,320ms / 81%」，已同步更正；
  逐条 counts 无误，错在手算合计。数据：`ops_timings_orig.json` /
  `ops_timings_v7clean.json` / `ops_timings.json`（两次复跑取中位，抖动列自证）。
- **v9（2026-10-21 当轮六，边界巡检）**：40 例畸形输入巡检 **0 个 500**；
  修两类横向不一致——① 目录当文件：diff / history-file 从 200 空改成 400
  「不是文件(是目录)」（与 /api/file E23 同一句话），blame 从「400 + git 原文 +
  169ms」变「400 + 干净文案 + 1.4ms」；② 参数解析泄漏 Python 原文
  （`invalid literal for int()…`）改成「参数 X 必须是整数」，并修掉两处绕过
  `_qi` 的内联解析（log 的 limit、/api/gen 的 count）。回归 test_aigit 60/60、
  contract 18/18。登记未改 4 项（log 负 limit 静默空、diff 缺文件 200、reflog
  裸数组契约、PUT/DELETE 501），见 §12.9。
- **v8（2026-10-21 基线收口）**：28 变异体全量基线 **28/28 KILLED，0 VOID，
  1,281.3s，rc=0**（快照 `d1115fc` = 当轮工作区；`baseline_wt_20261021.json`，
  summary 独立核对 valid=28/killed=28/survived=0）；因惰性守卫在该快照之后，
  受影响的 3 个 `history_ops.py` 靶另用含惰性化的新快照补跑 **3/3 KILLED**
  （54.7s，`baseline_lazyg_20261021.json`）。干净复测：blame 80.8ms、reflog 42.5ms、
  22 操作 2,239ms/43 spawns（−21.9%）。
- **v7（2026-10-21 当轮五，惰性守卫）**：blame 的 `_require_commits` 移到失败路径
  （成功路径 **2→1 spawn**，错误文案签名逐字不变，锚点 4/4）；reflog 去掉
  `_has_commits`（空仓原本就 200+[]，可观察行为不变，**2→1 spawn**）；
  新增性能契约测试（成功路径恰 1 次 spawn）。回归：history_ops 37/37、
  contract 18/18。**墙钟复测受后台变异基线负载污染，干净数待补**（§12.7.2）。
- **v6（2026-10-21 当轮四，两刀落地）**：① **status v2 快路径进主线**——`status()`
  改单次 `--porcelain=v2 --branch`（移植 E13 解析器；**101.1→54.8ms，2→1 spawn**），
  全部写操作 4→3 spawn；语义判据 = 同仓同时刻 10-fixture 双解析 10/10 SAME
  （`compare_parsers.py`），套件 60/60 + 18/18 + 20/20 + 并发 rc=0；
  ② **conflict/state 6→2 spawn**——`_gd()` 缓存 gitdir + `ORIG_HEAD` 去重复读
  （**260.5→91.9ms**，conflict/list 99.8→45.1 白捡）；
  合计 22 操作 **2,868→2,296ms（−19.9%）**，spawn 58→45，地板占比升至 78%。
  **未跑**：28 变异体全量 + 33 探针（改动触碰 status/合并态，下轮全量须复核）。
  复测数据：`ops_timings_before.json` / `ops_timings.json`。
- **v5（2026-10-21 当轮三）**：新增 §12.7 全操作时间表（13 读 + 9 写 = 22 操作，
  合计 ≈2,868ms，spawn 计费 2,320ms = **81%**）；据此重排 P2（§12.4.1）：
  `conflict/state` 6×rev-parse（92% 是 spawn，含 `_read_gd("ORIG_HEAD")` 重复读
  两遍的纯浪费）> `commit` 9 spawn（`diff` 三遍）> 6 个写操作的 4-spawn 套餐
  （status+symbolic-ref 账单在写面再收一次）。conflicts 路径头部计数**实测关闭**
  （conflict/state 报 6、conflict/list 报 2）。实测来源：`ops_timings.py` →
  `ops_timings.json`，rc=0。
- **v4（2026-10-21 当轮二）**：用户拍板两项并完成——① **计数器覆盖 23→91**
  （方案 a 回调注入：server `_count_hook` + 三工厂注入 + 三模块咽喉点判
  `COUNT_HOOK`；blame 头实测 `2`、reflog `2`、`status 2` 不扰动；四套件
  2/2 + 37/37 + 20/20 + 56/56 全绿；conflicts HTTP 头计数**待验证**见 §12.2.1）；
  ② **blame 守卫旧形态签名已钉**——新文件 `test_blame_guard_signatures.py`
  （HTTP 四例逐字 400 + 错误路径 spawn 基线 1/2 次；unittest，rc=0）。
  实测来源：`probe_signatures.py` → `blame_guard_signatures.json`、
  `blame_error_spawns.json`。
- **v3（2026-10-21 追加轮）**：新增 §12 端点解剖实测（7 端点真账 + 计数器覆盖缺口
  23/91 + 解析非瓶颈 + P2 肉量重划 ≈80ms + blame 键级构成 + /api/file 2.04× 双份回吐）；
  修正两处不准——P1「未动」改为「已存在但只罩 23/91」（§3），blame「全量回吐」
  改为「有 -L 500 截断」（§2.2）；§8 收工线回写三行（§12.8）。
  实测来源：anatomy1.py / anatomy2.py（rc 均 0）+ 两份 out.json。
- **v2（2026-10-21，本文）**：正式版落 `aigit-perf.md`。吸收 20261021 并行初稿全部实测项；
  新增 quick 实测数（status 112.7 / log 54.2 / blame 113.1@178KB / diff 49.6@183KB /
  stage 3000 路径 2773.6ms、校准 PASS、分页当前未挂载实锤）；
  stage 收工线由"空承诺 ≤2s"改为"以实测 2773.6ms 为基线的 ≤2s+拆账待定"；
  blame/diff 大响应体登记为 P5 候选（牵契约，需用户点头）。
- v1（2026-10-21 初稿，并行线落 `aigit-perf-roadmap-20261021.md`）：P0–P5 骨架；
  含两处自我审查更正（206 已修；首版误把它当缺口）。
