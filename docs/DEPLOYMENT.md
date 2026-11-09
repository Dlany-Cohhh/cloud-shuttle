# 云梭 CloudShuttle · 部署与运维

面向从零部署一份新的云梭控制台。文中所有路径、账号、密码都是占位符，请按自己的环境替换。

## 1. 端口与依赖

| 组件 | 端口 / 路径 | 说明 |
| --- | --- | --- |
| download-console | `:5566` | 同一端口按 TLS 首字节自动区分 HTTP / HTTPS |
| rclone-backend rcd（主） | `127.0.0.1:5572` | 直连下载，只绑回环 |
| rclone rcd（代理） | `127.0.0.1:5573` | 控制台按需拉起，走 `HTTP(S)_PROXY` |
| mihomo（可选） | `127.0.0.1:7890` | 代理下载用 |
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

```bash
cp .env.example .env
# 修改：PROJECT_ROOT / DOWNLOAD_ROOT / RCLONE_CONFIG_DIR
# 修改：RCLONE_RC_USER / RCLONE_RC_PASS（自己生成随机值）

docker compose -f docker-compose.example.yml up -d --build
docker compose -f docker-compose.example.yml logs -f download-console
# 看到「云梭 CloudShuttle v3.54」启动横幅即正常

curl -s -X POST http://127.0.0.1:5566/api \
  -H 'Content-Type: application/json' -d '{"action":"version"}'
```

浏览器打开 `http://<NAS 地址>:5566/`，按 **Ctrl+F5** 强制刷新一次（静态资源有浏览器缓存）。

RC 凭据必须两边一致：控制台容器的 `RCLONE_RC_USER/PASS` 环境变量 = `rclone-backend` 的 `--rc-user/--rc-pass`。不一致时所有任务会静默认证失败。

如果控制台挂在反向代理后面，且希望「重新登录」下载的采集脚本回推到固定域名，可给控制台加 `CONSOLE_PUBLIC_HOST=你的域名:端口`；不设置时按浏览器请求的 `Host` 头自动判断。

## 4. 可选服务

把下面片段按需加进 compose（密码用 `.env` 变量，不要写死）。启用后控制台「下载任务」页会多出对应入口，账号页也能看到 `openlist` 这类 WebDAV remote。

```yaml
  mihomo:                       # 代理下载
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
| 账号改名 / 删除后，一点测试旧名字又出现 | 两个 rcd 各持一份内存配置，PikPak 后端把 `captcha_token` 回写给旧段并落盘 | v3.51 起 `accounts` 会过滤空壳，`acctrename` / `acctdel` 会自动清两个进程；旧版本需分别 `config/delete` |
| 所有任务一提交就失败、日志显示认证错误 | RC 凭据不一致或没设环境变量 | 核对第 3 节，两边用同一组 `RCLONE_RC_USER/PASS` |
| 在线播放报 `MEDIA_ELEMENT_ERROR` | 非安全上下文 | 配第 6 节的自签证书，用 https 打开 |
| 本地文件页为空 / 看不到刚下载的文件 | 两个容器挂载的不是同一个宿主机目录 | `DOWNLOAD_ROOT` 必须同时挂到 `/data` 与 `/vol2/download` |
| 下载出来的文件属主不对 | rclone-backend 的运行 uid | compose 里加 `user: "${PUID}:${PGID}"` |
| 改了前端没变化 | 浏览器缓存 | Ctrl+F5；改了 Python 还要重启容器 |
| 「返回 NAS 主页」跳错地址 | 默认示例值是 `http://127.0.0.1:8080/` | 「设置 → NAS 主页地址」改成自己的导航页，保存后立即生效 |
