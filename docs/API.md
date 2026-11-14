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
| `dirs` / `localdirs` / `scanpath` | 云盘与本地目录浏览 |
| `localfiles` / `localrenamebatch` | 本地文件浏览 / NAS 侧重命名（批量与单项共用，items 传 1 项即单项） |
| `renamebatch` | 云盘目录内批量重命名 |
| `version` | 版本与运行状态 |

## 全部 action（由源码自动提取，共 80 个）

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
- `sharedel`
- `sharelimit`
- `shareparse`
- `sharesave`
- `speedtest`
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
（需用户点徽标确认才消失）。判定口径为「云端文件夹内文件逐项在 NAS 上找同名同大小」，
详见 [CHANGELOG.md](../CHANGELOG.md) v3.81 一节。

