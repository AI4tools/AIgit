# aigit —— 本地 git 图形工具（零参数可跑）

[![ci](https://github.com/AI4tools/AIgit/actions/workflows/ci.yml/badge.svg)](https://github.com/AI4tools/AIgit/actions/workflows/ci.yml)
[![release](https://github.com/AI4tools/AIgit/actions/workflows/release.yml/badge.svg)](https://github.com/AI4tools/AIgit/actions/workflows/release.yml)
[![license: GPL-2.0](https://img.shields.io/badge/license-GPL--2.0-blue.svg)](LICENSE)


`aigit` 是一个只跑在本机的 git 前端：`server.py` 用标准库起一个小 HTTP 服务，
浏览器打开页面后可以看改动、暂存、提交、造分支、试合并。
没有数据库，没有账号，没有网络请求。

> **目录布局（2026-11-04）**：产品实现是 C 版 `c/build/aigit.exe`，启动器默认跑它。
> Python 参考实现与它的测试已整体移到 `python-ref/`，只作为一致性判据的对照；
> 下文命令里的 `server.py` / `test_aigit.py` 等，实际路径都是 `python-ref/` 下的同名文件。

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

**默认不会自动打开浏览器，也不会弹任何窗口。** 启动器只在控制台打印一行
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

## C 版（可选，同一套 HTTP 契约）

仓库里还有一份 C 重写：`c/build/aigit.exe`，单个静态 exe、无运行时依赖。
它和 `server.py` 是**对等实现**（JSON/状态码/副作用按同一套判据对齐），
构建与运行：

```powershell
$env:PATH='C:\Environment\cpp\mingw64\bin;'+$env:PATH
mingw32-make -C c -j4
c\build\aigit.exe --root D:\path\to\repo --port 8899
```

支持 `--sandbox`、`--web <dir>`；Python 版 `server.py` 的全部路由（含 `/api/gen`、
`remote/{fetch,pull,push}`）与静态页都已接。怎么验、当前覆盖与已知差异见
`c/README.md`；一页交付视图见 `doc/aigit-delivery.md` 第 6 节。

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

### smoke.py 报失败

先看每条 `[FAIL]` 下面的「期望 / 实际 / 响应体」三行 —— 那三行直接写明了
判据要什么、实际拿到什么。**别按条数猜原因**：本文件历史上就因为「8 条失败
全是一个原因」这种推断栽过（见下一节的更正记录）。

---

## 更正记录

### ~~`/api/merge` 每次调用都抛 `AttributeError`~~ —— 已修（2026-10-06 核实）

> 本节此前写着「**当前状态：未修**」，并称 `smoke.py` 有 8 条断言因此红着。
> **那是过期的**，2026-10-06 实测 `smoke.py` 通过 62 / 失败 0 / 退出码 0。
> 保留这段是为了：**万一再看到同一个 AttributeError，知道该往哪儿看。**

当时的症状（保留原样）：

```
HTTP 500  {"ok": false, "error": "AttributeError: 'str' object has no attribute 'returncode'"}
```

原因：`Repo.merge` 调 `self._run([...], check=False)` 然后读 `p.returncode`，
而 `_run` 返回的是 **`p.stdout`（`str`）**，不是 `CompletedProcess`。

**实际采用的修法**：新增 `Repo._run_full(args, check, stdin)` 返回真正的
`CompletedProcess`；`Repo._run` 退化成它的 `.stdout` 包装（既有调用点一个字没改），
`Repo.merge` 改调 `_run_full`。2026-10-06 按 **函数名**核实（不用行号——行号会漂）：

- `Repo._run_full` 存在，`Repo._run` 的返回是 `self._run_full(args, ...).stdout`
- `Repo.merge` 里是 `p = self._run_full(['merge', '--no-edit', name], check=False)`
  之后才读 `p.returncode`
- 实测 `smoke.py` 的 merge 相关 8 条断言（11/12/20/21/29/30/38/39）全 PASS

> 教训与自查方式：这一节的原文把**行号**（`server.py:220-226`、`server.py:63`）
> 写进了判据，而行号必然随编辑过期 —— 同一份文件里另一段「横幅」也是这么
> 被自己的绝对值咬了三次。**引用代码位置请用函数名 + 一条复算命令，别写死行号。**

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
...
[PASS] 43  diff 看已暂存改动                        -> 200 新增行=["+smoke line 1", "+smoke line 2"]
[PASS] 44  commit 提交                          -> 200 output=[main 4d37b85] smoke: commit 1 file changed, 2 insertions(+) create mode 100644 smoke_edit.txt
[PASS] 45  log 出现新提交                          -> 200 首条 subject='smoke: commit' 共 5 条
...
  通过 62    失败 0    跳过 0
  退出码 0
```

> 上面是 **2026-10-06 的一次实跑输出**（当次 HEAD `2f0f2c1` + 本轮未提交改动），
> 用来展示每条长什么样，不是现状保证。`44 commit 提交` 会打印 `output=` ——
> 那是 git 的真实回显，这个键曾经丢过，见「更正记录」。

- 全部通过退 `0`，有任何失败退 `1`（可以直接接 CI）。
- 演练仓建在 `%TEMP%\aigit-smoke-<pid>`，跑完自动删；日志在同级 `.log`，也删。
- 子进程收尾走 `terminate → kill → taskkill /F /T` 逐级升级，保证不留僵尸。
- git 的 object 文件在 Windows 上是只读的，删除会失败，所以脚本自己实现了
  `force_rmtree()`（先 `os.chmod` 去掉只读位再删，最多重试 6 次）。
- 覆盖范围：`health` / `status` / `log` / `branches` / `diff` / `file` /
  `stage` / `unstage` / `discard` / `commit` / `branch` / `checkout` / `reset` /
  四类 `gen` + 各自的 `merge` / `merge-abort`，外加路径越界和 shell 注入的拒绝。

## 测试集：有哪些套件、怎么跑

`smoke.py` 是无头端到端演练；下面这些是**分层的套件**，覆盖比 smoke 细得多。
每个都能单独 `python <文件>` 跑，全部走 `%TEMP%` 下的临时仓，**不碰任何真实仓库**。

| 套件 | 干什么 | `--selftest` |
|---|---|---|
| `selftest.py` | 生成器（`gen.py`）自检：真合并 / 确定性 / 脏工作区拒绝 | 无 |
| `test_aigit.py` | 主套件，端点行为 + 路径越权 + 分支名白名单 | 无 |
| `test_aigit_extra.py` | 补充端点与边界 | 无 |
| `test_aigit_gate.py` | 闸门类判据 | 无 |
| `test_aigit_net.py` | 网络 / fetch / pull / 代理 | 无 |
| `test_aigit_web.py` | 前端行为（无头 DOM） | 无 |
| `test_aigit_concurrency.py` | 并发判据 C1~C6（锁、原子性） | 无 |
| `test_f06_guard.py` | F-06 仓外逃逸的**五道**防线 | 无 |
| `test_history_ops.py` | `history_ops.py` 的 stash / revert / branch / tag | 无 |
| `test_conflicts.py` | 冲突解决与完成合并 | 无 |
| `test_aigit_invariants.py` | 不变量（最快，1 秒） | 无 |
| **`test_aigit_contract.py`** | **响应形状契约**（见下） | **有** |

### `test_aigit_contract.py`：响应形状也是契约

**为什么单独立一个文件。** 2026-10-06 第一次真跑全套件时发现：`f155916`
（消除并发判据 C3）把 `/api/commit` 的响应从

```python
self._json({'ok': True, 'output': out, **REPO.status()})   # f155916^
```

改成 `self._ok_with_status(st, err)`，`out` 拿到手**一次都没再被读过**，
`output` 键就此从响应里消失。而 `web/index.html:2095` 一直读 `d.output`,
于是**每次提交前端都显示「(git 无输出)」**。那一版没有任何套件发现：
全仓只有 `smoke.py:513` 断言过它，其余 5 个套件都是 **0 处**。

所以「每个响应有哪些键」也得进测试：

```
python test_aigit_contract.py --selftest    # 只跑自证, 秒出
python test_aigit_contract.py               # 自证 + 真实端点(起 server 子进程)
```

它钉 8 个端点的必需键（`health` / `status` / `stage` / `commit` /
`checkout` / `branch` / `conflict/state` / `remote/ahead-behind`）。三条设计约束写在文件头，这里只说结论：

- **期望键集是写死的字面量**，依据是 `git show f155916^:server.py` 的历史
  handler 与 smoke.py 实测响应体。**不从现行 `server.py` 自动推导** ——
  那样就变成「拿被检查者的说明书去检查被检查者」，永远绿。
- **契约条数 < 6 直接抛**。0 项检查恒真，静默跳过比判错更险。
- **阳性对照先单跑**：缺键必被抓、键齐必放过、响应非对象必被抓，
  跑真实端点之前先证明这把尺子会响。

### `find_test_gaps.py`：找「零提及」的响应面

上面那条契约测试是**我挑着钉的**（当时 6 个端点）。问题是：**还有哪些响应键根本没人管？**
这个工具回答它。它取代的是我原来那套 grep 关键词的做法 —— 那套**三次被推翻**
（把注释里的同名字当成覆盖；`stash/pop` 说没测其实测了；拿 `_run_full` 去 doc 里找
可那段在 README 里）。三条根因逐条堵：

```
python find_test_gaps.py --selftest    # 只跑自证, 不碰网络, 秒出
python find_test_gaps.py               # 自证 + 真起 server 探面
```

- **只认 AST，不碰原文**：注释里的同名字不算数。
- **面来自行为**：真起一遍 `server.py --sandbox`，记录每个端点**实际**返回什么键。
  不从源码/文档推导 —— 那是「拿被检查者的说明书去检查被检查者」。
- **尺子先证明自己会响**：自证组 8 条，含阳性哨兵（会命中必须命中）、
  阴性哨兵（不存在的必须不命中）、注释哨兵（**不许**算进来）。

**它只测一个方向，刻意不反推**：

| 观察 | 能下什么结论 |
|---|---|
| 键在任何套件 AST 里都没出现 | **一定没被测**（下界，可信）|
| 键出现了 | **不等于**被测（出现 ≠ 断言）|

所以输出叫「零提及」，不叫「未覆盖」。它还单列一个**灰区**：原文里有、
AST 字符串常量里没有（比如只出现在注释里）—— 那正是 grep 会误判的坑，
交给人看，**不算零提及**。

它**不负责**发现「只被一个套件覆盖」这类脆弱面 —— 那正是 `/api/commit` 的
`output` 回归那一类（当时 `smoke.py:513` 确实断言了它）。那种面要靠**行为化的
变异测试**，见下节。

**首跑结果（2026-10-06，rc=0，2.4 秒）**：探 14 个端点 / 10 个返回带键 JSON /
**31 个键**参与核对，报出 **4 个零提及**键 + 1 个灰区：
| 档 | 键 | 出处 |
|---|---|---|
| 零提及 | `currentBranch` / `origHead` / `mergeMsgPath` | `/api/conflict/state` |
| 零提及 | `trackingRefExists` | `/api/remote/ahead-behind` |
| 灰区 | `behind` | 只在 `test_aigit_net.py` 的**注释**里（grep 会误判成已覆盖）|

这 4 个键在整仓 grep 只命中产出它们的源码、文档、和 `conflicts-panel.js` 的一行
**注释** —— 没有任何测试文件命中，零提及成立。**已按此补测试**：契约测试从 6 个
端点扩到 8 个（钉住 4 个键），`test_aigit_net.py` 的 `b1()` 另加了**语义**判据
（没推过 ⇒ `trackingRefExists is False` 且 `behind is None`，不是 `0`）。
补完后重跑本工具：**零提及 4 → 0，灰区 1 → 0**。

**扩面（2026-10-06 晚些时候）**：面从「只有 GET」扩到「GET + 安全的 POST」
（`stage` / `commit` / `branch` / `merge`），并让 `call` **读 4xx 响应体**
（`/api/merge` 无冲突时返回 400 + `{ok, error}`，那个 `error` 键正是要探的）。
探 **18 个端点 / 15 个带键 / 36 个键**，**零提及 0、灰区 0**。

键从 31 → 36，多出的 5 个是 `code` / `current` / `error` / `output` / `required`。
**归因要算准**（实测，不是推测）：只有 `output` 来自新加的 POST 端点；
`code` / `current` / `error` / `required` 来自「开始读 4xx body」
（`/api/remote/heads` 的 400 响应体）。**加 POST 没有挖出新缺口** ——
因为 `stage`/`branch`/`commit`/`checkout` 返回的都是 status 形状（契约测试的
`STATUS_KEYS` 已经钉住）加一个 `output`（也已钉住）。**这反过来证明契约测试的
status 形状钉是承重的**：它同时覆盖了 POST 面。

**扩面到带参 GET（2026-10-20）**：又加了 7 条**带查询参数**的 GET
（`/api/file` / `/api/diff` / `/api/history/blame` / `/api/history/file` /
`/api/discard/plan` / `/api/reset/plan` / `/api/history/stash`）。

⚠ **关键发现**：`--sandbox` 只建 `README.md`（`server.py:2184` 实测），
所以原来那条 `/api/diff?file=probe.txt` 探的是**不存在**的文件 —— 吃到的是
400 错误体，不是正常形状。新加的 7 条一律指向**真实存在**的 `README.md`。

探 **25 个端点 / 22 个带键 / 59 个键**，**零提及 3 个、灰区 3 个** ——
和 POST 那次（零收获）不同，这次**真挖到缺口**：

| 键 | 端点 | 说明 |
|---|---|---|
| `requested_first` / `requested_end` | `/api/history/blame` | 本次请求的行区间（与 `first_line`/`last_line` 不是一回事）|
| `exists_in_worktree` | `/api/history/file` | 文件在盘上还是已被删 |
| 灰区 `follow` / `tracked` / `undone_commits` | blame / file / reset-plan | 原文有、AST 常量里没有 |

补完 3 个零提及键后重跑：**零提及 3 → 0，灰区 3 → 2**（`follow` / `undone_commits`）。
补的地方是 `test_history_ops.py` 的 `t_H2`（blame）与 `file_history` 那条，
**值先量后写**（沙盒实测 `requested_first=1` / `requested_end=3` /
`exists_in_worktree=True`），套件 **37/37 全过**。

**扩面到只读 plan 预演（2026-10-21）**：又加了 5 条 plan 端点
（`history/plan/amend` / `revert` / `branch-rename`、`remote/plan/fetch` / `push`）。

先量后设计（`probe_plans.py`）：这 5 条都返回 200 且键很丰富
（amend 13 键 / push 13 / revert 12 / branch-rename 10 / fetch 10）。

探 **30 个端点 / 27 个带键 / 80 个键**，**零提及 8 个、灰区 3 个** ——
**plan 端点的响应形状基本没人测**：

| 零提及键 | 端点 |
|---|---|
| `aheadBehind` | `/api/remote/plan/fetch` |
| `revRange` | `/api/remote/plan/push` |
| `published` / `old_short` / `old_subject` / `message_changes` / `new_message` | `/api/history/plan/amend` |
| `legacy_op` | `/api/history/plan/branch-rename` |

**形状变异体**（删键）+ 补断言，逐条两组实测：

| 变异体 | 不带 overlay | 带 overlay |
|---|---|---|
| `G-fetch-aheadbehind-off` | **SURVIVED** 0/1 | **KILLED** 1/1 |
| `G-push-revrange-off` | **SURVIVED** 0/1 | **KILLED** 1/1 |
| `H-amend-published-off` | **KILLED**（删掉 `published` 会让 `plan_amend()` 抛 `KeyError`，现有测试直接崩） | — |

⇒ **`published` 是又一个「零提及 ≠ 没被测」**：测试没写这个字符串，
但只要 `plan_amend()` 崩就被咬住。

补完 2 个（`aheadBehind` / `revRange`）：**零提及 8 → 5**。`test_aigit_net.py` **56/56 全过**。

**再补剩下 5 个（同一轮继续，2026-10-21）**：

| 键 | 补在哪 |
|---|---|
| `legacy_op` | `test_aigit.py` 的 `t_n2`（`N-history-routes-wired`）|
| `message_changes` / `new_message` / `old_short` / `old_subject` | `test_history_ops.py` 的 `t_C2` |

**值先量后写**（沙盒实测）：

```
old_short        = old_sha[:8]        ('27aede1f')
old_subject      = 'init: sandbox'
new_message      = None / 'hello'     (传/不传 message)
message_changes  = False / True
op               = 'branch-rename'
legacy_op        = 'rename-branch'    (内部旧名, 服务端归一到 URL 段)
```

**零提及 8 → 0，灰区 4。30 个端点 / 80 个键全部有提及。**
`test_history_ops.py` **37/37 全过**，`test_aigit.py` 的
`N-history-routes-wired` 通过。

### 变异测试：测「测试本身有没有用」

`mutation_runner.py` 把某道安全闸**故意拆掉**，看有没有套件能咬住它。
判据是三态（`LANDED` / `NO-OP` / `PROBE-DRIFT`），先跑 `--verify-probes`
确认探针在对照上签名逐字正确，再引用它的结论。

```
python mutation_runner.py --list                      # 列出全部变异体(1.5 秒)
python mutation_runner.py --verify-probes             # 探针可证伪性总检(快)
python mutation_runner.py --all --workers 4           # 全跑(28 个变异体)
python mutation_runner.py --mutate S-commit-leak-guard-off
python mutation_runner.py --all --base-ref <sha>      # 钉死基线, 可复现
```

沙盒建在 `%TEMP%\opencode\mut\`，每个变异体有自己的 TEMP/TMP。
⚠ **开工前会先查磁盘余量，不够就直接退 4** —— 它上一轮就是这么被磁盘杀掉的
（结果全丢），现在改成先查先拒。

### 一次实测基线（2026-10-06，HEAD `2f0f2c1`）

这是**某一天的实测结果，不是承诺**；改了代码请重跑，别拿这张表当现状。

| 项 | 结果 |
|---|---|
| 变异体（15 个那次） | 有效 15 / KILLED 15 / 存活 **0** / VOID 0，杀灭率 **100.0%**（分母 = 15 个真正打上去并测出结论的），1393 秒 |
| 变异体（**18 个那次**，带 overlay） | 有效 18 / KILLED **18** / 存活 **0** / VOID 0，杀灭率 **100.0%**，1453.8 秒，rc=0，**无 ENGINE-ERROR** |
| 变异体（**21 个那次**，带 overlay） | 有效 21 / KILLED **21** / 存活 **0** / VOID 0，杀灭率 **100.0%**，rc=0，**无 ENGINE-ERROR**（见 §16）|
| 变异体（**24 个那次**，带 overlay） | 有效 24 / KILLED **24** / 存活 **0** / VOID 0，杀灭率 **100.0%**，rc=0，**无 ENGINE-ERROR**（见 §17）|
| 变异体（**25 个那次**，带 overlay） | 有效 25 / KILLED **25** / 存活 **0** / VOID 0，杀灭率 **100.0%**，1263 秒，rc=0，**无 ENGINE-ERROR**（见 §19）|
| 变异体（**28 个那次**，带 overlay） | 有效 28 / KILLED **28** / 存活 **0** / VOID 0，杀灭率 **100.0%**，1228 秒，rc=0，**无 ENGINE-ERROR**（见 §20）|
| 探针可证伪性 | 33/33 在对照上签名逐字正确 |
| 解析器自测 | **12/12** 份 fixture 通过，覆盖 **10/10** 个套件 |
| `smoke.py` | 62 条断言，**62 过 / 0 败**，退 0 |

⚠ **上表那 15 个之外，2026-10-06 又加了第 16 个 `C-merge-add-scope-off`，它在
「已提交的基线」上会 SURVIVED。这不是它无效，而是已提交的 `test_f06_guard.py`
里那版旧 T3 判据本身是瞎的** —— 旧 T3 断言的是「防线关掉后**必须**泄漏」
（`ck(lp and lc, ...)`），恰好把这条变异想暴露的后果当成了期望值。
带上工作区里修好的 T3 就咬住了。两组实测：

| 运行 | 用的 T3 | 结果 |
|---|---|---|
| 不带 overlay | 已提交的旧 T3（期望泄漏） | **SURVIVED**，0/1 = 0.0% |
| `--overlay-test test_f06_guard.py` | 修好的新 T3（断言不泄漏） | **KILLED**，1/1 = 100.0% |

杀它的那条断言原文：`FAIL T3  第四道已拆, 仓外路径却还是进了索引或进了历史 ——
**第五道(作用域受限 add)没有独立挡住**, (leaked_path=True leaked_content=True)`。

⇒ 在 T3 修复**提交之前**跑 `--all` 会看到 15/16，这是真事实，不粉饰；
修复提交后即回到 16/16。（`--base-ref` 取的是**已提交**的测试文件，
所以工作区改动必须靠 `--overlay-test` 才进沙盒。）

⚠ **同一类情况又多了两条（第 17 / 18 个）**，都是 2026-10-06 为「零提及」键补的
**形状变异体**（`C-conf-state-key-off` / `S-ab-trackingref-off`）。它们杀的是
**新加的**断言，而那些断言都在**未提交**的工作区里，所以默认 `--all` 下同样
SURVIVED。逐条实测（各 `--workers 1`）：

| 变异体 | 不带 overlay | 带 overlay | 杀它的断言原文 |
|---|---|---|---|
| `C-conf-state-key-off` | SURVIVED，0/1 = 0.0% | **KILLED**，1/1 = 100.0% | `KeyError: 'currentBranch'`（`eq("main", st["currentBranch"], ...)`）|
| `S-ab-trackingref-off` | SURVIVED，0/1 = 0.0% | **KILLED**，1/1 = 100.0% | `KeyError: 'trackingRefExists'`（`ck_eq(ab0["trackingRefExists"], False, ...)`）|

⇒ 提交前跑 `--all` 会看到 **15/18 = 83.3%**（3 条存活，全部有解释）；
提交后回到 18/18。**这不是回归，是「新测试还没落地」的如实记录。**

⚠ **第 19 / 20 / 21 个（2026-10-06 晚些时候）**：用 **AST 普查**（`sweep_raises2.py`，
只认 AST 字符串常量，判据是「消息被切成的**每一段**有没有出现在任何测试里」）
从源码 **131 条 `raise` 里捞出 81 条零提及**，再逐条核实哪些**真的**没被测住。

排查中**收回两条过重判词**（都是「以为没覆盖，其实有」）：
- `NetGate.require()` —— 普查报零提及，但 `test_aigit_net.py` 的 `a11` 断言的是
  `code="net_disabled"`，不是那条中文消息。**它早被覆盖。**
- `server.py:1354`「没有可提交的改动」—— 实测 `git commit` 把
  "nothing to commit" 写在 **stdout**，所以 `out` 永远非空，**这道闸是死代码**，
  拆掉它没有任何可观测差异（探针对它 PROBE-NOOP）。**不该做变异体。**

最终选定 3 条**危害是真的**（不是「报错变差」）的闸，逐条两组实测：

| 变异体 | 拆掉的闸 | 危害 | 不带 overlay | 带 overlay |
|---|---|---|---|---|
| `C-del-stale-worktree-off` | `delete_file` 的 sha256 比对 | 过期客户端会**删掉它没见过的内容** | SURVIVED 0/1 | **KILLED** 1/1 |
| `C-unresolve-notresolved-off` | `unresolve` 拒「还没解决的路径」 | `checkout --merge` 会**冲掉用户刚解决好的内容** | SURVIVED 0/1 | **KILLED** 1/1 |
| `G-no-main-branch-off` | `gen` 要求 main 分支当基准点 | 在**没有基准分支**的仓上生成场景 | SURVIVED 0/1 | **KILLED** 1/1 |

杀它们的是 `test_conflicts.py` 新增的 3 条（`t_delete_sha` / `t_unresolve_guard` /
`t_gen_nomain`），该套件 14 → **17 条全过**。

⚠ **第四条 `S-delete-tree-link-off` 被移除**（不是保留）：它复用了一条
「已存在但没有任何变异体引用」的 `delete_tree_link` 探针，看着像缺口，
实测 **PROBE-NOOP** —— `_delete_tree` 顶部还有 `_assert_pinned_plain` 那道
**真闸**，server.py:881 的 `is_reparse_point` 检查只是**冗余后挡**。
**探针存在 ≠ 那道闸是承重的。** 目录回到 **21 条、0 VOID**。

### 第 22 / 23 / 24 个（同一方法，再一轮）

同样的流程又捞到 3 条，全部对 `test_conflicts.py` 零提及、对变异体零提及：

| 变异体 | 拆掉的闸 | 危害 | 不带 overlay | 带 overlay |
|---|---|---|---|---|
| `G-dirty-worktree-off` | `_check_clean`（有未提交改动就拒） | 往**用户未提交的活**里叠随机内容 | SURVIVED 0/1 | **KILLED** 1/1 |
| `G-branch-clash-off` | gen 拒同名分支 | 反复生成互相踩，留下**半成品仓** | SURVIVED 0/1 | **KILLED** 1/1 |
| `C-guard-unavailable-off` | 拿不到仓外联接防线时转成 `ConflictError` | 拒绝信号**裸奔成 ImportError**，调用方接不住 | SURVIVED 0/1 | **KILLED** 1/1 |

杀它们的是 `test_conflicts.py` 的 `t_gen_dirty` / `t_gen_branch_clash` /
`t_guard_unavailable`，该套件 17 → **20 条全过**。

⚠ **这两条探针第一次都写错了判据形状**：一条写成「只 sig 有没有抛」→
`PROBE-NOOP`（关掉 clash 闸后 git 自己也会抛 GenError，签名没变）；
另一条让 ImportError 裸奔把探针本身打崩 → `MUTANT-UNLOADABLE`。
改成 **sig 消息内容 / 错误归类** 才测出来 —— 与 `C-merge-add-scope-off` 那次
同一个教训：**判据要判「哪条闸拒的」，不是「有没有拒绝」**。

⇒ 提交前跑 `--all` 会看到 **20/28 = 71.4%**（8 条存活，全部有解释）；提交后回到 28/28。

**全量基线（带 overlay）**：有效 28 / KILLED **28** / 存活 **0** / VOID **0**，
杀灭率 **100.0%**，1228 秒，rc=0，无 ENGINE-ERROR。
落盘 `D:\aigit_mut_tmp\mut\baseline28.json`。

顺带修掉一个**引擎自身的真缺陷**：`S-ab-trackingref-off` 第一次跑直接
`ENGINE-ERROR: KeyError: 'test_aigit_net.py'` —— `SUITE_PARSERS` 里没有这个
套件（与 `test_f06_guard.py` 那次同一类）。补解析器时又发现**点计数**有个
潜在 bug：`dot-pass-%d` 用**行内**下标，两行各有 5 个点会生成两套同名、去重后
只剩 5 个。实测 `test_aigit_net.py` 的 56 个点散在 **12 行**上，旧写法只会数出
13；`test_history_ops.py` 恰好把 37 个点挤在**一行**，所以一直没暴露。改成
**跨行全局计数** + 新增 `dots_prefix_are_pass`（数行首连续的点，因为该套件的点
后面紧跟着用例自己打的说明文字）。`--selftest-parsers` → **12/12 份 fixture，
覆盖 10/10 个套件**；拿**真实**输出喂解析器：net 56/56、history 37/37 全对。

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
| `.gitattributes` | UTF-8 无 BOM + LF | 钉住工作区换行：`.py/.md/.txt`=LF、`.ps1/.bat`=CRLF。**没有它，新克隆会把 `.py` 变 CRLF，门禁必红** |
| `aigit.bat` | **GBK** + CRLF | 双击运行的启动器，中文报错。**别用 UTF-8 保存** |
| `aigit.ps1` | ASCII + UTF-8 无 BOM + CRLF | PowerShell 启动器，英文报错，参数更多 |
| `smoke.py` | UTF-8 无 BOM + LF | 纯 stdlib 无头自检，62 条断言 |
| `test_aigit_contract.py` | UTF-8 无 BOM + LF | 响应形状契约测试，8 个端点 + 自证组，`--selftest` 秒出 |
| `mutation_runner.py` | UTF-8 无 BOM + LF | 变异测试引擎：拆掉安全闸看有没有套件咬住（见「测试集」一节） |
| `find_test_gaps.py` | UTF-8 无 BOM + LF | 找「零提及」响应面（AST-only，面来自真跑 server；只给下界，不反推） |
| `tools/check-encoding.ps1` | UTF-8 无 BOM + **CRLF** | 编码门禁本体。仓搬到 `D:\project\aigit` 后必须**跟着搬**，否则 `litematica-preview` 那份扫不到本仓（见「为什么需要 `.gitattributes`」末节） |
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

### 为什么需要 `.gitattributes`（2026-10-06 补）

本机 `core.autocrlf = true`，而仓库里原先**没有** `.gitattributes`。后果不是
「现在红」，而是**新克隆必红**：git 会把 `.py` / `.md` / `.txt` 全部转成
CRLF，而门禁要求它们是 LF。当时工作区之所以还是 LF，只是因为这些文件从没被
重新 checkout 过 —— 属于时序运气，不是配置正确。

反事实实测（`git checkout-index` 从索引导出到临时目录，有/无那份文件各跑一次）：

| 文件 | 无 `.gitattributes` | 有 `.gitattributes` |
|---|---|---|
| `test_aigit.py` | **CRLF** | LF |
| `README.md` | **CRLF** | LF |
| `aigit.ps1` | CRLF | CRLF |
| `aigit.bat` | CRLF | CRLF |

属性优先于 `core.autocrlf`，所以写死之后换机器 / 新克隆出来的换行形态也一致。
只钉**门禁真正检查**的后缀（`.py/.md/.txt`=LF、`.ps1`=CRLF），外加 `.bat`
（README 已写明它是 GBK+CRLF）。`.html` / `.js` **故意不钉**：门禁不检查它们，
而仓里现有 2 个 `.html` 的工作区形态并不一致（1 个 CRLF / 1 个 LF），给它们加
规则会顺手改掉现状，属于本次授权之外。

### 编码门禁现在在**仓内**（2026-10-21 补）

仓从 `litematica-preview\tools\aigit` 搬到 `D:\project\aigit` 之后，
`litematica-preview\tools\check-encoding.ps1` 的 `$Root` 是
`litematica-preview`，**不再扫 aigit** —— 门禁对它是失效的。

修法：把那份脚本复制进 **`tools\check-encoding.ps1`**（`$Root = Split-Path -Parent
$PSScriptRoot` = 仓根），aigit 从此**自带门禁**，不依赖兄弟项目。

实测：

| 步骤 | 结果 |
|---|---|
| `powershell -File tools\check-encoding.ps1` | **OK，0 advisory，rc=0**，3.8 秒 |
| **阳性对照**：塞一个 UTF-16LE BOM 的 `.py` | `rc=1`，报 `\_enc_positive_control.py  UTF-16LE BOM  6 bytes  True` |
| 移除后 | 回到 **OK，rc=0** |

⚠ **复制过来时必须转成 CRLF**：`$expect` 表要求 `.ps1` = CRLF，而**源文件本身是
LF**（167 LF / 0 CRLF）—— 直接拷贝会让门禁**把自己报成 advisory**。
转成 CRLF 后 0 advisory。

⚠ 源文件（`litematica-preview\tools\check-encoding.ps1`）**仍是 LF**，那边跑门禁
会把自己报成 advisory。**那是另一条线的事，本轮没动它。**

## 许可

GPL-2.0（与 git 相同）。完整文本见 [LICENSE](LICENSE)。
发行版（`c/dist/aigit-c.zip`）里也带一份 `LICENSE`，并注明源码地址。

## CI / CD

| 工作流 | 触发 | 做什么 |
|---|---|---|
| [.github/workflows/ci.yml](.github/workflows/ci.yml) | push main / PR / 手动 | 4 个 job 并行：构建冒烟、16 片一致性判据、质量（加固/Unicode/批量/4 个演练）、压力（并发/长跑/畸形/变异 3 靶）。**PR 只跑前两个**（省钱），全量在 main 上跑 |
| [.github/workflows/release.yml](.github/workflows/release.yml) | 打 `v*` 标签 | 构建 + 打包 zip + 生成 `SHA256SUMS.txt` + 发布 GitHub Release |

本地等价命令（CI 跑的就是它们）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File c\build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File c\tests\run-all-c.ps1
```

CI 用 MSYS2 的 mingw64（`AIGIT_MINGW=C:\msys64\mingw64`）；
`build.ps1` 的顺序是 `AIGIT_MINGW` → 常见安装目录 → PATH，三个本地/CI 都能用。
