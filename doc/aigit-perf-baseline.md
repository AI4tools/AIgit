# aigit 性能基线（P0 可复现基准台）
> 本文件由 `bench_aigit.py` **整份重写**（手工改会在下一次运行时被覆盖）；机器读的完整统计在同文件目录aigit-perf-baseline.json 里。
>
> 生成本份的运行时间: 2026-10-21T14:10:55

## 尺子自证
- 量法：单独 20 次「裸起新 git rev-parse HEAD 子进程」的墙钟，取 median（另 1 次热身丢弃，临时 fixture 仓里跑）。
- 本轮：median=41.47 ms（min 40.29 / max 51.72，n=20）→ 落在 [30.0, 60.0] ms 区间 → **已校准，可对比**
- 判据出处：Windows 上每次 git 子进程 ≈40 ms 地板（A5/E13 实测，闭合 status 账过 ±2.5 ms）；此区间即它 ±50% 的容差。

## 环境
- git: `git version 2.50.1.windows.1`
- python: `3.11.0`（D:\cangku\python311\python.exe）
- cpu: Intel64 Family 6 Model 154 Stepping 3, GenuineIntel（12 核）
- os: Windows-10-10.0.22621-SP0

## 方法
1. **fixture**（系统临时目录，真实仓库不碰）：每档造 1k / 5k / 10k 个已跟踪文件（每个 10 行文本）、200 个 untracked、1 个 10000 行大文件；`git add -A && git commit` 单提交，随后挂 untracked（自证列恰 200 行 `??`）。
2. 起服务：仿 `measure_blame.py` —— `subprocess.Popen([python,'-X','utf8',server.py,'--root',<tmp>,'--port',<随机>])`，轮询 `/api/health` 到 `isRepo:true`。**不传 `--sandbox`**：那会 `rmtree(root)` 重建（server.py:2178），与我们预造仓的意图相反。
3. 端点对每档规模跑：`/api/status`、`/api/log?limit=50`、`/api/diff`、`/api/file`、`/api/history/blame?limit=500`、`/api/history/file?limit=50`（GET），加 `POST /api/stage` 两档批量（500 / 3000 路径，JSON `files` 数组）。sampled 小文件位均为 10 行干净文件位或大文件位变体（~10 行位、10000 行位各一）。
4. **协议**：每项预热 1 次 + 重复 7 次，墙钟（perf_counter，含 HTTP 请求发送 + 全响应读取），单位 ms；min / max 见 JSON，md 表只展示 median。
5. **stage 批语义**：批量路径全部来自已跟踪且干净的 fixture 文件（不含大文件、不含 untracked）—— server 侧 `git add --pathspec-from-file=-` 进去是 no-op，样本之间 index/工作区状态零漂移，sampler 无需任何重置即自洽、可重复；3000 > 档容量时（1k 档）用循环填充到恒 3000 条路径（JSON 里 `body_paths_unique` 记录 unique 数）。
6. **温度**：单机发热条件（timeout=120 s、quiet server、无并发客户端），单机跑。
7. **WinError 206 悬崖**：每次 `stage3000` 批都走 server 的 stdin pathspec 通道（`--pathspec-from-file=-`，server.py `_run_paths`/E8 修复）；它每次 HTTP 200 即说明 32K 命令行悬崖的修复仍在生效 —— 这颗钉子由本基准长期量着（JSON `results[].http_status`）。

## 三档规模——中位数（ms）
| # | endpoint | method | 1k | 5k | 10k |
|--:|---|---|---|---|---|
| 1 | `/api/status` | GET | 101.1 | 117.8 | 155.8 |
| 2 | `/api/log?limit=50` | GET | 48.2 | 43.4 | 53.8 |
| 3 | `/api/diff?file=d000/f000000.txt` | GET | 46.7 | 51.5 | 47.9 |
| 4 | `/api/diff?file=big_10000_lines.txt` | GET | 53.1 | 56.0 | 48.0 |
| 5 | `/api/file?file=d000/f000000.txt` | GET | 42.3 | 52.6 | 46.9 |
| 6 | `/api/file?file=big_10000_lines.txt` | GET | 52.4 | 52.4 | 54.6 |
| 7 | `/api/history/blame?file=d000/f000000.txt&limit=500` | GET | 100.9 | 101.3 | 102.7 |
| 8 | `/api/history/blame?file=big_10000_lines.txt&limit=500` | GET | 117.7 | 123.5 | 117.3 |
| 9 | `/api/history/file?file=d000/f000000.txt&limit=50` | GET | 140.8 | 144.8 | 145.4 |
| 10 | `/api/stage (batch=500)` | POST | 311.0 | 349.9 | 413.2 |
| 11 | `/api/stage (batch=3000)` | POST | 914.0 | 1038.8 | 1260.6 |

> 各档数据实测时间戳：1k=2026-10-21T14:04:06、5k=2026-10-21T14:04:50、10k=2026-10-21T14:10:54。每档数据来自它测的那一刻的尺子校准（JSON `runs[]` 里有每轮的尺子中位数）。
> median：7 样本中位数（先丢弃 1 预热）。min/max 及样本明细见 JSON 里 `results[]` 的 `min_ms / max_ms / samples`；`payload_bytes / body_paths / body_bytes` 也在同处，大文件位（~500KB 响应体）别看错了它与其他行的结论。

## 有什么会失败 / 失败怎么算
- 任何 endpoint 非 HTTP 200 或响应缺 `ok:true` 记一行`ok:false`，进 JSON `failures[]`，退出码 2；其他该档依然照测。
- 尺子不在 [30,60] ms → `calibrated:false` + md 红字，**但不挡电池继续跑**（数字仍然落盘，只是不能对比）。
- server 起不来 3 次、fixture 构错 → 那一档直接记失败跳过，其他档照跑。

## 怎么复跑
```powershell
# Windows 控制台 cp936，先设环境变量再跑（否则中文乱码 —— 判断只靠文件）:
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
python bench_aigit.py --scales 1k,5k --out doc/aigit-perf-baseline.json
python bench_aigit.py --scales 10k --out doc/aigit-perf-baseline.json   # 10k 造仓慢，单独跑
```
- 参数: `--scales` 取 1k/5k/10k 之一或多个；`--out` JSON 路径（md 默认同名换 `.md` 后缀）；`--repeats` / `--warmup` / `--keep`（中可查） 另有可调项。
- 跑完后脚本打印一行 ASCII 汇总；`failures=N` 非 0 时退出码 2，但 md/JSON 照常落盘（问题数字会被标出来）。
- 结果合并规则：**重跑的 scale 覆盖该档，其他档保留**；mulitiple runs 会写进 JSON `runs[]` 追加历史。
- **对比纪律**：旧基线与新基线对比前提是双方尺子都在 [30,60]ms区间内且环境同 git/python/机器。机器换了、git 换大版本 → 只拿同环境前后对比，否则别下结论。

## 本轮失败项
（本轮 0 个失败。）

## package 约束
- 本基准只**读**真实仓库的 `server.py` 作为被测件，真实仓库一个字节不碰（fixture 在临时目录）。
- 结论纪律：不要抄道——'status ~ 43ms' 这类细节只在本文件与小 JSON 里，配合 git_version/python/cpu 才能比。
