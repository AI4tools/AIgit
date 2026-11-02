# P2/P3 候选审计:一次请求内可省掉的 git spawn(只读静态审计)

- 日期:2026-10-21。审计方式:**纯读码**。没有跑任何测试、探针或 mutation_runner。
- 范围:`server.py`(do_GET/do_POST 路由表 + Repo 全部方法)、`history_ops.py`(全部 plan/do)、
  `conflicts.py`(Git 封装 / merge_state / inspect / list_conflicts / commit_merge)、`gitops.py`(本地只读面)。
- 行号是审计时刻工作树的快照。并行线正在改 `_run_full`(server.py:487 一带,加计数器),
  行号会漂;每条候选带 **函数名 + git 命令** 两个锚,以锚为准,行号只作起点。
- 成本基准(引用,非本报告实测):Windows 上每次 git 子进程约 40ms(A5/E13 实测,±2.5ms)。
  「可省 spawn × 40ms」即该端点一次请求的下限收益。
- 已排除的假缺口:`Repo._run_paths`(server.py:523-548)已是 `--pathspec-from-file=- --pathspec-file-nul`
  + 路径走 stdin 的单次调用;WinError 206 老结论作废,不再列。
- spawn 计数口径:只有过 `_run_full`/`_run`(server.py)、`HistoryOps._run`(history_ops.py:162)、
  `conflicts.Git.run / cat_batch`(conflicts.py:233/257)、`gitops._run`(gitops.py:471)的调用才算一次 spawn。
  路径校验、lstat、目录 pin、os.walk 等文件系统操作与纯解析不算。

## 「依据」列说明

- `读码确认`:调用链逐行读到过,行号如实。
- `推断`:调用链读到了,但能否省掉依赖未实测的 git 语义(某命令的 rc/输出形态),或需要一次行为改动
  (如「静默 no-op 变 400」)——写明待验点。
- `待核`:信息不足(如 gen.py 正文没读),不下结论。

---

## 一、同请求内重复(同一请求里同一事实被反复 spawn)

| 端点 | 文件:行号 | 调用链(handler → 方法 → git 命令) | 重复/浪费点 | 可省 spawn | 风险 | 怎么验 | 依据 |
|---|---|---|---|---|---|---|---|
| POST /api/history/stash/pop | history_ops.py:525-539 | `do_stash_pop` → `_stash_ref`(:495 `stash list -200`,第 1 次)→ `do_stash_apply` 内 `_stash_ref` 又一次(第 2 次)→ `stash apply --index`(:516)→ `_conflicted_paths`(:517→:562)→ apply 尾 :523 `list_stashes()`(第 3 次)→ `stash drop`(:535)→ pop 尾 :539 `list_stashes()`(第 4 次) | `git stash list` 一个请求跑 4 次。第 2 次与第 1 次是同一事实(同一个 index);第 3 次在 apply 之后取条目数——apply 不删 stash 条目,数必然没变,可复用;只有第 4 次是 drop 之后的**新鲜事实** | 2 | `_stash_ref` 用 limit=200,尾部 `list_stashes()` 用缺省 50,>50 条时口径不一致;记忆化要统一 limit | 计数同一请求里 `stash list` 的条数,期望 4→2;回归 test_history_ops.py 的 pop 组(冲突路径断言) | 读码确认 |
| POST /api/history/stash/apply | history_ops.py:513-523 | `do_stash_apply` → `_stash_ref`(`stash list`,1)→ `stash apply --index`(:516)→ `diff --name-only --diff-filter=U`(:562)→ 尾 :523 `list_stashes()`(第 2 次) | 尾部与开头是同请求重复:apply 不改 stash 条目数 | 1 | stash 被外部进程并发增删时复用值过期;那类改动本来就不受 aigit 锁保护 | 计数 `stash list`,期望 2→1 | 读码确认 |
| POST /api/history/stash/drop | history_ops.py:552-558 | `do_stash_drop` → `plan_stash_drop`(:541-550): `_stash_ref`(list 第 1 次)+ :550 `stash_count=len(self.list_stashes())`(list 第 2 次,同一个 plan 内同事实)→ `stash drop`(:555)→ 尾 :558 `list_stashes()`(第 3 次) | plan 内部对 `stash list` 连跑 2 次;只有 drop 之后那次是新鲜事实 | 1 | 无;两条重复之间只有只读操作 | 计数 `stash list`,期望 3→2 | 读码确认 |
| GET /api/history/plan/branch-delete(POST /api/history/branch/delete 同链再跑一份) | history_ops.py:950-1008 | `plan_branch_delete`: `_branch_exists`(:954 `rev-parse refs/heads/<name>`)→ `list_branches`(:957 `for-each-ref refs/heads`)→ `_is_current_branch`(:966,内 :373 `symbolic-ref --short HEAD`)→ `default_branch`(:975,`for-each-ref refs/remotes/*/HEAD`)→ `_unmerged_commits`(:981):内 :381 `default_branch` 第 2 次 + :382 `_branch_exists(dflt)` + :384 `rev-list --count` + :387 `log -N`;:983、:994、:1006 `default_branch` 第 3/4/5 次;:1005 `_is_current_branch` 第 2 次;:1008 `_branch_sha(name)` 与 :954 同 ref 同命令 | 13 个 spawn 里:`default_branch` 同请求 5 次;`rev-parse refs/heads/<name>`(:954 vs :1008)2 次;`symbolic-ref`(:966 vs :1005)2 次 | ≈6(default_branch ×4 + rev-parse ×1 + symbolic-ref ×1) | `default_branch` 只能**本请求内**复用(remote HEAD ref 被 fetch 改写后跨请求缓存会陈旧);plan 层 :954→:1008 之间只有只读命令,可复用;do 层 `branch -D` 之后的重取是新鲜事实,不许省 | 按命令打点:`for-each-ref refs/remotes/*` 期望 5→1、`rev-parse refs/heads/<name>` plan 内 2→1、`symbolic-ref` 2→1;unmerged_count/blocked/警告文案逐字段比对 | 读码确认 |
| GET /api/history/plan/amend | history_ops.py:567-605 | `plan_amend`: `_require_commits`(:570→:262 `rev-parse --verify --quiet HEAD`)→ `dirty`(:571→:291 `status --porcelain=v1 -z --untracked-files=all`)→ `head_sha`(:572:内 :270 `_require_commits` 又一次 + :271 `rev-parse HEAD`)→ `published_info`(:588:内 :324 第三次 `_require_commits` + :325 `branch -r --contains <sha>`)→ `subject_of`(:602→:279 `show -s --format=%s`) | 「HEAD 存在且 sha 是 X」这一个事实问 4 条 rev-parse(:262、:270、:271、:324);一条 `rev-parse --verify --quiet HEAD` 的 rc+stdout 两问一起答 | 3 | 无;:262→后续之间只有只读命令 | 计数 `rev-parse`,期望 4→1;old_sha/old_subject/警告文案逐字段比对 | 读码确认 |
| GET /api/history/plan/undo-last-commit | history_ops.py:655-699 | `plan_undo`: `_require_commits`(:659)→ `head_sha`(:660,内部 2 条 rev-parse)→ `dirty`(:662)→ `published_info`(:663,内部又 1 条 rev-parse + `branch -r --contains`)→ `parents_of`(:686)→ `subject_of`(:695)→ `_reflog_hint`(:698 `reflog --format=%H`) | 与 plan/amend 同型:HEAD 存在性+sha 重复问 4 次 | 3 | 无;:713 注释:该 sha 是锁外快照,不得升格为权威 | 同上 | 读码确认 |
| GET /api/conflict/state | conflicts.py:586-614 | `merge_state`: `_read_gd("MERGE_HEAD")`(:588)→ `_read_gd("MERGE_MSG")`(:589)→ `list_conflicts`(:597,内 :867 又 `_read_gd("MERGE_HEAD")` 第 3 次)→ :603 同一表达式里 `self._read_gd("ORIG_HEAD")` 出现两次;而 `_read_gd` → `_gd`(:543-549)每次先 :545 `rev-parse --git-dir` | `rev-parse --git-dir` 一个请求跑 5 次(588、589、867、603、603),git-dir 对同一仓是常数;`ORIG_HEAD` 同表达式 2 次读 | 4(--git-dir ×5→1)+ 1(ORIG_HEAD ×2→1) | `--git-dir` 按 ConflictResolver 实例缓存即安全(仓库目录布局常数,跨请求也稳);`ORIG_HEAD` 只做请求内去重 | 计数 `rev-parse --git-dir`,期望 5→1;断言 mergeHead/origHead/remaining 不变 | 读码确认 |
| POST /api/commit(未出生 HEAD,首次提交场景) | server.py:1930→:1336→:1279 | `commit_and_status` → `commit` → `stage_everything_safely`: `_add_scope`(:1108 `ls-files -z`)→ :1326 `before = _index_delta()`: :1234 `rev-parse --verify --quiet HEAD`(失败)→ :1237 `ls-files -z` 回落 → `add`(:1328)→ `assert_index_scope`: :1266 `_index_delta` 又一轮 rev-parse + ls-files | 未出生时同请求 `ls-files -z` 与 `rev-parse --verify --quiet HEAD` 各跑 3 次(同命令);之间只有 FS 遍历/pin/一条 add;rev-parse 三次全失败是可判的 | 4(ls-files ×2 + rev-parse ×2;仅首次提交场景,常规 HEAD 无此项) | after 那份 ls-files 必须保持新鲜(它是 `_assert_index_scope` 的哨兵输入,:1262-1264 注释),只能省 before 一份与全失败的 rev-parse 后两次 | 首次提交场景打点:期望两命令各 3→1;常规 HEAD 场景回归确认无变化 | 读码确认 |
| GET /api/remote/plan/fetch(闸默认 off 时 0 spawn,开 net 后才值钱) | gitops.py:635-648 | `plan_fetch` → `_has_tracking`(:650-653): `_head_branch`(:610 `rev-parse --abbrev-ref HEAD`)+ :652 `rev-parse --verify <r>/<b>` → :638 `ahead_behind`(:591): :610 `_head_branch` 第 2 次 + :614 `rev-parse --verify <up>` 第 2 次 + :622 `rev-list --left-right --count` | `_head_branch` 与 `rev-parse <up>` 各 2 次 | 2 | 闸的唯一执行点是 `_run`(:471);不得在合并路径加第二份闸检查;AIGIT_NET 没开时整条不 spawn | AIGIT_NET=read 下打点,两命令计数各 2→1 | 推断 |

---

## 二、一条命令已含另一条的答案(合并/替换,不是去重)

| 端点 | 文件:行号 | 调用链(handler → 方法 → git 命令) | 重复/浪费点 | 可省 spawn | 风险 | 怎么验 | 依据 |
|---|---|---|---|---|---|---|---|
| GET /api/status + 所有走 `_ok_with_status()` 的 POST 尾(server.py:1687-1701) | server.py:651-694 | `status()`: :669-670 `status --porcelain=v1 -z --untracked-files=all` + `_head_branch`(:624-649:先 :643 `symbolic-ref --short HEAD`,游离时退 :646 `rev-parse --abbrev-ref HEAD`) | `status --porcelain=v2 --branch` 输出自带 `# branch.oid` / `# branch.head`,一次 spawn 同时回答「三段改动」与「分支名 / 游离」——今天是两条 | 每次取 status 省 1:每个 POST 尾 1 条 + 每次 GET /api/status 1 条,全表乘数最大 | v2 行格式与 v1 完全不同(`1 <XY>` / `2` / `u` / `? <path>`),`porcelain_z_parse`(server.py:696-722)要重写;`# branch.head` 在三种 HEAD 态下各输出什么(正常/`(detached)`?未出生?)未实测——:634-641 那张实测写下的三态表就是验收标准 | E13 式探针:三种 HEAD 态跑 `status --porcelain=v2 --branch` 先看输出再动解析器;计数器期望 /api/status 2→1、POST 尾 -1 | 读码确认(重复存在) / 推断(v2 字段语义) |
| POST /api/commit(常规 HEAD) | server.py:1930→:1336→:1279→:1108/:1229/:1246/:1352 | `commit`: `escaping_links`(纯 FS,0 spawn)→ `stage_everything_safely` → `_add_scope`(:1108): :1127 `diff --no-renames --name-only -z` + :1135 `ls-files --others --exclude-standard -z` + :1124 `ls-files -z`(tracked);:1326 `_index_delta`: :1234 `rev-parse --verify` + :1239 `diff --cached --name-only -z HEAD`(before)→ :1328 `add` → `_assert_index_scope`: :1266 `_index_delta`(after)→ :1352 `commit -m` → `_status_quiet`(:1364→status 两条) | `status --porcelain=v2 -z` 一次覆盖三问:工作区 vs 索引(Y 列,替代 :1127)、未跟踪(? 条,替代 :1135)、索引 vs HEAD(X 列,替代 :1239 的 before) | 3(/请求,全链 11→8) | 三处都是 fail-closed 闸的输入(:1129-1133 / :1137-1139 / :1241-1243 都是 rc≠0 即拒),替代后语义必须保住;R/C rename 条目的 -z 双路径解析要保留现有 porcelain_z_parse 的处理;合并态下 X/Y/U 组合口径要逐格核对;after 那次必须保持新鲜(见第一节末条) | 计数器期望 commit 全链 11→8;跑 mutation_runner 的 fail-closed T 组与 test_aigit 的 commit 断言 | 读码确认(命令清单) / 推断(等价性) |
| GET /api/conflict/inspect | conflicts.py:664-727 | `inspect`: `_stages`(:623 `ls-files -u -z -- <p>`,1)→ `_blob` × 每个 stage(:638 `cat-file blob <sha>`,≤3)→ `_merge_base`(:658,1)→ `_show` ×2(:646 `git show HEAD:<p>` / `MERGE_HEAD:<p>`) | 本模块已有批量基础设施 `Git.cat_batch`(:257-298,`git cat-file --batch`,spec 支持 `<sha>` 与 `<rev>:<path>`,list_conflicts :887/:895 已在用)。inspect 的 ≤6 个对象读取可并成 1 批 | ≈5(/文件) | `_blob/_show` 的 rc≠0→(None,False) 与 cat_batch 的 missing 头归一后,binary 判定(:642/:647/:294 都是「前 8000 字节含 NUL」)已对齐,但需逐位复核 | 同一冲突文件改造前后各打点一次:期望 ~6-7→2;test_conflicts.py 的 inspect 组回归,xcheck 字段逐一比对 | 读码确认(基础设施已在) / 推断(接线收益) |
| GET /api/history/blame(工作区已删除的文件路径) | history_ops.py:1162-1257 | `blame`: `_require_commits`(:1170,`rev-parse`)→ :1173 `cat-file -e HEAD:<rel>`(工作区存在时短路,缺失才 spawn)→ :1182 `cat-file blob HEAD:<rel>`(二进制探测)→ `_file_total_lines`(:1145): :1153 `cat-file -s HEAD:<rel>` + :1157 `cat-file blob HEAD:<rel>`——同一 spec 第二次拉全量 → :1206 `blame --line-porcelain` | 缺失路径下同一 spec 的 blob 同请求拉 2 次(:1182 与 :1157);`-e` 的存在性可由 blob rc 回答;`-s` 的 size 在 blob 响应头里 | 2(仅缺失路径;工作区路径走 :1173 短路 + 本地行数,已近地板) | 行数语义(:1156-1160,`count("\n")` + size 判空)要照抄到合并版 | 造「已跟踪但被删」的文件跑 blame:`cat-file` 族计数期望 6→4;test_history_ops.py blame 组回归 | 读码确认 |
| GET /api/history/plan/tag-delete | history_ops.py:1072-1084 | :1075 `for-each-ref --count=1 refs/tags/<name>` → :1078 `rev-list -1 <name>` | `for-each-ref --format` 里 `%(objectname)`(tag 对象 sha)与 `%(*objectname)`(annotated 剥离后的 commit sha)同 spawn 已交出;:1078 是把 :1075 的答案又问一次 | 1 | :1078 现在取的是提交 sha(annotated 时不同于对象 sha);替换时必须优先 `%(*objectname)`、缺失回退 objectname,否则轻量/annotated 两种 tag 的 sha 静默变化 | 轻量与 annotated 各探针一遍断言 sha 等值;`for-each-ref` 计数期望 2→1 | 读码确认 |
| POST /api/stage、/api/unstage | server.py:774-790 | `stage`/`unstage`: `_tracked_set`(:770-772 `ls-files -z`)+ `_run_paths(["add"]/["reset","HEAD"])`(:781/:790,已是单次) | `_tracked_set` 只为 `_live_paths`(:610-621)服务:滤掉「既不在工作区也未跟踪」的路径,防 pathspec 全不命中 rc=128 让请求 500。**git 的 add 本身对全不命中的 pathspec 就会 rc=128 报错**——这道前置查询问的是 git 本来就会随命令失败自己回答的事 | 1(/请求) | 行为变化:今天「全不存在」是静默 no-op(:613-617 注),直连后变 400,要备案给前端与测试;「部分命中部分不命中」时 add 的副作用范围未实测,没验不许动;已跟踪但被删的文件必须仍可 stage(:613-617 与 :541 注)——这一支不受影响 | 探针:1 存在+1 不存在的 add,看 rc 与暂存结果;全不存在场景的返回语义变更备案 | 推断 |
| POST /api/remote/ahead-behind(及 `_unpushed` 同型) | gitops.py:591-627 | `ahead_behind`: :614 `rev-parse --verify --quiet <up>` → :622 `rev-list --left-right --count <up>...<branch>`;`_unpushed`(:678/:684)、`_has_tracking`(:652)同型 | ref 不存在时 rev-list 自己会 rc≠0:rev-parse 的答案已包含在下一步的 rc 里;现状两条总是都跑 | 1(/请求,仅当 `<up>` 存在,即常见路径) | 「缺 upstream」与「其他错误」要靠 stderr 匹配(bad revision),有点脆,且不得用来替代任何闸;net 默认关时无收益 | AIGIT_NET=read 下有/无 upstream 两场景打点:2→1、退路 2→2 | 推断 |
| GET /api/history/plan/reflog-restore(POST /api/history/reflog/restore 同链重跑) | history_ops.py:764-785 | `plan_restore`: `list_reflog`(:767): :744 `_has_commits`(`rev-parse --verify --quiet HEAD`)+ :749 `reflog -N --date=iso --pretty=…`(check=False)→ :785 `head_sha`(2 条 rev-parse) | `_has_commits` 的「无提交→返回 []」可由 reflog 的 rc/空输出直接回答(:749 check=False,空输出解析出来就是 []) | 1(+ 同型 head_sha 双 rev-parse 1) | 未出生 HEAD 下 `git reflog` 的精确 rc/stdout 要探针(:743-745 注释要求「返回空 list 不是错误」) | 空 repo / 正常 repo 各打点并比对 rows;期望 rev-parse 3→1、reflog 不变 | 推断 |
| POST /api/conflict/commit | conflicts.py:1195-1276 | `_commit_merge_locked`: `require_merge`(:555 `_read_gd`→`_gd`)→ `_read_gd("MERGE_MSG")`(:1197,又一次 `_gd`)→ `list_conflicts`(:1207:6 spawn:diff -z :864、`_read_gd` 内 `_gd` 第 3 次 :867、`ls-files -u -z` :872、`merge-base` :884、`cat_batch` ×2 :887/:895)→ `_merge_add_scope`(:1254,diff ×3 :1154-1156)→ `add -- paths`(:1256)→ `commit`(:1258)→ :1263 `rev-parse HEAD` + :1274 `log -1 --pretty=%s` | :1274 `log -1 --pretty=%s` 取的是 :1223-1227 本地刚算出的 subject(text 首行)——本地已知的值又去问 git;`_gd`(rev-parse --git-dir)×3 | 1 + 2(`--git-dir` 合并到实例级缓存) | git 对多行 message 的 subject 语义(首段还是首行)未实测,单行安全;建成 path 后必须跑多行 message 对照;commit 后的归属链(rev-parse HEAD)保持,不在这个候选里 | test_conflicts.py commit 组:多行 message 实测 `log -1 --pretty=%s` 输出与 `text.splitlines()[0]` 对照;计数 log 期望 2→1 | 读码确认(命令清单) / 推断(替换点) |
| GET /api/file(HEAD 里真有「空文件」的场景) | server.py:1745-1767 | handler: `show_file`(:731-734 `_run(["show", "<ref>:<rel>"])`——:734 空 stdout 与「不存在」都返回 None)→ :1763-1765 `if old is None and ref == "HEAD": old = REPO.staged_file(rel)`(:736-739 `git show :<rel>`) | HEAD 里确实有但内容为空的文件,在 :734 被误判为「HEAD 没有」,多打一次 staged_file | 1(边缘场景) | 根因是 `show_file` 走 `_run` 把 returncode 扔了,rc==0 的空输出与 rc!=0 谁也分不清;改 `show_file` 用 `_run_full` 认 rc,等于顺带修正「空文件 vs 不存在」的语义混叠——语义修复要单独备案 | 造一个已提交的空文件仓:期望 `show` 2→1,响应 old 字段为 "" | 读码确认 |

---

## 三、sha 键记忆化候选(只限不可变对象)

| 端点 | 文件:行号 | 调用链(handler → 方法 → git 命令) | 浪费点 | 建议/铁律 | 可省 spawn | 怎么验 | 依据 |
|---|---|---|---|---|---|---|---|
| GET /api/history/plan/revert(POST /api/history/revert 同链) | history_ops.py:807-847 | `plan_revert`: `_check_sha`(:810 `rev-parse <rev>^{commit}`)→ `parents_of`(:811 `show -s --format=%P`)→(merge 场景 :822 对每个 parent `subject_of(p)`:832 `subject_of(parents[mainline-1])`)→ :846 `subject_of(real)`(+ :837 `dirty()`) | commit 对象的 subject/parents 不可变,但 plan→do 两步之间、:822 与 :846 之间对同一 sha 反复 spawn | 键:`^[0-9a-f]{40}$` 的完整 sha → (subject, parents),模块级 LRU(cap ~512)。把 `HEAD`/`HEAD~1`/`@{n}`/短 sha `rev`(可变)设为不可进键;`_check_sha`(:245-257)的 rev→sha 归一化同样不缓存 | plan→do 两步合计 3-4 | test_history_ops.py revert 组回归;同一 sha 多次请求只见一次 `git show -s` | 读码确认 |
| GET /api/history/blame | history_ops.py:1162-1257 | `blame --line-porcelain -L <first>,<last> -- <rel>`;前端逐页拉同文件 | 同页翻回时全量重 spawn | 键:(HEAD 40 位 sha, rel 规范化路径, 工作区文件的 mtime+size, first, last, limit) → rows。注意 `git blame` 读的是**工作区文件**:未提交行标 "Not Committed Yet",所以工作区指纹(mtime+size)必须进键;HEAD 变则自动失效 | 老页面重拉省 `blame` 1 次/页 | 前端翻页打点;断言 recs 深相等(:1231-1243 字段结构照抄) | 读码确认(键形状由设计定) |
| POST /api/conflict/*(解决流程内的重复读) | conflicts.py:636-649 | `_blob`(`cat-file blob <sha>`)/ `_show`(`git show <rev>:<p>`) | blob sha 内容寻址不可变量,同一请求内 base/ours/theirs + cross-check 多处散读 | 键=40 位 blob sha →(raw, binary) LRU;`<rev>:<path>` 型键(含 HEAD/MERGE_HEAD)**只允许请求内同值字典**,跨请求禁(HEAD 可动) | 同一请求内 ≤2 次 | raw 长度封顶(防内存;16MB 级);binary 判定 :642/:647/:294 逐位对齐 | 读码确认 |

### 禁止缓存的对象(黑名单)

- **一切基于工作区/索引状态的都是 fail-closed 闸的输入,严禁缓存**(它们每请求必新取):
  server.py 的 `status`/`_status_quiet`(:1364-1385)、`_untracked_list`(:1004-1012)、
  `_add_scope` 的 `diff --name-only`/`ls-files --others`(:1108-1141)、`_index_delta`(:1229-1244)、
  conflicts.py `_merge_add_scope`(:1137-1163)、`_conflicted_paths`(:560-563)、
  history_ops `dirty`/`is_clean`(:289-316)、`_unmerged_commits`(:374-398)、
  `discard_plan` 的 tracked/untracked 分类(server.py:947-1002)。
  server.py:1129-1133 / :1137-1139 / :1241-1243 三处的 `raise …已拒绝提交` 语义:数据拿不到就必须拒绝提交——也就是说每次都要现值,绝不允许使用旧值。
- **.git/ 状态文件**(conflicts.py `require_merge` :555-563 / `_read_gd` :551-553,history_ops `_revert_in_progress` :881-903):**禁止跨请求缓存**。它们是 merge/revert 生命周期闸门;`:1195` `commit_merge` / `resolve`(:965-985)/ `abort`(:1278-1283)都吃新鲜值。唯一例外:`_gd` 底下那条 `rev-parse --git-dir`(:545)—— 仓库目录布局级常数,实例级缓存安全。
- **ref 级查询**:server.py `branches`(:757-767)、`_tracked_set`(:770-772);history_ops `list_stashes`(:469)/`list_branches`(:346)/`_branch_sha`(:359-365)/`list_tags`(:1031)/`list_reflog`(:742);server.py:1473-1475 的 `show-ref --verify refs/heads/<name>`。branch/tag/stash 的写操作当场改变这些事实;`show-ref` 那一挡还是 :1461-1466 注释里明写的 TOCTOU 防线——缓存它等于拆防线。
- **`escaping_links`(server.py :1014-1074;conflicts.py :1215 调用)**:文件系统对质对手进程,对手没有 aigit 锁;任何记忆化都错。

---

## 四、已到地板、无肉(免重挖清单)

| 端点 | 文件:行号 | 调用链(handler → 方法 → git 命令) | 为什么没有肉 | 依据 |
|---|---|---|---|---|
| GET /api/log | server.py:1731-1733 | `Repo.log`(:741-755,`log -N --pretty=format:%H… --date=short`) | 单 spawn;截断和 sha 缩写全在输出层解析 | 读码确认 |
| GET /api/branches | server.py:1734-1735 | `Repo.branches`(:757-767,`for-each-ref --format=%(refname:short)%FS%(HEAD) refs/heads`) | 单 spawn;分支名+当前标记同 spawn 内交付 | 读码确认 |
| GET /api/diff | server.py:1736-1744 | `Repo.diff`(:724-729,`diff [--cached] --no-color --no-ext-diff -- <rel>`) | 单 spawn | 读码确认 |
| POST /api/checkout、/api/branch | server.py:1938/1941 | `checkout_and_status`/`new_branch_and_status`(:1413-1431/:1433-1444)→ `checkout` :1472-1476 同锁内 `show-ref --verify --quiet refs/heads/<n>` + `checkout <n>`(2 条)+ `_status_quiet`→status(2 条) | `show-ref` 那条是 :1447-1466 注释里**明写不要动的 F-04 防线**(拒掉 sha 前缀与不存在的分支(F-04 修复 + checkout 的 TOCTOU 防线);`show-ref` 与 `checkout` 合一是一次破坏性重造(需按 F-04 的三个实测值重新验收),不列;status 尾程已由第二节表第一行的 porcelain v2 候选覆盖 | 读码确认 |
| GET /api/discard/plan | server.py:1837-1842 | `discard_plan`(:947-1002): `_tracked_set`(:961 `ls-files -z`)+ `_untracked_list`(:992 `ls-files --others --exclude-standard --directory --no-empty-directory`) | 两条是**不可合并的两个事实**:① tracked 全体成员——:920/:977 的「目录里含已跟踪文件」前缀判定需要连**未变更**的成员也知道,porcelain 只答变更条目;② 目录折叠的 untracked(:1010-1011 `--directory` 语义)。合并只能自己造 ignore 语义(:1108-1141 的警告:自造 ignore 的漂移方向是把被 ignore 的文件提交进索引) | 读码确认 |
| GET /api/remote/capability、/api/remote/list | server.py:1850-1855 | gitops.py:780-781 `capability`(0 spawn)、:523-548 `list_remotes`(1 条 `git remote -v`) | 无重复,已在地板;闸开与不开对本报告的 P2/P3 主战场没影响 | 读码确认 |
| POST /api/gen | server.py:2089-2094 | `Repo.generate`(:1540-1585)→ gen.py `ScenarioGen.generate` | gen.py 正文本次**没读**(批量数据生成器,需新建分支/提交,多 spawn 属本职);待核,不列候选 | 待核 |
| 其余 do_*:amend / undo / revert / reflog-restore / rename / tag(那些 plan 层重复已在第一/二节标出) | history_ops.py:607-651、:701-723、:849-879、:787-803、:939-946、:1010-1027、:1051-1092 | 例 do_amend:锁内 :644 `commit --amend` → :646 `head_sha` + :647 `subject_of`;:651 `dirty` | 这些端点剩下的 spawn 都是 post-op 的**新鲜事实**:head_sha/subject 属归属判据(history_ops.py:619-642、:706-715、:756-796、:859-864 注释,C4B 防回归都在锁内),**不允许用 plan 里的旧值替代**——已属结构地板 | 读码确认 |

---

## 收口(优先级 + 最值钱)

1. **全站乘数**:status() 改 `status --porcelain=v2 --branch`——每个 POST 尾 -1 + `GET /api/status` -1。改前先做三态探针(`# branch.head` 在正常/游离/未出生的值),这是全表验收门槛。(见第二节表第一行。)
2. **单请求最大肉**:plan_branch_delete 全链(13 spawn)≈ -6;`merge_state` 的 `--git-dir` 实例缓存 + ORIG_HEAD 同表达式去重 -5/请求;commit 链 porcelain 三合一 -3;do_stash_pop -2。
3. **最便宜的一笔**:`_gd` 的 `rev-parse --git-dir` 实例级缓存——一行改动,直接跨请求省 4-5 条。
4. sha 键 LRU(subject_of / parents_of / blame)进 P3:正确性前提是键锁 40 位 sha + 工作区指纹(blame 的情形),对象本身不可变,无失效负担。
