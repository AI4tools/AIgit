# aigit gitguard —— 仓库自带 git 配置的代码执行面:中和报告

日期:2026-10-02
环境:git 2.50.1.windows.1 / Python 3.11.0 / Windows
约束:不联网(全部本地仓 + 私有 TEMP `C:\Users\Public\tmp\aigit-guard`)、不改 `web/**`、不改任何 `test_*.py`、不 commit/push

---

## 0. 一句话结论

**中和了 9 条实测会执行的配置项**(每条都有「关掉它就漏」的标记文件证据),
**2 条加了覆盖但离线无法验证触发**(明说没验证),
**`filter.*` / `diff.*` / `merge.*` 三族没有通配符可用,靠「枚举 + 逐名覆盖」解决**——
枚举是我自己写的容错解析器,**读不全就是漏**,这是本方案最大的残余风险。

顺带**推翻了三条既有说法**(见 §5),其中两条会导致防线形同虚设。

---

## 1. 方法:判据是什么

**探针 = 一个只往标记文件追加一行的 `.bat`**,被配置指向它。**只测触发,不构造破坏链。**

每一行结论都必须同时过两关:
1. **标记文件**:中和前命中 / 中和后不命中;
2. **退出码**:中和后 `rc` 仍要是产品能接受的值。**只标记不命中但 `rc=129` 算失败**
   —— 这条判据是踩出来的:我第一版把 `-c` 拼在子命令后面,`git add -A` 全变 `rc=129`,
   标记确实没命中,功能却全坏了。

### 探针自身的三个坑(都是我自己栽的,记下来免得再犯)

| 坑 | 现象 | 真因 |
|---|---|---|
| 引号里的 Windows 路径 | `fatal: bad config line 2 in file .gitconfig` | git config 里**反斜杠是转义符**,`"C:\Users\..."` 的 `\U` 非法。改用正斜杠 |
| `from probe1 import *` | `NameError: _force_rm` | 下划线开头的名字不导进 `*` |
| 探针自己用了裸 `git diff` | 误报「中和失效」 | `git diff` 比对工作区时会跑 **clean filter**,所以它自己触发了标记 |

第三坑最要紧:它让我一度以为中和有洞。**探针的输出只能证明探针自己看到了什么。**

---

## 2. 中和了什么(逐条含证据)

全部经**产品代码路径**复核(`server.Repo` / `conflicts.Git` / `history_ops` / `gen.Git` / `gitops.GitOps`),
不是只测 `gitguard.for_root`。

| # | 配置项 | 中和前(实测命中) | 中和后 | 手法 | 判据 |
|---|---|---|---|---|---|
| 1 | `filter.<n>.clean/smudge/process` | 裸 `add -A` 命中 | 不命中,`rc=0`, **index 内容原样** | 枚举后逐名置空 + `required=false` | 标记 + `show :f.txt` == `hello2` |
| 2 | `diff.<n>.textconv` | 裸 `diff`/`diff HEAD`/`show`/`log -p` 全命中 | 全不命中,`rc=0` | **`--no-textconv` flag** | 标记 |
| 3 | `diff.<n>.command` | 裸 `diff`/`diff HEAD` 命中 | 不命中,`rc=0` | **`--no-ext-diff` flag** | 标记 |
| 4 | `merge.<n>.driver` | 裸 `merge --no-edit` 命中 | 不命中,`rc=1`,`U=f.txt`,三个 stage 齐全 | 逐名置空 | 标记 + `diff --diff-filter=U` |
| 5 | `core.hooksPath` | `commit -m` 命中 `pre-commit` **和** `post-commit` | 不命中,`rc=0` | 指向一个**空目录** | 标记(两条 hook 都在) |
| 6 | `commit.gpgsign` + `gpg.program` | `commit -m` spawn 出 `ARGV:--status-fd=2 -bsau deadbeef` | 不 spawn,`rc=0`,普通提交正常 | `commit.gpgsign=false` | 标记 + 探针记录到的 argv |
| 7 | `core.fsmonitor` | 裸 `status` **和** 裸 `add -A` 都命中 | 两条都不命中 | 置空 | 标记 |
| 8 | `GIT_CONFIG_COUNT/KEY_n/VALUE_n` | 裸 `add -A` 命中一个**枚举看不到**的 driver | 不命中 | **从 env 删除** | 标记,且 `report()['filter_names']==[]` 证明枚举确实看不到 |
| 9 | 用户级 `~/.gitconfig` 里的 `filter.*` | `HOME` 指过去时 `add -A` 命中 | 闸关:不命中;闸开:不命中(靠枚举) | 闸关时 `GIT_CONFIG_GLOBAL` 指向不存在路径;闸开时枚举 | 标记 |

### 附带覆盖(纵深防御,**未实测触发**,如实标注)

| 配置项 | 状态 |
|---|---|
| `core.editor` / `sequence.editor` | `commit -m` / 无 tty `commit` / `rebase -i` 实测**都不触发**。仍覆盖 |
| `core.pager` | 实测不触发(`capture_output` + `GIT_PAGER=cat` 已在)。仍加 `core.pager=cat` 双保险 |
| `alias.<n>` | 实测 `alias.status` / `alias.log` **劫持不了**内置动词(2.50 内置优先)。仍覆盖 |
| `core.attributesFile` | 指向不存在的路径,挡全局 attributes。**树内 `.gitattributes` 挡不掉**(见 §4) |
| `tag.gpgSign` | 覆盖(`tag -a` 实测不触发) |

### 闸控类(闸管「能不能联网」,中和管「仓库配置能不能执行」——两回事)

| 配置项 | 手段 | 证据 |
|---|---|---|
| `credential.helper` | **闸关**时 `-c` 置空 | 实测**能压住**:stderr 从 `git: 'credential-...'` 变成 `could not read Username ... prompts disabled`。**这一条 E 线没测出来**(它在 DNS 就失败了) |
| `core.sshCommand` | **闸关**时置空 | 实测 `ls-remote git@host:repo` **命中**;本地路径 remote **不命中**。闸开时保留用户的,否则 https/ssh 凭证全废 |

---

## 3. 没中和什么(这一栏不许空)

| 项 | 状态 | 残余风险需要什么前提 |
|---|---|---|
| `uploadpack.packObjectsHook` | 加了覆盖,**离线测不出触发** | 需要真实 remote fetch。本地路径 remote 上实测 MISS。**不声称已中和** |
| `core.gitProxy` | 加了覆盖,**离线测不出触发** | 同上(要 `git://` 协议)。**不声称已中和** |
| **枚举读不全 = 漏** | 自写的容错解析器 | 配置用了 include/includeIf 之外的机制,或解析失败。`report()['parse_error_count']` 非零即打折 |
| **TOCTOU** | 按 `(mtime, size)` 缓存 | 两次 git 调用之间换掉 `.git/config`。威胁模型里配置是**静态**到手,接受,但不是零 |
| `include.path` 深度 > 8 / 有环 | 截断 | 构造超过 8 层的 include 链 |
| 子段名含 `-c` 解析不了的字符 | 未测 | 名字里带 `=`、换行等。名字**带空格**已测可覆盖 |
| `.git/config` 本身 | **读不掉,只逐键覆盖** | 写一个我没想到的可执行键 |
| 树内 `.gitattributes` | **读不掉** | 它写 `filter=X`/`merge=X`/`diff=X` 照样选中 driver。**门槛在 config 那一半,属性文件是已提交文件、clone 会带** |
| `GIT_CONFIG_GLOBAL` 旧 git 不认 | 靠枚举兜 | git < 2.32 |
| 中和后 merge 的**冲突 UI** | 4/6 配置 `rc=1`+stage 齐全;**6 项全中的那一档 `rc=0`/无冲突,未完全定因** | 见 §6 待办 |

### 硬门槛(不夸大威胁模型)

`core.fsmonitor` 的真实路径是**下载/解压/拷贝/U 盘/共享盘**,不是 clone ——
源仓设了 fsmonitor,`git clone` 后 `git config --get core.fsmonitor` 为空(此结论来自 E 线,我未独立复测)。
`filter.*` / `textconv` / `merge.*` **没有这个门槛**(属性文件是已提交文件,clone 会带)。

**不宣称 RCE。** 全程只证明「配置指定的程序被 spawn 了」,没构造利用链。

---

## 4. 「看起来像防线其实不是」——专门找的同类

E 线给了两处,我又找出四处:

| # | 位置 | 它**看起来**像 | 实际是 |
|---|---|---|---|
| 1 | `gen.py:59` 注释 | 「`merge.ours.driver` / `diff.external` 等**一律不碰**,只读原始字节」——读起来像安全声明 | **方向反了**。「不传值」≠「不执行」。实测仓里 `merge.drv.driver` 指向探针时裸 `merge` 就命中。**已就地加警告注释** |
| 2 | `server.py:501`(原) | 在 `init()` 里 `git config commit.gpgsign false`,看着像关掉了签名 | **只对 aigit 自己 `init()` 出来的仓生效**。别人给的仓上从没跑过。E 线已指出,现由 gitguard 全局覆盖 |
| 3 | `conflicts._GIT_CONFIG` / `gen._GIT_CONFIG` 里的 `commit.gpgsign=false` | 像是安全项 | 是**功能项**(免得卡在密码输入)。安全那一层此前不存在 |
| 4 | `server_base.py`(117 KB,未跟踪) | 和 `server.py` 几乎一样的 `Repo._run_full`,连注释都一样 —— 读目录树的人会以为**它也被覆盖了** | **没有任何文件 import 它**(全库只在一个测试的 docstring 里被提到)。它保留着**未加固**的原始 `_run_full` |
| 5 | `GIT_EDITOR` / `GIT_SEQUENCE_EDITOR` | `history_ops` / `conflicts` 都设了,像是想到过编辑器这一类 | **`server.py` 当时没设** —— 一处真实的不一致。实测 `-m`/`--no-edit` 挡住了触发,但这是运气不是防线。现由 gitguard 统一设 |
| 6 | `test_aigit_net.py:566` 的 `-c core.hooksPath=/tmp/evil` | 像在测 hooksPath 中和 | 是拿这句话当**畸形 remote 名**测 argv 注入 |
| 7 | `test_aigit_net.py:526-541` | 像在测配置值传给 git 前被中和 | 测的是 **`NetGate`**,不是中和 |
| 8 | `NetGate` 整体 | 「能力闸」听着像沙箱 | **闸控,不是中和**。闸开着时仓库配置照样会被执行 —— 所以 gitguard 对远程动词**也**生效,闸只管「这次动作」 |
| 9 | `gitops._FallbackGuard`(我自己新加的) | 名字叫 Guard,和 `gitguard` 长得一样 | **能力不等**。它不枚举,所以 `filter.*` / `merge.*` 仍然执行(实测 HIT)。已在上表标明,并在类 docstring 里写明「不是等价替代」 |
| 10 | `importlib` 那条路(**我差点走**) | 能让 `c3` 变绿 | 会让测试**报绿而性质为假**。没走,记录在此以免后人当成捷径 |

---

## 5. 推翻了三条既有说法(都会影响正确性)

### 5.1 `--no-ext-diff` **压不住** textconv;`--no-textconv` **也压不住** `diff.<n>.command`

任务书和 E 线都只测了一个方向。实测(旗标放在子命令**之后**):

| driver | 裸 | 只 `--no-ext-diff` | 只 `--no-textconv` | 两个都给 |
|---|---|---|---|---|
| `diff.X.textconv` | **命中** | **命中** | 不命中 | 不命中 |
| `diff.X.command` | **命中** | 不命中 | **命中** | 不命中 |

**两个 flag 必须同时给,只给一个等于没给。** E 线说的 D5(`--no-ext-diff` 压不住)在
「配 `--no-textconv`」时成立;反过来也成立。

⚠️ 旗标**必须放在子命令之后**。放前面 git 报 `unknown option`(`rc=129`)——
我第一版就踩了,而**标记确实没命中**,只看标记会误判成成功。

### 5.2 `-c diff.X.*=` 走不通:diff 直接 `rc=128`

`error: cannot spawn : No such file or directory` / `fatal: unable to read files to diff`。
**所以 `diff.*` 这一族只能靠 flag,不能靠 `-c`**(flag 仍然保留为纵深防御)。

### 5.3 `-c filter.X.required=false` 单独用**无效**,而且**会改坏内容**

实测:标记照样命中,且 `show :f.txt` 内容已被篡改(`content_intact=False`)。
必须覆盖 `clean`/`smudge`/`process` 三个键;空值语义是「当作没有 filter」。

---

## 6. 接进哪些调用点(自查过)

**任务书说四个文件,实际是五个。** `gen.py:84` 也是真 git 调用点,而且 `server.py` **import 了 gen**。

| 文件 | 位置 | 走法 |
|---|---|---|
| `server.py` | `_run_full` (455) | `gitguard.for_root` |
| `conflicts.py` | `Git.run` (242) / `Git.cat_batch` (275) | 本模块新增 `_argv()` / `_env()` 单一入口 |
| `gitops.py` | `_run` (421) | **注入**的 guard,`net_enabled = (level != "local")` |
| `history_ops.py` | `_run` (158) | `gitguard.for_root` |
| `gen.py` | `Git.run` (93) | `gitguard.build(extra_pre=_GIT_CONFIG)` |

### gitops.py 为什么是「注入」而不是 import

`test_aigit_net.py` 的 `c3` 用 **`ast.walk`** 检查 gitops 只 import 标准库、不 import
任何 aigit 模块 —— 理由写在用例注释里(防逃逸闭环的纵深隔离)。
`ast.walk` 会走到**函数内部**的 import,所以「延迟 import」也一样被算。

我第一版直接加了 `import gitguard`,`test_aigit_net` 从 56/56 掉到 **55/56**。两条路可选:

1. `importlib.import_module("gitguard")` —— 能骗过 AST 检查,让测试变绿;
2. **依赖注入**(`GitOps(..., guard=gitguard)`,由 `server.py` 传进来)。

选 2。理由:路径 1 会让测试**报绿而它的性质是假的** —— 那正是本任务要找的
「看起来像防线其实不是」,我不能自己造一个。路径 2 让 `gitops.py` 真的保持
零 aigit 依赖,测试的意图被尊重而不是被绕过。

代价:`guard=None`(例如测试直接 `GitOps(root, gate)`)会退到 `gitops._FallbackGuard`。
**那一档的能力边界是实测出来的,不是断言的**:

| 配置项 | 降级档(`guard=None`) | 注入 gitguard |
|---|---|---|
| `core.fsmonitor` | 不命中 ✔ | 不命中 ✔ |
| `core.hooksPath` | 不命中 ✔(`rc=0`) | 不命中 ✔ |
| `gpg.program` + `commit.gpgsign` | 不命中 ✔(`rc=0`) | 不命中 ✔ |
| `diff.<n>.textconv` | 不命中 ✔(靠 flag) | 不命中 ✔ |
| `diff.<n>.command` | 不命中 ✔(靠 flag) | 不命中 ✔ |
| **`filter.<n>.clean`** | **命中 ✘** | 不命中 ✔ |
| **`merge.<n>.driver`** | **命中 ✘** | 不命中 ✔(`rc=1`) |
| 正常 `add`/`commit`/`status`/`log` | 全 `rc=0` | 全 `rc=0` |

差的那一样东西是**枚举** —— git 配置里没有通配符,那三族的子段名只能靠解析
`.git/config` 拿到,而降级档是纯标准库、不做这件事。
**所以降级档是「别把事情做得更糟」,不是等价替代**;生产路径(server.py)一律注入。

自查判据:对 5 个产品文件 `Select-String 'subprocess\.run'` 得 **6 处**,逐处看
紧随其后的 argv 构造,全部经过 gitguard。`smoke.py:294` 是 `taskkill`,不是 git。

### 原有行为逐条保留(不是「换成 gitguard 就完事」)

- `GIT_PAGER=cat` / `GIT_TERMINAL_PROMPT=0` / `GIT_EDITOR` / `GIT_SEQUENCE_EDITOR` /
  `GIT_MERGE_AUTOEDIT` / `GIT_TERGE_AUTOEDIT` —— 全部在 `_ENV_KEEP` 里,语义不变。
- `--literal-pathspecs`(conflicts)、`_GIT_CONFIG` 的 `user.*`/`autocrlf`/`safecrlf`
  (conflicts/gen)—— 走 `extra_pre`,原样保留。
- **`server.py` 原来没有 `GIT_EDITOR`**,现由 gitguard 补上(见 §4 #5)。

### 一处必要的连带修改

`safe_env()` 在闸关时把 `GIT_CONFIG_GLOBAL` 指向不存在的路径 → **用户级配置整个不可达**
→ 提交身份只能来自仓级。所以 `server.Repo.init()` 里写 `user.email`/`user.name`
**比以前更要紧**(已在原处加注释)。实测 `history_ops` 的 `commit --amend` 正常(`rc=0`)。

---

## 7. 回归

私有 TEMP `C:\Users\Public\tmp\aigit-guard`,不联网。

| 套件 | 结果 | 备注 |
|---|---|---|
| `test_aigit.py` | **58/58 通过**,FAIL 0,ERROR 0 | |
| `test_conflicts.py` | **14/14 通过** | conflicts.py 改动最多的模块 |
| `test_history_ops.py` | **37/37 通过**,失败 0,异常 0 | |
| `test_f06_guard.py` | **10/10 PASS**,FAIL 0 | 并发/临界区 |
| `test_aigit_net.py` | **56/56 通过**,失败 0 | 修完注入后重跑,由 55/56 恢复 |

接线后的正向功能抽查(`rc` 全部 0):
`status` / `ls-files` / `add -A` / `commit` / `diff` / `log` / `show` / `rev-parse` /
`cat-file` / `symbolic-ref`;`conflicts.cat_batch` 仍返回 `b'hello'`;
`show HEAD` 与 `log -p -1` **仍输出真 patch(27 行)**,不是被 flag 掏空;
`history_ops` 的 `commit --amend` 正常(身份改由仓级配置提供)。

### 探针汇总(每条都要求「标记状态 + rc」同时成立)

| 探针 | 结果 |
|---|---|
| H(gitguard 本身,9 类触发) | 38/40 —— 2 条是**探针自身**设计错(H6/H9),已由 I 取代 |
| I(gpg / env 注入 / 修正) | **10/10** |
| J(用户级配置 + `_home(base)` 修正) | **9/9** |
| K(**产品代码路径**:5 个类) | **31/31** |
| M(merge 路径逐步二分) | 守卫路径全 MISS;仅 2 条**故意裸跑**的 `git diff` EXEC |
| N(`uploadpack.packObjectsHook` / `core.gitProxy`) | 离线**测不出触发**,已标注未验证 |
| O(`_FallbackGuard` 覆盖面) | **15/15** |

---

## 8. 待办 / 建议

1. **合并后的冲突 UI 要人看一眼。** 6 项配置全中的那一档,中和后 merge 是 `rc=0`/无冲突
   (stage 为空);只污染 `merge.*` 时是 `rc=1` + `U=f.txt` + stage `base/mine/other` 齐全。
   两者都**不执行**外部程序(安全目标达成),但产品行为不一致,**我没完全定因**。
   aigit 的卖点就是冲突演示,这条建议优先。
2. **`server_base.py` 要么删,要么加个「未加固」的横幅。** 它现在是个陷阱。
3. `gitguard.report(root)` 建议在 `/api/status` 里露一眼 `parse_error_count`,
   让「枚举打折」可见,而不是静默。
4. 联网闸打开时的 `uploadpack.packObjectsHook` / `core.gitProxy` 需要一次**联网**复测
   (本次硬约束不联网,已明确标注未验证)。

---

## 9. 纪律声明

- **实测通过**:§2 全部 9 条 + §3 的 `credential.helper`/`core.sshCommand` +
  §5 三条推翻 + §6 的 `_FallbackGuard` 覆盖面表 + §7 五个套件与七个探针。
  每条都能追到探针文件、argv、退出码与判据。
- **改了但没验证**:`uploadpack.packObjectsHook`、`core.gitProxy`(本地 remote 上
  触发不了,要联网);§6 `net_enabled=True` 的真实远程凭证链路。
- **只是分析**:§4 里 #4(`server_base.py` 无引用)是用全库 import 检索确认的,
  其余是对**代码文本**的判断;#1/#2/#5 有实测支撑。
- **`core.fsmonitor` 的 clone 门槛**沿用 E 线结论,我**未独立复测**。
- **不宣称 RCE。** 全程只证明「配置指定的程序被 spawn 了」,探针只写标记文件。

### 硬约束核对

| 约束 | 状态 |
|---|---|
| 不联网 | ✅ 全部本地仓;`uploadpack`/`gitProxy` 因需要联网而**明说没测** |
| 私有 TEMP | ✅ `C:\Users\Public\tmp\aigit-guard`,收尾已删 |
| 不改 `web/**`、不改 `test_*.py` | ✅ 我只新增/改了 `gitguard.py`、`doc/`、5 个产品模块。`test_aigit_gate.py`(17:33)与 `test_aigit_concurrency.py`(17:01)**不是我建的** |
| 不 commit / 不 push | ✅ |
| 全局 git 配置 | ✅ `~/.gitconfig` mtime **2026-07-23 16:13:11**,远早于本会话;内容无危险键;**未改动** |
| 端口 | ✅ 8765 上是 `receiver.py`(2026-10-01 起的既有服务,**不是我的,没动**);测试起的 aigit server 已随测试退出 |

### 协作风险(必须提醒)

- **`conflicts.py` 归 F-06 线所有**。我在里面加了 `_argv()` / `_env()` 两个模块级函数
  并改了 2 处 `subprocess.run`。**对方后续编辑可能覆盖掉**——收口时要重新核一遍
  §6 表里的 6 个接入点。
- `git diff --stat` 对 `conflicts.py` 显示 455 行、`server.py` 435 行,
  **那里面大部分不是我的改动** —— 会话开始时 `git status` 就已经把它们标成 `M`
  (F-06 等线的未提交工作)。我的改动只是其中很小一部分。
- 我全程**没有 commit**,所以这些改动和别人的未提交工作混在同一工作区里。

### ⚠️ 我把 `test_aigit_mutation.py` 的行号锚点搞失效了(必须转给那条线)

`test_aigit_mutation.py` **不是测试套件**,它是变异目录:
`Mutation(target=..., edits=[Edit(lineno, expect子串, new)])`,
由 `mutation_runner` 加载。它按**行号**定位编辑点,自己文档里写明
「行号对不上或该行不含 expect 子串就报 **DRIFT** 不动笔」。

**我的插入改了行号。** 实测(`check_anchors.py`,15 条变异 / 15 个锚点):

| 版本 | EXACT | SHIFTED | GONE |
|---|---|---|---|
| `HEAD`(提交基线) | 9 | 6 | 0 |
| 工作区(**含我的改动**) | **0** | 3 | 12 |

**所有 15 条的 `expect` 子串都还在文件里**(我按 `[-8,+60)` 窗口搜才误报 GONE;
`server.py` 实际位移到 +218,窗口不够)。所以**全部可以靠改行号恢复**,
没有一条需要重写。实测当前行号:

| 变异 | 文件 | 锚点 | 现址 |
|---|---|---|---|
| `S-ref-colon-inject` | server.py | 190 | 264 |
| `S-resolve-rootself-off` | server.py | 458 | 553 |
| `S-resolve-gitdir-off` | server.py | 460 | 555 |
| `S-discard-pin-plain-off` | server.py | 683 | 785 |
| `S-discard-dir-guard-off` | server.py | 770 | 865 |
| `S-commit-leak-guard-off` | server.py | 854 | 1016 |
| `S-checkout-branchexists-off` | server.py | 884 | 1045 |
| `S-branch-seed-prefix-off` | server.py | 940 | 1142 |
| `S-api-file-dir-e23-off` | server.py | 1072 | 1290 |
| `H-validate-refname-slash-off` | history_ops.py | 193 | 202 |
| `H-require-confirm-off` | history_ops.py | 385 | 395 |
| `G-assert-prefix-off` | gen.py | 296 | 305 |
| `C-abs-dotdot-off` | conflicts.py | 302 | 537 |
| `C-abs-gitdir-off` | conflicts.py | 311 | 544 |
| `U-esc-quote-off` | **web/index.html** | 466 | 542 |

**我该负多少责**:`gen.py` +9、`history_ops.py` +9/+10 是我的;
`server.py` 我加了约 +35(而总位移最大 +218)、`conflicts.py` 我加了约 +30(总位移 +233);
`web/index.html` 的 +76 **完全不是我的**(我没碰 `web/**`),
说明工作区的漂移**大部分来自别的线的未提交改动**。

`Edit` 自带 `expect` 子串,锚点漂移会被发现而不是静默改错行 ——
所以这是**麻烦,不是正确性事故**。但那条线要重新校准一遍才能跑。
