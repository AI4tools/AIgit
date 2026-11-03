# aigit C 版

Python 参考实现在仓库根目录（`server.py` 等），这里是**同契约**的 C 重写。
目标不是「再写一个 git 前端」，而是：单文件静态 exe、无运行时依赖、JSON 逐字段与
Python 版相等，从而直接复用同一套黑盒判据。

## 构建

```powershell
$env:PATH='C:\Environment\cpp\mingw64\bin;'+$env:PATH
mingw32-make -C c -j4          # 产出 c/build/aigit.exe（静态链接，单文件）
```

要求 MinGW-W64（UCRT）与 cmake 无关；makefile 只用了 `gcc` 与 `windres` 级别的工具。

## 运行

```powershell
c\build\aigit.exe --root D:\path\to\repo --port 8765
```

`--root` 必须是仓库工作区根目录。服务只读 + 少量写端点（M2 范围内），默认监听
`127.0.0.1`。

## 当前覆盖（2026-11-03，M1 读面 + 计划面完成）

Python 版 `do_GET` 里的 21 组路由已经**全部**接上：

| 组 | 端点 |
|---|---|
| 基础 | `/api/health`、`/api/status`、`/api/branches`、`/api/log`、`/api/file`、`/api/diff` |
| 历史 | `/api/history/file`、`/api/history/blame`、`/api/history/tags`、`/api/history/reflog`、`/api/history/stashes`、`/api/history/stash`、`/api/history/plan/<op>` |
| 冲突/远程 | `/api/conflict/state`、`/api/conflict/list`、`/api/conflict/inspect`、`/api/remote/capability`、`/api/remote/list`、`/api/remote/heads`、`/api/remote/ahead-behind`、`/api/remote/plan/<op>` |
| 预演 | `/api/reset/plan`、`/api/discard/plan` |

v1 明确不做：写操作（M2，POST 面）与 `/api/gen`。对这些路径返回 404，与 Python 版
「没有这个接口」一致。远程能力闸（NetGate）默认关闭，`/api/remote/heads` 在关闭时与
Python 一样回 400 `net_disabled`（连 `X-aigit-git-calls` 也一并对齐）。

## 判据

```powershell
python -X utf8 c\tests\run_conformance.py          # 8 片，2,442 项
python -X utf8 c\tests\concurrency_c.py 12 25     # 并发冒烟，300 请求
```

每片都是「同一仓、同一时刻，起 Python 参考服务与 C 服务，逐端点比较
HTTP 码 + JSON 深比较」（读面分片还比 `X-aigit-git-calls`/`cmds`）。分片：

- `conformance_m0.py` —— health/status/404/405/CRLF（9 项）
- `conformance_basic.py` —— branches/log/file/diff（98 项）
- `conformance_histfile.py` —— history/file、history/blame（96 项）
- `conformance_histlist.py` —— history/tags、reflog、stashes（35 项）
- `conformance_conflict.py` —— conflict/*、remote/{capability,list,ahead-behind}（107 项）
- `conformance_plan.py` —— reset/plan、discard/plan（92 项）
- `conformance_histplan.py` —— history/stash、history/plan/<op>（185 项）
- `conformance_remoteplan.py` —— remote/heads、remote/plan/<op>（1,820 项，含 5 档能力闸）

读面退出判据是 275 项「与 git 直接比对」的差分（`cmp_vs_git_c.py`，在
`D:\aigit_mut_tmp`，不在仓库内）：

```powershell
$env:AIGIT_SERVER_CMD='D:\project\aigit\c\build\aigit.exe'
python -X utf8 D:\aigit_mut_tmp\cmp_vs_git_c.py
# 合计 275 项检查，失败 0（symlink 场景因本机无符号链接权限 SKIP）
```

## 与 Python 版仍存在的已知差异

只列会影响结果的；都是边角：

1. `limit` / `index` / `start` / `end` 的**非 ASCII 数字**（如 `limit=٥`）：Python 的
   `int()` 收，C 回 400。
2. 超过 2^126 的 start/end / index 在 C 里饱和（Python 任意精度）。
3. 工作区文件读失败（被独占/无权限）时，Python 抛 500，C 按「不许 500」返回空内容。
4. 请求头/参数上限：C 的 query 值超过 64 KiB 会回 400（Python 无此上限）；
   请求体在响应前会先读走（避免 RST）。
5. Unicode 细节：C 的 `strip`/`\w` 只近似 Python 的 Unicode 语义（NBSP、全角空格、
   emoji 等边角会分叉）；git 输出里的非法 UTF-8，Python 转 U+FFFD，C 原样透传。
6. 符号链接语义未验证：本机建链接需要开发者模式/管理员权限（WinError 1314）。
7. `net_enabled` 时 C 的 guard 仍塞 `credential.helper=""`/`core.sshCommand=""`，
   接真 https/ssh 远程会拿不到用户凭据（本地路径 remote 无差异）。

## 目录

```
c/src/
  main.c       参数解析 / 启动
  http.c       WinSock2 HTTP + 路由分派
  json.c       JSON 渲染与转义
  util.c       buf / UTF-8 转换 / 字符串工具
  git.c        git 调用 + 环境隔离 + 语义配置透传 + 调用计数
  params.c     query 解析（percent-decode，NUL 可见）
  paths.c      请求路径 → 仓内相对路径（安全关键）
  status.c     porcelain v2 解析与 JSON
  catfile.c    常驻 git cat-file --batch（不计数，带状态查询）
  read_basic.c       branches/log/file/diff
  read_history.c     history/file、history/blame
  read_history_list.c history/tags、reflog、stashes（直读 .git）
  read_conflict.c    conflict/*、remote/{capability,list,ahead-behind}
  read_plan.c        reset/plan、discard/plan
  read_histplan.c    history/stash、history/plan/<op>
  read_remoteplan.c  remote/heads、remote/plan/<op>
  routes.h     读模块的分派约定
```
