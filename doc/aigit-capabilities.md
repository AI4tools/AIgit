# aigit 能力清单 —— 相较 git 多提供了什么

日期:2026-10-06
对象仓:`tools/aigit`(HEAD `2f0f2c1`)
环境:Python 3.11 / Windows / 本地,全程不联网
口径:本文件里每个数字都注明**怎么量的**。**实测**=当场跑命令得到退出码与输出;
**只是分析**=读代码或读文档推断,没有独立跑通。两者不混。

---

## 0. 一句话

aigit 不是「git 的图形壳」,它在 git 之上加了四层 git 结构上没有的东西:
**破坏性操作的两步闸**(且闸的装法本身可执行审计)、
**把仓库自带 `.git/config` 当代码执行面中和掉**、
**把冲突建模成对象而不是文件里的标记**、
**用变异测试量化「我的测试到底能抓多少东西」**。

下面每一节都给出判据来源。凡是 git 本来就有的,明确标「git 已有」。

---

## 1. 破坏性操作:每个都两步,且「闸真的装上了」是可执行断言

### 1.1 两张登记表(实测,当场 `import history_ops`)

| 表 | 条数 | 内容 |
|---|---|---|
| `DESTRUCTIVE_OPS` | **8** | `amend` / `undo-last-commit` / `undo-dropped` / `delete-branch` / `rename-branch` / `stash-drop` / `tag-delete` / `restore-from-reflog`,每条一对 `plan_*`(只读)+ `do_*` |
| `SAFE_STEPS` | **6** | `stash` / `stash-apply` / `stash-pop` / `revert` / `revert-abort` / `tag-create` |
| `FORBIDDEN_STANDALONE` | **19** | 一步到位的裸动词名单(`amend` `reset` `revert` `restore` `drop` `force` …),出现在类上就报错 |

### 1.2 plan 层函数(实测,全仓 AST)

- 含 "plan" 的函数共 **17** 个;其中严格 `plan_` 前缀 **12** 个。
  - `gitops.py`:`plan_fetch` `plan_push`
  - `history_ops.py`:`_plan` + `plan_amend` `plan_branch_delete` `plan_branch_rename` `plan_restore_from_reflog` `plan_revert` `plan_stash` `plan_stash_drop` `plan_tag_delete` `plan_undo_last_commit`
  - `server.py`:工作区层的 `discard_plan` `reset_plan`,以及分发用的 `_history_plan` `_remote_plan`
- 分母说明:这是**函数个数**,不是「破坏性操作数」。一个 `plan_revert` 被 `revert` 与 `revert-abort` 两条 `SAFE_STEPS` 共用,所以 12 个 plan 函数 < 14 个操作。

### 1.3 闸的可执行审计 `history_ops.audit_two_step()`(实测)

它不查注释,它 `inspect.getsource()` 读每个 `do_*` 的**函数体**,确认里面真的调了
`_require_confirm(`。查五件事:

1. `DESTRUCTIVE_OPS` 每条的 `plan_*` / `do_*` 都存在;
2. 每个破坏性 `do_*` 源码里含 `_require_confirm(`(登记 ≠ 装了闸);
3. `SAFE_STEPS` 的 `do_*` **不该**装闸(装了是过度保守,弹框疲劳);
4. 每个 `do_*` 都被两张表之一覆盖(漏登记 = 危险等级没人审);
5. 没有命中 `FORBIDDEN_STANDALONE` 的裸动词(一步到位的入口)。

基线实跑:`audit_two_step()` 返回 **0 个问题**(rc 见运行)。

### 1.4 四条阳性对照 + 一条阴性(实测,阳性对照故意造违规)

我在真模块 `HistoryOps` 上挂违规函数,确认审计会咬:

| 对照 | 造的违规 | `audit_two_step()` 报的 |
|---|---|---|
| A | 破坏性 `do_` 不装 `_require_confirm` | 报「登记为破坏性但没调 `_require_confirm()`」 |
| B | 裸动词名不在 `FORBIDDEN` 里(如 `probe_reset`) | **0 漏**(阴性:名字不在名单,设计如此) |
| B2 | 裸动词名 `reset`(命中名单) | 报「一步到位的破坏性入口」 |
| C | `SAFE_STEPS` 的 `do_` 装了闸 | 报「登记为安全却装了确认闸」 |
| D | `do_` 两张表都没登记 | 报「危险等级没人审过」 |

每个对照撤掉后都回到基线 0。**结论:这个尺子会响。**
(阳性对照必须真在函数体里写 `_require_confirm`,`inspect.getsource` 才读得到——
所以对照函数必须落成真 `.py` 文件,不能用 `exec` 拼字符串,这是实测踩到的。)

---

## 2. 中和仓库自带的 git 配置执行面 `gitguard.py`(实测)

git 会**执行** `.git/config` 里写着的程序(`filter.*.clean`、`diff.*.textconv`、
`diff.*.command`、`merge.*.driver`、`core.fsmonitor` 等)。aigit 把目录交给不受信
来源(下载/解压/U 盘/共享盘),所以要中和这条面。

### 2.1 实跑三条(当场 `import gitguard`)

- `safe_argv(['diff','HEAD'])` → `['diff','--no-ext-diff','--no-textconv','HEAD']`
  —— 两个 flag **同时**加;只加一个实测仍可被触发。
- `safe_env()` 删掉 `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_EXTERNAL_DIFF` /
  `GIT_DIFF_OPTS`(实测这些键在返回的环境里**不存在**),并把 `GIT_EDITOR=true`、
  `GIT_PAGER=cat` 钉死。删 `GIT_CONFIG_COUNT` 是**必需**不是保险:实测它能注入
  枚举看不见的 driver 名。
- `report(<aigit仓根>)` → `config_keys=9`、`filter/diff/merge/alias` 名全空、
  `parse_error_count=0`,并带回一条 `caveat`:**枚举读不全 = 漏**。
  它**如实**返回解析失败,包括失败本身(`parse_errors` 字段)。

### 2.2 设计原则(引模块 docstring,非我加)

1. **白名单不是黑名单**,`filter.*` / `diff.*` / `merge.*` 子段名任意、git 无通配符,
   只能枚举逐名覆盖;`GIT_CONFIG_COUNT` 还能注入枚举看不到的,所以删环境变量是必需的。
2. 环境变量优先于一切。`-c` 只挡同名键。
3. 每条覆盖都注明「实测过 / 没实测过」。

---

## 3. 冲突是对象,不是文件里的标记(实测)

- `conflicts.ConflictResolver` 有 **10** 个公开方法:`require_merge` `merge_state`
  `inspect` `list_conflicts` `resolve` `choose` `delete_file` `unresolve`
  `commit_merge` `abort`;另有 `class Git`(4 方法)与 `class ConflictError`。
- `parse_markers` 把带冲突标记的文件拆成「公共块 + 冲突块」,返回
  `(regions, skeleton)`;支持三种 style(`merge` / `diff3` / `zdiff3`)但**走同一套
  状态机**——不靠「style 是哪种」判断,所以别人改了 `merge.conflictStyle` 也不会解析错。
  这是 git 没有的:git 只把标记塞进文件,不建模。

### 3.1 逃逸与 TOCTOU(实测读 docstring)

- `escaping_links(root)` 列**在仓里但指向仓外**的 reparse point(目录联接/符号链接)。
- `repolock.repo_git_lock(root)` 给每仓唯一互斥锁(按 realpath 归一),docstring 逐句
  论证锁图无环。
- `escaping_links` docstring 明确:调用点必须**已持有**该锁——扫描与 `add -A` 之间有
  TOCTOU 窗口,没互斥等于没扫。

---

## 4. 变异测试:量化「套件能抓多少东西」(实测)

`mutation_runner.py` 回答:那 7 个套件、几百条断言,到底能抓出多少?「全绿」本身
不证明代码对——断言可能压根没连到它声称测的那段行为。

### 4.1 清单规模(实测,AST 读 `test_aigit_mutation.py` 的 `CATALOG`)

| 口径 | 数 |
|---|---|
| 变异用例(`CATALOG` 条目) | **15** |
| `Edit` 总数 | **15** |
| 不同锚点串 | **15**(无重复) |
| 不同目标文件 | **5**:`server.py`×9、`history_ops.py`×2、`conflicts.py`×2、`gen.py`×1、`web/index.html`×1 |
| `guard_class` 分布 | `safety`×12、`boundary`×1、`logic`×1、`ui`×1 |
| 不同探针 | **15**(每个用例一个) |

15 个全是「把某道守卫关掉」(`*-off` 后缀),所以这套问的是**关掉安全闸,套件会不会红**。
每个 `Edit` 的签名是 `Edit(anchor, expect, new, why='', window=0)`。

### 4.2 三态纪律(引模块 docstring)

每个变异体先自证生效,标成 `LANDED` / `NO-OP` / `PROBE-DRIFT`,**只允许 LANDED 进
统计**。探针带 `expect`,对照签名必须逐字等于它,对不上是 `PROBE_DRIFT`。
**`VOID ≠ SURVIVED`**:锚点没了的变异体单独计数,不混进杀灭率分母。

### 4.3 覆盖映射(`SUITES_BY_TARGET`,实测读到)

`server.py`→4 套件;`conflicts.py`→`test_conflicts.py`;`history_ops.py`→
`test_history_ops.py`;`gen.py`→`selftest.py`+`test_aigit.py`;`index.html` 与
`conflicts-demo.html`→`test_aigit_web.py`。

---

## 5. 测试数据生成器(实测)

`gen.py` + `/api/gen`:按需、确定性地产出三类场景——`conflict`(对照组)、
`mergeable`、`unrelated`、`adddel`。不 import `server.py`,不知道 HTTP 存在,
所以能不起服务直接单测。

`selftest.py` 验证「可合并」时**自己再跑一遍 `git merge`**,不转述生成器自报的
`verified` 字段——生成器自证 + 独立复核,两层都过才算。这是 git 没有的:
git 无法「给我一个必冲突的仓」。

---

## 6. 零依赖 + 可移植分发(实测读 build 脚本)

`build_aigit.py` → `aigit.pyz`(zipapp,需目标机 Python 3.5+)或 `aigit.exe`
(PyInstaller,仅当本机装了才产出)。零第三方依赖是全仓底线(与 `server.py` 一致)。

## 7. 性能层(读模块 docstring)

`aigit_perf.py` 只做三件事:分页、diff 截断、`status` 换单次子进程 v2 解析。
**它不测量任何东西、没有判据**,名字里的 `perf` 指「性能增强层」不是基准测试。
诚实标注:分页**不减少** git 的工作量。

---

## 8. web 层(实测字节数)

`web/` 5 个文件共 **270,639 B**:`index.html` 101,959 / `history-panel.js` 86,761 /
`conflicts-panel.js` 60,701 / `conflicts-demo.html` 14,476 / `virtuallist.js` 6,742。

---

## 9. HTTP API 面(实测,正则抽 `server.py`+`server_base.py` 的 `/api/…` 字面量)

**53 个**不同端点,分组:根级 **18**、`/api/history/*` **19**、`/api/conflict/*` **8**、
`/api/remote/*` **8**。破坏性操作除 `history_ops` 外,`server.py` 的工作区层也走两步:
`/api/discard/plan`、`/api/reset/plan`。

`/api/remote/capability` 探测远端支持什么,`/api/remote/ahead-behind` 算领先/落后——
这两条 git 要靠额外子命令,这里是一等端点。

---

## 10. 一张对照表:aigit 在 git 之上加了什么

| 能力 | git 里有吗 | aigit 的做法 |
|---|---|---|
| 破坏性操作两步 + 闸装上了没 | 无(`git reset --hard` 一步到位) | plan/do 分离 + `_require_confirm` 装在函数体 + `audit_two_step()` 可执行审计 |
| 仓库自带配置当代码执行面 | 无防护(`filter.*.clean` 会被 spawn) | `gitguard` 白名单中和 + 删 `GIT_CONFIG_COUNT` + `report()` 如实报漏 |
| 冲突建模 | 只在文件里塞标记 | `ConflictResolver` 10 方法,`parse_markers` 一套状态机吃三种 style |
| reparse point 逃逸 | 无 | `escaping_links` + `repo_git_lock` 挡 TOCTOU |
| 「我的测试能抓多少」 | 无 | 15 用例变异测试,LANDED/NO-OP/PROBE-DRIFT 三态 |
| 「给我一个必冲突的仓」 | 无 | `gen.py` 三场景确定性生成 + 双层独立复核 |
| 零依赖可移植 | git 本身零依赖 | zipapp `.pyz` / `.exe` |

---

## 附:本文件里哪些是实测、哪些是引用

- **实测**(当场跑命令、有退出码):§1.1 两张表条数、§1.2 plan 函数计数(AST)、
  §1.3 `audit_two_step()` 基线 0、§1.4 四条阳性对照、§2.1 `safe_argv`/`safe_env`/`report`、
  §3 `ConflictResolver` 方法数(AST)、§4.1 `CATALOG` 规模(AST)、§5 生成器结构、
  §8 web 字节数、§9 端点总数与分组。
- **引用模块 docstring**(不是我独立跑通的结论):§2.2 设计原则、§4.2 三态纪律、
  §6 分发形态、§7 性能层定位、§3.1 TOCTOU 论证。这些是代码作者写的,我未逐条复测。
- **不是 git 已有**:上表右列全部是 git 结构上没有的;左列「git 里有吗」依据 git 常识,
  非本机实测。

**未验证 / 待办**
- 联网闸打开时 `uploadpack.packObjectsHook` / `core.gitProxy` 需联网复测
  (本环境不联网,沿用 `doc/aigit-gitguard.md` 的标注)。
- `server_base.py` 是 `server.py` 的死副本(UTF-16LE + 乱码),**AST 不可解析**,
  本清单的所有「实测」都不含它;详见 `doc/aigit-gitguard.md`。
