# aigit —— 本地 git 图形工具（零参数可跑）

`aigit` 是一个只跑在本机的 git 前端：`server.py` 用标准库起一个小 HTTP 服务，
浏览器打开页面后可以看改动、暂存、提交、造分支、试合并。
没有数据库，没有账号，没有网络请求。

**先分清楚两层覆盖范围 —— 界面和 API 不是一回事：**

| | 网页界面 | HTTP API |
|---|---|---|
| 改动 / 暂存 / 提交 / 分支 / 合并 / 提交历史 | ✓ 三个标签页 | ✓ |
| 冲突解决 | — | ✓ 8 条路由 |
| stash / amend / revert / 标签 / reflog / blame | — | ✓ 18 条路由 + 1 个 `plan/<op>` 预演模式 |

（路由数对着 `server.py` 数过：冲突 8 条；历史 18 条具体路径，外加 `plan/<op>` 这一个
前缀模式服务 9 个危险操作，合计 19 个 URL 模式。改路由时记得同步这个数字。）

**界面上点不到，不等于功能不存在，但也不等于有图形界面** —— 第二类操作目前只能手工发请求。
路由表见「冲突解决与历史操作」一节。

**永远不会自动打开浏览器，也永远不会弹任何窗口。** 启动器只在控制台打印一行
`http://127.0.0.1:8777/`，你自己复制到浏览器地址栏。这是刻意的：多线并行开发时
一个突然弹出的浏览器窗口会把终端输出冲掉。

---

## 快速开始

```
双击 tools\aigit\aigit.bat
```

三行之内应该看到：Python 版本探测结果 → 仓库路径 → `http://127.0.0.1:8777/`。
把那个地址复制到浏览器就完事了。

**第一次请用演练仓**，别直接在工作仓上点按钮：

```
tools\aigit\aigit.bat -Sandbox
```

`-Sandbox` 会把目标目录整个删掉、重建成一个全新的 git 仓（带一次初始提交）。
目录位置用 `-Repo` 指定，默认 `%TEMP%\aigit-sandbox`。

PowerShell 版功能一样，参数更多：

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\aigit\aigit.ps1 -Sandbox -Pause
```

`-Pause` 会在退出前等一下回车，脚本跑完不会一闪而过。

### 端口

默认 `8777`。被别的程序占了就换：

```
tools\aigit\aigit.bat -Port 9000
```

---

## 冲突解决与历史操作

`conflicts.py` 与 `history_ops.py` 是两个独立模块（各自有自己的对抗性测试套件），
`server.py` 接线后才对外可达。接线前这些接口全是 404 —— 模块测得再好，产品里够不着不算数。

**冲突解决**（路径参数一律先过 `resolve_in_root`：`.git` 拦截与联接拦截同样生效）

| 接口 | 做什么 |
|---|---|
| `GET /api/conflict/state` | 合并态快照：在不在合并中、参与分支、还剩几个没解决 |
| `GET /api/conflict/list` | 冲突文件清单（每项带三栏/块数/是否已核对等摘要） |
| `GET /api/conflict/inspect?file=` | 单个文件的三方全文与逐块对齐 |
| `POST /api/conflict/resolve` | 写回用户编辑后的内容并 `git add`。**`content` 必须显式给** |
| `POST /api/conflict/choose` | 整份取一侧（`side` ∈ `ours` / `theirs` / `base`） |
| `POST /api/conflict/delete` | 「一边删一边改」时标记删除 |
| `POST /api/conflict/commit` | 提交合并结果；还有未合并路径就 400 |
| `POST /api/conflict/abort` | 放弃合并 |

**历史与危险操作**（`history_ops.py`）。破坏性的几步是 `plan` / `do` 两步设计：
`GET /api/history/plan/<op>` 只读预演，`POST` 那一步必须带 `confirmed: true`，
**没有一步到位的入口**。可选 `<op>`：`stash` / `stash-drop` / `amend` /
`undo-last-commit` / `reflog-restore` / `revert` / `branch-rename` / `branch-delete` / `tag-delete`。

| 接口 | 做什么 |
|---|---|
| `GET /api/history/reflog` / `tags` / `stashes` | 三张清单 |
| `GET /api/history/blame?file=&start=&end=` | 逐行归属 |
| `GET /api/history/file?file=&limit=&follow=` | 单个文件的提交史 |
| `POST /api/history/stash` / `stash/apply` / `stash/pop` / `stash/drop` | 暂存一组 |
| `POST /api/history/amend` / `undo-last-commit` / `reflog/restore` | 改写或回退 |
| `POST /api/history/revert` / `revert/abort` | 反向提交 |
| `POST /api/history/branch/rename` / `branch/delete` | 分支增删改名 |
| `POST /api/history/tag/create` / `tag/delete` | 标签增删 |

两处接线时特意加严、值得记一笔的地方：

- **`/api/conflict/resolve` 的 `content` 必须是字符串且必须显式给。**
  `resolve()` 内部写的是 `(content or "").encode()`，于是「漏传字段」与
  「用户故意把文件清空」在模块里是同一个结果：落一个 0 字节文件。清空是合法结果
  （两边都删），所以不拒；但漏传是客户端 bug，让它变成静默数据丢失是接线引入的坑。
- **请求体里的布尔开关只认真 `true` / `false`。** 这些开关里有 `confirmed`，
  而 `"confirmed": "false"` 在 Python 里是**真的**。用 `bool(x)` 收字段等于把
  「前端把 false 序列化成字符串」这个极常见的 bug 变成一次误删。

---

## 可选：性能层（默认**不生效**）

`aigit_perf.py` 是一个**可选**的性能增强层，给大仓加三样东西：

| 能力 | 接口 | 做什么 |
|---|---|---|
| 状态分页 | `GET /api/status?offset=&limit=` | 大仓一次只回一页，附 `page` / `totals` |
| diff 截断 | `GET /api/diff?file=&maxLines=` | 超长 diff 只回头部，如实报告砍掉多少 |
| status 快路径 | 同上 | `git status` 从两次子进程减到一次（分支名从同一次输出里拿） |

**它默认不在启动路径上。** `aigit.bat` / `aigit.ps1` / `python server.py`
三条都不调用 `install()`，所以上面三样一个都没生效。这是刻意的：性能工具应当
**可选**，不该每次启动都跑（详见 `doc/aigit-E13-perf-layer.md` §4）。

**要开就显式开，从仓库任何位置都能跑（不用先 `cd` 进 `tools\aigit`）：**

```
python tools\aigit\aigit_perf.py --serve --root <仓库> --port 8777
```

`--root` / `--port` / `--sandbox` 原样转交 `server.py`；本文件自己认的额外参数：

| 参数 | 默认 | 含义 |
|---|---|---|
| `--max-diff-lines N` | 5000 | 单文件 diff 超过这么多行就截断 |
| `--page-limit N` | 200 | 只给了 `offset` 没给 `limit` 时用多少条 |
| `--no-fast-status` | 关 | 保留 `server.py` 原有的 v1 解析器（做对照测量用） |

**不开就还是普通 aigit，功能不受任何影响。**

跑它之前先看一眼自检（纯内存，不建仓、不碰 git、几毫秒，有失败退 1）：

```
python tools\aigit\aigit_perf.py
```

想看它到底省了什么，看 `doc/aigit-E13-perf-layer.md`；要提醒的两点：
**它不是基准测试**（文件里一行计时代码都没有），以及**它靠 monkeypatch 与
`server.py` 耦合**——`server.py` 改路由时它不会自动跟着改。

---

## 启动器参数

`aigit.bat` 和 `aigit.ps1` 认同一套核心参数。批处理版帮助是中文，PowerShell 版是英文
（原因见文末「为什么 .ps1 是英文的」）。

| 参数 | 含义 |
|---|---|
| `-Repo <路径>` | 要打开的仓库。默认 `%TEMP%\aigit-sandbox`。 |
| `-Port <端口>` | 监听端口，1–65535。默认 `8777`。 |
| `-Sandbox` | 把 `-Repo` 指向的目录**整个删掉**重建。指向配了远端的真仓库会被拦下。 |
| `-Force` | 跳过下面「安全须知」里的两道自动拦截。 |
| `-h` / `-Help` | 打印帮助，不启动。 |
| `-Python <exe>` | （仅 ps1）显式指定 Python 3，可用于绿色版 / embeddable 版。 |
| `-ServerArgs <string[]>` | （仅 ps1）额外参数，原样转交 `server.py`。 |
| `-Pause` | （仅 ps1）退出前等回车。 |

批处理版**不带任何参数直接双击即可运行**，仓库和端口都用默认值。
它会依次尝试 `py -3`、`python`、`python3`，谁先成功用谁。
不认识的参数不报错，原样透传给 `server.py`（保持服务端可扩展）。

---

## 界面说明

页面分四块：顶栏、左侧文件列表、右侧三个标签页、底部提交框。

### 顶栏

| 元素 | 怎么造 | 怎么验 | 期望结果 |
|---|---|---|---|
| 仓库路径 `repoPath` | 启动时由 `-Repo` 决定 | 看它显示的绝对路径 | 与你传的 `-Repo` 一致 |
| 分支下拉 `branchSel` | `-Sandbox` 生成后自动出现 | 点开看列表 | 列出 `main` 和造出来的 `aigit_gen/...` 分支 |
| `⟳ 刷新` | 手动点，或在另一个终端改完文件后点 | 文件列表是否更新 | 状态、文件列表、分支下拉三处一起刷新 |

### 「测试数据」按钮条

这五个按钮是 aigit 的核心用途：**造出能验证合并语义的测试数据**。
生成器自己会先跑一遍真合并，把结论写进每个场景的 `verified` 字段，页面上直接显示
「✓ 通过 / ✗ 未达预期」。

| 按钮 | 造什么 | 怎么验 | 期望结果 |
|---|---|---|---|
| `生成 · 可合并变更` | 两个分支改**同一文件的不同区域**（A 改头部 3 行、B 改尾部 3 行，中间隔 ≥20 行未触碰） | 到「合并」标签，点分支右侧「合并进来」 | 合并成功，工作区干净，目标文件里**没有**冲突标记 |
| `生成 · 无关变更` | A 改一个文件、B 改另一个文件 | 同上 | 合并成功，**两个文件的改动都在** |
| `生成 · 增删变更` | A 新增一个文件、B 删除**另一个**文件 | 同上 | 合并成功，新增的在、删掉的没了 |
| `生成 · 冲突变更` | A、B 改**同一行** | 同上 | **合并失败**，文件里出现 `<<<<<<<` 标记，「已暂存」区多出冲突条目 |
| `一键全生成` | 上面四类一次全造（每类 3 个场景） | 切到「合并」标签 | 场景表列出 12 行，每行有类型、分支、涉及文件、预期、生成器自检 |
| `丢弃全部改动` | 点它 | 弹确认框，逐条读内容 | 弹窗写明 `reset --hard HEAD` + `clean -fd`，点「全部丢弃」后回到最近一次提交 |
| `新建分支` | 点它 | 顶部输入名字 | 名字只允许字母数字和 `_ . / -`，非法会被服务端 400 拒绝 |

分支名固定形如 `aigit_gen/<kind>/<i>/a` 和 `.../b`，本次新生成的分支在列表里带
「本次生成」标签，当前分支显示 `✓ 当前分支`（不可点）。

### 左侧「源代码管理」

三段分组：**已暂存** / **未暂存** / **未跟踪**（`?` 标记）。

| 元素 | 怎么造 | 怎么验 | 期望结果 |
|---|---|---|---|
| 文件行 | 在仓库里改一个已跟踪文件 | 点 `⟳ 刷新` | 出现在「未暂存」，前面带 `M` |
| 行内 `暂存` | 点行尾的「暂存」 | 看分组 | 文件从「未暂存」移到「已暂存」，标记变 `A`/`M` |
| 行内 `取消暂存` | 对已暂存文件点一下 | 看分组 | 移回「未暂存」 |
| 行内 `✕ 丢弃` | 只在未跟踪文件行上出现 | 点它 | 文件被**直接删除**（不是回滚），确认框会说明 |
| 点文件名 | 点行本身 | 切到「改动详情」 | 出现左右分栏 diff |
| `全部暂存` | 点它 | 看 `sideCount` | 所有已跟踪改动进索引 |
| `全部取消暂存` | 点它 | 看分组 | 全部移出索引 |

### 标签页一：改动详情

左右分栏显示同一个文件的新旧内容，行为 `+` 绿、`-` 红。

| 元素 | 怎么造 | 怎么验 | 期望结果 |
|---|---|---|---|
| 左栏 / 右栏 | 点左侧任一文件行 | 看颜色 | 左边是 HEAD 版本，右边是工作区版本 |
| 空状态 | 什么都不改就点这个标签 | — | 提示「没有改动」而不是空白 |

### 标签页二：合并

| 元素 | 怎么造 | 怎么验 | 期望结果 |
|---|---|---|---|
| 场景表 | 先生成一批数据 | 看最后两列 | 「预期」是人话描述，「生成器自检」是 `✓ 通过` |
| 分支行内 `合并进来` | 点某分支的按钮 | 看下方「最近一次合并输出」 | 干净合并输出 `Merge made by the 'ort' strategy`；冲突时输出里出现 `CONFLICT` 且页面自动切过来 |
| `终止合并（merge --abort）` | 制造一次冲突后点 | 看工作区 | 回到合并前状态，冲突条目消失 |
| 合并后自动 | 完成一次干净合并 | 看标签页 | 自动切到「合并」并显示输出 |
| 当前分支行 | — | — | 显示 `✓ 当前分支`，按钮禁用 |

**验证顺序很重要**：生成器把两个分支都从同一个 base 拉出来，再把 `main` 回滚到 base。
所以在 `main` 上直接合并 A 是**快进**，git 压根不做内容合并，什么都验证不到。
正确做法是**先切到分支 A，再合并 B** —— 这时 A 和 B 是兄弟分支，合并才是真正的三方合并。
UI 里点分支行内按钮前，先用顶栏下拉切到 A。

### 标签页三：提交历史

最近 30 条提交，每条含 sha、作者、时间、subject。`刷新历史` 按钮手动重取。

### 底部提交框

| 元素 | 怎么造 | 怎么验 | 期望结果 |
|---|---|---|---|
| 提交信息框 | 点它打字 | — | 留空点提交会被拒（`不能为空`） |
| `提交` | 暂存一些改动，写好信息，点它（或 `Ctrl+Enter`） | 看历史标签 | 生成一条新提交，清空工作区 |
| `▾` | 点 `提交` 右边的小箭头 | 看菜单 | 两项：`提交（全部改动）`、`提交并查看历史` |
| `刷新历史` | 点它 | 看列表 | 立刻重取 |

**注意**：`/api/commit` 内部会先跑 `git add -A`，所以**「提交」会把未暂存的改动一起提交**，
不是只提交已暂存部分。这一点页面下方有常驻提示。

提交前还有一道拦截：`git add -A` 会**穿过目录联接**，所以工作区里只要有一个指向
仓外的联接（`mklink /J`、符号链接、挂载点），`/api/commit` 会返回 400 并列出那个路径，
**一个字节都不提交**。原因在 `Repo.escaping_links()`：不拦住的话，一次提交就会把
仓外文件复制进对象库，删掉联接也删不掉已经写进去的历史。这一点与已暂存/未暂存无关，
所以它拦的是整次提交而不是某个文件。

---

## 三类测试场景

| 场景 | 按钮 | 改了什么 | 期望结果 | 是 bug 吗 |
|---|---|---|---|---|
| 可合并 | `生成 · 可合并变更` | 同一文件的头部 3 行 vs 尾部 3 行 | **合并成功**，无冲突标记 | 是 bug |
| 无关 | `生成 · 无关变更` | A 改文件 X、B 改文件 Y | **合并成功**，两处改动都在 | 是 bug |
| 增删 | `生成 · 增删变更` | A 新增文件、B 删除另一个文件 | **合并成功**，增删都生效 | 是 bug |
| **冲突（对照组）** | `生成 · 冲突变更` | A、B 改**同一行** | **合并失败**，出现 `<<<<<<<` 标记 | **不是 bug，这是正确行为** |

### `conflict` 档为什么期望「失败」

这一档是**对照组**。前 3 档证明工具能处理"应该成功"的情况，这一档证明工具没有
一味地把所有合并都判成成功 —— 它能识别出真的冲突并如实报告。

所以看到 `conflict` 档合并失败，**不要去修它**。如果哪天它突然"成功"了，
那才是 bug：说明冲突检测失效了。判断标准很简单：

- 干净合并报「成功」，冲突合并报「失败」→ 工具对
- 两种都报「成功」→ 冲突检测坏了
- 两种都报「失败」→ 合并功能坏了

`smoke.py` 里这个对照组的检查分两层，顺序不能反：先看端点自身有没有抛异常
（响应里不能带 `error` 字段），再看文件里有没有冲突标记。只做第二层会出**假通过** ——
端点挂掉时返回 `ok=false`，而对照组的期望恰好也是"该有冲突"，两个坏现象撞在一起会被记成 PASS。

---

## 安全须知

**请先读这一节再点任何按钮。**

1. **服务只 bind `127.0.0.1`**，局域网和公网都访问不到。`server.py:406` 写死了
   `ThreadingHTTPServer(("127.0.0.1", a.port), Handler)`。没有鉴权，因为它假定只有
   本机能连。**不要用端口转发 / SSH 隧道把它暴露出去。**

2. **不要在有未提交重要改动的真实仓库上跑。** 启动器会检测：
   `git status --porcelain -uno` 有输出（已跟踪文件被改、被暂存、或有冲突）→ **直接拦下**，
   提示你先用 `-Force` 跳过。仅剩未跟踪文件 → 只**警告**不拦（因为 `discard` 对未跟踪文件
   是直接删，没有"回滚到上一版"这回事）。两道拦截都用 `-Force` 跳过。

   本仓库自己就是个真实例子：工作区里有别人正在改的 `interpreter/core/*.hpp`、
   `interpreter/main.cpp`、`test/arena_selftest.cpp` 等一堆未提交改动。在这种仓上跑
   `丢弃全部改动` 等于替别人删掉他们的活儿。

3. **两个操作不可撤销，没有 undo：**
   - `丢弃全部改动` / `/api/reset` → `git reset --hard HEAD` + `git clean -fd`
   - 逐文件 `✕ 丢弃` → 直接删除该文件

   前者会连带**删掉所有未跟踪文件和目录**。已提交的历史不受影响，但没提交的一律没了。
   两个操作都有确认弹窗，但弹窗点「全部丢弃」就真删了。

4. **先 `-Sandbox` 演练。** 养成习惯：

   ```
   tools\aigit\aigit.bat -Sandbox
   ```

   它把目标目录整个删掉重建。指向**配了远端的真仓库**会被拦下（`-Force` 可跳过）——
   这就是防止手滑敲成 `-Sandbox -Repo C:\work\important`。

5. **`-Sandbox` 本身是删目录操作。** 确认你的 `-Repo` 没指向任何有价值的东西。

6. **生成器只动 `aigit_gen/` 前缀的分支和 `aigit_gen_*.txt` 文件。**
   `python gen.py --root <repo> --clean` 只收拾这些，不会碰你自己的文件。

---

## 故障排查

### 「端口已被占用」

```
[错误] 端口 8777 已被占用(WinError 10048 那一类)。
       换一个:  aigit.bat -Port 9000
```

启动器在启动前会先探端口（批处理用 `netstat -ano -p TCP`，PowerShell 用
`TcpListener` 试 bind），所以你看到的是一行人话提示，而不是 `server.py` 抛出来的
Python 堆栈。找到占用者：

```
netstat -ano -p TCP | findstr :8777
```

最后一列是 PID，`tasklist | findstr <pid>` 看是什么程序。也可以直接换端口，
`--port` 只是个数字，不冲突就行。

### 「这不是一个 git 仓库」

```
[错误] 路径不存在:  C:\work\nosuchdir
[错误] 不是 git 仓库(没有 .git 目录):  C:\work\somedir
```

`-Repo` 指到了不存在的目录，或者指向了一个普通文件夹。两种修法：

```
aigit.bat -Repo C:\work\myrepo      # 指对
aigit.bat -Sandbox                   # 随便一个路径，删掉重建
```

注意是 `git init` 的那种仓（有 `.git` 目录）。从 GitHub / SVN 下载的源码包没有
`.git`，直接 `git init && git add -A && git commit` 一次就能用。

### 「找不到 Python」

```
[错误] 没找到 Python 3。

  去哪装:  https://www.python.org/downloads/
  装哪个:  3.10 或更新的 64 位版(3.12 实测没问题)
  关键一步: 安装向导第一页务必勾上 "Add python.exe to PATH",
            不勾的话这里就找不到 —— 这是最常见的失败原因。

  装完重开一个终端窗口(让新的 PATH 生效),再跑一次本文件。
```

启动器依次试 `py -3` → `python` → `python3`。上例是三个都被排除后才打这段话 ——
典型场景是 PATH 被裁剪过，或者 Python 装了但没勾 PATH。
绿色版 / embeddable 版可以显式指定：

```
powershell -File aigit.ps1 -Python C:\Tools\python-3.12\python.exe
```

### 中文乱码

`aigit.bat` 是 **GBK 编码**的，**不要用 UTF-8 保存它**。用记事本另存为 UTF-8 之后
所有中文提示都会变成乱码（或者直接语法报错）。

原因：`aigit.bat` 顶部有 `chcp 936`，把控制台代码页切成简体中文的 cp936，好让
GBK 字节正确显示。代价是它本身必须是 GBK。这是有意的取舍，详见
`tools\check-encoding.ps1` 里的规则（`.bat` 不参与编码门禁，因为它不在 PS 里跑）。

`aigit.ps1` 是**纯 ASCII**（一条中文都没有），UTF-8 无 BOM + CRLF。
原因见下一节。`smoke.py` 和本文件是 UTF-8 无 BOM + LF。

如果你在 Windows Terminal 里看到 `.bat` 的中文提示是 `?` 或方块，
先确认控制台字体里有微软雅黑；确认不了就改用 `.ps1`（纯英文，不会有这个问题）。

### 端口起来了但页面一直转圈

后端没起来。切到启动器那个终端窗口看有没有 Python 异常。
常见是仓库里 `core.autocrlf` 或 `core.hooksPath` 的配置把 `server.py` 的
`git status` 卡住了（smoke.py 用 120 秒超时兜着，超时会明确报出来）。

### smoke.py 报 `通过 54 失败 8`

先看每条 `[FAIL]` 下面的「期望 / 实际 / 响应体」三行。当前这 8 条失败**全部**
指向同一个缺陷，见下一节。

---

## 已知缺陷

### `/api/merge` 每次调用都抛 `AttributeError`

`server.py:220-226`：

```python
def merge(self, name):
    p = self._run(["merge", "--no-edit", name], check=False)
    return {"ok": p.returncode == 0, ...}
```

但 `server.py:63` 的 `_run` 返回的是 **`p.stdout`（`str`）**，不是 `CompletedProcess`：

```python
return p.stdout
```

所以 `p.returncode` 在一个 `str` 上取值，每次 `/api/merge` 都炸：

```
HTTP 500  {"ok": false, "error": "AttributeError: 'str' object has no attribute 'returncode'"}
```

`repo.py` 的 `_run` 在 `check=True` 时会抛 `GitError`，从设计上就不打算把
`CompletedProcess` 交出去。修法是让 `merge` 单独跑 `subprocess.run` 自己看
`returncode`，或者给 `_run` 加一个 `want_proc=True` 开关。

**当前状态：未修。** `smoke.py` 的 8 条失败全部是这一个原因：

```
[FAIL] 11  mergeable merge 端点自身不报错
       期望: HTTP 200 且响应里没有 error 字段(说明真的执行到了 merge 逻辑)
       实际: HTTP 500  error=AttributeError: 'str' object has no attribute 'returncode'
[FAIL] 12  mergeable 合并 B 进 A(应干净)
       ...
```

注意 `smoke.py` 特意把「端点自身没报错」和「合并结果对不对」拆成两条独立断言。
如果只判后者，对照组（`conflict` 档期望冲突）会因为 `ok=false` 而被误判成 PASS。
`smoke.py` 的 62 条断言里有 8 条因为这个缺陷红着，其余 54 条全绿。

### `/api/gen` 的 `branches` 字段是场景对象列表

`server.py:244-251` 把 `gen.py` 的 `scenarios` 映射回老前端要用的形状：

```python
out["branches"] = [{"file": ", ".join(sc["files"]),
                    "branches": list(sc["branches"]),
                    "expect": sc["expect"], "id": sc["id"],
                    "verified": sc["verified"]} for sc in out["scenarios"]]
```

所以 `branches` **不是分支名的列表**，是「每个场景一个 dict」的列表，
而且 `file` 是几个文件名用 `", "` 拼起来的字符串。要拿分支名得展平两层。
`scenarios`（`gen.py` 出的原始结构）信息更全，`verified` 字段就在那儿。
`web/index.html:1425` 已经两个都读、优先 `scenarios`；`smoke.py` 同理。

---

## 自检：smoke.py

纯标准库、无头、不需要浏览器。起一个 `server.py` 子进程打 HTTP，逐项打印**实际返回**。

```
cd tools\aigit
python smoke.py
```

```
[PASS] 01  server 起来并应答 health                -> 200 {"ok": true, "root": "...", "isRepo": true}
[FAIL] 11  mergeable merge 端点自身不报错
       期望: HTTP 200 且响应里没有 error 字段(说明真的执行到了 merge 逻辑)
       实际: HTTP 500  error=AttributeError: 'str' object has no attribute 'returncode'
       响应体: {"ok": false, "error": "AttributeError: ..."}
  通过 54    失败 8    跳过 0
  退出码 1
```

- 全部通过退 `0`，有任何失败退 `1`（可以直接接 CI）。
- 演练仓建在 `%TEMP%\aigit-smoke-<pid>`，跑完自动删；日志在同级 `.log`，也删。
- 子进程收尾走 `terminate → kill → taskkill /F /T` 逐级升级，保证不留僵尸。
- git 的 object 文件在 Windows 上是只读的，删除会失败，所以脚本自己实现了
  `force_rmtree()`（先 `os.chmod` 去掉只读位再删，最多重试 6 次）。
- 覆盖范围：`health` / `status` / `log` / `branches` / `diff` / `file` /
  `stage` / `unstage` / `discard` / `commit` / `branch` / `checkout` / `reset` /
  四类 `gen` + 各自的 `merge` / `merge-abort`，外加路径越界和 shell 注入的拒绝。

其他自检脚本（由其他开发线维护）：

```
python gen.py --root <repo> --kind all --count 2 --seed 42
python selftest.py
```

细节见 `gen-notes.md`。

---

## 文件清单

| 文件 | 编码 / 换行 | 作用 |
|---|---|---|
| `aigit.bat` | **GBK** + CRLF | 双击运行的启动器，中文报错。**别用 UTF-8 保存** |
| `aigit.ps1` | ASCII + UTF-8 无 BOM + CRLF | PowerShell 启动器，英文报错，参数更多 |
| `smoke.py` | UTF-8 无 BOM + LF | 纯 stdlib 无头自检，62 条断言 |
| `server.py` | — | HTTP 层，本机服务，只 bind `127.0.0.1` |
| `gen.py` | — | 测试数据生成器，不依赖 HTTP，可单独跑 |
| `aigit_perf.py` | UTF-8 无 BOM + LF | **可选**性能层（分页 / diff 截断 / status 快路径）。默认不挂，靠 `--serve` 显式开；不带参数跑 = 纯内存自检 |
| `web/index.html` | UTF-8 | 前端整个界面（单文件） |
| `gen-notes.md` | UTF-8 | 生成器设计与验证方法 |

### 为什么 `.ps1` 是英文的

两个原因，都不是偷懒：

1. **`tools\check-encoding.ps1` 的编码门禁要求 `.ps1` 是有效 UTF-8**，
   否则判 `FATAL`（退出码 1）点亮全仓检查。GBK 会直接挂门禁。
2. **PowerShell 5.1 对无 BOM 的文件按系统 ANSI 代码页（简体中文 = 936）解码。**
   一份「UTF-8 无 BOM」里含汉字的 `.ps1`，在 PS 5.1 眼里是 GBK 字节，
   汉字会被误认成引号 / 反引号一类结构字符 —— 报出来的错信息完全指不到真正的位置。
   本项目已经在这个坑上栽过两次。

两条加起来，`.ps1` 唯一稳的选择就是**纯 ASCII**：门禁过得去，PS 5.1 也解得对。
用户提示因此用英文；`.bat` 那份是 cmd，跑 cp936，用 GBK 存中文反而是对的。

`.bat` 不参与 `check-encoding.ps1` 的门禁（它的 `$expect` 表里没有 `.bat` 条目，
整个检查对 `.bat` 跳过），因为 cmd 的编码规则和 PowerShell 完全不同，
用同一把尺子量必然误报。
