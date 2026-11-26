# 结构优化方案（基于实测数据）

> 实测日期 2026-11-26。数据由 `ast` 静态分析得出，不含人工估算。

## 一、诊断：问题不在总行数

| 指标 | 实测值 |
|---|---|
| `download-console.py` 总行数 | **11,927** |
| 顶层函数 / 类 / 常量 | 338 函数 / 2 类 / 238 常量 |
| `api()` 函数行数 | **2,114（占 17.7%）**，单函数 |
| `api()` 内 `if action == "..."` 分支 | **90 个**，中位数 16 行，最大 138 行 |
| 第二大函数 `_safe_dir_worker` | 300 行 |
| 前 20 大函数合计 | 约 4,300 行（含 api） |
| 顶层可变状态（dict / list / set / Lock） | **59 个** |
| `global` 语句 | 2 处 |

三张表读出来的结论：

1. **文件不小，但颗粒度基本健康**。除 `api()` 外，338 个函数里最大的才 300 行，绝大多数在 20–120 行。这不是屎山，是一个正常代码库被塞进了一个文件。
2. **唯一真正失控的是 `api()`**：2,114 行、90 个分支、占全文 17.7%。改任何一个接口，都要在一个 2000 行的函数里定位。
3. **耦合是真实的**：把 90 个分支与其调用的函数做并查集，88 个分支 + 329 个函数落在**同一个连通簇**（9,826 行）。也就是说业务域之间通过 worker / job 引擎互相喂养，**按"账号""任务""通知"这种业务域切文件，会切出一张 329 节点的引用网**。

## 二、红线：单文件是这个项目的分发契约

拆文件之前必须先认清，以下 6 处把「单文件」写进了协议：

| 位置 | 内容 | 拆包后必须改 |
|---|---|---|
| `download-console.py:8961` | `UPDATE_FILES = frozenset((8 个路径))` | 扩表到所有新文件，否则更新后缺文件 |
| `download-console.py:8965` | `UPDATE_REQUIRED` 3 个必需文件 | 加入包入口 |
| `download-console.py:9028 / 9161 / 9183` | `_update_safe_path(UPDATE_APP_DIR, rel)` 逐文件替换 | 目录级替换 + 回滚逻辑 |
| `scripts/build_release.py` | 用 `ast` 从**单文件**顶层 Assign 解析 `UPDATE_FILES` / `APP_VERSION` | 改成扫包，否则常量读不到 |
| `Dockerfile` | `CMD ["python3","-u","download-console.py"]` | 改 `-m cloudshuttle` |
| `download-console.py:18` | `DATA_DIR = ... or dirname(abspath(__file__))` | 包内取 `parent`，否则状态 JSON 落进包目录 |
| `tests/*.py`（22 个文件） | `spec_from_file_location("dc", download-console.py)`，共 144 个符号走 `m.xxx` | 全部改导入路径 |

**一句话**：拆包不是"删代码"，是改分发协议 + 改 341 个测试的导入方式。这是风险的主要来源，不是代码本身。

## 三、处方：四步，按收益/风险排序

### 第 1 步：把 `api()` 从 2,114 行铺平成注册表 + 90 个函数（收益最大，风险最低）

现状形态极规整，每个分支都是 `if action == "x": <自包含体> ... return {...}`，末尾 `raise RuntimeError("未知操作")`，**没有 elif 链、没有跨分支共享变量**。

```python
# 改造后：api() 收成 20 行
HANDLERS = {}

def action(name):
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco

@action("updatecheck")
def h_updatecheck(body):
    return update_check(bool(body.get("force")))

...

def api(action_name, body):
    fn = HANDLERS.get(action_name)
    if fn is None:
        raise RuntimeError("未知操作")
    return fn(body)
```

收益：单函数 2,114 → ~20 行；90 个 handler 可单独测试；`sorted(HANDLERS)` 直接生成 API 清单，能自动比对 `docs/API.md` 和 `static/app.js`。

**验证方式（关键）**：不要靠肉眼。用 AST 逐分支比对改造前后的函数体是否等价，再跑 `tests/` 全绿 + 抽 10 个 action 手工对返回值。

### 第 2 步：路径常量归一（20 分钟，纯防雷）

把 5 处 `os.path.dirname(os.path.abspath(__file__))`（18、87、8958、11381、11386）收敛成一个 `APP_HOME`。行为不变，但之后无论怎么拆，只需要改这一行。

### 第 3 步：切"叶子模块"成独立文件（可选，看收益）

**前提是先做依赖统计确认「对外引用 ≤ 5 个符号」**——只有这种域才能干净切出，否则切完就是一堆循环 import。候选（需验证）：`update`、`naming`（safe_basename / name_map / file_plan）、`classify_error` + 文本工具、`pikpak token`。

**任何新增文件都必须同步改**：`UPDATE_FILES`、`UPDATE_REQUIRED`、`build_release.py` 的解析方式。否则老用户在线更新后会因缺文件无法启动——这是本方案唯一的"会伤用户"的地方。

### 第 4 步：彻底拆包（最后做，收益中等，成本最高）

如果坚持要做，最小可行包结构：

```
cloudshuttle/
  __init__.py        # APP_VERSION / APP_HOME / DATA_DIR / UPDATE_* 常量集中于此
  rc.py              # rclone 调用、代理守护
  pikpak.py          # 令牌、登录、目录
  state.py           # settings / jobs / meta 的 JSON 读写（59 个可变状态的归属地）
  naming.py          # 安全文件名、name_map、file_plan
  jobs.py            # launch / worker / 队列 / 孤儿清理
  transfer.py        # 传输、copy、autotune、remux
  ops.py             # 分享、解压、改名、校验
  notify.py          # 通知与去重
  update.py          # 在线更新
  handlers/          # 90 个 action，按域分子文件
  server.py          # Handler / 静态资源 / main
download-console.py  # 只剩 3 行 shim，保留旧入口兼容
```

代价清单：`DATA_DIR` 取 parent、`UPDATE_FILES` 扩到 ~20 路径、Dockerfile CMD、`build_release.py` 重写、22 个测试文件 144 处 `m.` 引用、`docs/CODE-MAP` 更新。预计 1–2 天，且必须做一次真实的"旧版本 → 新版本"在线更新演练。

## 四、明确不建议做的事

- **不要按业务域一口气切文件**。88/90 个 handler 同簇，一次切完必然出现循环 import 和状态副本不一致。
- **不要动前端"零构建"**。`static/index.html` 280KB + `app.js` 209KB 拆成模块会引入构建步骤，违背项目核心取舍；真要减负只做 `app.js` 内部注释分区。
- **不要把 59 个模块级状态搬进类里**。那是第二次重构，先让 handler 独立，再谈状态归属。

## 五、第 1 步的验收标准

1. `api()` 函数 ≤ 40 行，只有注册表查找 + 未知 action 报错。
2. 90 个 `H:*` 分支全部变成 `@action("x")` 函数，AST 与原文逐一对应（脚本比对，不靠肉眼）。
3. `Get-ChildItem tests\test_*.py | ForEach-Object { python $_.FullName }` 全绿（341 用例）。
4. `sorted(HANDLERS)` 输出与 `docs/API.md`、`static/app.js` 的 action 清单可对账（这一步顺带解决文档漂移）。
