# 完整性检测 + Bug 猎杀 + 结构优化 · 文档夹

> 入口文件 · 2026-11-26 建立 · 基线代码 `v4.20`（11,927 行实测）

这个文件夹是「用 AI 检查云梭的完整性与 bug + 优化上万行结构」这一整套工作的完整归档：执行计划、提示词、施工图、数据底稿、可复现分析脚本、待填追踪表，全部在此，不散落。

## 文件清单与阅读顺序

| 顺序 | 文件 | 角色 |
|---|---|---|
| ① | `EXECUTION-PLAN.md` | **总指挥**：五阶段流程、依赖顺序、验收关卡、时间预算 |
| ② | `AI-CODE-REVIEW-PROMPTS.md` | **弹药库**：提示词 A–E 原文（接口对齐 / 子系统猎杀 / 前端一致 / diff 审查 / 回归测试） |
| ③ | `STRUCTURE-OPTIMIZATION-GUIDE.md` | **施工图**：重构四步的操作、验收、回滚细节 |
| ④ | `ARCHITECTURE-REFACTOR.md` | **数据底稿**：AST 实测指标、分发协议七处红线、决策依据 |

## templates/ —— 执行时填写的追踪表

| 文件 | 对应阶段 | 状态 |
|---|---|---|
| `BASELINE-RECORD.md` | 阶段 0 机械基线 | 已预填 2026-11-26 实测值 |
| `API-DIFF-TRACKER.md` | 阶段 1 接口对齐 | 空表待填 |
| `BUG-TRACKER.md` | 阶段 2 bug 猎杀闭环 | 空表待填 |

## tools/ —— 可复现的分析脚本

| 脚本 | 用途 | 用法 |
|---|---|---|
| `analyze.py` | 函数规模排行（谁最大、占比多少） | `python tools/analyze.py` |
| `cluster.py` | api() 分支与函数的依赖簇并查集 | `python tools/cluster.py` |

两个脚本自动向上定位仓库根目录，在仓库任意位置运行均可；无需第三方依赖（仅标准库 `ast`）。

## 快速开始

```powershell
cd E:\A-MyProject\cloud-shuttle
# 阶段 0：锁基线
python -m py_compile download-console.py
Get-ChildItem tests\test_*.py | ForEach-Object { python $_.FullName }
# 复现本文档体系的数据
python docs\audit-refactor\tools\analyze.py
python docs\audit-refactor\tools\cluster.py
```

然后按 `EXECUTION-PLAN.md` 的阶段顺序执行：**锁基线 → 提示词 A/C 穷举断链 → 提示词 B/D 闭环猎杀 → 铺平 api() → 复跑基线并接口对账 → （满足触发条件才）拆包。**

## 执行进度（2026-11-26 第二轮）

✅ **阶段 0–4 已全部完成**（详见 `EXECUTION-PLAN.md` 第 5 节与三张追踪表）：
- 阶段 1 修复 2 项文档漂移 + 2 项字段断链（proxyScope）；
- 阶段 2 四子系统猎杀 0 确认缺陷（v4.15–v4.20 连续修复后基线健康）；
- 阶段 3 `api()` 2,114 行 → 5 行，96 action 铺平为 `@action` 注册表（AST 逐分支等价断言通过），路径常量归一 `APP_HOME`；
- 阶段 4 复跑基线全绿，`scripts/api_audit.py` 升级为从 `HANDLERS` 直接对账。
- 阶段 5（拆包）触发条件未满足，按计划不做。

## 核心结论备忘（三句话）

1. 病灶是一个 2,114 行、90 分支的 `api()` 函数，不是文件行数本身；338 个函数里其余最大才 300 行。
2. 单文件是在线更新的分发协议（`UPDATE_FILES` 白名单等 7 处），拆文件前必须先走阶段 3 的零风险内部重构。
3. AI 审查三铁律：每条结论带 `文件:行号`+原文片段、每视角至少 3 条结论、设计取舍（单机内网 / 无鉴权 / JSON 持久化 / 零构建）不作为缺陷。
