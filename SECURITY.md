# 云梭 CloudShuttle · 安全须知

## 绝不要提交的文件

| 文件 / 目录 | 里面有什么 |
| --- | --- |
| `rclone.conf` | 所有网盘账号的 `refresh_token`、`token`、账号与密码（obscured） |
| `.env` | RC 凭据、各服务密码、本机路径 |
| `settings.json` | 运行设置、NAS 主页地址 |
| `accounts_meta.json` | 账号 ↔ PikPak 身份（sub / 邮箱）映射 |
| `jobs.json` / `history.json` / `share_limits.json` | 任务记录、分享链接、历史文件名 |
| `tls/key.pem`、`tls/cert.pem` | HTTPS 私钥与自签证书 |
| `*.bak*`、`_databak*/` | 上面这些文件的历史副本，同样含凭据 |
| 私有运维笔记 | 内网 IP、主机名、各服务明文密码与账号邮箱 |

`.gitignore` 已覆盖以上全部内容。本项目**从一台真实运行的 NAS 整理而来**，历史上这些文件一直和源码放在同一个目录里，所以格外需要注意。

> 推送时请使用本仓库目录。不要直接在 NAS 的运行目录里 `git init && git add .`：那里同时躺着几十个历史备份、TLS 私钥、真实 `rclone.conf` 与运维文档，一旦整目录提交就很难收回来。

如果已经误提交了任何凭据：先从 GitHub 删除仓库 / 改写历史，然后**立即轮换对应凭据**（PikPak 重新登录、改 RC 与各服务密码）。只删文件不轮换是没用的，git 历史里仍然看得到。

## 凭据怎么配

- `RCLONE_RC_USER` / `RCLONE_RC_PASS`：写在 `.env`（不提交），由 compose 同时注入控制台容器和 `rclone-backend` 的 `rcd` 启动参数。源码里不再有任何默认密码。
- PikPak 账号：只存在 `rclone.conf`，建议权限 `600`，只挂进 `rclone-backend` 容器。
- 设置页保存的 `settings.json` 建议限制为仅管理员可读写。

## 已知边界

- **没有登录鉴权，也没有 CSRF 防护**：`/api` 与静态页面对能访问到端口的人完全开放。仅在内网 / 反向代理认证之后使用，不要把 5566 映射到公网。
- 挂载 `/var/run/docker.sock` 给控制台容器，等价于给它宿主机 Docker 的根级控制权，请确保控制台只在可信网络内可访问。
- HTTPS 用的是自签证书，只解决浏览器「安全上下文」需求（在线播放），不提供身份认证。
- 后端会用 `mask_secret()` 给疑似 token 的长串打码，但**日志文件与 rclone 自身日志**仍可能含敏感信息，分享日志前请自行检查。

## 报告问题

请在 GitHub Issues 里描述现象，并**不要**粘贴 `rclone.conf`、`refresh_token`、完整任务 JSON 或含真实账号邮箱的日志。
