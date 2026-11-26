# 接口差异追踪表（阶段 1 产物）

> 来源：提示词 A（前后端 + `docs/API.md` 三方全量比对）+ 提示词 C（前端字段/幽灵字段/事件名）。
> 闭环规则：每条发现必须经过「确认缺陷 / 待验证 / 误报」三档复核；已修复的填处置提交，豁免的写明理由。
> 执行方式：AI 结论全部由脚本 `scripts/api_audit.py`、`scripts/ghost_check.py`、`scripts/field_check.py` 生成，人工逐条复核。

## A 类：接口对齐（action 三方比对）

> 2026-11-26 审计轮次 1。集合规模：前端 F=82（另 2 处动态调用人工解析）后端 B=96 文档 D=96。

| # | action | 类型 | 前端行号 | 后端行号 | API.md 位置 | 复核结论 | 处置（提交/豁免理由） |
|---|---|---|---|---|---|---|---|
| 1 | remuxscan | 未文档化 | app.js:3155 | 10132 | - | 确认缺陷（文档漂移） | ✅ 已修复：API.md「全部 action」清单按后端 96 个 action 重新生成 |
| 2 | pushplustest / autotune / autotuneinfo | 未文档化 | app.js:3140 / 2985 / 2957 | 10271 / 10151 / 10170 | - | 确认缺陷（同上，一批） | ✅ 同 #1 一并修复（清单重生成） |
| 3 | API.md 清单数 | 声称 87 个、实列 86 项 | - | - | 31 行 | 确认缺陷（漂移） | ✅ 同 #1 一并修复（现 96 个，与后端一致） |
| 4 | remove | 后端死接口（脚本误报） | 经 jobOp(act,id) 动态调用：index.html:842 → app.js:1990 | 10069 | 有 | 误报 | 动态调用，实际在用 |
| 5 | localtrashpurge / localtrashrestore | 后端死接口（脚本误报） | app.js:3777 三元分支 → 3779 动态调用 | 11333 / 11330 | 有 | 误报 | 动态调用，实际在用 |
| 6 | settings | 后端死接口 | 无（settings 数据随 jobs 响应下发：app.js:1126） | 10123 | 有 | 豁免 | 供 curl 使用的公开 API；settings/savesettings 文档配套 |
| 7 | tput / notifystate / credcheck / acctimport | 后端死接口（前端无引用） | - | 10178 / 10286 / 10604 / 10961 | 有 | 豁免 | API.md 明确定位为 curl 速查文档，保留脚本调用入口 |
| 8 | btadd/btlist/btctrl/ytadd/ytlist/ytdel | 后端死接口 + 未文档化 | - | 11276 | 清单新增 | 豁免 | 有意保留：给未强刷旧页面返回「已停用」提示（代码注释言明） |

**A 类小结**：无「前端孤儿调用」（F⊆B）、无「文档承诺未实现」（D⊆B）、无命名不一致；唯二问题是文档清单漂移，已修复归零。

## B 类：前端一致性（提示词 C）

| # | 问题类型 | 前端位置 | 后端位置 | 用户可见现象 | 复核结论 | 处置 |
|---|---|---|---|---|---|---|
| 1 | 字段断链：jobs 响应缺 proxyScope | app.js:1128（v3.77 专门写了同步逻辑） | payload 构造 9925–9931 无此键 | 外部改动代理模式/后端自动回落时，全局模式指示不同步 | 确认缺陷 | ✅ 已修复：payload 补 `"proxyScope": proxy_scope()` |
| 2 | 字段断链：resetsettings 响应缺 proxyScope | app.js:3073 | 10230 无此键 | 恢复默认设置后代理模式指示不同步 | 确认缺陷 | ✅ 已修复：返回 dict 补 `"proxyScope": proxy_scope()` |
| 3 | 幽灵字段 | 全模板 1864 个表达式、524 个标识符检查 | - | - | 无发现（background 为 CSS 键名误报；myShareKey 定义于 app.js:3165 注释同行） | 误报闭环 |
| 4 | 事件名缺失 | 模板全部事件方法比对（367 个定义） | - | - | 无发现 | 通过 |
| 5 | 高频 20 action 字段比对 | field_check.py 候选 8 项 | - | - | 误报闭环：`error` 由 do_POST 统一异常包装（11842–11846）；`length/get/replace` 为 JS 方法；`state/ok/used/free/total` 来自 accttest 分支内 dict | 通过 |

**B 类小结**：2 条确认缺陷已修复（补字段后全量 22 文件测试全绿）；其余为脚本候选的误报，逐条复核闭环。

## 动态拼接、无法静态判定的 action

| # | 位置 | 拼接逻辑 | 判定方式 | 结论 |
|---|---|---|---|---|
| 1 | app.js:1990 `this.api(act, {id})` | `jobOp(act, id)`，调用点 index.html:838/839/840/842 传 stop/resume/remove；app.js:1998 传 resume | 人工核对全部调用点 | stop / resume / remove 均有后端分支 ✅ |
| 2 | app.js:3779 `this.api(action, {ids, all}, 120000)` | 3777 行三元：op 为 purge/purgeAll → localtrashpurge，否则 localtrashrestore | 人工核对 | 两分支均存在 ✅ |

---

执行记录：

- 审计轮次 1：执行日期 2026-11-26，覆盖 action 96 个（前端 82 字面量 + 2 动态调用点解析），发现 A 类 2 项（合并处置 1 次文档修复）+ B 类 2 项字段断链，修复 3 处（docs/API.md、download-console.py ×2），误报豁免 2 类。
- 工具沉淀：`scripts/api_audit.py`（三方对账，长期使用）、`scripts/ghost_check.py`、`scripts/field_check.py`（一次性审计脚本，保留复现）。
