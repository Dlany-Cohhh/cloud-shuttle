# 基线记录（阶段 0 产物）

> 每次进入一轮新的「检测 + 优化」循环，先复制一份本表填新值；历史基线往下追加，不覆盖。

## 当前基线

| 项 | 值 |
|---|---|
| 记录日期 | 2026-11-26 |
| 代码版本 | v4.20 |
| git 提交 | 51c258e（docs: 补 v4.19 开发记录）+ 5 个未提交改动（CHANGELOG / README / API.md / download-console.py / test_notify.py） |
| Python | 3.13.18（注意：与容器内 3.12 不同，测试仅测逻辑不测版本特性） |
| `py_compile` | ✅ 通过 |
| 测试 | ✅ 22 文件 341 用例全绿（逐文件循环跑，`unittest discover` 因缺 `__init__.py` 不可用） |

## 实测结构数据（tools/ 脚本输出，2026-11-26）

| 指标 | 值 |
|---|---|
| `download-console.py` 总行数 | 11,927（591 KB） |
| 顶层函数 / 类 / 常量 | 338 / 2（`Handler` 334 行、`_JobAbort` 6 行）/ 238 |
| `api()` 单函数 | 2,114 行，占 17.7%，90 个 action 分支（中位 16 行，最大 138 行） |
| 其余最大函数 | `_safe_dir_worker` 300 行 |
| 顶层可变状态（dict/list/set/Lock） | 59 个；`global` 语句仅 2 处 |
| 依赖簇 | 88/90 handler + 329 函数同簇（9,826 行，占 82%）；仅 `remotes`、`providers` 独立 |
| 前端 | `index.html` 281 KB + `app.js` 209 KB（97 处 `api()` 调用），零构建，不在本轮范围 |

## 分发协议红线位置（重构/拆包前必查）

| 位置 | 内容 |
|---|---|
| `download-console.py:8961` | `UPDATE_FILES` 白名单（8 路径） |
| `download-console.py:8965` | `UPDATE_REQUIRED`（3 必需文件） |
| `download-console.py:9028/9161/9183` | 逐文件替换 + 回滚逻辑 |
| `scripts/build_release.py` | 从单文件 AST 解析常量 |
| `Dockerfile` | `CMD python3 -u download-console.py` |
| `download-console.py:18/87/11381` | 5 处 `dirname(abspath(__file__))` |
| `tests/*.py` | `spec_from_file_location` 载入 + 144 处 `m.xxx` 引用 |

---

## 历史基线归档

### 第 2 轮 · 2026-11-26（完整性审计 + Bug 猎杀 + 铺平 api() 之后）

| 项 | 值 |
|---|---|
| 触发 | 本文件夹执行计划阶段 0–4 全部完成 |
| git 提交 | 基于基线 51c258e 之上：+ 阶段 1 修复（API.md 重生成、proxyScope ×2）+ 阶段 2 回归测试 + 阶段 3 铺平重构（待提交） |
| `py_compile` | ✅ 通过 |
| 测试 | ✅ 23 文件 337 用例全绿（+1 文件 2 用例：test_regression_proxyscope.py） |
| `download-console.py` 总行数 | 11,973（重构搬移带来 +46 行注释/装饰器/空行） |
| `api()` 行数 | **5 行（原 2,114 行）**；96 个 action = 90 个 `@action("x")` + 1 个 `@action(...)` 六名组（bt/yt 停用提示） |
| 最大函数 | `_safe_dir_worker` 300 行（原第 2 名上位）；`h_start` 138 行为最大 handler |
| AST 等价断言 | ✅ `scripts/verify_refactor.py`：96/96 action 等价（只搬不改） |
| 路径常量 | 6 处 `dirname(abspath(__file__))` 归一为 `APP_HOME`（唯一出处，download-console.py:20） |
| 三方对账 | `scripts/api_audit.py`：F=82 B=96 D=96，与重构前完全一致（14 条已知豁免） |
| 冒烟 | HTTP 起服：GET / 200（281KB）；POST version/stats/jobs/health 等正常；未知 action 回「未知操作」 |
| 顶层函数数 | 430（含 97 个新 handler + 注册器） |

