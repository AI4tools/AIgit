# aigit 免安装分发 + 大仓性能

任务:aigit8。本文件是打包步骤、实测性能数字、已知限制。

---

## 1. 交付物

| 文件 | 大小 | 性质 | 说明 |
|---|---|---|---|
| `build_aigit.py` | 11608 B | 新建 | zipapp 打包;检测 PyInstaller;从 server.py 读版本号 |
| `aigit_perf.py` | 15542 B | 新建 | 分页 + 大 diff 截断 + status 快路径 |
| `web/virtuallist.js` | 6742 B | 新建 | 固定行高虚拟滚动 |
| `aigit-dist.md` | — | 新建 | 本文件 |
| `web/perf-demo.html` | — | **未交付** | 见 §8「为什么没做」 |

### 超出任务书的一项:`aigit_perf.py`

任务书要求实现 (a) 分页 和 (b) 大 diff 截断,这两项都**必须改 `server.py`** ——
`Repo.status()` 不接受 `offset`/`limit`,`/api/diff` 路由写死了完整返回。而
`server.py` 在所有权表里是「不许碰」。

所以这两项做成了**外挂层**:不改 server.py 一个字,由 `install()` 在运行时挂到
`Repo.status` 和 `Handler.do_GET` 上。这是第四个新文件,超出任务书列的三项,
理由如上。如果集成时更希望直接改 server.py,`aigit_perf.py` 里的逻辑可以直接搬
进去,函数是独立的、没有隐藏状态。

---

## 2. ⚠ 阻塞项:server.py 里没有版本号

任务书要求「版本号从 server.py 里读,不要硬编码两份」,但**整个 aigit 项目里
不存在任何版本号** —— server.py、gen.py、aigit.bat、web/index.html 都没有。

因此 `build_aigit.py` 现在会**直接报错退出**:

```
C:\...\aigit\server.py 里没有 AIGIT_VERSION 常量。
版本号的唯一来源必须是 server.py —— 本脚本不会自己写一份。
请在 server.py 顶部加一行(该文件归 aigit1 所有,本任务不碰):
    AIGIT_VERSION = "<版本号>"
注意本脚本里不含任何版本号字面量,包括这条提示 —— 两份版本号迟早分叉。
```

**需要 aigit1 做一行改动**(该文件不归本任务所有,我没碰):

```python
AIGIT_VERSION = "0.1.0"     # 放在 server.py 顶部 import 之后
```

为什么坚持报错而不是自己写一份默认值:任务书明确禁止硬编码第二份,而版本号的
唯一价值就是「和代码同步」。让 build 脚本自己兜一个默认值,等于把版本号变成两处,
第一次有人只改 server.py 就会静默发错版 —— 这类 bug 上线后极难查。

**已验证**:这个机制本身是对的。在 `%TEMP%` 里搭了一份 server.py 的**副本**并加上
那一行,全流程跑通(见 §4),包内 `server.py` 与源文件逐字节相同,注入的版本号一致,
且 `build_aigit.py` 自身不含任何版本号字面量。真实 `server.py` 全程未被修改
(测试里有断言)。

---

## 3. 打包步骤(实测)

```bat
cd C:\poject\litematica-preview\tools\aigit
python build_aigit.py            REM 打包到 .\dist
python build_aigit.py --check    REM 只检查环境与版本号,不产出
python build_aigit.py --exe      REM 强制要求 PyInstaller(没有则报错)
python build_aigit.py --no-exe   REM 即使装了 PyInstaller 也只出 .pyz
python build_aigit.py --out D:\somewhere
python build_aigit.py --max-diff-lines 3000
```

实测输出:

```
版本号(读自 server.py): 0.1.0
本机 Python          : 3.12.4
PyInstaller          : 没有
打包清单              : server.py, aigit_perf.py, gen.py
已生成 ...\dist\aigit.pyz  (48256 字节)
未生成 .exe —— 本机没有 PyInstaller,只出了 .pyz。
```

### 本机只能出 .pyz,不是 .exe

本机没有 PyInstaller(`ModuleNotFoundError: No module named 'PyInstaller'`),
按任务书「没有就退回 zipapp 并说明」走 zipapp。

**要真正免安装(目标机器无 Python)**,在任一装了 PyInstaller 的机器上重跑:

```bat
pip install pyinstaller
python build_aigit.py --exe
```

这是**唯一允许装依赖的场合**,而且只影响构建机 —— 发出去的 `aigit.pyz` 本身零依赖。
`--exe` 会额外产出 `aigit.exe`（PyInstaller `--onefile`）。

### 打包清单是自动发现的,不是写死的

`server.py` 现在 `import gen`,而 `gen.py` 是 aigit2 的文件。所以清单是**递归扫描
import 语句**算出来的,不是手写列表:

```
打包清单: server.py, aigit_perf.py, gen.py
```

写死列表今天对、明天就漏,而漏掉的那个模块**只在打包产物里**炸
`ModuleNotFoundError`,源码树里跑一切正常 —— 这个坑我踩了一次
(第一次构建出来 `.pyz` 直接起不来),所以改成扫描。

---

## 4. 实测:打包产物能跑

`.pyz` 启动后打的真实 HTTP 请求(5 轮全部通过,共 32 项断言):

| 请求 | 结果 |
|---|---|
| `GET /` | 67462 字节,2ms |
| `GET /api/status?offset=0&limit=10` | items=10, totals={staged:50, unstaged:7001, conflicted:0, changes:7051} |
| `GET /api/status`(不带分页) | 字段 `['branch','conflicted','hasStaged','ok','root','staged','unstaged']` —— 与改造前同形 |
| `GET /api/diff?...&maxLines=5000` | truncated=true, totalLines=12005, 244202 字节 |
| `GET /api/diff?...&maxLines=0` | truncated=false, 586229 字节 |

**「无 Python 干净目录」的验证范围要说明白**:zipapp 是标准库 zipapp,**目标机器
必须自带 Python 3.5+**。我验证的是「只把 `aigit.pyz` 一个文件拷进空目录、
不带任何源码」能正常启动并响应全部接口 —— 这排除了对源码树的依赖,但**不等于**
「在没有 Python 的机器上能跑」。后者只有 PyInstaller 的 `.exe` 才成立,而本机
装不了 PyInstaller(见 §3)。

### 打包过程中踩到并修掉的 4 个真问题

都是「源码树里正常、打包产物里出错」这一类,记在这里免得集成时重踩:

1. **`ModuleNotFoundError: No module named 'gen'`** —— 见 §3,改为自动发现依赖。
2. **`main()` 覆盖 `server.WEB`** —— `server.py:391` 有 `WEB = Path(__file__).parent / "web"`。
   zipapp 里 `__file__` 是 `<...>/aigit.pyz/__main__.py`,算出来是 `<...>/aigit.pyz/web`,
   不是目录,首页 500。修法:包一层 `ThreadingHTTPServer`,在**构造器里**把 `WEB` 改回来
   —— `main()` 第 391 行改 WEB、第 406 行才构造 server、第 415 行才 `serve_forever`,
   所以这是确定性接缝,不需要线程也不用复制 aigit1 的参数解析。
   (先试过改 module 的 `__class__` 挡赋值,**无效**:函数里的 `WEB = ...` 编译成
   `STORE_GLOBAL`,直接写 `module.__dict__`,不经过 `__setattr__`。)
3. **`Path(...) / "aigit-web-%s" % name` 抛 TypeError** —— `/` 优先级高于 `%`。
   顺带修掉:`__file__.name` 在 zipapp 里恒为 `__main__.py`,不同产物会撞缓存目录名,
   改成「zip 路径哈希 + mtime」。
4. **`FileNotFoundError: aigit-web-xxx/index.html`** —— `_extract_web()` 返回的是缓存
   目录,但文件解压到了 `缓存目录/web/`,少了一层。

---

## 5. 性能基准数字

测试仓:5000 个受跟踪文件 + 2000 个未跟踪文件 + 1 个 10000 行文件;
改动后 `git status --porcelain` 有 5021 条,合计 7051 条改动(50 已暂存 / 7001 未暂存)。
大文件改了 2001 行,产生 12005 行的 diff。

**每个数字都是 5 轮的中位数**,不是最好看的那一轮 —— 机器上同时还有别的 agent 在跑测试。

### status

| | 中位数 | min / max |
|---|---|---|
| 改造前 v1 原实现(2 次子进程) | **139.7ms** | 122.1 / 149.6 |
| v2 快路径(1 次子进程) | **102.1ms** | 92.3 / 111.5 |
| 省下 | **29.8ms(21.3%)** | |

`GET /api/status` 走 HTTP 的完整往返:

| | 中位数 | 响应体 |
|---|---|---|
| 全量 | **204.8ms** | 454420 字节 |
| 分页 `?offset=0&limit=200` | **103.6ms** | 17519 字节 |
| 省下 | **101.2ms(49.4%)** | 响应体 −96.1% |

### 分页**不减少** git 的工作量 —— 这是最重要的一条

git status 本身要 **102ms**,而分页后的整条往返是 **103.6ms**。
也就是说分页后 **99% 的时间仍然花在 git 那一次 `status` 上**。

分页省下的是 JSON 序列化 + 传输 + 前端解析/渲染那 ~100ms,不是 git。
所以:响应体小 96% 是真的,服务端延迟减半也是真的,但**「几千文件的仓会卡」这件事
光靠分页解决不了**。真要再往下压,得动 git 本身(`core.fsmonitor`、或增量索引),
那超出本任务范围。

### 大 diff 截断

| | 中位数 | 响应体 |
|---|---|---|
| `git diff` 取全文 | 63.6ms | 574083 字节 |
| 截断到 5000 行(纯 Python) | **0.9ms** | — |
| HTTP 全文 | **64.0ms** | 586152 字节 |
| HTTP 截断 | **61.4ms** | 244202 字节 |

**省下的服务端时间只有 4.1%**,因为判定是否超长必须先拿到完整 diff —— git 那次子
进程的成本一分不省。截断真正省的是 **58% 的传输量**和**前端解析 + 构建 DOM 的成本**。

⚠ **前端那部分我没有测**:`innerHTML` 落进真实 DOM 后的 parse / reflow / paint 成本
拿不到(任务禁止开窗口)。所以「打开一个 10000 行文件的 diff 耗时」我只能给
**服务端 64.0ms(全文)/ 61.4ms(截断)**,前端渲染耗时未测,不敢编。

### 虚拟滚动(Node 22 + 最小 DOM 桩,实测)

逐行滚过 5000 行,每步触发一次重绘:

| | 数字 |
|---|---|
| 滚过 5000 行总耗时 | **63.9ms** |
| 重绘次数 | 5002 |
| 累计拼出行 HTML | **219378 行** |
| 无虚拟滚动的对照 | **25005000 行**(每次重画全部 5000 行) |
| 省下 | **114×** |
| 单次重绘中位 | **13us** |
| 占 16.7ms 帧预算 | **0.08%** |
| 单次 paint | 中位 0.012ms,p95 0.023ms,max 0.230ms |
| 随机跳转刷新 | 中位 0.011ms,max 0.226ms |
| 1 万行时的 DOM 节点数 | **36 个** |
| 追加 10000 条 | 18.68ms |

DOM 节点数与总行数**无关**:5000 行是 36 个(首屏,顶部没有上方缓冲可加)、
44 个(滚到中部,上下各 8 行缓冲)、1 万行仍是 36 个。

⚠ 这些数字测的是 **JS 侧工作量**(算区间 + 拼 HTML 字符串),用 DOM 桩驱动真实模块代码。
**浏览器自身的布局与合成没测** —— 本任务禁止开窗口。DOM 桩不产生真实 reflow,
所以「真浏览器里 5000 行滚动掉不掉帧」这句话我不能替浏览器下结论;
能说的是「JS 侧每次重绘 13us,占帧预算 0.08%,不是瓶颈所在」。

---

## 6. 集成说明

### 6.1 分页 / 截断的 API 契约

`install()` 之后,**不带参数的请求与改造前逐字节一致** —— aigit1 的 `web/index.html`
零改动可用。

`GET /api/status?offset=<n>&limit=<n>`

```json
{
  "ok": true,
  "branch": "main",
  "root": "...",
  "hasStaged": true,
  "totals": { "staged": 50, "unstaged": 7001, "conflicted": 0, "changes": 7051 },
  "page": { "offset": 0, "limit": 200, "total": 7051, "hasMore": true, "items": [...] }
}
```

- `totals` 和 `hasStaged` **按全量算,不是当前页**。这是刻意的:第 2 页里没有已暂存项,
  前端若自己数就会把「已暂存」徽标错误地清掉。
- `items` 是把 `conflicted → staged → unstaged` 顺序拍平、去重后的改动清单
  (顺序与 `index.html` 的 `buildRows()` 一致)。
- 去重键是 **`(path, staged)`**,不是光 `path`:同一文件「既已暂存又未暂存」是**两条
  独立改动**,前端也正是分开渲染两行。实测 7051 条改动只覆盖 7001 个路径,差的就是这 50 条。
- 非法参数不报错:`limit=abc` → 200,`limit=-5` → 1,`limit=999999` → 5000,
  `offset` 超界 → 空页。

`GET /api/diff?file=<p>&maxLines=<n>`

```json
{ "ok": true, "path": "...", "staged": false,
  "truncated": true, "totalLines": 12005, "shownLines": 5000,
  "maxLines": 5000, "diff": "..." }
```

`maxLines=0` 表示不限(前端「加载全部」用它)。

### 6.2 源码树里怎么用

源码树直接 `python server.py` **不会**有分页和截断 —— `.pyz` 的入口才调 `install()`。
要手动挂,改 `__main__` 那两行:

```python
import server, aigit_perf
aigit_perf.install(server)        # max_lines=5000, page_limit=200, fast_status=True
server.main()
```

`install()` 幂等,重复调用只生效一次;`fast_status=False` 可关掉快路径做对照测量。

### 6.3 virtuallist.js 怎么接

`web/index.html` 里**已经有**一段内联虚拟滚动(`ROW_H=22`、`OVERSCAN=8`、`translateY`
定位,行 650 附近的 `paintV()`)。`virtuallist.js` 是同一套算法抽出来的可复用版本
——能独立测试、别的面板也能用。两边并存不冲突,集成时**二选一**即可
(`index.html` 现在没引用本文件)。

```html
<script src="virtuallist.js"></script>
<script>
  var vl = VirtualList.create({
    scroller: $("vscroll"),     // 必须固定高度 + overflow:auto
    spacer:   $("vspacer"),    // 撑滚动高度
    viewport: $("vrows"),      // 行容器
    rowHeight: 22,             // 必须与 CSS 里 .row 实际高度一致
    overscan: 8,
    getKey: it => it.path + "|" + (it.staged ? 1 : 0),
    renderRow: (it, i, active) => '<div class="row">' + esc(it.path) + "</div>"
  });
  vl.setItems(page.items);     // 或 vl.append(下一页) 做无限滚动
</script>
```

`rowHeight` 必须等于 CSS 实际行高,否则偏移会累积错位 —— 这是固定行高方案唯一的
硬约束。可变行高要做两趟渲染,复杂度上一个台阶,当前用不上。

---

## 7. ⚠ 顺带发现:`server.py` 的 rename 解析有 bug

不属于本任务,但是我在验证 v2 快路径时撞出来的真 bug,报告给 aigit1。

`server.py` 的 `status()` 里有一句 `if " -> " in path: path = path.split(" -> ")[-1]`。
**这在 `-z` 模式下是死代码**:v1 porcelain 加 `-z` 时,rename 的新旧路径写成
**两个 NUL 分隔的字段**(`XY new\0old\0`),根本没有 `" -> "` 分隔符。

于是旧路径那个字段被当成一条独立记录再解析一遍:`x` 取到 `'p'`、`y` 取到 `'k'`、
`path` 被砍掉前两个字符。实测 `git mv a.txt b.txt`:

```
staged 51 项,其中多出两条幽灵条目:
    {'path': '00/mod_00150.txt', 'staged': True,  'status': 'p'}
    {'path': '00/mod_00150.txt', 'staged': False, 'status': 'k'}
```

两条都是坏路径(被砍掉 `pkg` 前缀),状态码也不是合法 git 状态码。UI 上会多出一行点不动的
条目。**v2 快路径没有这个问题**(v2 的 rename 显式区分新旧路径),所以装了性能层的
`.pyz` 顺带绕过了它;但源码树里 `python server.py` 仍然有。

修法两选一:改用 `--porcelain=v2` 解析,或修 v1 的 `-z` 分支(遇到 `X`/`R`/`C` 且
下一字段是旧路径时把两字段一起吃掉)。**这是 aigit1 的文件,我没动。**

---

## 8. 已知限制

1. **`.pyz` 仍需目标机器有 Python 3.5+**。真正免安装要 PyInstaller 的 `.exe`,
   本机装不了,没测过。`aigit_perf.py` / `build_aigit.py` 都只用标准库。
2. **`web/perf-demo.html` 没做**。任务书标注「可选」,而任务又禁止开窗口 ——
   做出来我也无法验证它,等于交一个没验证过的页面。虚拟滚动的验证改用 Node +
   DOM 桩(18 项断言全通过),比手写一个演示页更能说明问题。如果确实需要演示页,
   请明确要求,我补一个并说明它未被实测。
3. **前端渲染耗时未测**(见 §5)。禁止开窗口,浏览器 reflow/paint 的数字拿不到。
4. **diff 截断省不了 git 的时间**(4.1%),见 §5。这是格式决定的,不是实现问题。
5. **虚拟滚动只支持固定行高**。行高可变要两趟渲染。
6. **`aigit_perf.py` 靠 monkeypatch 挂载**。`install()` 幂等、`WEB` 有确定性接缝、
   不带参数的请求与改造前一致(均有测试覆盖),但它终究是外挂层。
   若 aigit1 愿意把分页/截断直接并进 `server.py`,这个文件可以删掉。
7. **status 快路径换成了 `--porcelain=v2`**。我逐项比对过 v1/v2 在
   staged/unstaged/conflicted 三段上的路径与状态码(7/7 一致),但如果 aigit1 之后
   改了 `status()` 的返回结构,这里要跟着改。
8. **测试仓只覆盖了单一形态**:普通改动、暂存、未跟踪、rename、一个 `UU` 冲突。
   没测子模块、LFS、`--intent-to-add`、稀疏检出。

---

## 9. 任务书 checklist 对照

| # | 项 | 结果 |
|---|---|---|
| 1 | zipapp 打包成功,能跑起来 | ✅ 48256 字节,启动并响应全部接口 |
| 2 | 打包产物在干净目录里也能跑 | ⚠️ **部分**:已验证「只拷 `.pyz` 进空目录、无源码」可跑;**未验证**「无 Python 的机器」——本机无 PyInstaller,`.pyz` 本身需要 Python 3.5+ |
| 3 | 版本号与 server.py 一致 | ⚠️ **机制已验证,真项目跑不了**:server.py 里没有版本号(§2),需 aigit1 加一行 |
| 4 | 分页 offset=0/10 与 10/10 不重叠、不遗漏 | ✅ 逐页走完 7051 条,零重复零遗漏,71 页 |
| 5 | 分页后 hasStaged 仍正确 | ✅ 按全量算;`totals.staged=50` 而当前页只有 10 条 |
| 6 | 大 diff 被截断,前端能拿到 truncated 标志 | ✅ `truncated=true` + `totalLines=12005` |
| 7 | 虚拟滚动 5000 文件不卡 | ⚠️ **JS 侧实测**:单次重绘 13us(帧预算 0.08%),累计少拼 114×;**浏览器合成未测**(禁止开窗口) |
| 8 | 5000 文件仓 status 耗时 | ✅ 102.1ms(改造前 139.7ms),响应体 454420 → 17519 字节 |
| 9 | 10000 行文件 diff 耗时 | ✅ 服务端 64.0ms 全文 / 61.4ms 截断;**前端渲染耗时未测** |
| 10 | 交付文件无 UTF-8 BOM | ✅ 三个文件均无 BOM、纯 LF、可 utf-8 解码、末尾有换行 |

断言全部通过:`bench.py` 23 项 + `test_virtuallist.js` 18 项 + `test_build.py` 32 项
= **73 项**。另有 `verify_v2.py` 12 项(v1/v2 逐项比对,需先跑 `reset_state.py`
造出同时含 rename + 冲突的仓)。

---

## 10. 复现

所有测试都在 `%TEMP%\aigit-perf-test\`,不碰 `C:\poject\` 下任何真实仓库。

**生成的数据已清理**:测试仓(8120 个文件)、`buildtest/`、`clean-dir/`、
各种日志 —— 清理前 7029 个文件 / 3.9MB,清理后 14 个文件 / 58.5KB。

**保留了 13 个测试脚本**(58.5KB)。它们是上面每个数字的证据,删掉的话本文件
的所有「实测」就退化成无法复现的说法。如果不想留,一条命令即可:

```bat
rmdir /s /q "%TEMP%\aigit-perf-test"
```

| 脚本 | 作用 |
|---|---|
| `mkrepo.py` | 造 5000 文件测试仓(批量生成,41.5s) |
| `reset_state.py` | 重置成「同时含 rename + 冲突 + 暂存 + 未暂存 + 未跟踪」 |
| `redirty.py` | 重置成「大脏仓」(7051 条改动 + 12005 行 diff) |
| `bench.py` | 23 项正确性断言 + 性能数字 |
| `multi.py` | 跑 N 轮取中位数(§5 的表就是这么来的) |
| `verify_v2.py` | v1 与 v2 解析逐项比对(12 项) |
| `profile_status.py` | 拆解 git status 各项耗时 |
| `debug_dup.py` / `check_rename.py` | 定位分页重复项 / 复现 rename 幽灵条目 |
| `test_virtuallist.js` | Node + DOM 桩,18 项 |
| `test_build.py` | 打包全流程,32 项 |
| `check_encoding.py` | BOM / CRLF 检查 |

重跑顺序:

```bat
cd "%TEMP%\aigit-perf-test"
python mkrepo.py repo 5000
python redirty.py repo
python bench.py repo
python multi.py repo 5
node test_virtuallist.js
python test_build.py
python check_encoding.py
```
