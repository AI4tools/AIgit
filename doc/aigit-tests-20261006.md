# aigit 测试基线与本轮修复（2026-10-06）

> **本文是一次实测记录，不是现状保证。** 判据里的每个数字都带命令名 + 退出码 +
> 计数口径；改了代码请重跑，别拿这张表当现状。
> 环境：`HEAD = 2f0f2c1`（另有本轮未提交改动），Python 3.11.0
> (`D:\cangku\python311\python.exe`)，Windows。

---

## 1. 这一天的起点：测试套件第一次被真跑

改之前**没有任何记录表明这些套件被执行过**。第一次实跑的结果：

| 套件 | rc | 实测计数 |
|---|---|---|
| `selftest.py` | 0 | 9 / 9 组 |
| `test_aigit.py` | 0 | 共 60 条 / PASS 60 / FAIL 0 |
| `test_aigit_extra.py` | 0 | 共 21 条 / PASS 20 / FAIL 0 / SKIP 1 |
| `test_aigit_gate.py` | 0 | 共 10 条 / PASS 10 / FAIL 0 |
| `test_aigit_net.py` | 0 | 共 56 条 / 过 56 / 失败 0 |
| `test_aigit_web.py` | 0 | 汇总 43 PASS / 0 FAIL / 1 WARN（共 109 条） |
| `test_aigit_concurrency.py` | — | 本轮未计入（改动前基线） |
| `test_conflicts.py` | 0 | 14 / 14 |
| `test_history_ops.py` | 0 | 共 37 条 / 过 37 |
| `test_aigit_invariants.py` | 0 | 4 / 4 |
| `test_f06_guard.py` | **1** | 共 9 条 / PASS 8 / **FAIL 1** |
| `smoke.py` | **1** | 通过 61 / **失败 1** |

**不给总数**：各家计数单位不同（「组」/「条」/「汇总行」），`test_aigit_web.py`
的「共 109 条」与「43 PASS」也不是同一口径，加总没有意义。

两条红的都在下面第 2、3 节处理。

---

## 2. 修复：`/api/commit` 的响应丢了 `output` 键（真回归，用户可见）

### 2.1 怎么发现的

跑 `smoke.py` 时唯一那条 FAIL：

```
[FAIL] 44  commit 提交
       期望: 200、ok=true、output 非空
       实际: 200 output=None
```

而紧跟着的 45 / 46 都 PASS（新提交确实出现、工作区确实干净）—— **断言的事实与
被测行为互相矛盾**，所以去查响应形状。

### 2.2 证据链（每条都有命令）

| 环节 | 证据 |
|---|---|
| 谁引入 | `git show f155916^:server.py` → `self._json({'ok': True, 'output': out, **REPO.status()})`；`f155916` → `self._ok_with_status(st, err)` |
| 引入提交 | `f155916` 「aigit: 消除并发判据 C3 的四处真缺陷, 并给 E3 兜底补上第一条测试」（`git log -S _and_status -- server.py` 唯一命中） |
| 根因（AST，不是文本） | `out` 在 handler 里被赋值、**Load 出现 0 处**；`_ok_with_status` 两条 return 都不含 `output` |
| 谁受害 | `web/index.html:2095` `esc(d.output \|\| "(git 无输出)")` |
| 谁测出 | **只有 `smoke.py` 1 处**；`test_aigit.py` / `_extra` / `_web` / `_concurrency` / `_gate` 全 **0 处**（`test_aigit_net.py` 那 3 处是 `/api/pull`，另一个端点） |

### 2.3 改法

`server.py` 的 `_ok_with_status` 加 `extra=None`，**两条分支都带**：

```python
def _ok_with_status(self, st=_STATUS_UNSET, err=None, extra=None):
```

- E3 兜底分支也带 —— 提交已经发生了，`output` 不该因为 status 读不出来就丢；
- 撞名时 **status 优先**（`{"ok": True, **extra, **st}`）—— status 是权威视图，
  `extra` 只能加键不能改键。

`/api/commit` handler：`self._ok_with_status(st, err, extra={"output": out})`。

### 2.4 改前 / 改后

| 验证 | 改前 | 改后 |
|---|---|---|
| 阳性对照（直接调方法，不依赖服务器） | — | **4/4**：正常支 `output='OUT-OK'`、E3 兜底支 `output='OUT-E3'`、阴性对照（不传 `extra` 不加键）、撞名 `branch='REAL'` |
| `test_aigit_contract.py` | 11/12 rc=1 | **12/12 rc=0** |
| `smoke.py` 第 44 条 | `[FAIL] … output=None` | `[PASS] … output=[main 4d37b85] smoke: commit 1 file changed, 2 insertions(+)` |
| `smoke.py` 总计 | 61 过 1 败 rc=1 | **62 过 0 败 rc=0** |

---

## 3. 修复：`test_f06_guard.py` 的 T3 判据（经用户授权改判据）

### 3.1 原判据为何不成立

原断言是「把 `escaping_links` 换成 no-op 之后，仓外内容**必须**泄漏」。
它**在本场景下永远不成立**：

```
commit_merge 被拒 = False        拒绝确实不来自这条闸（第一个断言过）
提交成功           8d25c7e guard disabled on purpose, MERGE_HEAD 已清
ls-files 里 escape* = []        空
status            = ?? escape/  未跟踪, 进不了索引
leaked()          = (False, False)
```

原因：`commit_merge` 里有**五道**防线，T3 只拆了第四道。第五道是作用域受限的
显式 `add -- <路径>`，只收 git「已跟踪」视图里的路径，未跟踪的一律不进 ——
它**独立**挡住了泄漏。`conflicts.py` 的注释写明「两道挡的是不同东西, 不是重复」。

所以原判据量的不是「这道闸是否承重」，而是「是否**只有**这一道防线」——
后者按设计就为假。

### 3.2 改成问什么

1. `not refused` —— 换 no-op 后**必须不再拒绝**（原样保留，这才是 T1 非空的证据）
2. `committed` —— 提交**必须真的成功**（少了这条，第 3 条可能是空真）
3. `not (lp or lc)` —— 仓外路径既没进索引、内容也从 HEAD 读不出来

### 3.3 阳性对照（就地做，不靠外部脚本）

「没泄漏」最容易变成恒真 —— `leaked()` 万一坏了也返回 False。所以测试**自己**
造一次真泄漏：手动 `git add -A` + commit 绕过第五道，然后要求 `leaked()` 必须
变成 `(True, True)`。

实测 `leaked()`：`(False, False) -> (True, True)`，**咬住**。

结果：`test_f06_guard.py` 共 9 条 **PASS 9 / FAIL 0**，rc=0（原 8/1 rc=1）。

> **只是分析，未实测**：若第五道（作用域受限 `add`）被拆，第 3 条应变红。
> 没有改 `conflicts.py` 去验这一条。

---

## 4. 新增：`test_aigit_contract.py`（响应形状契约）

补的是「响应形状没人测」这个洞 —— 2.2 节那个回归能溜过去，正是因为这个洞。

```
python test_aigit_contract.py --selftest    # 只跑自证, 0.2 秒, rc=0
python test_aigit_contract.py               # 自证 + 真实端点, 2.6 秒
```

实测：**12 条 / PASS 12 / FAIL 0**，rc=0。钉 6 个端点的必需键
（`health` / `status` / `stage` / `commit` / `checkout` / `branch`）。

三条设计约束：

- **判据不许与被检查对象同源。** 期望键集是**写死的字面量**，依据是
  `git show f155916^:server.py` 的历史 handler 与 smoke.py 实测响应体。
  从现行 `server.py` 自动推导 = 拿被检查者的说明书检查被检查者，永远绿。
- **0 项检查恒真。** 契约条数 `< 6` 直接抛。
- **阳性对照先单跑**：缺 2 键抓到 2 条、键齐 0 条、非对象 1 条、少 `output` 1 条。

### 4.1 踩到的坑：`--sandbox` 只能作用于**空目录**

第一版自己 `git init` 建仓再让 server 带 `--sandbox` 起 → `rc=1` +
`PermissionError: ...repo\.git\objects\55\ce85e...`。
因为 `--sandbox` 会 `shutil.rmtree(root)`（`server.py` 的 `main`），而 git 对象
文件在 Windows 上是只读的。**建仓这件事交给 `--sandbox` 自己干。**

（另一条：那一版我把 server 的 stderr 吞了，白跑一轮才发现原因。stderr 必须落盘。）

---

## 5. 变异测试基线

```
python mutation_runner.py --verify-probes   # 探针可证伪性总检
python mutation_runner.py --all --workers 4
```

| 项 | 实测 |
|---|---|
| 探针可证伪性 | **20 / 20** 在对照上签名逐字正确（13.6 秒） |
| 变异体 | 有效 **15** / KILLED **15** / 存活 **0** / VOID **0** |
| 杀灭率（剔作废） | **100.0%**，分母 = 15 个真正打上去并测出结论的 |
| 杀灭率（含作废） | **100.0%**，分母 = 本次选中的全部 15 个 |
| 用时 | 1393.4 秒，rc=0 |

> **变异测试会先查磁盘余量，不够直接退 4。** 它上一轮就是这么被磁盘杀掉的
> （11/15 结果全丢），现在改成先查先拒。当天 C: 只剩 0.35 GB，把 `TEMP`/`TMP`
> 指到 `D:`（13.75 GB）才跑得起来，**没删任何并行线目录**。

**结论：现有 15 道安全闸全被套件咬住，杀灭率 100%。** 所以加测试不该去补
「哪道闸没测」——那个洞不存在；该补的是**形状/契约**这类没有变异体覆盖的面
（第 4 节那个回归就是这么漏的）。

---

## 6. 改后全套件复跑

| 套件 | rc | 计数 |
|---|---|---|
| `test_aigit.py` | 0 | 共 60 条 / PASS 60 / FAIL 0（196.1s，含 `ok E3-status-fail-still-200`）|
| `test_aigit_extra.py` | 0 | 共 21 条 / PASS 20 / FAIL 0 / SKIP 1 |
| `test_aigit_net.py` | 0 | 共 56 条 / 过 56 |
| `test_aigit_web.py` | 0 | 汇总 43 PASS / 0 FAIL / 1 WARN（共 109 条）|
| `test_history_ops.py` | 0 | 共 37 条 / 过 37 |
| `test_conflicts.py` | 0 | 14 / 14 |
| `test_aigit_gate.py` | 0 | 共 10 条 / PASS 10 |
| `test_aigit_invariants.py` | 0 | 4 / 4 |
| `smoke.py` | 0 | 通过 62 / 失败 0 / 跳过 0 |
| `test_f06_guard.py` | 0 | 共 9 条 / PASS 9 |
| `test_aigit_contract.py` | 0 | 共 12 条 / PASS 12 |
| `selftest.py` | 0 | 9 / 9 组 |

**12 个套件，没有一个 rc≠0。**

改动涉及的文件（`server.py` 的 `_ok_with_status` 被 commit/checkout/branch 三个
端点共用，所以三个都要复跑）全部过 aigit 自己的编码门禁
`tools\check-encoding.ps1 -Root tools\aigit`：无 BOM / 纯 LF / 无 U+FFFD / 尾部 LF。

门禁整体 rc=1，但 2 项**都不是本轮碰的文件**：
`server_base.py`（UTF-16LE，既有死副本）、`test_aigit_web.py`（UTF-8 BOM，既有）。

---

## 7. 本轮我自己犯的错（都有当场证据）

1. **`--sandbox` 用在有 git 对象的仓上** → rc=1，而且我**把 stderr 吞了**，
   白跑一轮。stderr 必须落盘。
2. **`git status --porcelain` 的 if 判断写反**：空输出 = 干净，我却先打印了
   「有未提交改动」。
3. **找编码门禁用 `-Recurse` 全树** → 900 秒超时；门禁支持 `-Root`，
   限定到 `tools\aigit` 只要 **3.1 秒**。
4. **粗口径找测试缺口，三次被推翻**：先说「9 个端点没测过」，逐个查下去 8 个
   其实都测过（口径把带 query 的端点判成没出现）；又说 `/api/history/stash/pop`
   没测，底层其实在 `test_history_ops.py` 里测了。**aigit 现有测试比看上去密得多。**

---

## 8. 与本文无关但必须记的一笔

`C:\Users\Public\tmp\opencode\agverify`（本文件的临时工具目录）在 2026-10-06
**被整体删除**，含 `INDEX.md`、`write_ledger.py`、`wait_settle.py`、
`ast_inventory.py` 与当日实测日志。

排查结论（证据在 `CLEANUP-manifest-20261006.json`）：

- 该目录在清单里是 **`"decision": "keep", "reason": "在 KEEP 名单里"`**
  （41,803,529 B / 47,073 文件）——**计划保留**。
- 执行删除的 `cleanup_c.py` 的目标是 `man['delete'] ∩ content_confirm`，
  **keep 名单在删除时根本没被读**；而本目录**只出现在 keep 里**，结构上不可能
  被它删。`cleanup_retry.py` 只删 `man['failed']`，同样不含它。
- 清单写于 **12:40:53** 且记录了本目录的存在 ⇒ 删除发生在 **12:40:53 之后**。
- 该目录 12:40:53 之后被改动的 `.py` 里，**没有任何做目录删除的**
  （`ledger_patch_2516.py` 命中 "rmtree" 只在 markdown 字符串里）。

⇒ **删除来自这套清理链之外。** 查不到是谁，不猜。
教训：实测数据落盘在 `C:\Users\Public\tmp` 不安全，本文因此放在仓内 `doc/`。

---

## 9. 第 ⑤ 项：给第五道防线补变异体（2026-10-06 收口）

`mutation_runner.py` 的 `SUITE_PARSERS` **缺 `test_f06_guard.py`** 条目，
所以 `C-merge-add-scope-off` 第一次跑直接 `KeyError` → `ENGINE-ERROR`
（变异本身 `LANDED`，探针 cert `control=leaked=0 / mutant=leaked=1`，是真变异，
只是引擎不会读它的输出）。补齐：

- 加解析器：用例行 `^(ok|FAIL|skip)\s+(?P<nm>\S+)`（**行首无缩进**，
  与 MARK_RE 不同）；summary `# 共 N 条  PASS n  FAIL n  SKIP n`。
  ⚠ `base` 行是基线测量，**不进汇总**，解析器必须排除。
- 加 fixture（引擎规矩：没有 fixture 不算修好）。fixture 里**故意放一条 `base` 行**，
  若被算进去 `parsed_total` 会变 6 而 summary 说 5 → 交叉校验立刻红。
- `--selftest-parsers`：**10/10 份 fixture 通过；覆盖 9/9 个套件**，rc=0。

**决定性实验（各 `--workers 1`，两次的变异 sha 都是 `e2109a4ca748 -> b0dcf9543cf8`）：**

| 运行 | 用的 T3 | 结果 |
|---|---|---|
| 不带 overlay | **已提交的旧 T3**（断言「防线关掉后**必须**泄漏」）| **SURVIVED**，有效 1 / 杀 0 / 存 1 = **0.0%** |
| `--overlay-test test_f06_guard.py` | 我改的**新 T3**（断言不泄漏）| **KILLED**，有效 1 / 杀 1 / 存 0 = **100.0%** |

杀它的断言原文（带 overlay 那次日志）：

```
FAIL T3   第四道已拆, 仓外路径却还是进了索引或进了历史 ——
**第五道(作用域受限 add)没有独立挡住**, (leaked_path=True leaked_content=True)
# 共 9 条  PASS 8  FAIL 1  SKIP 0  用时 41.5s
```

⇒ 这两行合起来就是「旧 T3 对第五道防线是盲的」的**行为证据**，
不是我从源码推的。**代价**：T3 修复提交之前，`--all` 会显示 15/16
（`--base-ref` 取的是已提交的测试文件）；这是真事实，已写进 README，不粉饰。

---

## 10. 第 ⑦ 项：把「找测试缺口」的方法换掉（2026-10-06）

第 7 节第 4 条记的那三次翻车，根因是**用 grep 找覆盖**。新工具
`find_test_gaps.py`（16,055 B / UTF-8 无 BOM / 纯 LF / 尾部 0x0A / U+FFFD=0）
逐条堵：只认 AST；面来自**真跑一遍 server**（`--sandbox`，不碰真实仓）；
自证组 8 条含阳性/阴性/注释哨兵。

**自证（`--selftest`，rc=0）：8 条 PASS 8 FAIL 0**，其中：
- 阳性哨兵：合成源码里的串必须命中 ✅
- 阴性哨兵：不存在的串必须不命中 ✅
- **注释哨兵：写在注释里的串不许算进来** ✅（这条就是 grep 会翻车的地方）
- 真实仓库层面：`hasStaged` 判 mentioned（阳性）；随机哨兵判 zero（阴性）✅

**真实扫描（rc=0，2.4 秒，输出 90 行）：** 探 14 个端点，10 个返回带键 JSON，
共 **31 个不同的键**参与核对（面守卫 >= 12）。结论：

| 档 | 个数 | 键 |
|---|---|---|
| **零提及**（一定没被测） | **4** | `currentBranch`、`mergeMsgPath`、`origHead`、`trackingRefExists` |
| 灰区（原文有、AST 常量没有） | 1 | `behind`（只在 `test_aigit_net.py` 的**注释**里）|
| 有提及（≠ 被测） | 26 | … |

**独立核实（不信工具的一面之词）：** 对那 4 个键在整仓 grep，命中只有三类地方 ——
产出它们的源码（`conflicts.py:603/606/611`、`gitops.py:620/627`）、
文档（`aigit-conflicts.md:352/353/395`）、以及 `conflicts-panel.js:38` 的
**一行注释**。**没有任何测试文件命中**。⇒ 4 个零提及成立。

**灰区 `behind` 是真阳性，也是本工具价值的直接证据：**
`behind` 在 `test_aigit_net.py` 里出现的地方（L993-995）**全是注释**，
真正的断言是 `ab["ahead"]`（L1033）和 `"basis" in ab`（L1034）——
**没人断言过 `behind` 这个键**。用老办法（grep）会看到「behind 出现过」而判成覆盖，
**这正是它三次翻车的那种误判**。新工具把它挑出来交给人看。

**盲区（如实记）**：若某测试把整个响应 dict 与期望 dict 整体比较
（`ck_eq(resp, {...})`），键会被隐式覆盖却「零提及」。已核查本仓：唯一命中
`ck_eq(offenders, {})` 是比一个 offenders 集合，**不是响应形状** ⇒ 本次未触发。
这个盲区写进了工具 docstring，不假装没有。

**未做**：给这 4 个缺口补测试。`trackingRefExists` 价值最高 ——
`test_aigit_net.py:993-995` 记过一个真 bug：把 `behind=0` 误读，而
`trackingRefExists` 正是消歧义的那个键，却没人断言它。留待用户拍板。

---

## 11. 用第 ⑦ 项的工具补测试，并验证缺口真被堵上（2026-10-06）

先查了一件决定性的事：**这 4 个键有没有前端消费者**（有的话测试该按消费需求写）。
结果：`web/` 下 `currentBranch` 只在 `conflicts-panel.js:38` 的**注释**里出现一次，
`mergeMsgPath` / `origHead` / `trackingRefExists` **零命中** ⇒ **UI 不消费它们**，
是纯信息性 API 面。所以按「形状契约」补，而不是按前端行为补。

### 11.1 契约测试从 6 个端点扩到 8 个

`test_aigit_contract.py` 新增两组必需键 + 两条 CONTRACTS + 各自的自证对照：

- `CONFLICT_STATE_KEYS`（11 键）：依据 `doc/aigit-conflicts.md:352-353` 列出的形状。
  已核实 `conflicts.py` 的 `merge_state()` 是**一个无条件 dict 字面量** ⇒ 11 键恒在。
- `AHEAD_BEHIND_KEYS`（**7** 键）：取 `gitops.py` 两个 return 分支的**交集**。
  ⚠ `note` 只在 `trackingRefExists=False` 时出现（**条件键**），**刻意不进必需集**
  —— 否则在配了跟踪 ref 的仓里会假红。
- `MIN_CONTRACTS` 6 → 8；自证组新增 4 条（两组各自「键集非空」+「缺 1 键必须被抓」），
  理由是「共用同一个 `check_contract`」**不等于**「共用了同一份验证」。

**实测**：`--selftest` → **9 条 PASS 9 FAIL 0**，rc=0；自证 + 真实端点 →
**共 18 条 PASS 18 FAIL 0**，rc=0。8 个端点全部 200 且键齐。

### 11.2 `trackingRefExists` 的**语义**判据（比形状更值钱）

形状只钉「键在」。真正会咬人的是语义：**没推过 ⇒ `behind` 必须是 `None`，不是 `0`**
—— 这正是 `gitops.py:603-604` 记的坑（`behind=0` 被误读成「远程没新东西」）。
`test_aigit_net.py` 的 `b1()`（配了 remote、从没 push，正好是「无跟踪 ref」那条分支）
加了两条：

```python
ck_eq(ab0["trackingRefExists"], False, ...)
ck_eq(ab0["behind"], None, ...)
```

**实测**：`test_aigit_net.py` rc=0，**共 56 条过 56 失败 0**（该套件按**测试函数**
计数，不是按 `ck` 计数，所以加了 2 条断言计数不变 —— 别误读成「没生效」）。

**阳性对照（单独跑，证明这两条判据会响）** —— `D:\aigit_mut_tmp\ctrl_trackingref.py`：

```
实际返回值: {'ahead': 2, 'behind': None, 'trackingRefExists': False}
[PASS] 真值满足判据 (trackingRefExists=False, behind=None)
[PASS] 阳性对照咬住(期望 0 被判 Fail): 阳性对照: 故意期望 0
[PASS] 阳性对照2咬住(期望 True 被判 Fail): 阳性对照2: 故意期望 True
rc=0
```

判据（字面量 `False`/`None`）与被检查对象（真跑出来的 dict）**不同源**。

### 11.3 缺口闭合的**行为证据**

补完测试后重跑 `find_test_gaps.py`（rc=0，85 行）：

| 档 | 补之前 | 补之后 |
|---|---|---|
| 零提及 | **4** | **0** |
| 灰区 | 1（`behind`） | **0** |
| 有提及 | 26 | 31 |

`behind` 的灰区也一并消掉 —— 它现在作为 `AHEAD_BEHIND_KEYS` 的字符串常量进了
AST。**这是「改了而且验证了」**，不是「改了但没验证」。

---

## 12. 给新钉的 2 个端点补**形状变异体**（2026-10-06）

契约测试钉住了 4 个键，但**只有静态断言，没有变异体** —— 也就是说，还没证明
「删掉一个键真的会被咬住」。补两条变异体才算行为化证明。

### 12.1 先量后写：`merge_state()` 在真冲突进行中的实际值

`test_conflicts.py` 的 `t_identify`（L337）本来就有真冲突进行中，`st = r.merge_state()`
在 L344。**先量**（`D:\aigit_mut_tmp\measure_mergestate.py`，rc=0）：

```
ok=True  inMerge=True  mergeHead='e375c475…'  origHead='7b7ae37d…'
head='7b7ae37d…'  currentBranch='main'  branches=['aigit_gen/conflict/0/b']
defaultMessage="Merge branch 'aigit_gen/conflict/0/b'"
mergeMsgPath='…\.git\MERGE_MSG'  remaining=1  files=[1 条]
head == origHead ? True      mergeMsgPath 存在? True   以 MERGE_MSG 结尾? True
```

据此在 `t_identify` 加了 6 条断言（`currentBranch` 是并入方、并入方≠被并入方、
`head == origHead`、`origHead` 是 40 位 sha、`mergeMsgPath` 以 `MERGE_MSG` 结尾
且文件真存在）。**实测**：`test_conflicts.py` rc=0，**14/14 全过**。

### 12.2 两条变异体 + 两条探针

| 变异体 | 靶子 | 编辑 | 探针 |
|---|---|---|---|
| `C-conf-state-key-off` | `conflicts.py` | `merge_state()` 里把 `currentBranch` 改名成 `_mutated_away`（键消失）| `conf_state_keys` |
| `S-ab-trackingref-off` | `gitops.py` | `ahead_behind()` 的「无跟踪 ref」分支删掉 `trackingRefExists` | `ab_trackingref` |

探针都带**非空守卫**（`conflict=1` / `behind_none=1`），否则「没造出冲突」时
三条观测全是空真。`--verify-probes` → **23/23 全 OK**（含这两条新探针）。

### 12.3 决定性实验（各 `--workers 1`）

| 变异体 | 不带 overlay | 带 overlay | 杀它的断言原文 |
|---|---|---|---|
| `C-conf-state-key-off` | SURVIVED，0/1 = 0.0% | **KILLED**，1/1 = 100.0% | `KeyError: 'currentBranch'` |
| `S-ab-trackingref-off` | SURVIVED，0/1 = 0.0% | **KILLED**，1/1 = 100.0% | `KeyError: 'trackingRefExists'` |

不带 overlay 时 SURVIVED 是**真事实**：杀它们的断言都在**未提交**的工作区里，
而 `--base-ref` 取的是**已提交**的测试文件。⇒ 提交前跑 `--all` 会看到
**15/18 = 83.3%**（3 条存活，全部有解释），提交后回到 18/18。

### 12.4 顺带修掉引擎自身的两个真缺陷

`S-ab-trackingref-off` 第一次跑直接 `ENGINE-ERROR: KeyError: 'test_aigit_net.py'`
—— `SUITE_PARSERS` 里没有这个套件（与 `test_f06_guard.py` 那次同一类）。
补解析器时又发现**点计数**的潜在 bug：

- `dot-pass-%d` 用**行内**下标 ⇒ 两行各有 5 个点会生成两套同名，去重后只剩 5 个。
- 实测 `test_aigit_net.py` 的 56 个点散在 **12 行**上，旧写法只会数出 **13**；
  `test_history_ops.py` 恰好把 37 个点挤在**一行**，所以一直没暴露。
- 该套件的点后面**紧跟着**用例自己打的说明文字（`.        逐字段比对通过: …`），
  所以「整行只有点」永不成立 ⇒ 新增 `dots_prefix_are_pass`（数行首连续的点）。

改成**跨行全局计数**。`--selftest-parsers` → **12/12 份 fixture，覆盖 10/10 个套件**。
**阳性对照**（`D:\aigit_mut_tmp\ctrl_dotfix.py`）：同一份 fixture 在新逻辑下
`pass=17 / parsed_total=18 / crosscheck=OK`，在旧逻辑下 `pass=3 / parsed_total=4`
⇒ 会红，所以这份 fixture 确实钉住了那个洞。拿**真实**输出喂解析器：
net `pass=56 parsed_total=56 crosscheck=OK`、history `37/37` 全对。

---

## 13. 修 `core.autocrlf` / 加 `.gitattributes`（2026-10-06）

**问题**：本机 `core.autocrlf = true`，仓库里原先**没有** `.gitattributes`。后果不是
「现在红」，而是**新克隆必红** —— git 会把 `.py/.md/.txt` 全转成 CRLF，而
`tools/check-encoding.ps1` 要求它们是 LF。当时工作区还是 LF 只是因为这些文件
从没被重新 checkout 过（时序运气）。

**反事实实测**（`git checkout-index` 从索引导出到临时目录，有/无那份文件各一次）：

| 文件 | 无 `.gitattributes` | 有 `.gitattributes` |
|---|---|---|
| `test_aigit.py` | **CRLF** | LF |
| `README.md` | **CRLF** | LF |
| `aigit.ps1` | CRLF | CRLF |
| `aigit.bat` | CRLF | CRLF |

**修法**：新增 `.gitattributes`，只钉**门禁真正检查**的后缀
（`.py/.md/.txt`=LF、`.ps1`=CRLF），外加 `.bat`（README 已写明 GBK+CRLF）。
`.html`/`.js` **故意不钉**：门禁不检查它们，而仓里现有 2 个 `.html` 的工作区
形态并不一致（1 个 CRLF / 1 个 LF），加规则会顺手改掉现状。

**验证**：`git check-attr` 属性正确解析；`git ls-files --eol` 显示
`attr/text eol=lf`；`git status` **只多出 `.gitattributes`**，无意外重归一化；
编码门禁复跑 `OK no encoding issues under tools\aigit`，rc=0。

---

## 14. 把 `find_test_gaps.py` 的面扩到 POST（2026-10-06）

**动机**：原先只探 GET，理由是「POST 由契约测试覆盖」。但契约测试只钉**必需键**，
不保证返回里**每个**键都有人管。

**先量后设计**（`D:\aigit_mut_tmp\probe_posts.py`，rc=0）—— 沙盒里 7 个候选 POST
端点的实际返回：

| 端点 | HTTP | 键 |
|---|---|---|
| `POST /api/stage` | 200 | `branch, conflicted, hasStaged, ok, root, staged, unstaged` |
| `POST /api/commit` | 200 | 上面 + `output` |
| `POST /api/branch` | 200 | 同 stage |
| `POST /api/checkout` | 200 | 同 stage |
| `POST /api/merge` | **400** | `error, ok` |
| `POST /api/stash` | 404 | `error, ok` |
| `POST /api/resolve` | 404 | `error, ok` |

**改动**：新增 `PROBE_POSTS`（4 条）+ `PRE_STEPS`（先 stage，commit 才有东西可提）；
`call` 改成 `call(port, path, body=None)` —— body 为 None 就是 GET，否则 POST；
**并读 HTTPError 的 body**（`/api/merge` 的 400 响应体正是要探的 `error` 面）；
新增纯函数 `surface_entry()`（面键带 `GET `/`POST ` 前缀，否则撞同名路径会互相覆盖）。
探的顺序：**GET → PRE_STEPS → POST**（POST 会改仓，放后面才不污染 GET 面）。

**自证组 8 → 13 条**（新增 5 条：记录器阳性/阴性、`call` 读 400 body 阳性、
GET 200 阳性、死端口必须返回 -1 阴性）。`--selftest` → **13/13 PASS**，rc=0。

**实测**（rc=0，5.2 秒，104 行）：探 **18 个端点 / 15 个带键 / 36 个键**，
**零提及 0、灰区 0**。

**键 31 → 36 的归因（实测，不是推测）** —— `D:\aigit_mut_tmp\measure_surface.py`：

| 口径 | 键数 |
|---|---|
| 旧语义（只 GET，不读 4xx body） | **31** |
| 新语义（GET+POST，读 4xx body） | **36** |
| 只在新语义里出现 | `code`, `current`, `error`, `output`, `required` |

⇒ **只有 `output` 来自新加的 POST 端点**；`code`/`current`/`error`/`required`
来自「开始读 4xx body」（`/api/remote/heads` 的 400 响应体）。

**结论（是个有用的负面结果）**：加 POST **没有挖出新缺口** —— `stage`/`branch`/
`commit`/`checkout` 返回的都是 status 形状（契约测试的 `STATUS_KEYS` 已钉住）
加一个 `output`（也已钉住）。**这反过来证明契约测试的 status 形状钉是承重的**：
它同时覆盖了 POST 面。

---

## 15. 全量基线：18/18 = 100.0%（2026-10-06）

`mutation_runner.py --all --workers 4 --overlay-test test_f06_guard.py
--overlay-test test_conflicts.py --overlay-test test_aigit_net.py`
（`--base-ref` 默认 `HEAD`，即 `2f0f2c1`）：

```
有效变异 18  杀掉 18  存活 0  用时 1454s
作废(VOID) 0
杀灭率(剔作废) = 100.0% (18/18)
rc=0，无 ENGINE-ERROR，无 VOID
```

18 条逐条全 KILLED（`S-commit-leak-guard-off` … `U-esc-quote-off`）。
结果落盘 `D:\aigit_mut_tmp\mut\baseline18.json`（+ 增量账 `.jsonl` 18 条），
原始日志 `D:\aigit_mut_tmp\mut\logs18`（43 个文件）。

**注意口径**：这次带了 `--overlay-test`，因为 3 条变异体（`C-merge-add-scope-off` /
`C-conf-state-key-off` / `S-ab-trackingref-off`）杀的是**未提交**的工作区断言。
不带 overlay 的诚实数字是 **15/18 = 83.3%**（3 条存活，全部有解释），
提交后回到 18/18。

---

## 16. 第 19 / 20 / 21 个变异体：AST 普查找真缺口（2026-10-06 晚些时候）

### 16.1 方法：只认 AST，判据是「任何一段字面量」

写 `D:\aigit_mut_tmp\sweep_raises2.py`：用 `ast` 取源码（排除 `test_*`）里
**每一条 `raise` 的字面消息**，再取**所有测试文件**的字符串常量，判据是
「消息被 `%s`/`%d`/标点切成的**每一段**（长度 ≥ 5）有没有出现在任何测试里」，
或者 `raise(..., code='yyy')` 的 `yyy` 出现在测试里。

**为什么 v1 不够**：v1 只比「最长那段」，实测误报 —— `test_aigit_net.py` 的 `a11`
断言的是 `code="net_disabled"`，不是那条中文消息，于是 v1 把联网闸报成零提及。
v2 判据放宽到「任何一段 + code」，**131 条 `raise` → 有提及 50 / 零提及 81**。

### 16.2 排查中收回两条过重判词

| 候选 | 普查说 | 实测真相 | 结论 |
|---|---|---|---|
| `NetGate.require()` | 零提及 | `a11` 断言 `code="net_disabled"` + `extra` 三个字段 | **早已覆盖**，不做 |
| `server.py:1354`「没有可提交的改动」 | 零提及 | `git commit` 把 "nothing to commit" 写在 **stdout**，`out` 永远非空 | **死代码**，拆掉 PROBE-NOOP，不做 |

第二条是**真发现**：那道闸在当前 git 行为下**永远不可能触发**。
探针实测 `commit_nothing` → 对照 `raised=0|commits=1`（不是猜的 `raised=1`）。

### 16.3 选定的 3 条（危害是真的，不是「报错变差」）

| 变异体 | 文件:行 | 拆掉的闸 | 危害 |
|---|---|---|---|
| `C-del-stale-worktree-off` | conflicts.py:1070 | `delete_file` 的 sha256 比对 | 过期客户端**删掉它没见过的内容** |
| `C-unresolve-notresolved-off` | conflicts.py:1115 | `unresolve` 拒「还没解决的路径」 | `checkout --merge` **冲掉用户刚解决好的内容** |
| `G-no-main-branch-off` | gen.py:561 | `gen` 要求 main 分支当基准点 | 在**没有基准分支**的仓上生成场景 |

**为什么不用 `H-stash-clean`**（`history_ops.py:435`）：关掉它只是报错变差 ——
git 自己还会拒（"No local changes to save"），没有可观测的危害差异。
**为什么不用 `S-delete-tree-link-off`**（server.py:881）：实测 PROBE-NOOP ——
`_delete_tree` 顶部还有 `_assert_pinned_plain` 那道**真闸**，L881 只是冗余后挡。

### 16.4 决定性两组实测（各 `--workers 1`）

| 变异体 | 不带 overlay | 带 `--overlay-test test_conflicts.py` |
|---|---|---|
| `C-del-stale-worktree-off` | **SURVIVED** 0/1 | **KILLED** 1/1 |
| `C-unresolve-notresolved-off` | **SURVIVED** 0/1 | **KILLED** 1/1 |
| `G-no-main-branch-off` | **SURVIVED** 0/1 | **KILLED** 1/1 |

杀它们的是 `test_conflicts.py` 新增的 3 条：`t_delete_sha` / `t_unresolve_guard` /
`t_gen_nomain`。该套件 **14 → 17 条全过**（rc=0）。

### 16.5 引擎侧改动

- `mutation_runner.py`：新增 3 条探针 `conf_del_stale` / `conf_unresolve_notresolved` /
  `gen_no_main`。`--verify-probes` → **26/26**。
- `test_aigit_mutation.py`：新增 3 条 `Mutation`。`--list` → **21 个变异体**。

**踩到一个自己的坑（有当场证据）**：前两条变异体第一次跑全是 `VOID/GONE` ——
`Edit(anchor, expect, new)` 里 **`expect` 是「改之前」那行的内容**，
我写成了 `if False:`（和 `new` 一样），于是「唯一命中行不含 expect」。
`locate_edit` 的报错原文：`唯一命中行 1069 含锚点但不含 expect 'if False:'`。
契约写在 `Edit` 的 docstring 里，是我没读就写。

### 16.6 全量基线：21/21 = 100.0%，0 VOID（2026-10-06 晚些时候）

`mutation_runner.py --all --workers 4 --overlay-test test_f06_guard.py
--overlay-test test_conflicts.py --overlay-test test_aigit_net.py`：

```
有效变异 21  杀掉 21  存活 0  用时 1392s
作废(VOID) 0
杀灭率(剔作废) = 100.0% (21/21)
rc=0，无 ENGINE-ERROR，无 VOID
```

结果落盘 `D:\aigit_mut_tmp\mut\baseline21.json`。

**注意口径**：这次带了 `--overlay-test`，因为 3 条新变异体（`C-del-stale-worktree-off` /
`C-unresolve-notresolved-off` / `G-no-main-branch-off`）杀的是**未提交**的工作区断言。
不带 overlay 的诚实数字是 **18/21 = 85.7%**（3 条存活，全部有解释），
提交后回到 21/21。

### 16.7 一条变异体被**移除**（不是保留）

`S-delete-tree-link-off`（server.py:881）一度进了目录，实测 **PROBE-NOOP**：
`_delete_tree` 顶部还有 `_assert_pinned_plain` 那道**真闸**，L881 的
`is_reparse_point` 检查只是**冗余后挡** —— 拆掉它没有任何可观测差异。
已从目录移除，目录回到 **21 条、0 VOID**。

**教训**：探针存在 ≠ 那道闸是承重的。`delete_tree_link` 探针早就 OK，
但**没有任何变异体引用它** —— 这个「探针空转」本身就是缺口信号，
而正确的响应是**去查它测的是哪道闸**，不是直接拿它当变异体。

### 16.8 当前口径

- 提交前跑 `--all` 会看到 **18/21 = 85.7%**（3 条存活，全部有解释）；
  提交后回到 **21/21 = 100.0%**。
- 探针 **26/26**，解析器自测 **12/12 fixture / 10/10 套件**，探针分级 **9/9**。

---

## 17. 第 22 / 23 / 24 个变异体（2026-10-20）

### 17.1 仓搬了家

`aigit` 从 `D:\project\litematica-preview\tools\aigit` 搬到了 **`D:\project\aigit`**。
HEAD 仍是 `2f0f2c1`，21 条未提交一项不少（逐条按内容核对过）。
**副作用**：`tools\check-encoding.ps1` 的 `$Root` 是 `litematica-preview`，
aigit 搬出去之后**那道门不再扫它** —— 我用同一张 `$expect` 表在
`D:\aigit_mut_tmp\enc_gate_aigit.py` 里对 `D:\project\aigit` 复算，结果 **0/0**。

### 17.2 三条新闸（都是「危害是真的」）

| 变异体 | 文件 | 拆掉的闸 | 危害 |
|---|---|---|---|
| `G-dirty-worktree-off` | gen.py | `_check_clean`（有未提交改动就拒） | 往**用户未提交的活**里叠随机内容 |
| `G-branch-clash-off` | gen.py | gen 拒同名分支 | 反复生成互相踩，留下**半成品仓** |
| `C-guard-unavailable-off` | conflicts.py | 拿不到仓外联接防线时转成 `ConflictError` | 拒绝信号**裸奔成 ImportError**，调用方接不住 |

三条在改之前都核对过：`_check_clean` 在**测试与变异体里都零提及**；
「以下分支已存在」零提及；「防线不可用」零提及。

### 17.3 决定性两组实测

| 变异体 | 不带 overlay | 带 `--overlay-test test_conflicts.py` |
|---|---|---|
| `G-dirty-worktree-off` | **SURVIVED** 0/1 | **KILLED** 1/1 |
| `G-branch-clash-off` | **SURVIVED** 0/1 | **KILLED** 1/1 |
| `C-guard-unavailable-off` | **SURVIVED** 0/1 | **KILLED** 1/1 |

`test_conflicts.py` 17 → **20 条全过**。

### 17.4 两条探针第一次都写错了判据形状（有当场证据）

- `gen_branch_clash` 第一版 sig 的是 `raised`（有没有抛）→ **PROBE-NOOP**：
  关掉 clash 闸后 git 自己也会抛 `GenError`，签名没变。
  改成 sig **消息内容**（`"以下分支已存在" in msg`）才分得出来。
- `conf_guard_unavailable` 第一版让 ImportError 裸奔 → 探针自己被打崩，
  引擎报 **`VOID/MUTANT-UNLOADABLE`**。改成**分别 sig 两种异常**
  （`conflict` / `import_error`）才对。

⇒ 与 `C-merge-add-scope-off` 同一个教训：**判据要判「哪条闸拒的」，
不是「有没有拒绝」**。

### 17.5 全量基线：24/24 = 100.0%，0 VOID

```
有效变异 24  杀掉 24  存活 0  用时 1145s
作废(VOID) 0
杀灭率(剔作废) = 100.0% (24/24)
杀灭率(含作废) = 100.0% (24/24)
rc=0，无 ENGINE-ERROR
```

落盘 `D:\aigit_mut_tmp\mut\baseline24.json`。
**不带 overlay 是 18/24 = 75.0%**（6 条存活，全部有解释），提交后回到 24/24。

---

## 18. `find_test_gaps.py` 扩面到**带参 GET**（2026-10-20）

### 18.1 先量后设计：`--sandbox` 只建 `README.md`

`server.py:2184` 实测：`--sandbox` 建的仓里**只有 `README.md`**。
所以原来那条 `/api/diff?file=probe.txt` 探的是**不存在**的文件 ——
吃到的是 400 错误体，不是正常形状。**这是原来就有的盲点，本轮才看见。**

### 18.2 加了 7 条（全部指向真实存在的 `README.md`）

`/api/file` / `/api/diff` / `/api/history/blame` / `/api/history/file` /
`/api/discard/plan` / `/api/reset/plan` / `/api/history/stash`。

**探 25 个端点 / 22 个带键 / 59 个键**（扩面之前是 18 / 15 / 36）。

### 18.3 挖到 3 个零提及键（POST 那次是零收获）

| 键 | 端点 |
|---|---|
| `requested_first` / `requested_end` | `/api/history/blame` |
| `exists_in_worktree` | `/api/history/file` |

灰区另有 3 个：`follow` / `tracked` / `undone_commits`。

### 18.4 值先量后写，再补断言

`D:\aigit_mut_tmp\measure_blame.py` 实测沙盒返回值：

```
/api/history/blame?file=README.md  -> requested_first=1, requested_end=3,
                                      first_line=1, last_line=3, total_lines=3
/api/history/file?file=README.md   -> exists_in_worktree=true, tracked=true,
                                      follow=true, count=1
```

补在 `test_history_ops.py` 的 `t_H2`（blame）与 `file_history` 那条：
**套件 37/37 全过**。

重跑 `find_test_gaps.py`：**零提及 3 → 0，灰区 3 → 2**（`follow` / `undone_commits`）。

### 18.5 口径

- 这是**下界**（只报「AST 字符串常量里零提及」），不反推「已覆盖」。
- 灰区不算零提及 —— 原文里有、AST 常量里没有，需人看。
- 探面**来自真跑 server**，不从源码推导。

---

## 19. 第 25 个变异体 + 编码门禁搬进仓内（2026-10-21）

### 19.1 AST 普查接近枯竭（负面结论，有实测）

又试了 3 个零提及候选，结果**没有一个是「既没被覆盖、危害又真」的新缺口**：

| 候选 | 实测 | 处置 |
|---|---|---|
| `S-resolve-escape-off`（server.py:591 越界闸） | 关掉后 L594 的 `p.relative_to(root)` 抛**未捕获 ValueError**，函数仍然 fail-closed，只是 400 变 500 | **不做** —— 危害是「报错变差」，按自己的判据不合格 |
| `S-index-scope-off`（server.py:1268 暂存区核对） | **不带 overlay 就 KILLED**（223.8 秒） | **不做** —— 早被覆盖，且与 `C-merge-add-scope-off` 同一机制、跑一次 224 秒 |
| `C-merge-unresolved-off`（conflicts.py:1208 拒未解决合并） | **不带 overlay 就 KILLED**（90.7 秒） | **保留** —— 虽然早被覆盖，但它是**独立的闸**（没有别的变异体覆盖它），留作回归守卫 |

⇒ **「零提及」不等于「没被测」**。剩下的 78 条里，大量是被测试断言了**行为**
（只是没断言那句中文消息）、错误路径、或 fail-closed 后挡。
**继续按这个名单捞，性价比已经很低。**

### 19.2 目录：25 条，探针 30/30，全量基线 25/25

`--list` → **25 个变异体**；`--verify-probes` → **30/30**。

**全量基线（带 overlay）**：

```
有效变异 25  杀掉 25  存活 0  用时 1263s
作废(VOID) 0
杀灭率(剔作废) = 100.0% (25/25)
杀灭率(含作废) = 100.0% (25/25)
rc=0，无 ENGINE-ERROR
```

落盘 `D:\aigit_mut_tmp\mut\baseline25.json`。
**不带 overlay 是 19/25 = 76.0%**（6 条存活，全部有解释），提交后回到 25/25。

### 19.3 编码门禁搬进仓内

aigit 搬到 `D:\project\aigit` 后，`litematica-preview\tools\check-encoding.ps1`
的 `$Root` 是 `litematica-preview`，**不再扫 aigit** —— 门禁对它失效。

修法：复制到 **`D:\project\aigit\tools\check-encoding.ps1`**
（`$Root = Split-Path -Parent $PSScriptRoot` = 仓根），自带门禁，不依赖兄弟项目。

**踩到一个坑**：直接复制会**报自己** —— `$expect` 表要求 `.ps1` = CRLF，
而源文件本身是 **LF**（167 LF / 0 CRLF）。转成 CRLF 后 0 advisory。

**实测**：

| 步骤 | 结果 |
|---|---|
| `powershell -File tools\check-encoding.ps1` | **OK，0 advisory，rc=0**，3.8 秒 |
| 阳性对照：塞 UTF-16LE BOM 的 `.py` | `rc=1`，`\_enc_positive_control.py UTF-16LE BOM 6 bytes True` |
| 移除后 | **OK，rc=0** |

⚠ 源文件（`litematica-preview\tools\check-encoding.ps1`）**仍是 LF**，那边跑门禁
会把自己报成 advisory。**那是另一条线的事，本轮没动它。**

---

## 20. 换角度：按端点补**形状变异体**（2026-10-21）

### 20.1 为什么不继续按 `raise` 名单捞

§19.1 已经证明那条路枯竭了。换的角度是：**面**从 25 个端点扩到 30 个，
看新露出来的键里还有哪些零提及 —— 这次盯的是**响应形状**，不是守卫。

### 20.2 先量后设计

`D:\aigit_mut_tmp\probe_plans.py` 实测 11 个未探的只读端点，5 个返回 200
且键很丰富：

| 端点 | 键数 | 状态 |
|---|---|---|
| `/api/history/plan/amend` | 13 | 200 |
| `/api/remote/plan/push` | 13 | 200 |
| `/api/history/plan/revert?sha=HEAD` | 12 | 200 |
| `/api/history/plan/branch-rename?old=main&new=x` | 10 | 200 |
| `/api/remote/plan/fetch?remote=origin` | 10 | 200 |
| `history/plan/stash`、`reset`、`branch-delete`、`tag-delete`、`remote/plan/pull`、`conflict/inspect` | — | 400（前置不足） |

### 20.3 面：30 端点 / 80 键，零提及 8

| 零提及键 | 端点 |
|---|---|
| `aheadBehind` | `/api/remote/plan/fetch` |
| `revRange` | `/api/remote/plan/push` |
| `published` / `old_short` / `old_subject` / `message_changes` / `new_message` | `/api/history/plan/amend` |
| `legacy_op` | `/api/history/plan/branch-rename` |

### 20.4 形状变异体（删键）

| 变异体 | 删的键 | 不带 overlay | 带 overlay |
|---|---|---|---|
| `G-fetch-aheadbehind-off` | `aheadBehind` | **SURVIVED** 0/1 | **KILLED** 1/1 |
| `G-push-revrange-off` | `revRange` | **SURVIVED** 0/1 | **KILLED** 1/1 |
| `H-amend-published-off` | `published` | **KILLED** | — |

**`published` 那条是又一个「零提及 ≠ 没被测」**：测试里根本没写 `"published"`
这个字符串，但删掉它会让 `plan_amend()` 抛 `KeyError`，现有测试直接崩。
**零提及只说明「没断言这个键」，不说明「删了没人管」。**

### 20.5 补的断言（值先量后写）

- `test_aigit_net.py` `e3()`：`aheadBehind` 应是 `dict` 或 `None`
  （有上游跟踪 ref 时是 dict）
- `test_aigit_net.py` `e4()`：`revRange` 应以分支名结尾
  （有上游 ref 时 `origin/main..main`，没有时就是 `main`）

该套件 **56/56 全过**。

### 20.6 结果：零提及 8 → 0

先补 2 个（`aheadBehind` / `revRange`，形状变异体 + `test_aigit_net.py`），
零提及 8 → 5。

再把剩下 5 个补掉 —— **没有**加新变异体，因为这 5 个都是「键存在且值有明确语义」，
补断言就够：

| 键 | 值（沙盒实测，值先量后写） | 补在哪 |
|---|---|---|
| `old_short` | `old_sha[:8]`（`'27aede1f'`） | `test_history_ops.py` `t_C2` |
| `old_subject` | `'init: sandbox'`（HEAD 的标题） | 同上 |
| `new_message` | `None` / `'hello'`（传/不传 message） | 同上 |
| `message_changes` | `False` / `True` | 同上 |
| `legacy_op` | `'rename-branch'`（内部旧名，服务端归一到 URL 段） | `test_aigit.py` `t_n2` |

**零提及 8 → 0，灰区 4。30 个端点 / 80 个键全部有提及。**
`test_history_ops.py` **37/37**，`N-history-routes-wired` 通过。

**全量基线（带 overlay，28 条）**：

```
有效变异 28  杀掉 28  存活 0  用时 1228s
作废(VOID) 0
杀灭率(剔作废) = 100.0% (28/28)
杀灭率(含作废) = 100.0% (28/28)
rc=0，无 ENGINE-ERROR
```

落盘 `D:\aigit_mut_tmp\mut\baseline28.json`。
**不带 overlay 是 20/28 = 71.4%**（8 条存活，全部有解释），提交后回到 28/28。

### 20.7 t128 核对器永久落盘

`doc/_backup/verify_sha_claims.py`（7,275 B），从该位置实跑 **rc=0**。
放在 `_backup/` 的理由：它验的就是 `INDEX.md` 与台账的对应关系，
归档/搬迁时能跟着走；且**不碰 `INDEX.md` 本身**。
