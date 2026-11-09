# 在线检查更新与一键升级

> 适用版本：v3.54+。功能入口在「设置 → 版本与更新」。

## 它能做什么

1. 点「检查更新」→ 后端请求 GitHub Releases API 的 `releases/latest`，把最新 tag 与本地 `APP_VERSION` 比较。
2. 有新版本时显示版本号、发布时间与发行说明，并出现「下载并更新到 vX.YZ」按钮。
3. 确认后后端下载发行包，**校验整包 sha256 + 包内逐文件 sha256 + Python 语法**，全部通过才落盘。
4. 先把旧文件备份到 `update-backups/update-<时间戳>/`，再原子替换白名单里的程序文件，最后延迟 3 秒 `docker restart` 让 Python 改动生效。
5. 出问题可在同一个卡片里点「回滚上一版」，从备份恢复。

NAS 上正在下载的任务由 rclone 容器执行，重启控制台**不会中断传输**；但页面会短暂断开，前端会在 8 秒后自动刷新状态。

## 更新会动哪些文件

只包含 `download-console.py` 中 `UPDATE_FILES` 白名单里的程序文件：

```
download-console.py
static/index.html  static/app.js  static/app.css
static/bm.html     static/bm-code.js
collector.ps1      login-collector.ps1
```

**不会被覆盖**：`settings.json`、`jobs.json`、`history.json`、`accounts_meta.json`、`share_limits.json`、`rclone.conf`、`tls/`、`*.bak*`、下载目录里的任何文件，以及自托管的第三方前端库。也就是说设置、任务、账号和已下载文件都不会因更新丢失。

## 安全设计

| 风险 | 处理 |
| --- | --- |
| 下载到伪造包 | 可选校验 Release 里的 `.sha256` 资源；包内 `release-manifest.json` 逐文件 sha256 复核 |
| 压缩包写入任意路径（zip slip） | 成员名必须在 `UPDATE_FILES` 白名单内，且解析后的绝对路径不能越出程序目录 |
| 版本错配 | 包内 `version` 必须与 Release 的 `tag_name` 一致 |
| 坏包把服务打挂 | 落盘前 `compile()` 校验主程序语法；校验全部通过才开始备份与替换 |
| 替换中途失败 | 已写入的文件从备份逐个恢复 |
| 包体过大 | 整包与解压后总量都限制在 24 MB |
| 未授权触发 | `/api` 本身无鉴权（内网工具），更新来源固定为 `UPDATE_REPO` 指向的 GitHub 仓库；不要把 5566 暴露到公网 |

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `UPDATE_REPO` | `Dlany-Cohhh/cloud-shuttle` | 检查更新的 GitHub 仓库（`owner/repo`）；换成自己的 fork 后即可跟随自己的发行版 |
| `UPDATE_API` | `https://api.github.com` | API 根地址，自建/镜像/测试时可改 |
| `UPDATE_ASSET` | `cloud-shuttle.zip` | 发行包资源名，**必须与打包脚本产出的文件名一致** |
| `GITHUB_TOKEN` | 空 | 私有仓库或想提高 API 限额时填写，只在请求 `UPDATE_API` 时附带 |
| `UPDATE_TIMEOUT` | `15` | 检查请求的超时秒数（下载包固定至少 90 秒） |
| `UPDATE_AUTO_RESTART` | `1` | 设为 `0` 则只替换文件、不自动重启（需手动重启） |
| `UPDATE_CONTAINER` | `download-console` | 自动重启时用的容器名 |
| `CONSOLE_DATA_DIR` | 脚本所在目录 | 备份目录 `update-backups/` 与 `update-state.json` 放在这里 |

容器需要挂载 `/var/run/docker.sock`（示例 compose 已包含）才能自动重启；裸机直接 `python3 download-console.py` 运行时不会自动重启，请手动重启进程。

## 发布一个新版本（维护者）

1. 改完代码后把 `download-console.py` 里的 `APP_VERSION` 升到新版本，例如 `v3.55`。
2. 本地构建并校验资源包：

   ```bash
   python3 scripts/build_release.py
   # 产物：dist/cloud-shuttle.zip、dist/cloud-shuttle.zip.sha256
   ```

3. 提交、打 tag 并推送：

   ```bash
   git add -A && git commit -m "Release v3.55" && git push
   git tag v3.55 && git push origin v3.55
   ```

4. 发布 Release，二选一：

   ```bash
   # A. 本地一键发布（推荐，不依赖 CI 权限）
   python3 scripts/publish_release.py          # 自动构建 + 校验 + 上传资源

   # B. 启用 CI：把 docs/release-workflow.yml 复制到 .github/workflows/release.yml
   #    （在 GitHub 网页端点「Add file → Create new file」粘贴即可，
   #     或用 gh auth refresh -s workflow 给本机令牌补上 workflow 权限后 git push）
   #    之后每次 push v* tag 会自动构建并上传资源
   ```

5. 用户端进入「设置 → 版本与更新」点「检查更新」，即可看到 `v3.55` 并一键升级。

> CI 文件以模板形式放在 `docs/`，没有直接放进 `.github/workflows/`；在 GitHub 网页端新建同名文件粘贴内容即可生效。

> **统一 Release 约定**：一个 `v3.xx` Release 里同时放 Python 发行包和 Rust 原生版安装包。后端只按名字找 `cloud-shuttle.zip`，原生版只按名字找 `cloud-shuttle-rs-*`，因此无论附了多少个资产，`releases/latest` 始终能满足本功能的检查更新；反过来也**不要**为原生版单独发一个不含 `cloud-shuttle.zip` 的 Release，否则所有已部署实例的「检查更新」都会报「发行版缺少 cloud-shuttle.zip」。

> 发行包的**固定文件名**很重要：后端总是从最新 Release 里找 `cloud-shuttle.zip`，不要改成带版本号的名字；版本号放在 tag 与包内清单里。

## 排错

| 现象 | 原因与处理 |
| --- | --- |
| 检查更新提示网络错误 | NAS 访问 `api.github.com` 不通；给容器配置 `HTTPS_PROXY`，或把 `UPDATE_API` 指向可访问的镜像 |
| 提示 SSL 证书校验失败 | 镜像缺 CA 根证书；`Dockerfile` 已装 `ca-certificates`，自建镜像请确保包含该包 |
| 提示「发行版缺少 cloud-shuttle.zip」 | 该 Release 没有上传资源包；重跑 `python3 scripts/publish_release.py`，或手动上传两个 `dist/` 文件 |
| 检查更新正常但下载失败 | 后端会优先走 `api.github.com` 的 asset 端点，再回退 Release 直链；两个域名都不通时给容器配 `HTTPS_PROXY` |
| 提示校验失败 | 资源包与 `.sha256` 不是同一次构建；重新跑打包脚本并一起上传 |
| 更新完成后页面版本没变 | 自动重启未触发（裸机或 docker.sock 未挂载）；手动 `docker restart download-console` 后 Ctrl+F5 |
| 想回到旧版本 | 卡片里「回滚上一版」；也可手工从 `update-backups/update-*/` 复制旧文件回去 |

## 相关文件

- 后端实现：`download-console.py` → `update_check` / `update_apply` / `update_rollback` / `update_state`
- 前端界面：`static/index.html` 设置页「版本与更新」卡片、`static/app.js` 的 `checkUpdate` / `applyUpdate` / `rollbackUpdate`
- 打包脚本：`scripts/build_release.py`
- Rust 原生版：`rust/README.md`、`rust/scripts/build.ps1` / `build.sh`（`-ReleaseTag` / `--release-tag`）
- 本地发布：`scripts/publish_release.py`（自动附带 `rust/dist/` 原生包）· 统一 CI 模板：`docs/release-workflow.yml`
- API 说明：`docs/API.md`
