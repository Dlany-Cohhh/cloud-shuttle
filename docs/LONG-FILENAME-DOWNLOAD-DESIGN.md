# 超长文件名下载改造详细方案

> 状态：**阶段 1～3 已实施（v3.83，2026-11-20）**；阶段 4 的真机 / 压测未做，见第 14 节实施记录  
> 适用版本：当前仓库 `main` 分支（基线提交 `98b1bbf`，2026-11-15）  
> 目标：让下载机在无人值守场景下，遇到超长文件名时仍能自动、稳定、可追溯地下载，不出现“永远下载中、0 速度、进度不动”的假死。

---

## 1. 背景与现象

用户反馈：下载文件名过长的文件时，任务会卡住。

实际表现：

- 前端状态一直显示“下载中”。
- 速度为 `--` 或 0。
- 已完成字节数不增长。
- 目录任务中其他文件下完后，整体进度停在某个百分比不再前进。
- 最终可能很久以后才报错，也可能一直不脱离 `running`。

这不是前端渲染卡死，而是**写盘失败被当成可重试的网络错误**，任务状态没有正确进入终态。

---

## 2. 现状根因

### 2.1 文件名原样下发，无任何清洗或截断

当前代码路径：

- `download-console.py:6073`：`name = basename_of(sub)`，取云端文件名。
- `download-console.py:6118`：`job["leaf"] = leaf`，保存原始文件名。
- `download-console.py:1516-1518`：单文件下载时：

  ```python
  method = "operations/movefile" if move else "operations/copyfile"
  kv = dict(srcFs=job["src_dir"], srcRemote=job["leaf"],
            dstFs=job["dst_dir"], dstRemote=job["leaf"], _async="true")
  ```

  源端和目标端都直接使用原始文件名。

- `download-console.py:7255-7256`：URL 直链下载使用 `autoFilename="true"`，文件名由 rclone 从 URL 或 `Content-Disposition` 自动获取，项目侧不处理。

项目里唯一的文件名长度检查在 `download-console.py:2270`：

```python
def _bad_entry_name(n):
    s = str(n or "").strip()
    if not s or len(s) > 180 or s in (".", ".."):
        return True
    ...
```

但它只服务于手动新建目录、重命名、移动等 CRUD 操作，**下载链路没有调用**。

### 2.2 文件系统限制没有被处理

- Windows 默认 `MAX_PATH` 为 260 字符；即使开启长路径，单个文件名组件仍受 NTFS 255 UTF-16 字符限制。
- rclone 在 Docker/Linux 容器内运行时，Linux 单组件上限通常是 255 字节；中文按 UTF-8 编码通常 3 字节一个字，约 85 个汉字就可能触顶。
- 项目没有 Windows 非法字符处理：`\ / : * ? " < > |`。
- 没有处理结尾空格、结尾句点、保留设备名 `CON / PRN / AUX / NUL / COM1-9 / LPT1-9`。
- 没有处理大小写不敏感文件系统上的冲突，例如 `A.mp4` 和 `a.mp4`。

### 2.3 错误分类漏掉文件系统错误

`download-console.py:1573-1586` 的 `FATAL_JOB_ERRORS` 只包含业务错误：

- 下载额度用尽
- 存储空间不足
- captcha
- token 失效
- 账号不存在

没有下列关键字：

- `file name too long`
- `filename too long`
- `name too long`
- `path too long`
- `文件名或扩展名太长`

`_fatal_job_error()`（`download-console.py:1598-1605`）识别不了这类错误，返回 `None`。

### 2.4 非致命异常不改任务状态

`job_live()` 在 `download-console.py:3518-3529`：

```python
else:
    job["_miss"] = 0
    fatal = _fatal_job_error(msg)
    if fatal:
        ...
        job["state"], job["error"] = fatal
    else:
        job["error"] = msg[:200]
```

非致命错误只写 `error`，不改 `state`。因此 rclone 因文件名过长反复重试时，任务一直是 `running`。

注释 `download-console.py:1568-1571` 已经描述过这个现象：

> 命中后必须让任务脱离 running，否则 job_live 每轮都去问 job/status，任务会永远显示「运行中」却 0 速度、0 字节。

只是当时的补丁只覆盖了网盘业务错误。

### 2.5 重试配置放大卡住时长

- 默认重试次数：`download-console.py:186`，值为 10。
- 高级模式可配置到 100：`download-console.py:1293`、`1387`。

文件名过长是确定性失败，重试 100 次会把一次秒级错误拖成几十分钟的“假死”。

---

## 3. 设计目标

### 3.1 必达目标

1. **长文件名自动下载**：不需要用户手动改名。
2. **源文件名不丢失**：前端显示、续传、校验都使用原始文件名。
3. **确定性映射**：同一个源文件每次都生成同一个落盘名，重启、升级后不变。
4. **零冲突**：不同目录同名、前缀截断相同、大小写冲突都不会互相覆盖。
5. **失败隔离**：一个坏文件只影响自己，不阻塞整个目录任务。
6. **状态可终态化**：任何确定性错误都必须让任务或子任务脱离 `running`。
7. **可回滚**：通过开关关闭新逻辑，旧任务不受影响。

### 3.2 非目标

- 不批量重命名已经下载好的历史文件。
- 不把 Windows 注册表的长路径开关当作主要解决方案。
- 不恢复 aria2 通道；当前 aria2 已停用。
- 不改变 PikPak 云端源文件本身的名字。

---

## 4. 总体架构

```text
源路径清单
   ↓
安全映射（清洗 + 截断 + 哈希后缀）
   ↓
映射清单（原名 ↔ 落盘名）
   ↓
逐文件复制（源端原名，目标端安全名）
   ↓
错误分类（文件级 / 账号级 / 瞬时 / 磁盘权限）
   ↓
界面展示（始终显示原名）
```

关键原则：

> **源端永远使用原始文件名，目标端永远使用安全落盘名。**

---

## 5. 核心模块设计

### 5.1 安全文件名生成

新增模块建议放在 `download-console.py` 顶部工具函数区，或单独拆出 `safe_naming.py`。

#### 5.1.1 UTF-8 安全截断

```python
def truncate_utf8(s, limit_bytes):
    b = s.encode("utf-8")
    if len(b) <= limit_bytes:
        return s
    return b[:limit_bytes].decode("utf-8", "ignore")
```

必须按字节截断，不能按字符截断，否则中文可能被截成半个 UTF-8 序列。

#### 5.1.2 单路径段清洗

```python
WIN_RESERVED = set('<>:"/\\|?*')
WIN_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

def clean_segment(s):
    s = unicodedata.normalize("NFC", str(s or ""))
    s = "".join("_" if ch in WIN_RESERVED or ord(ch) < 32 else ch for ch in s)
    s = s.strip(" .")
    if not s:
        s = "unnamed"
    if s.split(".")[0].upper() in WIN_DEVICE_NAMES:
        s = "_" + s
    return s
```

#### 5.1.3 生成安全文件名

```python
def safe_basename(original, remote, src_rel, max_bytes=180, hash_len=12):
    original = clean_segment(original)
    stem, ext = os.path.splitext(original)

    ext = clean_segment(ext)
    if len(ext.encode("utf-8")) > 32:
        ext = truncate_utf8(ext, 32)

    digest = hashlib.sha256(
        (str(remote) + "\x00" + str(src_rel)).encode("utf-8")
    ).hexdigest()[:hash_len]

    suffix = "~" + digest
    need_hash = (
        original != str(original)
        or len(original.encode("utf-8")) > max_bytes
        or original.upper() in WIN_DEVICE_NAMES
    )

    if not need_hash:
        return original

    budget = max_bytes - len(suffix.encode("utf-8")) - len(ext.encode("utf-8")) - 1
    if budget < 16:
        budget = 16

    stem = truncate_utf8(stem, budget).rstrip(" .") or "file"
    return stem + suffix + ext
```

说明：

- 只在必要时追加哈希，正常文件名保持原样，避免所有文件都变成难看的哈希名。
- 哈希输入使用 `remote + 完整源相对路径`，保证不同网盘、不同目录下的同名文件不会冲突。
- 哈希只取前 12 位，冲突概率极低；同时保留冲突检测兜底。

#### 5.1.4 目录段映射

目录路径也要逐段清洗：

```python
def safe_relpath(src_rel, remote, max_total=240):
    parts = src_rel.split("/")
    safe_parts = []
    for part in parts:
        safe_parts.append(safe_basename(part, remote, src_rel, max_bytes=120))
    rel = "/".join(safe_parts)
    if len(rel.encode("utf-8")) > max_total:
        rel = truncate_utf8(rel, max_total)
    return rel
```

`max_total=240` 给 Windows 根路径前缀留出余量，避免“文件名合法但完整路径超长”。

#### 5.1.5 冲突处理

生成映射表时统一检测：

- 同一目标目录下大小写不敏感同名：`casefold()` 后比较。
- 截断后撞名：发现重复则追加 `-2`、`-3`。
- 保留名冲突：`CON.mp4` 等统一加前缀 `_`。

冲突处理必须在**任务扫描阶段一次性完成**，不能边下载边处理，否则重启后映射会漂移。

---

### 5.2 映射清单

#### 5.2.1 存储位置

每个卷根目录放一个隐藏清单：

```text
<volume-rclone-root>/.cloudshuttle-name-map.json
```

如果卷根是 rclone 远端表示，例如 `local:/volume1/download`，则实际路径为该卷对应本地目录。

#### 5.2.2 数据结构

```json
{
  "version": 2,
  "updated": "2026-11-20 15:00:00",
  "entries": {
    "pk:path/to/原始长文件名.mp4": {
      "original": "path/to/原始长文件名.mp4",
      "safe": "path/to/原始长文件名_~9f3c21a8.mp4",
      "remote": "pk",
      "size": 123456789,
      "status": "done",
      "attempts": 1,
      "error": "",
      "checksum": "",
      "updated": "2026-11-20 15:00:00"
    }
  }
}
```

#### 5.2.3 读写规则

- 读：任务启动时加载一次，常驻内存。
- 写：状态变化时更新对应条目。
- 原子写：先写 `xxx.json.tmp`，再 `os.replace(tmp, final)`。
- 并发：所有写操作放进现有全局 `LOCK`；如果后续拆分模块，改用独立 `NAME_LOCK`。
- 兼容：读取时如果 `version != 2`，不自动迁移，新建 v2 清单，旧文件保留。
- 容量：超过 5 万条时考虑切换到 SQLite；第一版 JSON 足够。

#### 5.2.4 查询接口

建议新增：

```python
load_name_map(volume) -> dict
save_name_map(volume, data) -> None
get_safe_path(volume, remote, src_rel) -> str
record_file_state(volume, remote, src_rel, **fields) -> None
```

---

### 5.3 任务状态机

#### 5.3.1 任务级状态

```text
queued → scanning → running → verifying → done
                         ↓
                      paused / stopped / error
```

含义：

- `scanning`：正在列目录、生成映射。
- `running`：传输中。
- `verifying`：校验文件数量和大小。
- `paused`：账号类错误，等用户处理。
- `stopped`：用户停止。
- `error`：任务级不可恢复错误。

#### 5.3.2 文件级状态

```text
pending → running → done
              ↓
         skipped / failed
```

含义：

- `skipped`：目标已存在且大小一致。
- `failed`：该文件失败，记录原因，可单独重试。
- `done`：下载完成并通过基础校验。

文件级失败不会改变任务级 `running`，除非全部文件都失败。

---

### 5.4 传输流程

#### 5.4.1 单文件下载

改动最小，优先实施：

1. `start` 阶段算出 `job["safe_leaf"] = safe_basename(...)`。
2. `launch_job()` 中改成：

   ```python
   kv = dict(srcFs=job["src_dir"], srcRemote=job["leaf"],
             dstFs=job["dst_dir"], dstRemote=job["safe_leaf"], _async="true")
   ```

3. 写入映射清单。
4. 前端 `curfile` 显示 `job["leaf"]`（原名），详情显示 `safe_leaf`。

#### 5.4.2 目录下载

当前 `sync/copy` 会原样保留文件名，无法做逐文件改名，必须改成逐文件复制：

1. 用 rclone RC 列源目录，递归拿到所有文件：

   ```python
   files = rc("operations/list", fs=job["src"], remote=sub, recurse=True)
   ```

2. 对每个文件生成安全相对路径，写入映射清单。
3. 建立目标目录结构。
4. 用线程池并发处理，线程数取 `s["transfers"]`。
5. 每个文件调用一次 `operations/copyfile`：

   ```python
   rc(
       "operations/copyfile",
       srcFs=src_dir,
       srcRemote=original_leaf,
       dstFs=dst_dir,
       dstRemote=safe_leaf,
       _timeout=file_timeout,
   )
   ```

   建议第一版用同步调用，而不是 `_async="true"`。好处是错误归属清晰、代码简单、状态机好维护。

6. 每个文件完成后更新映射清单和任务进度。
7. 全部完成后进入 `verifying`，核对文件数和总大小。

#### 5.4.3 已存在文件处理

复制前检查目标：

- 不存在：正常下载。
- 存在且大小一致：标记 `skipped`，不重复下载。
- 存在但大小不同：重命名为 `.conflict-<timestamp>` 后重新下载，不静默覆盖。
- 存在且状态为 `done` 但用户手动改坏：由用户点击“重新校验”触发重下。

---

### 5.5 错误分类

新增 `classify_error(msg, context)`，返回：

```python
("FILE_FATAL", friendly_msg)
("ACCOUNT_FATAL", friendly_msg)
("DISK_FATAL", friendly_msg)
("TRANSIENT", raw_msg)
("UNKNOWN", raw_msg)
```

#### 5.5.1 文件级永久错误

关键字：

```text
file name too long
filename too long
name too long
path too long
file name or extension is too long
文件名或扩展名太长
路径太长
invalid argument            # 仅在上下文包含路径/name 时才算文件级
```

处理：

- 单文件任务：整个任务进入 `error`。
- 目录任务：该文件标记 `failed`，继续下一个。

#### 5.5.2 账号级永久错误

沿用现有 `FATAL_JOB_ERRORS`：

```text
daily_download_size_exceeded
download_size_exceeded
storage_quota_exceeded
captcha
unauthorized
invalid_grant
invalid_token
no username
account_not_found
```

处理：整个任务进入 `paused` 或 `error`，停止所有 worker，提示用户处理。

#### 5.5.3 瞬时错误

关键字：

```text
timeout
timed out
tls handshake
eof
i/o timeout
connection reset
connection refused
temporarily unavailable
```

处理：指数退避重试，最多 3 次，间隔 2s / 8s / 30s。

#### 5.5.4 磁盘/权限错误

关键字：

```text
no space left
permission denied
access denied
read-only file system
disk full
```

处理：整个任务停止，不做文件级重试。

#### 5.5.5 未知错误

重试 3 次后仍失败：

- 目录任务：标记该文件 `failed`，继续。
- 单文件任务：任务进入 `error`。

#### 5.5.6 卡住看门狗

新增后台检查：

```python
if job.state == "running" and job.speed == 0 and job.bytes == 0:
    if now - job.last_progress_at > stuck_watchdog_min * 60:
        job["state"] = "error"
        job["error"] = "任务长时间无进度，已自动终止（疑似卡住）"
```

默认 `stuck_watchdog_min = 10`，高级模式可配置。

注意：只在 v2 任务上启用，避免影响旧任务。

---

### 5.6 前端改动

涉及 `static/app.js` 和可能的 `static/index.html`。

1. 任务列表中 `curfile` 显示原名。
2. 文件详情中增加两列：

   ```text
   原始文件名：很长很长的原名.mp4
   落盘文件名：很长很长的原名_~9f3c21a8.mp4
   ```

3. 失败文件区域支持：

   - 查看失败原因。
   - 复制原名。
   - 复制落盘路径。
   - “仅重试失败项”。

4. 进度显示改为：

   ```text
   已完成 812 / 1000 个文件（81.2%）
   已下载 123.4 GB / 150.1 GB
   ```

5. 对 `namingPolicy: v2` 的任务显示小标记“安全命名”。

---

### 5.7 配置项

在设置中新增，高级模式可见：

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `safeNaming` | `true` | 新任务是否启用安全命名 |
| `safeNameMaxBytes` | `180` | 单个文件名最大 UTF-8 字节数 |
| `safeNameHashLen` | `12` | 哈希后缀长度 |
| `safePathMaxBytes` | `240` | 相对路径总字节上限 |
| `stuckWatchdogMin` | `10` | 无进度自动终止阈值（分钟） |
| `fileRetryMax` | `3` | 单文件瞬时错误重试次数 |

兼容策略：

- 升级后 `safeNaming` 默认开启，但**只作用于新任务**。
- 已经 `queued` 或 `running` 的旧任务保持旧逻辑，不加字段、不改行为。
- 旧任务完成后自然消失，不需要迁移。

---

## 6. 分阶段实施计划

### 阶段 1：止血 + 单文件安全命名

范围最小，风险最低，优先上线。

改动：

1. 在 `FATAL_JOB_ERRORS` 增加文件名过长类关键字。
2. 新增 `safe_basename()`、`truncate_utf8()`。
3. `start` 阶段为单文件任务生成 `safe_leaf`。
4. `launch_job()` 把 `dstRemote` 改为 `safe_leaf`。
5. 新增映射清单读写函数。
6. 前端详情显示原名和落盘名。
7. 更新 `CHANGELOG.md`。

验收：

- 单个超长文件名任务能自动下载完成。
- 任务不会停在 `running` 不动。
- 映射清单里能查到原名和落盘名。

### 阶段 2：目录任务逐文件复制

改动：

1. 新增 `expand_source_files()` 列目录。
2. 新增 `build_name_map()` 一次性生成全部安全路径。
3. 新增 `transfer_one()` 逐文件复制。
4. 用线程池替换目录任务的 `sync/copy` 启动方式。
5. 文件级状态、任务级状态分离。
6. 已存在文件处理逻辑。

验收：

- 目录里混入 100 个超长文件名，全部自动下载。
- 其中一个文件失败，其余 99 个继续。
- 中途杀进程，重启后不会重复下载已完成文件。

### 阶段 3：失败管理与前端增强

改动：

1. 失败文件列表。
2. “仅重试失败项”。
3. 卡住看门狗。
4. 错误分类细化。
5. 高级配置项。

验收：

- token 失效时任务暂停并提示重新登录。
- 文件名错误只影响单个文件。
- 长时间 0 字节任务会自动终止并给出原因。

### 阶段 4：测试与稳定性

改动：

1. 单元测试。
2. 集成测试。
3. 24 小时压力测试。
4. 文档和发布说明。

验收：见第 8 节。

---

## 7. 受影响文件清单

| 文件 | 改动内容 |
|---|---|
| `download-console.py` | 安全命名函数、映射清单、错误分类、单文件与目录任务改造、状态机 |
| `static/app.js` | 显示原名、落盘名、失败文件列表、重试失败项 |
| `static/index.html` | 若需要新增配置项或详情区域 |
| `CHANGELOG.md` | 记录新版本修复 |
| `docs/LONG-FILENAME-DOWNLOAD-DESIGN.md` | 本文档 |
| 测试文件 | 新增命名与安全映射相关测试 |

---

## 8. 测试计划

### 8.1 单元测试

- UTF-8 按字节截断，不截出半个汉字。
- Windows 非法字符替换。
- 结尾空格和句点处理。
- 保留设备名处理。
- `A.mp4` 与 `a.mp4` 冲突处理。
- 不同目录相同文件名生成不同哈希。
- 同一源文件重复调用生成相同安全名。
- 超长扩展名截断。
- 映射清单原子写和读取。

### 8.2 集成测试

- 1000 个超长中文文件名连续下载。
- 单文件长名下载。
- 目录中夹杂长名文件。
- 下载中途杀进程，重启续传。
- 目标文件已存在且大小一致。
- 目标文件已存在但大小不同。
- token 失效。
- 磁盘空间不足。
- 模拟 `file name too long` 错误。

### 8.3 稳定性测试

- 10,000 文件列表扫描内存占用。
- 24 小时连续下载。
- 映射清单增长到 5 万条时的读写耗时。
- 多任务并发写映射清单。

---

## 9. 验收标准

上线后必须满足：

1. 长文件名文件**自动下载完成**，无需人工改名。
2. 前端始终显示原始文件名。
3. 映射清单能追溯每个文件的原名和落盘名。
4. 不会因为单个长文件名导致任务永久停在 `running`。
5. 目录任务中一个文件失败不影响其他文件。
6. 重启、升级后映射不变，不重复下载已完成文件。
7. 关闭 `safeNaming` 后新任务恢复旧行为，旧数据不受影响。
8. 24 小时压力测试后无内存持续增长、无清单损坏。

---

## 10. 风险与规避

| 风险 | 规避方式 |
|---|---|
| 映射清单写坏 | 临时文件 + `os.replace` 原子写 |
| 并发任务写冲突 | 全局锁 + 单进程内串行写 |
| 哈希后缀极低频碰撞 | 生成阶段检测冲突并追加序号 |
| 目录文件过多导致列目录慢 | 分页/流式列出，先写清单再开始传输 |
| 映射清单过大 | 超过 5 万条切 SQLite |
| 旧任务行为被破坏 | v2 字段隔离，旧任务不启用新逻辑 |
| 用户看不懂落盘名 | 前端显示原名，详情里再展示落盘名 |
| Windows 路径仍超长 | 限制相对路径总字节数，并建议开启系统长路径支持作为补充 |

---

## 11. 回滚方案

1. 设置里把 `safeNaming` 关掉。
2. 新任务恢复旧命名逻辑。
3. 已经完成的 v2 任务不回滚文件名，不批量重命名。
4. 映射清单独留，不影响旧逻辑读取。
5. 如需彻底回退代码，撤销对应提交即可，历史文件仍可正常访问。

---

## 12. 后续优化

- 映射清单从 JSON 迁移到 SQLite，支持更大规模。
- 增加“原名搜索”功能，方便用户从落盘名找回源文件。
- 增加文件校验开关，完成后按 size + mtime 或 hash 校验。
- 下载完成后自动生成一份 CSV 清单，便于 NAS 侧检索。
- 对已有历史目录做只读扫描，报告潜在超长名风险，但不自动改名。

---

## 13. 结论

当前问题的本质不是“文件名太长”本身，而是：

1. 下载链路没有文件名安全映射；
2. 文件系统错误没有被正确分类；
3. 任务状态无法从 `running` 脱离。

本方案通过“**原始名用于源端、安全名用于落盘、清单绑定两者、错误分级隔离**”四点，把长文件名从偶发卡死变成可自动处理、可追溯、可重试的普通事件，满足下载机长期无人值守运行的要求。

---

## 14. 实施记录（v3.83，2026-11-20）

阶段 1～3 已落地，测试见 `tests/test_safe_naming.py`（56 项全绿），变更说明见 `CHANGELOG.md` v3.83。
与上文设计稿的差异如下，均以「少动主干、行为可预期」为准：

1. **代码位置**：按仓库单文件约定，命名 / 清单 / 错误分类都放进 `download-console.py`
   （`truncate_utf8` 至 `classify_error` 一段 + 目录任务 worker 一段），没有拆 `safe_naming.py`。
2. **文件级状态的存放**：目录任务的执行计划放在数据目录 `jobfiles/<任务id>.json`，
   避免把几千条明细塞回 `jobs.json`（它每 2 秒被前端轮询一次）；每卷的
   `.cloudshuttle-name-map.json` 仍按 5.2 节记录原名 ↔ 落盘名与状态，用于跨任务追溯。
   删除任务记录 / 一键清理时计划文件一并删除，两个路径都已加入 `.gitignore`。
3. **设备名处理**：`CON.mp4` 这类只加 `_` 前缀，不再追加哈希（5.1.3 的 `need_hash` 原文
   `original != str(original)` 恒为 False，已按意图重写为「清洗有改动或超长才挂哈希」）。
4. **路径总长度兜底**：不直接对整条相对路径做字节截断（那会切掉文件名和扩展名），
   改为先压目录段、再压文件主名、最后折叠中间层为哈希目录。
5. **已存在文件检查**：目标目录在扫描阶段一次性列出做快照，而不是逐文件 `stat` ——
   本项目的 `rc()` 每次都是一次 `docker exec`，逐文件 stat 会把开销放大三倍。
6. **部分失败的终态**：有失败但有完成 → `done` + 提示「可仅重试失败项」；
   全部失败 → `error`。任务运行期间单个文件失败不影响其它文件（与 5.3.2 一致）。
7. **看门狗防误杀**：在文档条件（无速度、字节不涨）之上再加「没有在传文件」，
   大文件单线程复制可能十几分钟不涨字节，但 `files > 0`，不会被终止。
8. **`invalid argument` 判定**：上下文含路径分隔符也算「包含路径」，不只匹配 `name/path/file` 字样。
9. **设置项位置**：`safeNaming` 作为回滚总开关在设置页常显，其余 5 项放在高级参数区。
10. **未做**：阶段 4 的真机集成、24 小时压测、5 万条清单规模测试；SQLite 迁移与
    「原名搜索」等第 12 节后续优化也未做。上线前建议先做一次含超长名目录的真机闭环。
