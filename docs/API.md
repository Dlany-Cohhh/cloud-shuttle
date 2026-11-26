# PikPak 多账号下载器 · API 速查

所有接口都是 `POST /api`，请求体为 JSON，用 `action` 字段区分操作：

```bash
curl -s -X POST http://<控制台地址>/api \
  -H 'Content-Type: application/json' \
  -d '{"action":"settings"}'
```

返回体统一为 JSON；失败时带 `error` 字段。多数操作返回 `{"ok": true}` 或对应的数据字段。

> 本项目是**内网自用的单页控制台**，接口没有鉴权，也没有做 CSRF 防护。
> 请不要把 5566 端口暴露到公网，详见 [SECURITY.md](../SECURITY.md)。

## 常用操作

| action | 说明 |
| --- | --- |
| `settings` / `savesettings` | 读取 / 保存控制台设置 |
| `accounts` / `accttest` / `acctpurge` | 账号列表 / 单账号测试 / 清理无 type 的残留配置段 |
| `jobs` / `start` / `stop` / `resume` / `remove` | 任务列表 / 启动 / 停止 / 断点续传 / 删除 |
| `jobretryfailed` | v3.83：仅重试目录任务里失败的文件，已完成的不会重复下载 |
| `dirs` / `localdirs` / `scanpath` | 云盘与本地目录浏览 |
| `localfiles` / `localrenamebatch` | 本地文件浏览 / NAS 侧重命名（批量与单项共用，items 传 1 项即单项） |
| `renamebatch` | 云盘目录内批量重命名 |
| `autotune` / `autotuneinfo` | v3.94：发起自动测速调优（异步）/ 查进度与历史成绩 |
| `tput` | v3.94：量一次目标目录真实吞吐（MB/s），不改动任务 |
| `version` | 版本与运行状态 |

## 全部 action（由源码自动提取，共 87 个）

- `about`
- `accounts`
- `acctadd`
- `acctdel`
- `acctedit`
- `acctget`
- `acctident`
- `acctimport`
- `acctimporttext`
- `acctpurge`
- `acctrename`
- `acctrepair`
- `accttest`
- `bwlimit`
- `cleanup`
- `clearfinished`
- `cleartransfers`
- `cloudlink`
- `cloudmkdir`
- `cloudmove`
- `cloudmv`
- `cloudrm`
- `cloudrmbatch`
- `credcheck`
- `dirs`
- `dirsize`
- `folderack`
- `foldercovers`
- `folderstatus`
- `health`
- `history`
- `jobdetail`
- `jobreproxy`
- `jobretryfailed`
- `jobs`
- `localdirs`
- `localfiles`
- `localmkdir`
- `localmove`
- `localrenamebatch`
- `localsize`
- `localtrash`
- `localtrashlist`
- `localtrashpurge`
- `localtrashrestore`
- `localunzip`
- `myshares`
- `playinfo`
- `providers`
- `proxycheck`
- `quota`
- `remotes`
- `remove`
- `renamebatch`
- `resetsettings`
- `resume`
- `savesettings`
- `scanpath`
- `setbwlimit`
- `settings`
- `sharecreate`
- `sharedel`
- `sharelimit`
- `shareparse`
- `sharesave`
- `speedtest`
- `splitscan`
- `splitstart`
- `start`
- `stats`
- `stop`
- `transfers`
- `trashlist`
- `trashop`
- `unzip`
- `unzipinfo`
- `updateapply`
- `updatecheck`
- `updaterollback`
- `updatestate`
- `urldl`
- `verify`
- `version`
- `xferstart`

各 action 的完整参数、返回字段与调用位置见 `download-console.py` 的 `api()`：

```bash
grep -n 'action ==' download-console.py
```

### 存储空间（v3.57+）

后端用 `docker inspect` 把控制台与 `rclone-backend` 的挂载按宿主 Source 配对，自动识别全部下载卷；`docker.sock` 不可用时回退内置约定（`/data` ↔ `/vol2/download`）。

| 接口 | 字段 | 说明 |
| --- | --- | --- |
| `settings` / `jobs` | `state.storages` | 识别出的存储空间数组：`name`（存储空间 N）、`rclone`（rclone 侧根）、`console`（控制台挂载点）、`totalGB` / `freeGB` / `usedPct` / `low` |
| `settings` / `jobs` | `state.diskFree` / `diskTotal` | 兼容字段：最紧卷的剩余与总量（GB） |
| `savesettings` | `diskQuotaGB` | 每个存储空间的容量上限（GB，0 = 跟随实际容量），配额剩余不足即触发磁盘保护 |
| `localdirs` | `volumes` | 按空间分组的已有目录：`root`（rclone 侧根）/ `console` / `name` / `dirs`；顶层 `dirs` 仍是主卷平铺列表（兼容旧前端） |
| `localfiles` | 空路径 | 顶层返回存储空间列表：`dirs` 每项带 `volumeRoot` / `freeGB` / `totalGB`，`volumes` 为完整卷信息；卷内路径为控制台绝对路径 |
| `start` / `urldl` | `volume` | 可选，传 `localdirs.volumes[].root`，缺省 = 主卷（`/data` 对应卷） |
| `savesettings` | `autoTune` / `autoTuneSeconds` / `autoTuneSettle` | v3.94 自动测速调优：总开关（默认开）、每档测速窗口（15–300 秒，默认 45）、每档稳定期（5–120 秒，默认 20） |
| `autotune` | `id` / `seconds` / `settle` | v3.94：对指定目录任务逐档试并发，结束后写回最快档并继续下载；异步，用 `autotuneinfo` 轮询 |
| `autotuneinfo` | — | v3.94：`best`（最优档）、`history`（最近 5 次成绩）、`run`（进行中状态）、`maxConns`（连接上限 16） |
| `tput` | `id` / `seconds` | v3.94：目标目录真实落盘吞吐（MB/s），口径与测速一致 |
| `savesettings` | `stallSeconds` | v3.95 龟速让位：单文件连续低于 50 KB/s 满该秒数就让位（30–600，默认 90）；让位过的文件第二轮只占 1 个并发槽，尾池 30 分钟推不动 1 MB 判失败 |
| `savesettings` | `pushplusOn` / `pushplusToken` | v4.10 PushPlus 微信通知：开关（默认关）+ 16~64 位字母数字 token（非法值会被清空，避免每 5 秒报一次错） |
| `savesettings` | `autoRetryOn` / `autoRetryMin` / `autoRetryMax` | v4.13 失败任务自动重试：开关（默认关）、失败后等待分钟数（1~1440，默认 10）、最多重试次数（0~10，默认 3，0 = 只记录） |

自动重试的失败分类（v4.14）：普通失败直接重试；**账号失效 / 需重新登录 / 人机验证**先跑 `pikpak_verify(账号)`
（自动换 device_id、同步被轮换的凭证）修好账号再继续，修不好计一次尝试等下一轮；
**额度用尽**与**磁盘保护 / 空间不足**不重试（等多久都没用）。
| `pushplustest` | `token`（可省，省略则用已保存的） | v4.10：发一条测试微信，返回 `{ok,msg}` 或 `{error}`；不依赖保存，改完 token 可先测 |
| `notifystate` | — | v4.10：`{on, tokenSet, tokenTail(末 4 位), sentCount}`，用于页面显示状态，不返回 token 原文 |

v4.10 的通知规则：巡检线程每 5 秒扫一次 `jobs.json`，任务进入终态且状态跃迁时发一条 ——
`done` = 下载完成；`error` = 失败；**带 `error` 的 `stopped`**（额度用尽 / 卡住看门狗 / 低速重连用尽等故障停止）= 失败。
用户自己点「停止」的 `stopped`（无 error）不发；v4.11 起，错误文案以「控制台重启过」或「守护进程重启过」开头的
`stopped`（我们重启容器/守护进程导致的中断）也不发，否则每次升级都会收到一串「任务失败」。
去重表（v4.19）换成 `{"v":2,"jobs":{"<id>":{"st","sig","ts","nts"}}}`——**按任务记住上次见到的状态与错因，只有真变了才发**：

- 非终态（`running` / `queued` / `pending`…）只更新记录，不发
- 完成通知只认「非 `done` → `done`」这一次跃迁；已在 `done` 后再清空/改写错因文案（如「N 个文件失败」被清掉）一律不发
- 失败通知保留「同一任务 30 分钟内最多一条」兜底（`NOTIFY_REPEAT_SEC=1800`，只比 `nts`＝上次真正发出去的时间），
  自动重试 / 看门狗来回跑时不刷屏；`done → running → done`（真的又下一轮）会照常再发一次
- 一轮巡检最多 3 条，发送失败按 60→300 秒指数退避（退避期间不登记状态，结束后补发，不丢通知）
- **v4.20**：同一条事件最多尝试 `NOTIFY_TRY_MAX=6` 次（约 20 分钟），仍失败就登记状态并放弃补发
  （理由：网络/限流抖动时无限重试，最后会把二十分钟前的「下载完成」推到手机上，还把 PushPlus 打到限流）
- 清理去重表时**保留仍在任务列表里的条目**，避免「被删掉 → 下次当成新事件重推」
- 老格式（v4.15 的 `id|state|err → ts` 平表）启动时自动迁移：只登记现状、不补发历史，升级瞬间不会又炸一串

> v4.15 的旧键 `id|state|error[:60]` 配 30 分钟冷却是**平事件触发**：任务停在 `done` 时状态永不变，
> 冷却一过就被当成新事件，于是每半小时重播一条「下载完成」（2026-11-26 现场：11:09 连推 4 条昨天的任务）。

v4.15 的孤儿传输治理：逐文件异步 job 都带 `_group=cloudshuttle/<任务id>`，任务进入终态
（`bail()` / `_JobAbort` / worker 收尾）、点停止、删记录时按组 `job/stopgroup` 一次全停；
另有 `orphan_guard_loop()` 每 60 秒把没有在跑任务认领的 `cloudshuttle/*` 组清掉。
`stats` 在没有任何 running/queued 任务时速度强制返回 0（否则残留 job 会让累计字节继续涨，曲线假装还在下载）。

正文里的「文件」一行按可用数据渲染（v4.11）：目录任务 `5/7 个（失败 2）`；只有已完成数时 `3 个`；
单文件任务且成功显示 `1 个`；rclone 老任务既无总数也无完成数时整行不显示，不再出现 `0/0`。

磁盘空间保护按任务的目标卷判断（`start` / `resume` 的报错会指明具体哪块空间不足）。

`dirsize`（v3.60+）：入参 `remote` + `path`（云端目录），返回 `{bytes, count}`——`rclone size` 递归统计，结果缓存 5 分钟；云盘页勾选文件夹时按需调用。

### 自定义代理地址（v3.55+）

| 接口 | 字段 | 说明 |
| --- | --- | --- |
| `settings` / `jobs` | `effectiveProxyUrl` | 当前实际生效的代理地址；未启用代理时为「直连（未启用代理）」 |
| `savesettings` | `proxyUrl` | 自定义地址（http(s)/socks5，或 `off` 关闭），空串 = 用 `PROXY_URL` / 默认值；非法值会被丢弃 |
| `savesettings` 返回 | `proxyNote` | 地址变化后 5573 守护进程的重启结果 / 生效时机 |

优先级：设置页 `proxyUrl` > 环境变量 `PROXY_URL` > 默认 `http://127.0.0.1:7890`。

### 在线更新（v3.54+）

| action | 参数 | 返回要点 |
| --- | --- | --- |
| `updatecheck` | `force`（可选，跳过 10 分钟缓存） | `current` / `latest` / `hasUpdate` / `name` / `notes` / `publishedAt` / `htmlUrl` / `assetSize` / `checkedAt` |
| `updateapply` | 无（需先成功执行一次 `updatecheck`） | `ok` / `from` / `to` / `files` / `backup` / `restart` / `message` |
| `updatestate` | 无 | `current` / `state`（最近一次更新记录，含是否回滚） |
| `updaterollback` | 无 | `ok` / `message` |

更新包的格式、校验规则与发版流程见 [UPDATE.md](UPDATE.md)。

### 文件夹下载状态与 CRUD（v3.81+）

| action | 参数 | 返回要点 |
| --- | --- | --- |
| `folderstatus` | `remote`, `path`, `folders`（`[{name,id}]`）, `refresh`（可选） | `status[name] = {state: ""|done|updated, added, total, hit, complete}`；`partial` 表示有文件夹没算完 |
| `folderack` | `remote`, `path`, `names` | `status[name]` 为确认后的状态；只推进基线，不伪造下载状态 |
| `cloudmkdir` / `localmkdir` | `path`（父目录）, `name`；云端另需 `remote` | `ok` / `error`；重名或名称非法直接拒绝 |
| `cloudmove` / `localmove` | `path`（源父目录）, `items`（名称数组）, `dest`（目标目录完整路径）；云端另需 `remote` | `ok` / `total` / `results[]`（逐项 `name`/`ok`/`error`） |
| `cloudrmbatch` | `remote`, `path`, `items`, `hard`（可选） | 批量删除，默认进 PikPak 回收站 |
| `localtrash` | `path`, `items` | 移到 `.cloud-shuttle-trash`，可恢复 |
| `localtrashlist` | 无 | `items[] = {id,name,from,ts,isdir,exists}` |
| `localtrashrestore` / `localtrashpurge` | `ids` 或 `all` | `ok` / `total` / `results[]` |

`state` 的三态：`""` = 不显示任何标识；`done` = 全部下载完毕；`updated` = 完毕后云端有新文件
（需用户点徽标确认才消失）。判定口径是「云端文件夹内文件逐项在 NAS 上找同名同大小」，
但打标时会把 `{云端签名: 本地绝对路径}` 记进基线 `map`，之后优先按路径判定 ——
所以本地改名 / 移动不会掉标，只有文件真的不在了才掉标；云端改名按「净增」计算，
不会误报「有更新」。详见 [CHANGELOG.md](../CHANGELOG.md) v3.81 一节。

### 创建分享（v3.82+）

| action | 参数 | 返回要点 |
| --- | --- | --- |
| `sharecreate` | `remote`, `items`（`[{name,id}]`，`id` 取 `dirs` 里的 `id`）, `days`（`-1/1/7/30`，默认 7）, `passCode`（4~10 位，留空自动生成）, `limit`（提取次数上限，默认 100） | `ok` / `shareId` / `url` / `passCode` / `text`（链接 + 提取码，可直接转发）/ `count` / `names` |

多个 `items` 合并成**一条**分享，文件夹连同内容一起分享。仅 PikPak 账号可用。
这是写操作，**默认不做自动换路径重试**（避免网络抖动重复建分享）；唯一例外是服务端
明确返回地区限制（`Share Unavailable in Your Region` / `PROHIBITED`）——这类请求没有
产生任何分享，会自动改用预置代理地址重试一次，并在结果里带出 `via` / `retried`。
失败时返回可读中文错误。
生成的是官方加密链接，需要提取码；可在「我的分享」里查看提取次数、设上限或取消。

注意与 `cloudlink` 区分：`cloudlink` 走 `rclone link`，返回的是**临时下载直链**，
不是可转存的分享链接。

### 长文件名安全命名（v3.83+）

新任务会带 `naming: "v2"` 标记；老任务没有该字段，行为与 v3.82 完全一致。

| 接口 / 字段 | 说明 |
| --- | --- |
| `savesettings` | 新增 `safeNaming`（总开关，默认开）、`safeNameMaxBytes`（180）、`safeNameHashLen`（12）、`safePathMaxBytes`（240）、`stuckWatchdogMin`（10，0 = 关闭）、`fileRetryMax`（3） |
| `start` 返回的 job | `naming`（`v2`）；单文件任务另有 `leaf`（原始文件名）与 `safe_leaf`（安全落盘名） |
| `jobs` 中的 v2 目录任务 | `filesTotal` / `filesDone` / `filesFailed` 文件级计数；`phase` 为 `scan` / `copy` / `check`；`curfile` 始终是**原始**相对路径 |
| `jobdetail` | 新增 `naming` / `originalName` / `safeName`；目录任务另有 `fileStats{total,done,failed,pending}`、`failedFiles[]`（`src` 原路径 / `dst` 落盘路径 / `error` / `attempts`，最多 200 条）、`nameMap[]`（被改名文件的对照表） |
| `jobretryfailed` | 入参 `id`；把计划里 `failed` 的文件重置为待下载并重新排队，返回 `{ok, count}`。任务在跑 / 排队时拒绝 |

落盘映射写在每个下载卷根目录的 `.cloudshuttle-name-map.json`（v2，原子写）；
每个目录任务的文件级执行计划在数据目录 `jobfiles/<任务id>.json`，删除任务记录时一并清理。
设计细节见 [LONG-FILENAME-DOWNLOAD-DESIGN.md](LONG-FILENAME-DOWNLOAD-DESIGN.md)。

### 分账号下载（v4.01+）

同一份资源转存到多个账号之后，把目录里的文件按**字节均衡**拆给各账号并行下载，
每个账号一条独立任务、共用同一个落盘目录。

| 接口 | 入参 | 说明 |
| --- | --- | --- |
| `splitscan` | `path`、`remotes[]`、`filter`、`proxy` | 只读预检：逐账号列目录，返回 `accounts[]`（`state` / `listed` / `files` / `bytes` / `missing` / `extra` / `identical` / `error`）、`consistent`、`fileCount`、`bytes`、`commonCount`、`missingDir`、`mismatched`、`assign[]`、`warnings[]` |
| `splitstart` | `path`、`remotes[]`、`folder`、`volume`、`filter`、`proxy`、`force` | 建 N 条分片任务；`consistent=false` 时直接拒绝 |

- `accounts[].listed` 表示这次到底有没有列到这个目录（v4.02+）。`listed=false` 的账号
  `missing` 等于整份资源的文件数，而不是 0 —— 「看不到目录」和「一个都不缺」是两件事。
- `fileCount` / `bytes` 用**并集**口径（这份资源一共多大），`commonCount` 才是所有账号交集的大小；
  不一致时前者能说清规模，后者恒为 0 级别，不适合当摘要。
- `missingDir` / `mismatched` 分别是有账号看不到目录、能看到但集合不同的账号数，
  界面据此把结论说成「N 个账号看不到该目录」还是「文件集合不一致」。

- `splitscan.consistent` 只有在**所有账号的文件集合（路径 + 大小）完全一致**时才为 `true`，
  否则 `splitstart` 会拒绝执行：文件集合不一致意味着「分给 A 的文件 B 没有」，任务必然失败。
- 返回的每个 job 额外带 `split: {group, index, of, files, bytes}` 与 `fileList[]`（该账号分到的相对路径）。
  `fileList` 是**互斥**的：`expand_source_files` 只认清单里的文件。
- 过滤规则在拆分**之前**由后端用 Python 算一遍（`filter`），分片任务不再向 rclone 注入 `_filter`，
  避免两套匹配语义（大小写敏感度不同）导致「分到的文件悄悄不下」。
- 落点由后端统一计算一次，**不走 `dedupe_dest`**：分片必须落同一个目录，否则会被改成「目录-2」。
- 分片任务不受 `maxConcurrent` 限制（同源分片排队就失去了并行意义），每条任务仍是独立账号、独立连接预算。

