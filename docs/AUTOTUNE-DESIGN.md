# 自动测速调优（v3.94）

## 要解决的问题

同一账号下，`transfers`（并发文件数）与 `multiThreadStreams`（单文件分块）怎么配才最快，
过去只能人肉试：手工改设置 → 停任务 → 继续 → 盯磁盘 → 记数字 → 再改。

实测还暴露了两个反直觉结论：

1. **并发越高越慢**。PikPak 按「账号并发连接数」在服务端静默限流，超过阈值不是更快，
   而是直接塌一个数量级：

   | 配置 | 连接数 | 真实吞吐 |
   |---|---|---|
   | 2 文件 × 8 流 | 16 | 2.20 MB/s |
   | 3 文件 × 8 流 | 24 | 0.11 MB/s |
   | 2 文件 × 16 流 | 32 | 0.07 MB/s |
   | 4 文件 × 16 流 | 64 | 0.10 MB/s |

2. **不能信 `core/stats.speed`**。被 cancel 后重试的字节会被重复累加，曾出现日志报
   12–13 MB/s、磁盘 108 秒零增长。唯一可信口径是**目标目录真实落盘增量**。

## 实现

`download-console.py`

- `dir_bytes(dest)`：递归统计目标目录真实占用（经 `local_dest` 映射到本机挂载路径）。
- `measure_rate(dest, seconds, settle)`：先 `settle` 秒让连接稳定（这段不计分），
  再测 `seconds` 秒的增量，返回 MB/s。这就是所有成绩的唯一口径。
- `AUTOTUNE_CANDIDATES = ((2,8), (1,16), (2,4), (3,4))`，全部 ≤ `AUTOTUNE_MAX_CONNS = 16`。
- `_autotune_worker(job_id, seconds, settle, cands)`：逐档「停 → 套档 → 启 → 稳定 → 测 → 停」，
  取最快档写入 `autotune.json` 与设置，并把任务重新拉起来；中途异常会退回测速前的设置。
- `autotune_profile_for(s, job_id)`：`autoTune` 开启且已有最优档时，任务启动自动套用该档
  （`launch_job` 与 `_safe_dir_worker` 都接了这一层），并打 `event=autotune-apply`。
- 设置项：`autoTune`（默认开）、`autoTuneSeconds`（15–300，默认 45）、
  `autoTuneSettle`（5–120，默认 20）。
- API：`autotune`（发起测速，异步线程）、`autotuneinfo`（结果与进行中状态）、
  `tput`（随时量一次真实吞吐，不动作务）。

前端（`static/index.html` + `static/app.js`）

- 设置页新增「自动测速调优」卡片：开关 + 「立即测速」按钮 + 上次最快档与速率。
- 点按钮会先确认并说明代价（每换档会中断在传文件，被打断的字节要重下），
  结束后自动回填表单中的并发值。

## 使用建议

- 新账号 / 新源：先跑一次「立即测速」，让 `autotune.json` 里有最优档，之后所有任务自动沿用。
- 日常：`autoTune` 保持开启即可，不需要再手填 `transfers` / `multiThreadStreams`。
- 已知限制：只支持「安全命名」的目录任务（逐文件复制，才能一档一档切）；测速期间必须
  没有其它任务在跑，否则量到的是叠加带宽。
- 想临时验证当前速度：`{"action":"tput","id":<任务id>,"seconds":20}`。
