# 在他人的 NAS / 主机上部署

> 给拿到本仓库、想在自己设备上跑一份的人。**代理是可选增强，不装也能完整使用**；代码里没有硬编码任何 NAS IP、账号或下载目录。

## 先说结论

- 「下载任务走代理」是通用功能，支持任意 `http://` / `https://` / `socks5://` 代理地址，**不要求安装 mihomo**。Clash、sing-box、旁路由、公司网关都行。
- 完全没有代理也能用：部署时加 `--no-proxy`，或在设置页把代理地址填 `off`、关闭下载开关，所有任务走主守护进程（5572）直连。
- 默认地址 `http://127.0.0.1:7890` **只在「Linux + host 网络 + 宿主机跑着 7890 端口代理」时成立**。官方 compose 用 host 网络（因飞牛 fnOS 的 bridge 端口映射不可达），换环境通常要改。

## 部署前必改清单

| 项 | 在哪改 | 说明 |
|---|---|---|
| `UPDATE_REPO` | `.env` / compose environment | **默认指向原作者仓库**。不改就点「检查更新」会拉到原作者的 Release；改成自己的 `用户名/仓库`，或删掉这项关闭在线升级 |
| `PROXY_URL` | `.env` / `--proxy` / 设置页 | 见下表；不需要代理就填 `off` |
| `RCLONE_RC_USER` / `RCLONE_RC_PASS` | `.env` | 必填，改成自己的随机长字符串；一键部署脚本会自动生成 |
| `PROJECT_ROOT` / `DOWNLOAD_ROOT` / `RCLONE_CONFIG_DIR` | `.env` | 代码目录、下载落盘目录、rclone 配置目录 |
| `rclone.conf` | `RCLONE_CONFIG_DIR` | 放自己的账号配置；**含 refresh_token，绝不要提交进仓库** |
| `/var/run/docker.sock` | compose volumes | 必须挂载，控制台靠 `docker exec` 指挥 rclone-backend、拉起代理守护进程 |
| `PORT` / `RC_PORT` / `PROXY_RC_PORT` | `.env`（可选） | 端口冲突或多套部署并存时才改，默认 5566 / 5572 / 5573 |

一键部署：

```bash
# Linux / macOS
bash scripts/deploy.sh --proxy http://192.168.1.10:7890 --downloads /mnt/nas/downloads
bash scripts/deploy.sh --no-proxy          # 完全不装代理

# Windows Docker Desktop：双击 scripts\deploy.bat，按提示填代理地址或选不走代理
```

## 代理地址该怎么填

| 你的环境 | 填什么 |
|---|---|
| Linux host 网络 + 宿主机 Clash / mihomo 开 7890 | 默认 `http://127.0.0.1:7890` |
| Windows Docker Desktop（bridge） | `http://host.docker.internal:7890`（容器里 `127.0.0.1` 指容器自己） |
| 独立代理机 / 旁路由 | `http://192.168.x.x:7890` 或 `socks5://192.168.x.x:1080` |
| 同一个 docker bridge 里的代理容器 | `http://容器名:7890`（官方 compose 是 host 网络，需自行加网络） |
| 群晖 / 威联通等 bridge + 端口映射 | 宿主机 IP，或该平台支持的 `host.docker.internal` |
| 不装代理 | `off` / `none` / `direct` / `disabled` / `no`，或关闭下载开关 |

优先级：**设置页「进阶设置 → 自定义代理地址」 > `.env` 的 `PROXY_URL` > 默认 `127.0.0.1:7890`**。设置页改完立即生效，不用重启容器。

## 三个硬性前提

1. **地址要从 `rclone-backend` 容器内部可达**。`HTTP(S)_PROXY` 是 `docker exec -e` 注入到那个容器里的，不是注入控制台；「宿主机能连」不等于「容器能连」。
2. **控制台容器必须挂 `/var/run/docker.sock`**，否则拉不起 5573 代理守护进程。
3. **`socks5://` 只对 rclone 下载生效**。控制台自身访问 PikPak 接口 / 封面 CDN 的 Python 请求只认 http(s) 代理，遇到 socks5 会自动直连兜底。

另外：**端口通 ≠ 代理可用**。设置页「测试连通性」有一项「代理 CONNECT + TLS」：先用 HTTP CONNECT 建隧道，再在隧道里做 TLS 握手并请求一次，只有端到端通过才算代理可用。仅「TCP 能连上」或仅「CONNECT 返回 200」都不代表代理可用（代理可能回 200 后立刻断开隧道）。

## 排查

| 现象 | 先查 |
|---|---|
| 代理任务连不上 / 分享链接失败 | `PROXY_URL` 在容器里不可达；Windows 用 `host.docker.internal`，Linux host 网络用 `127.0.0.1`；可先填 `off` 验证直连是否正常 |
| 端口能连但下载仍失败 | 看「代理 CONNECT 隧道」是否通过；不通说明代理客户端没正常工作 |
| 文件夹大小一直不显示 | 设置页「云盘数据与封面」选「直连」再用「测试连通性」确认；v3.73 起失败会自动重试一次 |
| 点「检查更新」拉到别人的版本 | `UPDATE_REPO` 没改 |
| 升级后页面还是旧界面 | 浏览器强刷（Ctrl+F5）；v3.58 起有版本变化提示 |

## 安全提醒

这是**内网自用的无鉴权工具**：Web 界面与 `/api` 都没有登录校验。请把 5566 端口限制在局域网内，不要直接暴露到公网；使用第三方网盘服务时自行遵守其服务条款。详见 [SECURITY.md](../SECURITY.md)。
