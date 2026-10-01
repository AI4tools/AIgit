# aigit6 —— 历史与危险操作层

**交付物**:`history_ops.py`(1108 行)、`test_history_ops.py`(1025 行,37 条)
**没碰**:`server.py` / `gen.py` / `conflicts.py` / `web/index.html` / `test_aigit.py` / 启动器
**验证仓**:`%TEMP%\aigit-history-test\` 与 `%TEMP%\aigit6-demo-*`,全程无头,没碰 `C:\poject\` 下任何真实仓库

```
$ python test_history_ops.py
测试仓目录: C:\Users\W\AppData\Local\Temp\aigit-history-test
====================================================================
.....................................
====================================================================
共 37 条,过 37,失败 0,异常 0
====================================================================
清场完成 C:\Users\W\AppData\Local\Temp\aigit-history-test 已删除

$ python history_ops.py --audit
ok  破坏性操作 8 项,全部 plan/do 配对且装了确认闸;安全两步操作 6 项;无一步到位入口
```

---

## 一、为什么破坏性操作是两步,而"两步"本身不够

要求是"每个破坏性操作返回一个需要确认的描述,而不是直接执行"。实现成:

```python
plan_branch_delete(name)                    -> {"safe": ..., "warning": ..., "commits": [...]}
do_branch_delete(name, force=False, confirmed=False)
```

`confirmed` 是**必填的**,不给就抛 `HistoryError(code="needs_confirmation")`。

有两点值得单独说,它们不是形式主义:

**1. `do_*` 会把 `plan_*` 用过的每一道前置检查重跑一遍。**
两步之间仓库可能已经变了 —— 用户切了分支、别人推了新提交、刚才那个操作本身改了 index。
所以 `do_*` 不是"拿着前端手里那份已经过期的结论往下走",而是重新问一遍仓库。
这也是为什么 `do_branch_delete` 里能直接 `raise` 而不需要信任 plan 的返回值。

**2. "两步"不等于"危险"。**
第一版我把 `do_` 前缀当成了破坏性,`audit_two_step()` 立刻把 `do_stash` / `do_revert` /
`do_tag_create` 报成"漏登记" —— 因为它们确实是安全的第二步。
正确做法是把"第二步"和"需要确认"分成两件事:

- `DESTRUCTIVE_OPS` —— 8 项,`do_*` **必须**调 `_require_confirm()`
- `SAFE_STEPS` —— 6 项,是两步形状但**不该**装闸

写死成一张表,等于逼着每一个"我这条不危险"变成一次显式判断,而不是漏登记后的静默豁免。

---

## 二、审计:把"没有一步到位"变成可执行断言

光有约定不算数。`audit_two_step()` 读源码检查五件事:

1. `DESTRUCTIVE_OPS` 每项的 `plan_*` / `do_*` 都存在
2. **每个破坏性 `do_*` 的源码里真的调了 `_require_confirm()`** —— 登记进表不等于装了闸
3. `SAFE_STEPS` 里的 `do_*` 不该装闸(装了就是弹框疲劳)
4. 每个 `do_*` 都被两张表之一覆盖 —— 漏登记 = "危险等级没人审过"
5. 没有裸动词的导出函数(`amend()` / `delete_branch()` 这种)

```
### 10. 每个破坏性操作都有 plan/do 两步,没有一步到位的路径

  production code 自检 audit_two_step() ->
     []  (无问题)

  破坏性操作登记表 DESTRUCTIVE_OPS:
    amend                  plan_amend()                     do_amend()
    undo-last-commit       plan_undo_last_commit()          do_undo_last_commit()
    undo-dropped           plan_undo_last_commit()          do_undo_last_commit()
    delete-branch          plan_branch_delete()             do_branch_delete()
    rename-branch          plan_branch_rename()             do_branch_rename()
    stash-drop             plan_stash_drop()                do_stash_drop()
    tag-delete             plan_tag_delete()                do_tag_delete()
    restore-from-reflog    plan_restore_from_reflog()       do_restore_from_reflog()

  同样是两步、但不需要确认闸的 SAFE_STEPS:
    stash                  plan_stash()                     do_stash()
    stash-apply            plan_stash()                     do_stash_apply()
    stash-pop              plan_stash()                     do_stash_pop()
    revert                 plan_revert()                    do_revert()
    revert-abort           plan_revert()                    do_revert_abort()
    tag-create             (无需 plan)                        do_tag_create()

  扫描 HistoryOps 上所有导出的 do_*,看源码里到底有没有装确认闸:
    do_amend()                   破坏性  确认闸=有
    do_branch_delete()           破坏性  确认闸=有
    do_branch_rename()           破坏性  确认闸=有
    do_restore_from_reflog()     破坏性  确认闸=有
    do_revert()                  安全    确认闸=无
    do_revert_abort()            安全    确认闸=无
    do_stash()                   安全    确认闸=无
    do_stash_apply()             安全    确认闸=无
    do_stash_drop()              破坏性  确认闸=有
    do_stash_pop()               安全    确认闸=无
    do_tag_create()              安全    确认闸=无
    do_tag_delete()              破坏性  确认闸=有
    do_undo_last_commit()        破坏性  确认闸=有

  把 8 个破坏性 do_* 全调一遍,一个都不给 confirmed=True:
    do_amend                     code=needs_confirmation
    do_undo_last_commit          code=needs_confirmation
    do_undo_last_commit(drop)    code=needs_confirmation
    do_branch_delete             code=needs_confirmation
    do_branch_rename             code=needs_confirmation
    do_stash_drop                code=needs_confirmation
    do_tag_delete                code=needs_confirmation
    do_restore_from_reflog       code=needs_confirmation

  第一次调用的报错全文:
    「amend」是不可逆操作。必须先调对应的 plan_* 看清影响,再显式传 confirmed=True。这里没有一步到位的入口。

  仓库指纹前后对比:
    HEAD     537507c -> 537507c
    标签     [{'name': 'v1', ...}] -> [{'name': 'v1', ...}]
    分支     ['main', 'unmerged'] -> ['main', 'unmerged']

  反向对照:把 do_amend 换成没有确认闸的假实现,audit 必须报出来
    FAIL do_amend() 登记为破坏性操作,但源码里没有调 _require_confirm() —— 这样就能一步到位。确认闸必须装在函数体里,不能靠调用方自觉。
  还原后 audit -> []  (无问题)

  反向对照:挂一个裸动词方法 amend() 上去,audit 必须当一步到位入口报出来
    FAIL HistoryOps.amend 是一步到位的破坏性入口。破坏性操作只能以 plan_*(只读) + do_*(confirmed=True) 成对出现。
  还原后 audit -> []  (无问题)
```

**两条反向对照是关键。** 没有它们,上面那句 `[] (无问题)` 可能只是审计本身没在工作
—— 测试套件里 A2 / A3 两条把这两件事做成了常规用例,每次跑都重演。

---

## 三、逐条验证(真实终端输出)

### 3.1 amend:未暂存的改动不会被卷进去

这是要求里最容易做错的一条。`git commit --amend` 本身只用 index,真正的坑是我们"顺手"
给个 `-a`。所以 `do_amend` 刻意不传 `-a/--all`,并且在 `plan_amend` 里逐条说清。

```
### 1. plan_amend:工作区有未暂存改动时,它把话说全
  plan_amend('chore: 建立 a 与 b(修订)') ->
    safe          = False   <- amend 改写历史,永远不是 safe
    executable    = True
    old_short     = 483bbe33 chore: 建立 a 与 b
    staged        = ['a.txt']
    unstaged      = ['b.txt']
    untracked     = ['README.md']
    published     = False []
    · 工作区有 1 个**未暂存**改动。
    · amend 只提交**已暂存**的内容(不会加 -a/--all),所以它们不会被卷进这次提交,会原样留在工作区。
    · 如果你本意是把它们一起提交,先 stage 再 amend。
    · 有 1 个未跟踪文件,它们不属于任何一次提交,amend 不会碰它们。
    · 本地看不到它被推送过(只查了本地远端跟踪分支,没有联网)。
    · 如果你已经推过,请先确认远端。

  整个 plan_* 跑完,HEAD 一动没动 = True

### 2. amend 只提交已暂存的部分,未暂存的留在工作区
  amend 前 HEAD        = 483bbe3
  amend 前 提交里的 a  = 'a v1\n'
  amend 前 提交里的 b  = 'b v1\n'

  do_amend 不给 confirmed -> code=needs_confirmation
    「amend」是不可逆操作。必须先调对应的 plan_* 看清影响,再显式传 confirmed=True。这里没有一步到位的入口。

  do_amend(confirmed=True) -> old=483bbe3 new=7392c05 rewritten=True
    提交里的 a           = 'a v2 STAGED\n' <- 已暂存,进去了
    提交里的 b           = 'b v1\n' <- 未暂存,没进去
    工作区 b 逐字节不变 = True
    仍然算未暂存        = ['b.txt']
    提交信息            = chore: 建立 a 与 b(修订)
    README.md 没被提交  = True
```

`published` 那一项是个诚实的局限:只查本地已有的 remote-tracking ref,**不联网**。
所以文案必须写"查不到不等于没推送",不能报一个假的"安全"。

### 3.2 stash:存进去 → 干净 → pop 回来逐字节相同

```
### 3. stash -> 工作区干净 -> pop 回来,内容逐字节相同
  暂存前 doc.txt sha256 = 687335958ebcd587
  do_stash   -> stashed=2, now_clean=True, stash_count=1
  stash 列表 = [(0, 'WIP: 半成品')]
  此时 doc.txt = '原始内容\n' <- 已回到 HEAD
  干净工作区再 stash -> code=nothing_to_stash
    message = 工作区是干净的,没有东西可以暂存。git stash 在这种情况下会直接报错(而不是塞一个空条目进 stash 列表),所以这里提前拦下。
  stash 数量仍是 = 1 (没有塞空条目进去)

  do_stash_pop -> popped=stash@{0}, dropped=True, stash_count=0
  pop 后 doc.txt sha256 = 687335958ebcd587  与暂存前相同 = True
  pop 后 new.txt 也回来了 = True
```

用的 payload 是 `"第一行\n中文第二行\n\n末尾无换行:é"` —— 挑的是那些在文本工具里最容易走样的点
(非 ASCII、连续空行、末尾无换行),sha256 对得上才算通过。

### 3.3 stash pop 遇冲突:报错,不静默

`do_stash_pop` **没有**调 `git stash pop`。git 自己的 pop 在冲突时也不删 stash,但它把"贴回"和"删除"
揉进一个命令,失败时返回码和输出容易漏看。改成 apply 成功才 drop:

```
### 4. stash pop 遇冲突:明确报错,不静默失败
  stash 里有   = [(0, '待会儿会撞车')]
  do_stash_pop -> code=stash_conflict
    conflicted = ['base.txt']
    message    = stash 贴回失败(仓库已保留原样,这个 stash 没被删):
Auto-merging base.txt
CONFLICT (content): Merge conflict in base.txt
On branch main
Unmerged paths:
  (use "git restore --staged <file>..." to unstage)
  (use "git add <file>..." to mark resolution)
	both modified:   base.txt

no changes added to commit (use "git add" and/or "git commit -a")
Index was not unstashed.
other.txt already exists, no checkout
error: could not restore untracked files from stash
冲突路径:base.txt

  git 自己认的冲突路径 = ['base.txt']
  与我们报的一致       = True
  **stash 条目仍在**   = [(0, '待会儿会撞车')]
```

最后两行是重点:我们报的冲突路径要和 `git diff --diff-filter=U` 的结果一致(不是自己编的),
而且**stash 条目必须还在** —— 静默丢掉才是灾难。

### 3.4 删除未合并分支:plan 报危险,do 未 force 拒绝

```
### 5. 删除未合并分支:plan 报危险,do 未 force 拒绝
  分支 = ['feature', 'main']  HEAD = main

  plan_branch_delete('feature') ->
    safe           = False
    executable     = True
    blocked        = None
    unmerged_count = 3
    commits        = [('aae90da7', 'feat: 特性 2'), ('b976f5cf', 'feat: 特性 1'), ('8ec67c7e', 'feat: 特性 0')]
    · 该分支有 3 个未合并提交(相对 main),不加 force 会被拒绝

  不给 confirmed,直接 do ->
    code = needs_confirmation
    message = 「delete-branch」是不可逆操作。必须先调对应的 plan_* 看清影响,再显式传 confirmed=True。这里没有一步到位的入口。

  给了 confirmed,但没有 force ->
    code = unmerged_branch
    unmerged_count = 3
    message = 「feature」有 3 个未合并提交,不加 force 拒绝删除。
该分支有 3 个未合并提交(相对 main),不加 force 会被拒绝
确认要丢就再调一次,带 force=True(和 confirmed=True)。
    分支仍在 = True

  force=True + confirmed=True ->
    deleted=feature forced=True unmerged_count=3
    remaining = ['main']
```

`blocked` 和 `safe` 是两回事:`blocked=None` + `safe=False` 表示"能做,但危险,需要确认框";
`blocked` 非空表示"这一步现在就不该做",前端该把它**灰掉**而不是弹确认框。
多给一个 `executable` 字段就是为了让前端别把这两者混起来。

未合并的基准取 **default 分支**(不是当前 HEAD):否则"这个分支早就并进 main 了"的提示会永远挂着。

### 3.5 undo-last-commit 之后 reflog 还能恢复

```
### 6. undo-last-commit 之后,reflog 还能恢复
  撤销前 提交数 = 3  HEAD = b90266a
  plan_undo_last_commit() ->
    op               = undo-last-commit
    safe             = False
    mode             = soft
    target           = b90266ac chore: third
    recoverable_via  = git reset --soft HEAD@{0}

  do_undo_last_commit(confirmed=True) -> mode=soft head_now=28c0720
  撤销后 提交数 = 2

  reflog 前 4 条:
    [0] 28c0720 HEAD@{0}  reset: moving to HEAD~1
    [1] b90266a HEAD@{1}  commit: chore: third
    [2] 28c0720 HEAD@{2}  commit: chore: second
    [3] a7346ad HEAD@{3}  commit (initial): chore: first

  被撤销的 28c0720 在 reflog 第 0 条
  plan_restore_from_reflog(0) -> safe=True mode=soft target=28c0720
  恢复:
    把 HEAD 移到 28c0720(reset: moving to HEAD~1)。本次操作本身也会进 reflog,所以还能退回来,但别把 reflog 当保险箱 —— gc 之后就没了。

  do_restore_from_reflog -> restored=28c0720 head_now=28c0720
  HEAD 回到被撤销的那条 = True
  提交数又变回         = 2
```

`recoverable_via` 的 flag **跟着这次撤销的实际模式走**。第一版固定写 `--hard`,那意味着用户
照抄这条命令去恢复一次 soft 撤销,会顺手把自己的工作区也清掉 —— 恢复动作不该比原来的撤销更狠。
soft 撤销配 `--soft`,undo-dropped 才配 `--hard`。

### 3.6 revert merge commit:拒绝歧义

```
### 7. revert 一个 merge 提交:必须指定 mainline,拒绝歧义
  提交数 = 4   merge = 39a6916
  父提交 = [(1, '3684975', 'feat: main 引入 main.txt'), (2, '9b12b3b', 'feat: side 引入 side.txt')]

  plan_revert(merge)  [不给 mainline] ->
    safe       = False
    executable = False
    is_merge   = True
    blocked    = 39a6916a 是一个 merge 提交,有 2 个父提交。revert 一个 merge 必须用 -m 指定保留哪一侧(mainline),否则结果不确定:不同的 mainline 会得到完全相反的逆向改动。候选:1 = feat: main 引入 main.txt, 2 = feat: side 引入 side.txt

  do_revert(merge) -> code=revert_refused
    message = 39a6916a 是一个 merge 提交,...
  提交数没变 = 4

  给了 mainline=1 ->
    safe       = True  mainline = 1
    warning    = mainline=1,保留 feat: main 引入 main.txt 这一侧。revert 会把「相对这一侧的差异」反向应用到当前分支上。
  do_revert -> reverted=39a6916a new_head=36a9d96
  提交数     = 5 (只新增,不改写历史)
  side.txt 被撤掉 = True
  最新提交   = Revert "merge side"

  mainline 越界 ->
    mainline=0     code=bad_mainline  mainline 必须是 1..2,收到 0
    mainline=3     code=bad_mainline  mainline 必须是 1..2,收到 3
    mainline='1'   code=bad_mainline  mainline 必须是整数(1..2)
    mainline=True  code=bad_mainline  mainline 必须是整数(1..2)
```

`blocked` 不只是"不给 mainline 不行",它把**两个候选各自的 subject** 都列出来 ——
用户得知道选哪一侧代表什么,而不是只被告知参数缺失。`True` 也被 `bad_mainline` 挡掉
(`isinstance(True, int)` 在 Python 里是真的,这是很容易漏的一个坑)。

另外:`bad_mainline`(形式错)和 `revert_refused`(给普通提交指定 mainline,意图不成立)
是两个不同的 code,前端要区别对待。

### 3.7 blame 大文件能截断

```
### 8. blame 大文件能截断
  big.txt 总行数 = 5000

  blame('big.txt', limit=100) ->
    count       = 100
    first_line  = 1  last_line = 100
    total_lines = 5000
    truncated   = True
    next_line   = 101
    前两条:
          1  a0853aca  chore: 造一个 5000 行的大文件  aigit         2026-10-01 00:31:37+0800
          2  a0853aca  chore: 造一个 5000 行的大文件  aigit         2026-10-01 00:31:37+0800
  一次只取 100 条,不是 5000 条

  接着拉 start_line=101 ->
    first_line=101 last_line=150 next_line=151 首行内容='line 100'

  拉最后一段 start_line=4990 ->
    last_line=5000  truncated=False  next_line=None

  精确定位被改过的那一行:
    行号 4322  内容 'CHANGED 4321'
    提交 a1f67c9e (chore: 改掉第 4322 行)  作者 aigit  时间 2026-10-01 00:31:37+0800

  limit 被夹住(上限 20000):
    limit=999999  -> 实际 20000
    limit=0       -> 实际 1
    limit=1       -> 实际 1
  非法范围:
    {'start_line': '3'}                code=bad_line   start_line 必须是整数
    {'end_line': 5, 'start_line': 9}   code=bad_line   end_line(5) 不能小于 start_line(9)
    {'limit': 'many'}                  code=bad_number 必须是整数:'many'
```

截断是在**问 git 之前**决定的:先数出总行数,算出范围,再用 `git blame -L first,last` 卡住。
不是让 git 把整份 `--line-porcelain`(十万行会吐几 MB)吐出来再由我们扔掉。
`next_line` 让前端能续拉,`truncated=False` 时它是 `None`,两种情况都有测试。

每行给到:行号 / 短 sha / 作者 / 邮箱 / 时间(带时区偏移)/ 提交摘要 / 文件名 / 内容。

### 3.8 file history 跟得上重命名

```
### 9. file history 跟得上重命名
  file_history('new.txt', follow=True) -> count=5 follow=True tracked=True
    91f6afc  chore: 改名之后的提交
    3726217  refactor: 把 old.txt 改名为 new.txt        <- R
    03166cb  chore: 修订 2
    339b925  chore: 修订 1
    21fd558  chore: 修订 0

  标出 R 的重命名条目 = [('old.txt', 'new.txt')]
  重命名**之前**的提交也在列表里 = True
  关掉 --follow -> count=2 (断在重命名处,跟不过去)

  反过来查旧路径 'old.txt' -> count=4 (也能跟回来)

  非法路径:
    '../../secret'         code=path_escape  路径越界,已拒绝:../../secret
    '..\\_outside\\x'      code=path_escape  路径越界,已拒绝:../_outside/x
    'C:/Windows'           code=path_escape  路径越界,已拒绝:C:/Windows
    ''                     code=bad_path     路径为空
```

`关掉 --follow -> count=2` 那行是用来说明 `--follow` 真的在起作用,而不是碰巧。

### 3.9 undo-dropped:丢改动

```
### 12. undo-dropped:丢改动,plan 把"连未提交的一起丢"说在前面
  plan_undo_last_commit(drop_changes=True) ->
    op              = undo-dropped
    mode            = hard
    target          = 522afbed chore: second 这次要丢
    parents         = 1
    recoverable_via = git reset --hard HEAD@{0}  <- 注意是 --hard,和 mode 对得上
    · **丢弃**这个提交的改动:reset --hard 之后工作区与该提交完全一致,提交里的内容就没了。
    · 丢弃模式下 reset --hard 会**连同工作区里尚未提交的改动一起抹掉**(1 个未暂存改动、1 个未跟踪文件)。

  do -> mode=hard head_now=18e4f39
    a.txt 内容回到   = 'v1\n'
    工作区干净       = False
    tmp.txt 还在     = True (reset --hard 不碰未跟踪文件,这是刻意的)
```

注意 `工作区干净 = False`:`reset --hard` 删掉的是已跟踪文件的改动,**未跟踪文件留着**。
这是刻意的(顺手 `git clean -fd` 会吃掉用户随手放的东西),但 plan 里必须说明,
不能让人以为"丢掉"是彻底的。

### 3.10 其余护栏

```
### 11. 其余硬护栏:当前分支 / 唯一分支 / default 分支 / 空仓 / 空 reflog
  (a) 仓库只有 main 一个分支,删它 ->
    blocked = 这是仓库里唯一的分支,删掉之后 HEAD 就没有落脚点了。git 本身也会拒绝。
    无 force        code=delete_refused   这是仓库里唯一的分支,...
    {'force': True} code=delete_refused   这是仓库里唯一的分支,...
    main 还在 = True

  (b) 造第二个分支,停在它上面,问能不能删 main(default) ->
    HEAD = scratch  default_branch() = main
    blocked = 不能删除 default 分支 main。它是别人 clone 之后的落脚点,也是判别「这个提交算不算已合并」的基准。想删它,先改名字(plan_branch_rename)再删。
    force=True 也拦下 code=delete_refused
    绕过:先改名再删 -> blocked=None safe=True
    删掉了,remaining = ['scratch']

  (c) 停在某个分支上,问能不能删**脚下这个** ->
    分支 = ['another', 'scratch']  HEAD = scratch
    blocked = 不能删除当前所在的分支 scratch。先切到别的分支(git checkout another),再删它。
    无 force        code=delete_refused   ...
    {'force': True} code=delete_refused   ...
    分支还在 = True

  (d) 空仓库:没有提交时 undo / amend / 恢复 / 打标签 ->
    list_reflog() = []
    plan_undo_last_commit      code=no_commits    仓库还没有任何提交,撤销上一次提交无从谈起
    plan_amend                 code=no_commits    仓库还没有任何提交,amend无从谈起
    plan_restore_from_reflog   code=empty_reflog  reflog 是空的(仓库还没有提交,或 reflog 被 gc 清掉),没有可恢复的历史。
    do_tag_create              code=no_commits    仓库还没有任何提交,打标签无从谈起
    do_restore_from_reflog     code=empty_reflog  reflog 是空的(...),没有可恢复的历史。
```

三处设计决定:

- **单分支仓里"当前分支"和"唯一分支"同时成立**,先判"唯一分支" —— 它解释了为什么切走也没用。
- **default 分支的保护不受 `force` 影响**。它保护的是别人的落脚点,不是"有没有合并"。
  绕过办法写进 `blocked` 文案(先改名再删),测试里也真的走了一遍这条路。
- **`(c)` 里建议切走的分支是从实际分支列表里挑的**,不是写死 `main`。
  写死的话,当被删的恰好是 `main` 时就变成"先切到 main" —— 废话,用户会直接放弃。
  测试里有一条断言在守这个:`建议切走的分支必须真实存在且不是被删的那个自己`。

```
### 13. 分支改名:撞名就停手,不动别人的分支
  分支 = ['main', 'old-name', 'taken']  HEAD = main
  plan_branch_rename('main', 'taken') ->
    blocked    = 已经有同名分支 taken。改名不会覆盖它 —— 那是另一个人的工作。
    executable = False
  do -> code=rename_refused
    taken 还在 = True  main 还在 = True

  do_branch_rename('old-name','new-name',confirmed=True) ->
    new = new-name  sha 与改名前一致 = True
    分支 = ['main', 'new-name', 'taken']

### 14. 标签:打 / 覆盖 / 删(删是两步)
  do_tag_create('v1.0.0', message=...) ->
    name=v1.0.0 annotated=True type=tag
    列表: v1.0.0   335d05f 2026-10-01       first release
  重复打同名标签(不带 force) -> code=tag_exists
    标签 v1.0.0 已存在。删了重建,或者传 force=True 覆盖。
  force=True 覆盖后 subject = 'force 覆盖'
  plan_tag_delete ->
    safe    = False
    warning = 删掉标签 v1.0.0(指向 18e4f39e)。标签通常不进 reflog,所以这一删基本不可恢复;提交本身还在,只是这个标记没了。
  do 不给 confirmed -> code=needs_confirmation
    标签还在 = ['v1.0.0']
  确认后 list_tags() = []
```

### 3.11 白名单:全部拒绝,仓外哨兵一字未动

```
### 15. 名字与路径白名单(全部拒绝,仓外哨兵一字未动)
  分支名/标签名:
    '-f'           code=bad_name  标签名不能以 - 开头:git 会把它当成选项读
    '--force'      code=bad_name  标签名不能以 - 开头:git 会把它当成选项读
    '..'           code=bad_name  标签名不能以 / 或 . 开头
    'a/../b'       code=bad_name  标签名不能包含 ..
    '@'            code=bad_name  标签名只允许字母数字和 _ . / -(正则 [\w./-]{1,100})
    'a@{0}'        code=bad_name  标签名只允许字母数字和 _ . / -(正则 [\w./-]{1,100})
    'a b'          code=bad_name  标签名只允许字母数字和 _ . / -(正则 [\w./-]{1,100})
    'a.lock'       code=bad_name  标签名不能以 .lock 结尾
    '/a'           code=bad_name  标签名不能以 / 或 . 开头
    'a/'           code=bad_name  标签名不能以 / 或 . 结尾
    'a.'           code=bad_name  标签名不能以 / 或 . 结尾
    'a//b'         code=bad_name  标签名不能包含 //
    ''             code=bad_name  标签名不能为空
    'x'*101        code=bad_name  标签名超过 100 字符
    'a;rm -rf /'   code=bad_name  标签名只允许字母数字和 _ . / -
    'a$b' / 'a|b' / 'a`b' / 'a>b'  code=bad_name  ...
    一个标签都没被建出来 = True
    合法名 release/2024.01_ok-1 能建 = ['release/2024.01_ok-1']

  rev:
    '' / '   '       code=bad_rev  提交不能为空
    '-x' / '--amend' code=bad_rev  提交不能以 - 开头
    'no-such-ref'    code=bad_rev  解析不了这个提交:no-such-ref
    'a'*200          code=bad_rev  提交含有非法字符
    'abc;rm -rf /'   code=bad_rev  提交含有非法字符:abc;rm -rf /
    'a|b' / 'a\nb' / 'a b' / '$(whoami)' / '`id`'  code=bad_rev  ...

  路径:
    blame     '../../secret'        code=path_escape  路径越界,已拒绝:../../secret
    file_history '../../secret'      code=path_escape  路径越界,已拒绝:../../secret
    blame     '..\..\canary.txt'    code=path_escape  路径越界,已拒绝:../_outside/canary.txt
    file_history '..\..\canary.txt'  code=path_escape  路径越界,已拒绝:../_outside/canary.txt
    blame     'C:/Windows'          code=path_escape  路径越界,已拒绝:C:/Windows
    blame     '/etc/passwd'         code=bad_path     路径不能是绝对路径:/etc/passwd
    blame     ''                    code=bad_path     路径为空

  仓外哨兵文件 C:\Users\W\AppData\Local\Temp\aigit6-outside\canary.txt
    内容一字未动 = True
```

`[\w./-]{1,100}`(与 `server.py` 的 `new_branch` 同严格度)允许前导 `-`,所以额外挡掉了它 ——
`git branch -foo` 会被 git 当成选项读,这是实打实的参数注入。`@{` 挡的是 reflog 语法,
`.lock` 挡的是 git 自身的保留形式。所有 git 参数走列表形式,不经 shell。

---

## 四、开发过程中被测试抓到的实现 bug

第一版 18 条红,其中 **4 个是 `history_ops.py` 自己的问题**,不是测试写错:

| # | 问题 | 后果 | 根因 |
|---|---|---|---|
| 1 | `list_tags` 用 `for-each-ref -N` | **标签列表恒为空** | `for-each-ref` 只认 `--count=N`,不认 `git log` 的 `-N` 简写。更糟的是我传了 `check=False`,把这个硬错误吞成了"一个标签都没有" —— 正是要求里点名要避免的静默失败。改成 `--count=` + `check=True` |
| 2 | `do_revert_abort` 拿 `Path` 调 `.strip()` | 冲突后无法收场,`AttributeError` | `self.root / x` 返回 `Path` |
| 3 | 探测 revert 状态用 `.git/REVERT_HEAD` | 恒定探测不到 | 那是**路径**不是 ref,`rev-parse --verify` 拿它当 rev 解析必然失败(实测 rc=1)。必须用裸 `REVERT_HEAD` |
| 4 | `_reflog_hint` 固定给 `--hard` | 照抄恢复命令会清掉工作区 | 恢复动作不该比原来的撤销更狠 |

另外三处是文案/接口层面的,也是测试逼出来的:

- `do_branch_delete` 的报错原本只在 `extra["hint"]` 里说"带 force",正文没提 → 现在正文里写明
- `_unmerged_commits` 的每条记录没有 `short` → 前端要在警告里点出具体是哪几个提交,40 位 sha 没人会去核对
- `list_reflog` 的 `selector` 用了 `%gd`,配了 `--date=iso` 之后 git 吐出的是 `HEAD@{2026-10-01 00:21:27 +0800}`,那不是能粘回命令行的东西 → 改成自己拼 `HEAD@{N}`

还有一处是**测试自己写错**但暴露了设计问题:单分支仓里"当前分支"和"唯一分支"同时成立,
原来的判据顺序让"当前分支"先命中,给出的理由解释了为什么切走也没用 —— 换了顺序。

### 4.1 测试套件自己的清场是假的

37 条全绿之后我顺手看了一眼 `%TEMP%`,`aigit-history-test` 里还剩 589 个文件。
根因不是 Windows 句柄没释放(我一开始是这么猜的),而是:

```
RuntimeError: 删不掉 C:\Users\W\AppData\Local\Temp\aigit-history-test,
残留内容会造成下一轮测试建在脏目录上:
[WinError 5] 拒绝访问。: '...\aigit-history-test\a04\.git\objects\0a\d40f7b3...'
```

残留路径**全在 `.git/objects/` 下面** —— git 的 object 文件是以只读(444)方式落盘的。
Windows 没有 Unix 那套"目录可写 + 里面的文件就可删"的模型,`shutil.rmtree` 走到只读文件
就 PermissionError。

要命的是我当初写的是 `shutil.rmtree(TESTROOT, ignore_errors=True)`。它把两件事一起吞了:
删不动只读文件,和真的删不动。后果不是"留点垃圾",而是**下一轮测试建在这个脏目录上** ——
`git init` 一个还带着旧 `.git` 的目录,后面所有断言的结论都不可信,而且看不出是哪一步坏的。

改成 `purge()`:`onexc` 回调里 chmod 放开再删一次 + 退避重试 6 轮,
**删不掉就抛出来,首轮清场失败直接 `return 2` 拒绝开跑**,收尾清场失败也不许被"全绿"盖过去。

```
跑之前残留 = True        <- 上一版留下的 589 项
$ python test_history_ops.py
共 37 条,过 37,失败 0,异常 0
清场完成 C:\Users\W\AppData\Local\Temp\aigit-history-test 已删除
跑之后残留 = False

--- 第 1 轮 ---  共 37 条,过 37,失败 0,异常 0   残留 = False
--- 第 2 轮 ---  共 37 条,过 37,失败 0,异常 0   残留 = False
--- 第 3 轮 ---  共 37 条,过 37,失败 0,异常 0   残留 = False
```

顺带加了双重保险:只允许删 `%TEMP%` 下、且基名以 `aigit-history-test` 开头的路径。
这个断言不是靠自觉 —— 一旦 `TESTROOT` 被改错,下一行 `rmtree` 就是往真实仓库头上砍。

---

## 五、可复现的演示脚本

报告里每一段终端输出来自这些脚本(都在 `%TEMP%` 下,不改项目仓库):

```
%TEMP%\opencode\aigit6-demos\
  demo1_amend.py           3.1  amend:未暂存的不会被卷进去
  demo3_stash.py           3.2  stash -> 干净 -> pop 逐字节相同
  demo4_stash_conflict.py  3.3  pop 遇冲突
  demo5_branches.py        3.4  删未合并分支
  demo6_reflog.py          3.5  undo 后 reflog 恢复
  demo7_revert.py          3.6  revert merge 拒绝歧义
  demo8_blame.py           3.7  blame 截断
  demo9_history.py         3.8  file history 跟重命名
  demo10_audit.py          3.9  两步走审计 + 两条反向对照
  demo11_tags.py           3.10 其余护栏
  demo12_rest.py           3.11 undo-dropped / 改名 / 标签 / 白名单
```

---

## 六、集成说明

`server.py` 侧零改动即可用,本模块自带 `_run` 与锁,不依赖 `server.py` 的任何东西:

```python
from history_ops import HistoryOps, HistoryError, audit_two_step

ops = HistoryOps(repo_root)          # 构造后不写仓库,第一次写发生在某个 do_* 里

# 建议在启动时调一次,把这个当断言用
probs = audit_two_step()
if probs:
    for p in probs:
        print("FAIL " + p)
    raise SystemExit(1)

try:
    info = ops.plan_branch_delete("feature")
    # 前端: info["executable"] 为假 -> 灰掉;否则按 info["safe"] 决定要不要弹确认框
    if info["executable"] and user_confirmed:
        ops.do_branch_delete("feature", force=info["unmerged_count"] > 0,
                             confirmed=True)
except HistoryError as e:
    return e.to_dict()               # {"ok": False, "error": ..., "code": ...}
```

**返回值约定**

- `plan_*` → `{op, safe, warning, blocked, executable, requires_confirmation, ...op 专属字段}`
  - `blocked` 非空 = 这一步现在就不该做(前端灰掉,别弹确认框)
  - `blocked` 为空 + `safe=False` = 能做但危险(弹确认框,展示 `warning`)
  - `safe=True` + `executable=True` = 直接做
- `do_*` → `{ok: True, ...}`;失败抛 `HistoryError`,`.to_dict()` 带 `code` 供前端分支处理
- `code` 是稳定契约(`needs_confirmation` / `unmerged_branch` / `delete_refused` /
  `rename_refused` / `bad_mainline` / `revert_refused` / `revert_conflict` /
  `stash_conflict` / `stash_drop_failed` / `nothing_to_stash` / `no_commits` /
  `empty_reflog` / `bad_name` / `bad_path` / `path_escape` / `bad_rev` /
  `bad_index` / `bad_line` / `bad_number` / `bad_mode` / `no_such_branch` /
  `no_such_tag` / `no_such_stash` / `tag_exists` / `binary_file` / `no_such_file` /
  `not_a_repo` / `amend_failed` / `reset_failed` / `tag_failed` /
  `tag_delete_failed` / `rename_failed` / `delete_failed` / `blame_failed` /
  `blame_empty` / `revert_abort_failed` / `no_revert_in_progress` / `history_error`)

**新增破坏性操作必须做三件事**:登记进 `DESTRUCTIVE_OPS`、在 `do_*` 第一行调
`_require_confirm()`、在函数体里重跑 `plan_*` 的全部前置检查。
漏掉任何一件,`audit_two_step()` 会报,而它被测试每次都跑。
