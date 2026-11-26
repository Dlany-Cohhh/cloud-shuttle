# 云梭 CloudShuttle 后端结构优化说明文

> 版本：v1 · 2026-11-26 · 适用代码基线 `v4.20`（11,927 行实测）
> 配套文档：`ARCHITECTURE-REFACTOR.md`（精简方案版）、`AI-CODE-REVIEW-PROMPTS.md`（AI 审查提示词）、`EXECUTION-PLAN.md`（总执行计划）、`README.md`（文件夹入口）
> 本文档自成一体，不依赖其他文档即可阅读。

---

## 1. 这份说明文解决什么问题

「`download-console.py` 上万行了，太大了，怎么优化结构？」

这个问题要拆成三个小问题回答，本文按同样顺序展开：

1. **大在哪里**——用数据定位真正的痛点，而不是凭感觉；
2. **什么不能动**——单文件是分发协议，先划清红线再动手；
3. **怎么一步步变小**——四步方案，每步给出操作、验收、回滚三件套。

---

## 2. 诊断：大在哪里

以下全部数字来自 2026-11-26 对 `v4.20` 代码的 `ast` 静态分析，非估算。

### 2.1 基础指标

| 指标 | 实测值 |
|---|---|
| 文件总行数 | 11,927 |
| 文件体积 | 591 KB |
| 顶层函数 | 338 个 |
| 顶层类 | 2 个（`Handler` 334 行、`_JobAbort` 6 行） |
| 顶层常量赋值 | 238 处 |
| 顶层可变状态（dict/list/set/Lock） | 59 个 |
| `global` 语句 | 仅 2 处 |

### 2.2 关键发现一：失控的是一个函数，不是整个文件

| 指标 | 实测值 |
|---|---|
| `api()` 单函数行数 | **2,114 行，占全文 17.7%** |
| `api()` 内 `if action == "..."` 分支 | **90 个** |
| 分支行数分布 | 最小 2 行 / 中位 16 行 / 最大 138 行 |
| 除 `api()` 外最大的函数 | `_safe_dir_worker`，300 行 |
| 第 3–20 大函数 | 84–211 行 |

**解读**：338 个函数里约 330 个在 20–120 行的健康区间。这不是无人维护的屎山，而是一个**结构正常、但被一个巨型分发函数卡住脖子**的代码库。它是一个内部多函数粘连的「包」，而非一个真正的「文件」。

### 2.3 关键发现二：业务域之间高度连通，不能按功能切

把 90 个 action 分支与它们调用的顶层函数做并查集（调用即合并），得到：

- **88 个分支 + 329 个函数落在同一个连通簇里，合计 9,826 行**（占全文 82%）；
- 只有 `remotes`、`providers` 两个分支完全独立；
- 没有任何一个顶层函数不被 handler 簇触达。

**解读**：任务引擎、worker、重试、通知、更新……互相之间通过 job 字典和共享状态喂养。如果按「账号 / 任务 / 通知 / 文件」这样的业务域切文件，切出来不是一个模块图，而是一张 329 节点的引用网——循环 import 和状态副本不一致几乎是必然结果。

### 2.4 关键发现三：分支形态规整，机械搬移即可

`api()` 的 90 个分支呈现严格统一的形态：

```python
if action == "xxx":
    ...自包含逻辑...
    return {...}
```

没有 elif 链、没有跨分支共享的局部变量、结尾统一 `raise RuntimeError("未知操作")`。这意味着第 4 节的第 1 步改造可以**纯机械完成**，可用 AST 逐分支断言等价，不需要任何语义判断。

### 2.4.1 前端不需要动

`static/index.html`（281 KB）+ `static/app.js`（209 KB）虽大，但「零构建、改一行刷新即生效」是这个项目的核心设计取舍（README 明文承诺），拆前端模块会引入 npm 构建步骤，收益为负。本方案**不碰前端**。

---

## 3. 红线：为什么不能随手拆

单文件不只是习惯，它是**三条外部协议的载体**。任何「新增一个 `.py` 文件」的改动，都会牵动下面 7 处。这就是拆包风险的主要来源——不在代码，在协议。

| # | 位置 | 现状 | 拆包后必须改什么 |
|---|---|---|---|
| 1 | `download-console.py:8961` | `UPDATE_FILES = frozenset((8 个路径))` 在线更新白名单 | 每个新文件都要进白名单，漏一个 = 更新后起不来 |
| 2 | `download-console.py:8965` | `UPDATE_REQUIRED` 3 个必需文件 | 包入口文件必须加入 |
| 3 | `download-console.py:9028/9161/9183` | `_update_safe_path(UPDATE_APP_DIR, rel)` 逐文件替换 + 回滚 | 替换/回滚逻辑要支持目录 |
| 4 | `scripts/build_release.py` | 用 `ast` 从**单文件**顶层解析 `UPDATE_FILES` / `APP_VERSION` / `UPDATE_ASSET` | 常量搬走后解析不到，构建直接报错 |
| 5 | `Dockerfile` | `CMD ["python3","-u","download-console.py"]` | 改为 `python3 -m cloudshuttle` |
| 6 | `download-console.py:18/87/11381` | `DATA_DIR` 等 5 处 `dirname(abspath(__file__))` | 包内取 parent，否则状态 JSON 会写进包目录 |
| 7 | `tests/*.py`（22 个文件） | `spec_from_file_location("dc", .../download-console.py)` 载入，144 个 `m.xxx` 符号引用 | 全部改导入方式 |

**特别提醒第 1 条**：老用户的容器正在跑旧代码，在线更新只会下载白名单里的文件。你本地拆好了 20 个文件、跑通了全部测试，发版之后任何一个白名单外的文件都会缺失——这是本方案唯一可能**伤到真实用户**的环节，演练流程见 4.4.5。

---

## 4. 处方：四步方案

总原则：**先做零协议风险的内部重构，再谈拆文件**。每步独立验收、独立提交、独立可回滚。

```
第 1 步 api() 铺平        ── 不新增文件，不碰协议      收益 ★★★ 风险 ★
第 2 步 路径常量归一       ── 不新增文件，不碰协议      收益 ★★  风险 ☆
第 3 步 切叶子模块        ── 新增 2–4 个文件，碰协议   收益 ★★  风险 ★★
第 4 步 彻底拆包          ── 新增 ~12 个文件，重写协议 收益 ★   风险 ★★★
```

### 4.1 第 1 步：把 `api()` 铺平成注册表 + 90 个函数（必做）

#### 4.1.1 目标形态

```python
# ---- action 注册表 ----
HANDLERS = {}

def action(name):
    """把函数注册为 /api 的一个 action。"""
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco


@action("updatecheck")
def h_updatecheck(body):
    return update_check(bool(body.get("force")))


@action("health")
def h_health(body):
    try:
        m = rc("core/memstats")
    except Exception as e:
        return {"error": str(e)[:150]}
    ...


def api(action_name, body):
    fn = HANDLERS.get(action_name)
    if fn is None:
        raise RuntimeError("未知操作")
    return fn(body)
```

改造后 `api()` 从 2,114 行缩到约 20 行；90 个 handler 变成可单测的独立函数。

#### 4.1.2 操作要点

1. **逐分支搬移，一次一个**：分支体原样剪切为函数体，`body.get(...)` 等参数写法不变，只补一个函数签名 `def h_xxx(body):`。分支里有嵌套 `def` 的（如 `_vol_dirs`）照搬，嵌套 def 不受影响。
2. **命名规则**：`h_<action>`，handler 与 action 名一一对应，肉眼可查。
3. **保持注册顺序 = 原文件分支顺序**，diff 面积最小。
4. **不要顺手改逻辑**。这一步只搬移不改写；发现可疑代码记 TODO，另开提交处理。

#### 4.1.3 验收（关键：不靠肉眼）

- **AST 等价断言**：写一次性脚本，解析改造前后的源码，逐分支比对新函数体与原分支体的 `ast.dump()` 是否一致（仅允许签名行的差异）；
- **全量测试**：

```powershell
cd E:\A-MyProject\cloud-shuttle
Get-ChildItem tests\test_*.py | ForEach-Object { python $_.FullName }   # 22 文件 341 用例须全绿
```

- **运行冒烟**：起服务，前端打开「概览 / 云盘预览 / 任务 / 设置 / 检查更新」各点一遍；
- **接口对账**：`print(sorted(HANDLERS))` 的 90 项与 `static/app.js` 的 `api("...")` 字面量、`docs/API.md` 声明三方比对（此前文档已有对齐审计提示词，可直接复用）。

#### 4.1.4 回滚

单文件内部改动，`git checkout -- download-console.py` 即可整体回退。

### 4.2 第 2 步：路径常量归一（20 分钟，纯防雷）

现状 5 处 `os.path.dirname(os.path.abspath(__file__))` 分散在 18、87、8958、11381、11386 行。收敛为：

```python
APP_HOME = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CONSOLE_DATA_DIR") or APP_HOME
```

其余 4 处全部引用 `APP_HOME`。行为完全不变，但第 3/4 步无论怎么搬文件，路径语义只需维护一行。

### 4.3 第 3 步：切「叶子模块」（可选，先验证再切）

**准入条件（先测再切）**：目标域内函数对外部的引用 ≤ 5 个符号。超过就先不切，等第 1 步完成后依赖会自然下降再评估。

按此标准从 2.3 的簇里挑候选：

| 候选模块 | 内容 | 理由 |
|---|---|---|
| `wcupdate.py` | 在线更新（8950–9250 行一带） | 对外只依赖 `APP_HOME`、几个工具函数；自身被 `h_updatecheck` 等 4 个 handler 调用 |
| `wcnaming.py` | `safe_basename`/`safe_relpath`/name_map/file_plan（2043–2420 行一带） | 近乎纯函数，测试 `test_safe_naming.py` 已单独覆盖 |
| `wcerror.py` | `classify_error`/`is_net_error`/`trim_err`/`mask_secret` 等文本工具 | 纯函数，无状态 |

**每个新文件落地时的固定动作清单**（对应第 3 节红线，逐项打勾）：

1. `UPDATE_FILES` 加路径；
2. 非必需文件不进 `UPDATE_REQUIRED`（缺了也能启动的才算可选）；
3. `build_release.py` 的 `source_constants()` 改为「多文件扫描或读 manifest」，跑一次 `python scripts/build_release.py` 验证能出包；
4. 跑全量测试 + 冒烟；
5. 打一个真实 Release 走一遍「旧版 → 新版」更新（见 4.4.5）。

### 4.4 第 4 步：彻底拆包（最后做，成本最高）

#### 4.4.1 何时值得做

满足任意一条再做：出现第二位贡献者；前后端接口超过 150 个 action；单文件让 AI 审查/编辑工具频繁出错。**只追求行数变小，不是做这一步的理由**。

#### 4.4.2 目标结构

```
cloudshuttle/
  __init__.py        # APP_VERSION / APP_HOME / DATA_DIR / 全部 UPDATE_* 常量（唯一出处）
  rc.py              # rclone 调用层、代理 rcd 守护
  pikpak.py          # 令牌、登录、目录遍历
  state.py           # settings / jobs / meta 的 JSON 读写（59 个可变状态归这里管）
  naming.py          # 安全文件名、name_map、file_plan
  jobs.py            # launch / worker 两代退出 / 队列 / 孤儿清理
  transfer.py        # 传输、copy、autotune、remux
  ops.py             # 分享、解压、改名、校验
  notify.py          # 通知与去重
  update.py          # 在线更新
  handlers/          # 90 个 @action 函数，按域分子文件
  server.py          # Handler、静态资源、main()
download-console.py  # 3 行 shim：from cloudshuttle.server import main; main()
```

保留 shim 是为了兼容「裸机 python download-console.py」的老习惯，同时 Dockerfile 可改 `python3 -u -m cloudshuttle`。

#### 4.4.3 代价清单（预计 1–2 天，含测试）

- 第 3 节 7 处协议全部触发：白名单扩到 ~20 路径、`build_release.py` 重写、Dockerfile、`DATA_DIR` 取 parent、22 个测试文件的 144 处 `m.` 引用改为 `import cloudshuttle.jobs as m` 风格；
- 59 个模块级可变状态按归属搬进 `state.py`，搬迁时**逐个域迁移**，迁一个跑一轮测试；
- `docs/API.md`、`README.md` 目录结构章节同步。

#### 4.4.4 拆包顺序（依赖方向只能自上而下）

```
__init__（常量）→ wcerror（纯工具）→ naming → state → rc → pikpak
→ jobs → transfer → ops → notify → update → handlers → server → shim
```

每并入一个模块跑一轮全量测试；出现循环 import 立即停，说明切分边界错了，回到当前提交重新划界，**不要用局部 import 糊过去**。

#### 4.4.5 发版前的强制演练

```powershell
# 本地模拟老用户升级路径：
# 1. 用旧版本 zip（v4.20）启动一个容器
# 2. 在「检查更新」里指向新包 Release
# 3. 观察更新日志中 20 个路径逐一替换成功、容器自动重启、版本号变化
# 4. 回滚按钮点一遍，确认能回旧版
```

四项全过才算拆包完成。任何一项失败，先修 `update.py`，不要让用户帮你测更新。

### 4.5 不建议做的事及理由

| 不要做 | 理由 |
|---|---|
| 按业务域一口气切文件 | 88/90 个 handler 同簇（2.3 节），必然循环 import |
| 拆前端为零构建模块 | 违背 README 承诺的核心取舍，引入 npm 后「改一行刷新即生效」消失 |
| 把 59 个模块级状态搬进类 | 是第二次重构；先让 handler 独立，状态归属等问题一轮解决不了 |
| 用 `importlib` 动态加载绕过白名单 | 更新协议照旧失败，只是把报错推迟到运行时 |
| 顺手重构可疑代码 | 搬移提交与改写提交必须分开，否则出问题时无法定位是搬错还是改错 |

---

## 5. 每步共同的验收基线

无论做到第几步，每次提交前固定跑：

```powershell
cd E:\A-MyProject\cloud-shuttle
python -m py_compile download-console.py        # 语法（拆包后加上各新文件）
Get-ChildItem tests\test_*.py | ForEach-Object { python $_.FullName }   # 341 用例
python scripts\build_release.py                 # 从第 3 步起必跑：确认能出包
```

注意：`tests/` 没有 `__init__.py`，`python -m unittest discover` 会报 *Start directory is not importable*，必须逐文件循环跑。这不是 bug，是现状约定，拆包时顺带补 `__init__.py` 即可解决。

---

## 6. 风险与应急

| 风险 | 触发条件 | 应急 |
|---|---|---|
| 在线更新后缺文件 | 新文件没进 `UPDATE_FILES` | 4.4.5 演练强制覆盖；发布前脚本断言 `包内文件 == 白名单` |
| 新旧 Python 混跑 | 更新后容器没重启 | `_update_restart` 已处理（v3.80 逻辑），拆包后回归测试要保住这条 |
| 状态 JSON 写错位置 | `DATA_DIR` 未取 parent | 第 2 步先做常量归一；启动后检查 `APP_HOME` 下无新增 json |
| 搬移引入语义差异 | 手滑改了逻辑 | AST 等价断言 + 搬移/改写分离提交 |
| 循环 import | 模块边界画错 | 立即回退到上一个绿提交，重新划界 |

---

## 7. 附录

### 7.1 本文所有实测数据的来源

- 行数 / 函数规模 / 分支统计：Python `ast` 模块静态解析 `v4.20` 源码（`end_lineno` 精确到行）；
- 连通簇：分支与其调用的顶层函数做并查集合并；
- 协议清单：`grep` `UPDATE_FILES|UPDATE_APP_DIR|__file__` 及 `build_release.py` 源码；
- 测试基线：2026-11-26 实跑，22 文件 341 用例全绿，`py_compile` 通过。

### 7.2 术语

- **handler**：`POST /api` 里 `if action == "x"` 分支的改后形态，一个 action 一个函数；
- **分发协议**：`UPDATE_FILES` 白名单 + 发行包结构 + Dockerfile 入口 + 测试加载方式，四者共同构成「单文件对外承诺」；
- **叶子模块**：对外引用 ≤ 5 个符号、可独立成文件且不触发循环 import 的代码域；
- **簇**：经由直接调用关系连通的函数集合，用来度量业务域之间的耦合。

### 7.3 一句话总结

**不要为了行数拆文件；先把 2,114 行的 `api()` 铺平成 90 个可单测的 handler，把路径常量归一；确有第二贡献者或接口继续膨胀时，再按「叶子先行、协议同步、演练护航」的次序拆包。**
