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
| `localfiles` / `localrenamebatch` | 本地文件浏览 / NAS 侧批量重命名 |
| `renamebatch` | 云盘目录内批量重命名 |
| `version` | 版本与运行状态 |

## 全部 action（由源码自动提取，共 65 个）

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
- `cloudmv`
- `cloudrm`
- `credcheck`
- `dirs`
- `health`
- `history`
- `jobdetail`
- `jobreproxy`
- `jobs`
- `localdirs`
- `localfiles`
- `localrenamebatch`
- `localunzip`
- `myshares`
- `playinfo`
- `providers`
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

各 action 的完整参数、返回字段与调用位置见 `download-console.py` 的 `handle_api()`：

```bash
grep -n 'action ==' download-console.py
```

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
