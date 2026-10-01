# aigit5 —— 冲突解决

**范围**：`builtin/` 无关,只碰 `tools/aigit/` 三个新文件。
**日期**：2026-10-01 · **Python** 3.12.4 · **git** 2.47.1.windows.2 · **Windows / msys64 无关**(纯标准库)

| 文件 | 行数 | 说明 |
|---|---:|---|
| `conflicts.py` | 838 | 本模块唯一入口 `ConflictResolver` + CLI |
| `test_conflicts.py` | 891 | 13 个用例,覆盖任务书全部 8 条 + 5 条边界 |
| `web/conflicts-demo.html` | — | 自测页(可选) |

**没动的文件**：`server.py`、`gen.py`、`web/index.html`、`test_aigit.py`、`aigit.bat`、`aigit.ps1`、`test_history_ops.py`。
`tools/aigit` 整个目录**不在任何 git 仓内**(`git -C … status` → `fatal: not a git repository`),所以本条线天然零冲突面;也没有在任何真实仓库里执行过 `git add/commit/reset/stash`。

---

## 一、`gen.py` 在不在

**在。** `tools/aigit/gen.py`(735 行,aigit2 那条线)已落地,`--kind conflict` 可用。

**没等**,直接用了它,而且冲突仓就是它真跑出来的:

```
$ python test_conflicts.py -v
    gen.py 场景: conflict#0  文件=['aigit_gen_clash_0.txt']  分支=['aigit_gen/conflict/0/a', 'aigit_gen/conflict/0/b']
    识别到冲突文件: aigit_gen_clash_0.txt  blocks=1  columns=3  verified=True
    MERGE_MSG 首行: "Merge branch 'aigit_gen/conflict/0/b'"  →  branches=['aigit_gen/conflict/0/b']
ok 1 - identify: 用 gen.py 造冲突场景,并识别出冲突文件
```

**但 `gen.py` 的 conflict 场景只改同一行**,造不出两种形态,所以那两条用例**自己搭了最小仓库**(其余全部用 gen.py):

| 形态 | 来源 | 为什么不用 gen.py |
|---|---|---|
| 单行冲突 | **gen.py** `conflict` | — |
| 一个文件**多个**冲突块 | 自建 `multi_region` | gen.py 的 `_apply_conflict` 只改 `target` 一行 |
| 双方新增同名文件(**无祖先**) | 自建 `no_base` | gen.py 四个场景全部从共同 base 分叉,必然有祖先 |
| modify/delete(两个方向) | 自建 `delete_modify` / `_reversed` | gen.py 无此类场景 |

这是刻意的分工,不是绕过 —— 已写进 `test_conflicts.py` 的模块 docstring。

---

## 二、任务书 8 条验收(实测输出)

命令:`python test_conflicts.py -v`,结果 **`# passed all 13 test(s)`**。

### [x] 1. 能造出冲突仓库并识别出冲突文件

```
ok 1 - identify: 用 gen.py 造冲突场景,并识别出冲突文件
    识别到冲突文件: aigit_gen_clash_0.txt  blocks=1  columns=3  verified=True
    MERGE_MSG 首行: "Merge branch 'aigit_gen/conflict/0/b'"  →  branches=['aigit_gen/conflict/0/b']
```

### [x] 2. ours/theirs/base 三方数据正确(逐字节对 `git show`)

不只对 `git show`,还**同时**对索引 stage,并把两处结果交叉校验:

```
ok 2 - threeway: ours/theirs/base 逐字节等于 git show 的输出
    stages: 1(base)=daaa6e2b8e  2(ours)=39e4dbdd0a  3(theirs)=d771e1e46f
    ours   逐字节 == git show HEAD         len=3076  sha=39e4dbdd0a
    theirs 逐字节 == git show MERGE_HEAD   len=3074  sha=d771e1e46f
    base   逐字节 == git show aa18537dce027ccede41ceb2a38028ee130acd6e  len=3067  sha=daaa6e2b8e
    三方交叉校验: 无不一致, region 自检 verified=True
```

断言了三件事:`git show <rev>:<file>` 的字节 == 模块给的 `text`;对应 stage 的 sha == 模块给的 `sha`;`crossCheck[*].matchesIndex` 全 true 且 `crossCheckMismatch` 为空。

### [x] 3. 多冲突块的文件,块数与起止行号正确

自建仓库:base 60 行,ours 改第 10、40 行,theirs 改**同样两行** → 必然 2 个冲突块。

```
ok 3 - multi: 多冲突块的文件,块数与起止行号正确
    region#0  标记 L10/L12/L14   工作区 ours=[11,11] theirs=[13,13]   源文件 ours=[10,10] theirs=[10,10] base=[10,10]
               ours  ='    # OURS  line 010 side-ours\n'
               theirs='    # OURS  line 010 side-theirs\n'
               base  ='line 010 original\n'
    region#1  标记 L44/L46/L48   工作区 ours=[45,45] theirs=[47,47]   源文件 ours=[40,40] theirs=[40,40] base=[40,40]
               ours  ='    # OURS  line 040 side-ours\n'
               theirs='    # OURS  line 040 side-theirs\n'
               base  ='line 040 original\n'
```

**两组行号都验了**(这是本模块最容易出错的地方,见下节):
- **工作区行号** `oursStart/oursEnd`:拿去切工作区文件,必须逐字等于 `oursText`;
- **源文件行号** `oursLineStart/End`、`theirsLineStart/End`、`baseLineStart/End`:拿去切三方各自的源文件,必须自洽。

注意第 0 块工作区是 `[11,11]` 而 ours 文件是 `[10,10]` —— 差 1 正是前面三个标记行。

### [x] 4. 无 base 的冲突(双方新增同名文件)被正确标记 no-base

```
ok 4 - nobase: 双方新增同名文件 → 正确标记 no-base(merge-base 返回非零)
    merge-base 退出码 = 1,输出为空(非 128 —— 128 是「MERGE_HEAD 不存在」才会给的码,量错时机就会看错)
    noBase=True columns=2 reason='no-merge-base(rc=1)'
    stages=['2', '3']  (无 stage 1)
    region#0 base 相关字段: baseText=None baseLineStart=None
    ours='OURS side content\n' / theirs='THEIRS side content\n'
```

`--allow-unrelated-histories` 合两个无关联根提交。`noBase=true`、`columns=2`(前端改两栏)、索引只有 stage 2/3、`baseLocatedText` 为 `None`。**没有抛异常。**

### [x] 5. resolve 写回后 `git add` 成功,文件不再被标记 U

手工把冲突块换成自己的一行(两侧都看得到),带 `expect_sha256` 乐观并发写回:

```
ok 6 - resolve: 写回后 git add 成功,文件不再被标记 U
    手工编辑: 72 行 → 68 行(原 ours 1 行 + theirs 1 行,替换成 1 行)
    resolve 返回: staged=True stillUnmerged=False remaining=[]
    索引状态: M  aigit_gen_clash_0.txt
    落盘内容含我加的行: True
```

`git status --porcelain` 从 `UU <file>` 变成 `M <file>`,`git diff --diff-filter=U` 为空。

乐观并发单列一条:

```
ok 7 - resolve-sha: expect_sha256 不匹配时拒绝覆盖(乐观并发)
    错 sha256 被拒: 工作区已被改动(期望 sha256=000000000000,实际 4c0ef92e9c62),拒绝覆盖
    被拒后文件未变=True 仍在冲突=True
    对 sha256 正常写回: stillUnmerged=False
```

### [x] 6. 全部解决后能完成合并,最终仓库干净

```
ok 8 - commit: 全部解决后能完成合并,最终仓库干净,提交信息含分支名
    未解决时提交被拒: 还有 1 个文件没解决,已拒绝提交:
    choose(ours) 之后 unmerged=[]
    提交 741d8bf999  message="Merge branch 'aigit_gen/conflict/0/b'"  branches=['aigit_gen/conflict/0/b']
    最终 porcelain=''  提交双亲数=2  status=
    git log 标题: "Merge branch 'aigit_gen/conflict/0/b'"
    自定义信息: '我自己写的提交信息\n\nMerged: aigit_gen/conflict/0/b'
           branches=['aigit_gen/conflict/0/b']  (从 MERGE_MSG 读,非臆造)
    git log 全文: '我自己写的提交信息 |  | Merged: aigit_gen/conflict/0/b'
```

- 带 U 时提交**被拒**且仓库状态不变;
- 提交后 `git status --porcelain` 为空、`.git/MERGE_HEAD` 已清;
- `git log -1 --pretty=%P` 有 **2 个双亲** —— 确实是合并提交;
- 分支名从 `.git/MERGE_MSG` 解析,**没自己编**;
- 自带提交信息时会自动补 `Merged: <分支>` 行。

### [x] 7. 合并已结束时 resolve 被拒绝

四个写接口逐个试,并核对仓库**逐字节没动**:

```
ok 9 - guard-closed: 合并已结束时 resolve 被拒绝,不动一个已完成的仓库
    resolve      被拒: 当前不在合并态(.git/MERGE_HEAD 不存在)
    choose       被拒: 当前不在合并态(.git/MERGE_HEAD 不存在)
    delete_file  被拒: 当前不在合并态(.git/MERGE_HEAD 不存在)
    commit_merge 被拒: 当前不在合并态(.git/MERGE_HEAD 不存在)
    四次拒绝后校验: HEAD 未动=True  文件未动=True  porcelain=''  MERGE_HEAD 不存在=True
```

### [x] 8. 路径越界被拒绝

```
ok 10 - guard-path: 路径越界被拒绝(各种花样)
    resolve('../../../Windows/System32/drivers/etc/hosts') 被拒
    resolve('..\\..\\..\\Windows\\system.ini') 被拒
    resolve('/etc/passwd') 被拒
    resolve('C:/Windows/System32/config/SAM') 被拒
    resolve('subdir/../../outside.txt') 被拒
    resolve('') 被拒
    resolve('   ') 被拒
    inspect(../outside.txt) 被拒
    resolve(.git/config) 被拒
    合法路径 'aigit_gen_clash_0.txt' 正常写回
```

**`inspect` 也拒** —— 不能只护写路径,读路径一样得护。`.git/` 内的路径也拒。

---

## 三、额外覆盖的 5 条边界

任务书没要求,但都是真会踩的坑:

```
ok 5  - delmod: 一边删一边改(两个方向)→ 缺失侧标 deleted,不能取空内容去 resolve
ok 11 - guard-notconflict: 对不是冲突的路径写回被拒绝
ok 12 - parse: 标记解析器 —— merge / diff3 / zdiff3 / 空块
ok 13 - read-nomerge: 不在合并态时只读接口不崩,写接口一律拒绝
   (+ resolve-sha 见上)
```

- **modify/delete 两个方向都测**(`DU` 与 `UD`)。哪一侧是 ours **取决于先合谁,不是取决于分支名** —— 这一点我一开始写错了,实测才发现。两种方向下缺失侧都标 `deleted`,且 `choose()` 取该侧被拒(不能悄悄写空文件),要删得显式调 `delete_file()`。
- **标记解析器单测**不过 git:merge / diff3 / zdiff3 三种 style、空 ours 块、双空块、多块、块贴文件首尾。

---

## 四、过程中修掉的 4 个真实缺陷

按严重度排。**全部是 `conflicts.py` 里的,不是测试写错。**

### 1. 两组行号互相覆盖(会前端高亮到错误位置)

`_attach_bases()` 把 region 的 `oursStart/oursEnd`(工作区行号)覆盖成了对齐算出来的 ours 文件行号。实测表现:工作区真实位置是 `[3,3]`,却报成 `[2,2]`;`theirsStart` 真实 `[5,5]`,报成 `[2,2]`。

这是**给前端的契约字段**,错了就是高亮到别的行上。已拆成两组并在代码里写明为什么不能混:

| 字段组 | 含义 | 谁算的 |
|---|---|---|
| `oursStart` / `oursEnd` / `theirsStart` / `theirsEnd` | 在**工作区文件**(带标记那份)里的行号 | `parse_markers` |
| `oursLineStart` / `oursLineEnd` / `theirsLineStart` / … / `baseLineStart` / … | 该段在**源文件**里的行号 | 锚点对齐 |

### 2. 定位 base 时用了"紧邻的公共行"当锚点,会落空

原实现取 region 之前**最后一个公共行**当锚点,但那一行未必在目标文件里逐字存在(比如 ours 那边同一处也改了)。改成取**所有已匹配锚点中最近的**一个(前一个 / 后一个),单调,不会错配。

### 3. `_align` 没关 `autojunk`

`difflib.SequenceMatcher` 默认启用启发式,会把"看起来像随机内容"的行整段丢弃。**本工具的测试数据恰好全是这类行**(大量重复的代码行),对齐会直接失效。已显式 `autojunk=False`,并在注释里写明为什么必须显式给。

### 4. Windows 上 `shutil.rmtree` 删不掉测试仓

git 的 object 文件(`.git/objects/xx/yyyy`)在 Windows 上落盘就是只读,`rmtree` 抛 `PermissionError`。测试台已自带 `onerror` 先放开只读位 —— 这是这个测试唯一的平台性坑,处理掉就能跨平台跑。

---

## 五、我自己测试代码里的 5 个 bug(如实记录)

不算模块缺陷,但都是"看起来对、其实错"的坑,写下来免得后人重蹈:

| 现象 | 真因 |
|---|---|
| `merge-base 退出码 = 128` | **在合并之前**测的,那时 `MERGE_HEAD` 还不存在,git 报 128(fatal: 不是合法对象名)。无祖先是 **1**。量错时机会得出"无祖先 = rc128"的错误结论,而模块内部报的 `rc=1` 反而看着像不一致 |
| `UnboundLocalError: e` | `except ... as e` 在块结束时把 `e` 删掉(Python 作用域),我在块外引用了它 |
| modify/delete 断言 ours 存在 | fixture 先合了删分支,ours **就是**已删的那一侧(git 给 `DU`)。模块报得对,注释写反了 |
| sha256 用例测不到 | resolve 成功后路径不再是未合并路径,复测会被更靠前的护栏拦下,根本走不到 sha256 那步。拆成独立用例 |
| 「文件内容不能动」失败 | 基准快照取自**冲突态**,而 `choose()` 本来就改了它。要证明的是"被拒绝的调用没改动",基准必须取自**合并刚完成那一刻** |

---

## 六、接口形态(前端照着接)

`conflicts.py` 不含 HTTP 层,`server.py` 的路由由你统一接。以下是 `ConflictResolver` 的方法与返回字段。

### 方法

| 方法 | 返回 | 说明 |
|---|---|---|
| `merge_state()` | dict | 在不在合并态 / 参与分支 / 默认提交信息 / 剩余数 |
| `list_conflicts()` | dict | 冲突文件清单,每项一行摘要(不展开全文) |
| `inspect(path)` | dict | **前端主要吃这个**,单文件三方全量数据 |
| `resolve(path, content, expect_sha256=None, encoding="utf-8")` | dict | 写回 + `git add` |
| `choose(path, side)` | dict | 整份取 `ours`/`theirs`/`base` |
| `delete_file(path, expect_sha256=None)` | dict | 标记删除并 `git add`(用于 modify/delete) |
| `commit_merge(message=None)` | dict | 全部解决后提交 |
| `abort()` | dict | 放弃合并 |

`encoding` 可传 `"base64"` 以支持二进制文件(默认 `"utf-8"`)。

### `inspect(path)` 字段

```jsonc
{
  "ok": true,
  "path": "aigit_gen_clash_0.txt",
  "inMerge": true,
  "noBase": false,              // 无共同祖先 → 前端改两栏
  "noBaseReason": null,         // 例 "no-merge-base(rc=1)"
  "binary": false,
  "mode": "100644",
  "mergeBase": "aa18537d…",     // null = 取不到
  "oursLabel": "HEAD",          // 从 <<<<<<< 标记里读的标签
  "theirsLabel": "aigit_gen/conflict/0/b",
  "columns": 3,                 // = noBase ? 2 : 3,前端直接照这个数画栏

  "stages": {                   // 索引里的三个 blob(git 真正会提交的那三份)
    "1": {"mode":"100644","sha":"daaa…","path":"…"},  // base
    "2": {"mode":"100644","sha":"39e4…","path":"…"},  // ours
    "3": {"mode":"100644","sha":"d771…","path":"…"}   // theirs
  },

  "sides": {
    "ours":   { "key":"ours",   "ref":"HEAD",        "label":"HEAD", "exists":true,
                "deleted":false, "binary":false, "mode":"100644",
                "sha":"39e4…", "stage":2, "text":"…", "lineCount":68 },
    "theirs": { "key":"theirs", "ref":"MERGE_HEAD",  "label":"aigit_gen/…", …, "stage":3 },
    "base":   { "key":"base",   "ref":"aa18537dc…",  "label":"aa18537dc", …, "stage":1 }
  },

  "worktree": {
    "exists": true,
    "text": "…带冲突标记的当前内容…",
    "lineCount": 85,
    "sha256": "bf1a329043548a20",   // ← 乐观并发用这个
    "byteLength": 3081,
    "hasMarkers": true,
    "manuallyEdited": false         // 标记被人手删了但索引还是 U
  },

  "regions": [ /* 见下 */ ],
  "regionCount": 1,
  "verified": true,                // 所有 region 都自检通过

  "crossCheck": {                  // 与 git show / merge-base 的交叉校验结果
    "ours":   {"ref":"HEAD",       "exists":true, "binary":false, "matchesIndex":true},
    "theirs": {"ref":"MERGE_HEAD", "exists":true, "binary":false, "matchesIndex":true},
    "base":   {"ref":"aa18537d…",  "exists":true, "binary":false, "matchesIndex":true}
  },
  "crossCheckMismatch": []         // 非空 = show 与索引对不上,以索引为准
}
```

### `regions[]` 字段

```jsonc
{
  "index": 0,
  "style": "merge",              // merge | diff3 | zdiff3
  "oursMarkerLine": 2,           // <<<<<<< 在**工作区**的行号
  "sepLine": 4,                  // =======
  "theirsMarkerLine": 6,         // >>>>>>>
  "oursLabel": "HEAD",
  "theirsLabel": "aigit_gen/conflict/0/b",

  "oursStart": 3, "oursEnd": 3,        // 工作区里的 ours 段行号(1-based 闭区间)
  "theirsStart": 5, "theirsEnd": 5,    // 工作区里的 theirs 段行号
  "oursLines": 1, "theirsLines": 1,    // 各段行数

  "oursText": "    # A-EDIT …\n",      // 从标记切出来的原文
  "theirsText": "…\n",
  "baseText": null,                    // 只在 diff3/zdiff3 风格下有值
  "baseOursText": null,                // zdiff3 才用
  "baseTheirsText": null,              // zdiff3 才用

  "oursLineStart": 2, "oursLineEnd": 2,       // 该段在**ours 文件**里的行号
  "theirsLineStart": 2, "theirsLineEnd": 2,   // 在 **theirs 文件**里
  "baseLineStart": 2, "baseLineEnd": 2,       // 在 **base 文件**里(noBase 时为 null)
  "oursLocatedText": "…",                     // 按上面行号切出来的,与 Text 一致
  "theirsLocatedText": "…",
  "baseLocatedText": "line 010 original\n",   // 由对齐算出,merge 风格下 baseText 是 null

  "verified": true                 // 本块三方拆分是否自检通过
}
```

> ⚠ **`oursStart` 与 `oursLineStart` 不是一回事。** 前者给「带标记的工作区文件」定位高亮,后者给三方分栏里的 ours 源文件定位。别混(我自己第一版就混了,见第四节缺陷 1)。

### 其余方法的返回

```
merge_state()  { ok, inMerge, mergeHead, origHead, head, currentBranch,
                 branches:[…], defaultMessage, mergeMsgPath, remaining, files:[…] }
list_conflicts() { ok, inMerge, count, files:[ { path, name, noBase, binary, regionCount,
                 manuallyEdited, verified, hasMarkers, columns, oursLabel, theirsLabel,
                 baseDeleted, oursDeleted, theirsDeleted, crossCheckMismatch, error? } ] }
resolve()      { ok, path, staged, stillUnmerged, remainingCount, remaining:[…],
                 mergeHead, byteLength, sha256 }
delete_file()  { ok, path, deleted, remainingCount, remaining:[…], mergeHead }
commit_merge() { ok, committed, commit, shortCommit, message, messageSource,
                 defaultMessage, branches:[…], mergeHead, subject }
abort()        { ok, aborted, inMerge }
```

`messageSource` = `"MERGE_MSG"`(用默认信息)或 `"caller"`(用户自带信息,已自动补 `Merged: …`)。

### 前端需知的三个语义

1. **`noBase` ≠ 有缺失侧。** `noBase` 是"取不到共同祖先"(两边各有独立根提交),此时 `columns=2`。modify/delete 那种**有 base 但缺一侧**仍是 `columns=3`,靠 `sides[k].deleted` 判断。
2. **`manuallyEdited=true`** 表示用户已手工删掉标记但索引仍是 U。此时 `regions=[]`、`verified=null`,提交前请提醒用户确认内容。
3. **`crossCheckMismatch` 非空**时以索引 stage 为准(那才是 git 会提交的内容),响应里已如实报出哪一侧对不上。

---

## 七、集成到 `server.py`(你来做)

我没动 `server.py`。demo 页已经按下面的端点写好了,照接即可:

```
GET  /api/conflict/state            → merge_state()
GET  /api/conflict/list             → list_conflicts()
GET  /api/conflict/inspect?file=    → inspect(file)
POST /api/conflict/resolve          {path, content, expectSha256?, encoding?}
POST /api/conflict/choose           {path, side}
POST /api/conflict/delete           {path, expectSha256?}
POST /api/conflict/commit           {message?}
POST /api/conflict/abort            → abort()
```

接线要点:

- **实例化一次**:`CONFLICTS = ConflictResolver(REPO.root)`,别每个请求新建(`Git` 每次构造都要 `Path.resolve()`)。
- **`ConflictError` → `{"ok": false, "error": str(e)}`,HTTP 400**,复用 `Handler._err()`。
  守栏拒绝(越界 / 非合并态 / 非冲突路径)都是 400,不是 500 —— 它们是**调用方的问题**,不是服务端故障。
- **`_err` 会把 `str(e)` 原样送出**。当前拒绝理由里会带仓库绝对路径(`mergeMsgPath`),前端不需要就自己过滤,或调 `ConflictResolver` 时传相对根。
- **`expectSha256` 字段名**:demo 页发的是 camelCase(`expectSha256`),转成 `expect_sha256=` 即可。
- `abort` 已有 `POST /api/merge-abort`,**保留它**;`conflicts.py` 的 `abort()` 只是让冲突页不必回旧接口,二选一即可,不必都接。

---

## 八、自测页

`web/conflicts-demo.html` —— 左栏冲突文件,右侧按 `columns` 画 2/3 栏,底部编辑框写回,并逐块标出 base 对应源文件第几行。

按纪律**没有开过浏览器**,所以改用能自动验证的方式:静态扫出页面读到的 **29 个字段名**,与 `inspect()/state()/list()` 的**真实输出 78 个键**逐一对账:

```
=== 对不上账的字段 ===
  (无)页面读到的每个字段都能在后端真实输出里找到
  已核实的假阳性(来自别的对象,非 inspect 的键): ['error', 'json']
```

两条假阳性已核实:`json` 是第 121 行 `r.json()` 的 `Response` 方法;`error` 只在 `!d.ok` 分支读,而 `inspect()` 失败时是**抛 `ConflictError`**、不返回 `{ok:false}` —— 那个形状由 `server.py` 的 `_err()` 产生(见第七节)。

region 的 17 个必备字段(两组行号 + 标记行号 + 三段文本 + `verified`)逐个确认存在于真实输出。

**页面未做视觉验证** —— 无浏览器,不做无法核实的断言。它引用的每个字段名都对得上账,这是我能给的全部保证。

---

## 九、未验证 / 已知限制

诚实列出:

1. **页面没在浏览器里跑过**(纪律禁止开窗口)。字段名对账已自动化,但布局、交互、滚动高亮未验证。
2. **zdiff3 风格只做了解析器单测**,没造真实 zdiff3 冲突仓端到端跑。`parse_markers` 的 zdiff3 分支按状态机走,与 diff3 共用路径;但真实 zdiff3 的 base 段内部 `||||||| 双方` 分隔没有真实样本验证过。
3. **二进制冲突没造过真实样本**。代码里按含 NUL 字节判定 `binary=true` 并把 `text` 置 `null`,`resolve(encoding="base64")` 通路写了但没实测过。
4. **rename 冲突(两分支各改各的路径)没测**。`git ls-files -u` 只按路径给三个 stage;rename/conflict 需要 `git status` 的额外信息才能还原"谁改名成谁"。当前实现会把它当普通 add/add 处理 —— **这是个真实缺口,不是已解决**。
5. **`git merge-file --diff3` 这条备选路线没用**。本实现自己用 `difflib` 对齐,不依赖 git 的合并策略与 `merge.conflictStyle` 配置;代价是 base 段位置靠对齐推断(有 `verified` 自检兜底)。没做两条路线的交叉比对。
6. **`gen.py` 的其它三类场景**(mergeable/unrelated/adddel)我没跑 —— 它们不产生冲突,与本模块无关。
7. **`test_aigit.py` 有 2 条失败**(`X-git-pathspec-magic`、`discard` 相关)。**与本条线无关**:该文件 `import server` / `import gen`,**不含** `conflicts` 字样(已 grep 确认),失败是别条线针对 `server.py` 的既有问题。我没碰 `server.py`。

---

## 十、纪律自查

| 要求 | 状态 |
|---|---|
| 不开浏览器 / 不开窗口 / 不截图 | ✅ 只写文件 + 跑命令行 |
| 测试仓建在 `%TEMP%\aigit-conflict-test\` | ✅ 13 个用例各一个子目录,跑完自动清理(已确认无残留) |
| 不碰 `C:\poject\` 下任何真实仓库 | ✅ 测试只用 `%TEMP%`;`tools/aigit` 本身**不是 git 仓**,没有可执行的 git 操作 |
| 不在项目仓里 `git commit/add/reset/stash` | ✅ 无 git 操作可执行(不是仓);所有 git 调用都带 `-C <%TEMP% 沙箱>` |
| 写文件用 write / Edit | ✅ 三个新文件全用 write/edit;清理沙箱用 Python `shutil` + `os.chmod`,未用 `Set-Content`/`Out-File`/`>` |
| 不改 `server.py` | ✅ 只读 |
| 不改 `gen.py` | ✅ 只读,`import` 它的 `ScenarioGen` |
| 不改 `web/index.html` | ✅ 新建的是 `web/conflicts-demo.html` |
| 不改 `test_aigit.py` / `aigit.bat` / `aigit.ps1` | ✅ |
| 无占位符 / 无 TODO | ✅ 无。全模块无占位符,`grep` 确认 |
| 不写"预计可行""应该没问题" | ✅ 全文只有实测输出 |
| `merge-base` 非零退出码要处理 | ✅ rc=1 → `noBase=true` + `noBaseReason`;测试断言 `eq(1, p.returncode)` |
