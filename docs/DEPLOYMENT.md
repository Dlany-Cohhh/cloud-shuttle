# PikPak 多账号下载器 · 部署与运维

面向从零部署一份新的 PikPak 多账号下载器（云梭控制台）。文中所有路径、账号、密码都是占位符，请按自己的环境替换。

## 1. 端口与依赖

| 组件 | 端口 / 路径 | 说明 |
| --- | --- | --- |
| download-console | `:5566` | 同一端口按 TLS 首字节自动区分 HTTP / HTTPS |
| rclone-backend rcd（主） | `127.0.0.1:5572` | 直连下载，只绑回环 |
| rclone rcd（代理） | `127.0.0.1:5573` | 控制台按需拉起，走 `HTTP(S)_PROXY` |
| 代理（可选） | 任意 `http(s)://` / `socks5://` 地址 | 见第 4 节；Windows 常用 `http://host.docker.internal:7890` |
| 下载目录 | 宿主机同一个目录 | 同时挂到控制台 `/vol2/download` 与 rclone `/data` |

示例 compose 使用 **host 网络**（某些 NAS 系统的 bridge 端口映射不可达）。普通 Docker 环境可以改成 `ports: - "5566:5566"`，但控制台仍然需要 `/var/run/docker.sock` 才能指挥 rclone 容器。

控制台镜像只装了 `docker-cli`，源码通过 volume 挂到 `/app`；因此**改动前端刷新浏览器即可，改动 Python 需要重启 `download-console` 容器**。

## 2. 准备 rclone.conf

在 `RCLONE_CONFIG_DIR` 指向的目录里放 `rclone.conf`，权限建议 `600`：

```ini
[pik-1]
type = pikpak
username = you@example.com
password = <用 rclone obscure <你的密码> 生成>
refresh_token = os.<你的 refresh_token>
token = {"access_token":"<占位>","token_type":"Bearer","refresh_token":"os.<占位>","expiry":"2026-01-01T00:00:00Z"}
```

最快的方式其实不是手写：启动控制台后进「账号」页，用「导入 / 采集 Token」粘贴 `账号名 + refresh_token`，或用「重新登录」下载现场生成的 `login-collector.ps1` 在 Windows 上跑一次。控制台会自己把配置写进 remote。

注意：

- 只有带 `type` 的段才是真正的 remote；只有 `captcha_token`、没有 `type` 的是残留空壳，rclone 无法实例化（见第 8 节）。
- 用 `docker exec rclone-backend rclone listremotes` 可以快速确认哪些账号真的可用。
- 多个 remote **不要共用同一个 refresh_token**：`refresh_token` 单次轮换，共用会互相顶掉导致批量失效。

## 3. 配置并启动

### 3.1 一键部署（推荐）

```bash
# Linux / macOS / NAS
git clone https://github.com/Dlany-Cohhh/cloud-shuttle.git && cd cloud-shuttle
bash scripts/deploy.sh
```

Windows Docker Desktop 双击 `scripts\deploy.bat`（或 `powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1`）。脚本会问下载目录、rclone 配置目录与**代理地址**（可填任意 `http(s)://` / `socks5://` 地址，或 `off` 关闭），自动生成随机 RC 凭据与 `.env`、创建空的 `rclone.conf`、构建启动并做健康检查。

| 常用参数 | Linux | Windows |
| --- | --- | --- |
| 非交互 | `--yes` | `-Yes` |
| 自定义代理 | `--proxy http://192.168.1.10:7890` | `-Proxy http://192.168.1.10:7890` |
| 不走代理 | `--no-proxy` | `-NoProxy` |
| 只生成配置 | `--dry-run` | `-DryRun` |
| 查看 / 跟随日志 / 停止 / 重启 | `bash scripts/deploy.sh status\|logs\|stop\|restart` | `powershell -File scripts\deploy.ps1 -Command status\|logs\|stop\|restart` |

Linux 用 `docker-compose.example.yml`（host 网络，固定 5566）；Windows 自动叠加 `docker-compose.windows.yml`（bridge + `${WEB_PORT:-5566}:5566` 映射），所以 Windows 可以换对外端口。

### 3.2 手动配置

```bash
cp .env.example .env
# 修改：PROJECT_ROOT / DOWNLOAD_ROOT / RCLONE_CONFIG_DIR
# 修改：RCLONE_RC_USER / RCLONE_RC_PASS（自己生成随机值）
# 修改：PROXY_URL（自定义代理地址，或填 off 关闭）

docker compose -f docker-compose.example.yml up -d --build
# Windows 额外加： -f docker-compose.windows.yml
docker compose -f docker-compose.example.yml logs -f download-console
# 看到「PikPak 多账号下载器（云梭 CloudShuttle）v3.56」启动横幅即正常

curl -s -X POST http://127.0.0.1:5566/api \
  -H 'Content-Type: application/json' -d '{"action":"version"}'
```

浏览器打开 `http://<NAS 地址>:5566/`（Windows bridge 模式用 `-Port` 指定的端口），按 **Ctrl+F5** 强制刷新一次（静态资源有浏览器缓存）。

RC 凭据必须两边一致：控制台容器的 `RCLONE_RC_USER/PASS` 环境变量 = `rclone-backend` 的 `--rc-user/--rc-pass`。不一致时所有任务会静默认证失败。

如果控制台挂在反向代理后面，且希望「重新登录」下载的采集脚本回推到固定域名，可给控制台加 `CONSOLE_PUBLIC_HOST=你的域名:端口`；不设置时按浏览器请求的 `Host` 头自动判断。

## 4. 可选服务

### 4.0 多存储空间 / 自定义配额（v3.57）

控制台启动时用 `docker inspect` 把两个容器的挂载按宿主 Source 配对，**挂了几个下载目录就自动识别几个存储空间**，概览页逐个显示真实容量（不再有写死的 926.4 GB）。想用第二个存储空间，只需在两个服务里把另一个宿主目录挂进容器（目标路径随意，两边挂同一个宿主目录即可）：

```yaml
services:
  download-console:
    volumes:
      - ${DOWNLOAD_ROOT}:/vol2/download      # 主卷（历史约定，rclone 侧对应 /data）
      - ${DOWNLOAD_ROOT2}:/mnt/disk2         # 第二个存储空间
  rclone-backend:
    volumes:
      - ${DOWNLOAD_ROOT}:/data
      - ${DOWNLOAD_ROOT2}:/mnt/disk2         # 与控制台同源挂载 → 自动识别
```

- 「下载」与「URL 下载」弹窗会出现「目标存储空间」下拉（多卷时）；`start` / `urldl` 的 `volume` 参数见 `docs/API.md`。
- 「本地文件」页顶层先列存储空间，点入后按绝对路径浏览；解压 / 改名 / 校验 / 清理跟随所在卷。
- 磁盘空间保护按卷判断：哪个空间不足只暂停目标在该空间的任务。

**自定义配额**：「设置 → 磁盘空间保护 → 存储空间配额」（`diskQuotaGB`）给每个存储空间设容量上限（GB，`0` = 跟随实际容量）。配额按整卷已用计算（同一卷上其他程序的数据也计入），配额剩余不足即触发磁盘保护；它只约束显示与任务调度，写盘动作仍有物理剩余空间检查兜底，不会写爆磁盘。

### 4.1 自定义代理地址（v3.55）

代理下载由独立的 rclone 守护进程（127.0.0.1:5573）承担，它启动时注入 `HTTP_PROXY` / `HTTPS_PROXY`。地址有三个来源，**优先级从高到低**：

1. 页面「设置 → 代理下载 → 自定义代理地址」（写进 `settings.json` 的 `proxyUrl`，保存后立即生效，不用重启控制台）；
2. 部署时 `.env` 的 `PROXY_URL`（由一键部署脚本写入，并作为环境变量传进 `download-console`）；
3. 内置默认 `http://127.0.0.1:7890`。

允许填 `http://`、`https://`、`socks5://`、`socks5h://` 的任意地址；填 `off` / `none` / `direct` 表示**完全不走代理**。注意 `socks5` 只对 rclone 下载生效，控制台自身访问 PikPak 接口的 Python 请求只支持 http(s) 代理，遇到 socks5 会自动走直连兜底。

- 宿主机上的 Clash / mihomo / 旁路由：Linux 填 `http://127.0.0.1:7890`（host 网络），Windows Docker Desktop 填 `http://host.docker.internal:7890`（容器在 Linux VM 里，`127.0.0.1` 指的是容器自己）。
- 独立代理机 / 网关：直接填 `http://192.168.x.x:7890` 或 `socks5://192.168.x.x:1080`。
- 换地址后，若 5573 上还有任务在传输，控制台会等任务结束再按新地址重启守护进程；设置页会返回一条 `proxyNote` 说明生效时机。
- 不需要在本项目里跑 mihomo 容器；下面只是可选片段。

### 4.2 其它可选服务

把下面片段按需加进 compose（密码用 `.env` 变量，不要写死）。启用后控制台「下载任务」页会多出对应入口，账号页也能看到 `openlist` 这类 WebDAV remote。`PROXY_URL` 可以指向任意代理，不一定要跑下面的 mihomo 容器。

```yaml
  mihomo:                       # 可选：本机代理客户端容器（PROXY_URL 指向它）
    image: metacubex/mihomo:latest
    network_mode: host
    restart: unless-stopped
    volumes:
      - ./mihomo:/root/.config/mihomo

  openlist:                     # 聚合网盘
    image: openlistteam/openlist:latest
    network_mode: host
    restart: unless-stopped
    environment:
      OPENLIST_ADMIN_PASSWORD: ${OPENLIST_ADMIN_PASSWORD}
      NO_PROXY: localhost,127.0.0.1
    volumes:
      - ./openlist/data:/opt/openlist/data

  aria2:                        # BT / 磁力
    image: p3terx/aria2-pro
    network_mode: host
    restart: unless-stopped
    environment:
      PUID: 1000
      PGID: 1000
      RPC_PORT: 6800
      RPC_SECRET: ${ARIA2_RPC_SECRET}
      DOWNLOAD_DIR: /downloads
    volumes:
      - ./aria2/conf:/config
      - ${DOWNLOAD_ROOT}:/downloads
```

`yt-dlp` 需要自己准备构建目录（镜像里要有 `yt-dlp` 与 `ffmpeg`），本项目仓库不包含它的 Dockerfile。

## 5. Token 续期（重要）

PikPak 的 `refresh_token` 是**单次轮换**的，长时间不下任务可能过期。建议 root crontab 每小时跑一次：

```cron
0 * * * * RCLONE_CONF=/path/to/rclone/config/rclone.conf TOKEN_LOG=/var/log/pikpak-token.log /usr/bin/bash /path/to/download-console/scripts/refresh-pikpak-token.sh
```

脚本会逐个账号刷新并**逐个立即落盘**，防止中途崩溃丢失轮换链。日志路径与文件属主可用 `RCLONE_CONF_UID` / `RCLONE_CONF_GID` 调整。

## 6. HTTPS（在线播放需要）

Chromium 在 `http + 私网 IP` 这种非安全上下文里不允许 media 元素加载跨源资源，在线播放会报 `MEDIA_ELEMENT_ERROR`。在项目目录放一张自签证书即可，服务端会自动在同一端口启用 HTTPS：

```bash
mkdir -p tls
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout tls/key.pem -out tls/cert.pem -subj "/CN=download-console"
chmod 600 tls/key.pem
docker restart download-console
```

然后用 `https://<NAS 地址>:5566/` 打开，浏览器忽略自签证书警告即可。没有证书时服务只跑 HTTP，其他功能不受影响。`tls/` 已被 `.gitignore` 忽略。

## 7. 数据文件、备份与升级

运行时数据都写在 `CONSOLE_DATA_DIR`（默认 = 脚本所在目录 = 容器内 `/app`）：

| 文件 | 内容 |
| --- | --- |
| `jobs.json` | 下载任务（含状态、进度、错误） |
| `settings.json` | 页面设置 |
| `history.json` | 下载历史 / 统计 |
| `accounts_meta.json` | 账号 ↔ PikPak 身份映射 |
| `share_limits.json` | 分享提取次数上限记录 |

约定：

- 改文件前先备份成 `*.bak-v<版本>-<日期>`；回滚用 `cat 备份 > 原文件`，再重启容器。
- `static/index.html` 与 `static/app.js` 是 **CRLF** 换行，`.gitattributes` 已禁止 Git 归一化；编辑时请保留，否则会产生整文件 diff。
- 每次改动同步更新 `APP_VERSION`，页脚版本号可用来确认线上是否已加载新代码。
- 重启 `download-console` 前先确认 `jobs.json` 里没有 running 任务，避免中断下载；重启控制台**不会**停掉 rclone 容器里已经在跑的传输。
- 不要轻易 `docker compose up -d --remove-orphans`：线上环境里可能还有你自己装的服务。
- v3.54 起可直接用「设置 → 版本与更新」升级，不必再手工传文件；原理、发版与回滚见 [UPDATE.md](UPDATE.md)。

## 8. 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `didn't find section in config file ("xxx")` | 配置段只有 `captcha_token`、缺 `type`，rclone 无法实例化 | 账号页「清理 N 个无效残留」，或调用 `acctpurge`；用 `rclone listremotes` 分辨 |
| 代理任务连不上 / 分享链接失败 | `PROXY_URL` 指向了容器里不可达的地址 | Windows 用 `host.docker.internal`，Linux host 网络用 `127.0.0.1`；也可在设置页填 `off` 先直连验证 |
| 账号改名 / 删除后，一点测试旧名字又出现 | 两个 rcd 各持一份内存配置，PikPak 后端把 `captcha_token` 回写给旧段并落盘 | v3.51 起 `accounts` 会过滤空壳，`acctrename` / `acctdel` 会自动清两个进程；旧版本需分别 `config/delete` |
| 所有任务一提交就失败、日志显示认证错误 | RC 凭据不一致或没设环境变量 | 核对第 3 节，两边用同一组 `RCLONE_RC_USER/PASS` |
| 在线播放报 `MEDIA_ELEMENT_ERROR` | 非安全上下文 | 配第 6 节的自签证书，用 https 打开 |
| 本地文件页为空 / 看不到刚下载的文件 | 两个容器挂载的不是同一个宿主机目录 | `DOWNLOAD_ROOT` 必须同时挂到 `/data` 与 `/vol2/download` |
| 下载出来的文件属主不对 | rclone-backend 的运行 uid | compose 里加 `user: "${PUID}:${PGID}"` |
| 改了前端没变化 | 浏览器缓存 | Ctrl+F5；改了 Python 还要重启容器 |
| 「返回 NAS 主页」跳错地址 | 默认示例值是 `http://127.0.0.1:8080/` | 「设置 → NAS 主页地址」改成自己的导航页，保存后立即生效 |
