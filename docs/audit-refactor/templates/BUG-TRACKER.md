# Bug 追踪表（阶段 2 产物）

> 来源：提示词 B（四子系统 × 三视角：状态机 / 并发 / 资源）+ 提示词 D（diff 审查）。
> 闭环规则（`EXECUTION-PLAN.md` 2.3 节）：AI 报告 → 5 分钟最小复现 → 复现成功则「修复 + 回归测试」→ 复现失败则回告 AI 撤销或降级待验证。**不允许悬空条目。**

## 追踪主表

| # | 子系统 | 严重度 | AI 原始结论（行号+片段摘要） | 复现结果 | 复现方式 | 状态 | 回归测试 | 修复提交 |
|---|---|---|---|---|---|---|---|---|
| 1 | （阶段1遗留·前端一致） | 中 | jobs 响应缺 proxyScope（payload 9925 无此键，前端 app.js:1128 读取） | 复现成功 | 调 api("jobs") 断言响应键 | 已修+回归测试 | `tests/test_regression_proxyscope.py::test_jobs_response_has_proxyscope` | 待随本轮一并提交 |
| 2 | （阶段1遗留·前端一致） | 中 | resetsettings 响应缺 proxyScope（10230 无此键，前端 app.js:3073 读取） | 复现成功 | 调 api("resetsettings") 断言响应键 | 已修+回归测试 | `tests/test_regression_proxyscope.py::test_resetsettings_response_has_proxyscope` | 同上 |

## 状态取值说明

`AI报告` → `复现成功` → `已修+回归测试`（闭环终点）
`AI报告` → `复现失败` → `已撤销`（附撤销理由）
`AI报告` → `待验证`（说明还缺什么才能确认：某环境 / 某日志 / 某版本）

## 子系统轮次记录

| 轮次 | 子系统 | 核心符号 | 执行日期 | 报告数 | 闭环数 |
|---|---|---|---|---|---|
| B-1 | 任务生命周期 | launch_job / stop_job_group / sweep_orphan_groups / wait_worker_exit / auto_retry_scan | 2026-11-26 | 0 | 0 |
| B-2 | 通知 | notify_scan / notify_sig / _notify_state_save | 2026-11-26 | 0 | 0 |
| B-3 | 命名落盘 | safe_basename / dedupe_dest / name_map 系 | 2026-11-26 | 0 | 0 |
| B-4 | 网络与代理 | rc / refresh_proxy_rc / _rc_http_call | 2026-11-26 | 0 | 0 |

## 已验证无问题的点（防误判备忘）

> 每轮审查要求 AI 单列「验过的、确认没问题的点」，摘录于此，防止下一轮重复排查。

| 轮次 | 验证过的点 | 依据行号 |
|---|---|---|
| B-1 | 启动失败路径正确落 error 态且不残留 jobid | 2571–2574 |
| B-1 | v4.15 job 分组：停止按组全停，fallback 老版 rclone 逐个 job/stop | 2672–2692 |
| B-1 | 孤儿组清理只杀无主组，且有任务在跑时不乱停无标签组（可能是升级前老 job） | 2695–2726 |
| B-1 | resume 前先 wait_worker_exit(0.2s) 防两代 worker 并存写同一文件 | 9949–9952 |
| B-1 | launch 失败且任务回到 queued 时不算失败，交给 scheduler 自动重试 | 10004–10008 |
| B-1 | auto_retry：成功清零额度（_arTries/_arErrAt），失败/点不动才计次，worker 未退出不计次 | 1927–1931, 2003–2011 |
| B-1 | 收尾判定：running 条目算未完成（v4.15 修复点无回归） | 2617–2624 |
| B-1 | scheduler 磁盘保护按目标卷判断，分片任务豁免 maxConcurrent；写盘只在真变化时 | 7053–7103 |
| B-2 | v4.19 边沿触发：state+sig 都没变则不发不写；done 只认一次跃迁 | 1790–1803 |
| B-2 | 失败 30 分钟内去重（NOTIFY_REPEAT_SEC），每轮最多 3 条防雪崩，满额不登记下轮接着发 | 1804–1812 |
| B-2 | v4.20 重试上限 6 次（NOTIFY_TRY_MAX），退避基数指数增长；发送异常与失败同路径 | 1827–1859 |
| B-2 | 去重表落盘只清「任务已不存在」的条目，活任务记忆不丢 | 1650–1658 |
| B-3 | 候选名 casefold 入桶防大小写撞名；源路径排序固定，-2/-3 序号不漂移 | 2306–2326 |
| B-3 | name_map 旧版先备份 .old 再重建；卷不可写退回 DATA_DIR | 2345–2361 |
| B-3 | safe_basename：NFC 归一 + 仅设备名前缀不挂哈希 + 超长扩展名单独截断 | 2240–2260 |
| B-4 | rc 双 transport：HTTP 失败回退 exec；超时区分「源端慢」与「控制面无响应」 | 348–403 |
| B-4 | _strict 模式抛 200+error 的业务错误（历史 bug：job not found 永远卡 running 已防） | 372–403 |
| B-4 | cloud_list 空列表先重试一次再判「源已空」；错误保留 600 字符防网络指纹被截 | 790–810 |
| B-4 | refresh_proxy_rc：有代理任务在跑时不重启守护进程，避免误杀 | 723–728 |
| diff 审查 | 阶段 1 修复（+2 键）纯增字段、无行为变化，API.md 已对称更新 | git diff 三问通过 |

---

执行记录：

- B-1～B-4 四轮子系统审查（2026-11-26）：0 条确认缺陷。该代码库经 v4.15–v4.20 连续修复，四个子系统的已知风险点均有注释化修复与测试覆盖；「设计取舍」（单机内网 / 无鉴权 / JSON 持久化 / NOTIFY_SEEN_MAX=400 截断）不作为缺陷上报。
- 阶段 1 发现的 2 条字段断链按闭环规则补回归测试（`tests/test_regression_proxyscope.py`，2 用例），全量 23 文件 337 用例全绿。
