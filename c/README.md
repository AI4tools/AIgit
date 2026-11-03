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

## 当前覆盖（2026-11-03，M1 读面完成）

45 个端点里的读面已经全部接上：

| 组 | 端点 |
|---|---|
| 基础 | `/api/health`、`/api/status`、`/api/branches`、`/api/log`、`/api/file`、`/api/diff` |
| 历史 | `/api/history/file`、`/api/history/blame`、`/api/history/tags`、`/api/history/reflog`、`/api/history/stashes` |
| 冲突/远程 | `/api/conflict/state`、`/api/conflict/list`、`/api/conflict/inspect`、`/api/remote/capability`、`/api/remote/list`、`/api/remote/ahead-behind` |

v1 明确不做：`/api/remote/{fetch,pull,push}`、`/api/gen`、以及写操作（M2）。
对这些路径返回 404，与 Python 版「没有这个接口」一致。

## 判据

```powershell
python -X utf8 c\tests\run_conformance.py          # 5 片，395 项
```

每片都是「同一仓、同一时刻，起 Python 参考服务与 C 服务，逐端点比较
HTTP 码 + JSON 深比较 + `X-aigit-git-calls`/`cmds`」。分片：

- `conformance_m0.py` —— health/status/404/405/CRLF（9 项）
- `conformance_basic.py` —— branches/log/file/diff（98 项）
- `conformance_histfile.py` —— history/file、history/blame（96 项）
- `conformance_histlist.py` —— history/tags、reflog、stashes（35 项）
- `conformance_conflict.py` —— conflict/*、remote/*（107 项）

读面退出判据是 275 项「与 git 直接比对」的差分（`cmp_vs_git_c.py`，在
`D:\aigit_mut_tmp`，不在仓库内）：

```powershell
$env:AIGIT_SERVER_CMD='D:\project\aigit\c\build\aigit.exe'
python -X utf8 D:\aigit_mut_tmp\cmp_vs_git_c.py
# 合计 275 项检查，失败 0（symlink 场景因本机无符号链接权限 SKIP）
```

## 与 Python 版仍存在的已知差异

只列会影响结果的；都是边角：

1. `limit` / `start` / `end` 的**非 ASCII 数字**（如 `limit=٥`）：Python 的 `int()` 收，
   C 回 400。
2. 超过 2^126 的 start/end 在 C 里饱和（Python 任意精度）。
3. 工作区文件读失败（被独占/无权限）时，Python 抛 500，C 按「不许 500」返回空内容。
4. 请求行超过 2047 字节会被 HTTP 层截断（Python 无此上限）。
5. 符号链接语义未验证：本机建链接需要开发者模式/管理员权限（WinError 1314）。

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
  read_basic.c       branches/log/file/diff
  read_history.c     history/file、history/blame
  read_history_list.c history/tags、reflog、stashes
  read_conflict.c    conflict/*、remote/*
  routes.h     四个读模块的分派约定
```
