# 云梭 CloudShuttle · Rust 原生实现（桌面窗口版 / 服务器版）

[PikPak 多账号下载器](https://github.com/Dlany-Cohhh/cloud-shuttle) 的 Rust 实现。同一个 `src/` 业务逻辑库（`api::dispatch`）驱动三个入口：

| 入口 | 产物 | 界面 | 适用 |
| --- | --- | --- | --- |
| **桌面窗口版（`webview`）** | `cloud-shuttle-desktop.exe` | 原生窗口渲染仓库根 `static/`，**与 Web 版 100% 一致** | Windows 上双击即用的单文件软件（默认发布形态） |
| **桌面轻量版（`gui`）** | 同名 exe | egui 纯 Rust 自绘，零 Web 依赖 | 不想依赖系统 WebView2 的场合 |
| **服务器版** | `cloud-shuttle-rs[.exe]` + `static/` | 浏览器访问本机 Web 控制台 | NAS / Linux / 开机自启 / 局域网访问 |

> 与 Python / Docker 版是**同一仓库里的三条发行线**，不是替换关系：
> - NAS 上要完整分享转存、回收站、云端解压、书签采集、在线热更新 → Python / Docker 版；
> - Windows 上要「双击就是软件、不碰浏览器」→ 桌面窗口版；
> - Linux / NAS 上要一个轻量常驻进程 → 服务器版。
>
> 三者共用兼容的 `settings.json` / `jobs.json` / `rclone.conf` 格式，以及**同一个 Release**（tag `v3.xx`，按资产名区分安装包）。默认桌面版与服务器版共用全部 Rust 业务代码**和同一份前端文件**，只是前端的载体不同；`-LegacyGui` 才换成自绘界面。

---

## 〇、仓库与下载

本目录是主仓库 [Dlany-Cohhh/cloud-shuttle](https://github.com/Dlany-Cohhh/cloud-shuttle) 的 `rust/` 子目录，与 Python / Docker 版**同一个仓库、同一个 Release**。本文件里的命令默认在 `rust/` 目录下执行；从仓库根目录执行时请加 `rust/` 前缀（例如 `bash rust/scripts/build.sh`）。

- 统一 Release：到 [Releases](https://github.com/Dlany-Cohhh/cloud-shuttle/releases) 下载 `cloud-shuttle-rs-windows-x86_64.zip`（Windows）或 `cloud-shuttle-rs-linux-x86_64.tar.gz`（Linux，由 CI 构建）。同一个 Release 里还有 Python / Docker 版在线更新用的 `cloud-shuttle.zip` + `.sha256`，两者按资产名区分，不抢 `releases/latest`。
- Windows 包解压后先跑 `cloud-shuttle-rs.exe --check`，再直接运行 exe（包内已含前端 `static/`）。
- Linux 暂无预编译产物时，在 Linux 上执行 `bash scripts/build.sh` 自行编译；统一构建模板见 [`../docs/release-workflow.yml`](../docs/release-workflow.yml)。
- 升级：原生版不能在线替换自身，下载新包覆盖可执行文件后重启即可，`data/` 里的设置、任务与账号配置全部保留。
- 桌面窗口版：同一个 Release 里的 `cloud-shuttle-desktop.exe`（单文件，约 9 MB），见下一节。

---

## 〇·五、桌面窗口版（不用浏览器）

`cloud-shuttle-desktop.exe` 双击就是应用窗口：**不启动 HTTP 服务、不打开浏览器、不需要 `static/` 目录**，也不依赖 Python / Docker。默认构建（`--features webview`）**复用仓库根 `static/` 的同一份前端**，所以界面与 NAS / Docker 上的 Web 控制台完全一样：多套 DaisyUI 主题、Tabler 图标、ECharts 图表、动效、七个页面、全部按钮原样保留。

实现要点（`src/gui_web/`）：

- `build.rs` 编译时把 `static/` 下 160 个文件（含 150 个 Tabler 字体）用 `include_bytes!` 编进 exe，不再需要外部资源目录；
- `wry` 自定义协议 `cs://localhost/` 在**进程内**响应 `/static/*` 与 `/media/*`（本地文件下载 / Range 在线播放），不监听任何端口；
- 前端唯一的 `fetch("/api", {method:"POST"})` 由初始化脚本接管：经 IPC 交给后台 tokio runtime 的 `api::dispatch`，结果由事件循环回调 resolve。前端源码一行不改，慢操作也不会卡住窗口；
- 关闭窗口即优雅退出，数据目录 `%APPDATA%\CloudShuttle\data\`、下载目录 `%USERPROFILE%\Downloads\CloudShuttle\`，可用 `CS_DATA_DIR` / `CS_DOWNLOAD_ROOT` 覆盖。

运行要求：Windows 10/11 自带的 **WebView2 Runtime**（极老的精简系统若缺失，启动会给出中文提示）；下载任务仍需要 `rclone.exe`，按「用户指定 → `%APPDATA%\CloudShuttle\bin\` → exe 同目录 → PATH」探测。首次启动缺失时，页面顶部出现与当前主题一致的引导条，点「一键下载 rclone」从官方源安装（约 80 MB，实测 v1.76.0）；装完重启应用即可。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-desktop.ps1 -ReleaseTag v3.56
# 默认 --features webview，产物 dist\desktop\cloud-shuttle-desktop.exe
# 加 -LegacyGui 构建零 Web 依赖的 egui 自绘版（界面不同）
```

差异说明：分享转存、云端解压、书签采集、在线热更新等 Rust 后端未实现的按钮会返回中文提示，完整能力仍以 Docker 版为准；「返回 NAS 主页」的出厂地址在桌面上没有对应服务，点击不会跳转（可在设置里改成自己的地址）；其它外链交给系统默认浏览器。

`--check` / `--version` / `--help` 在桌面版里用系统对话框显示（Release 构建隐藏控制台窗口）。Linux 原生窗口版需要为 eframe 启用 X11 / Wayland 后端，当前发布产物只含 Windows exe；Linux 请继续用服务器版。

---

## 一、快速开始

### 前置条件

1. **rclone**：本机可执行。
   - Windows：到 [rclone.org/downloads](https://rclone.org/downloads/) 下载 zip，把 `rclone.exe` 放到 PATH 或程序同目录。
   - Linux：`sudo apt install rclone` / `curl https://rclone.org/install.sh | sudo bash`。
2. **rclone.conf**：已有 PikPak 账号配置时可直接使用（例如从 Python 版数据目录复制）；没有也能先启动，再到「账号」页添加。
3. **前端静态资源**：把 Python 版仓库的 `static/` 整个目录放到可执行文件旁（构建脚本会自动做这一步）。

### Windows

```powershell
# 构建（自动编译 release + 复制前端资源到 dist\windows\）
powershell -ExecutionPolicy Bypass -File scripts\build.ps1

# 运行
.\dist\windows\cloud-shuttle-rs.exe --check        # 先自检
.\dist\windows\cloud-shuttle-rs.exe                # 启动，浏览器打开 http://127.0.0.1:5566/
```

不带参数时监听 `127.0.0.1:5566`，数据在 exe 同目录 `data\`。想改端口 / 目录：

```powershell
.\dist\windows\cloud-shuttle-rs.exe --port 5570 --data-dir D:\CloudShuttle\data --download-root D:\Downloads --rclone-bin D:\tools\rclone.exe
```

### Linux / NAS

```bash
bash scripts/build.sh                 # 产物 dist/linux/cloud-shuttle-rs + static/
./dist/linux/cloud-shuttle-rs --check
./dist/linux/cloud-shuttle-rs --bind 0.0.0.0 --port 5566 --download-root /vol2/download
```

开机自启（systemd）：

```bash
sudo cp dist/linux/cloud-shuttle-rs /opt/cloud-shuttle/
sudo cp -r dist/linux/static /opt/cloud-shuttle/
sudo cp packaging/cloud-shuttle-rs.service /etc/systemd/system/
sudoedit /etc/cloud-shuttle.env          # 参考 .env.example
sudo systemctl daemon-reload && sudo systemctl enable --now cloud-shuttle-rs
```

### 复用 NAS 上已有的 rclone-backend 容器

Rust 进程可以不碰宿主机 rclone，直接通过 `docker exec` 调用现有容器（账号配置、PikPak token 续期脚本都继续沿用）：

```bash
./cloud-shuttle-rs \
  --backend docker --container rclone-backend \
  --rclone-config /opt/cloud-shuttle/data/rclone.conf \
  --download-root /srv/downloads
# 需要时用 CS_CONTAINER_DOWNLOAD_ROOT 指定容器内挂载点（默认 /data）
```

Linux 默认 `auto` 会自动探测 `rclone-backend` 容器是否存在；Windows 默认一律 `local`。

---

## 二、配置

优先级：**命令行参数 > 环境变量 > 同目录 `.env` > 内置默认值**。

| 变量 / 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `CS_BIND` / `--bind` | 监听地址 | `127.0.0.1`（只本机可访问） |
| `CS_PORT` / `--port` | 监听端口 | `5566` |
| `CS_DATA_DIR` / `--data-dir` | `settings.json`、`jobs.json`、`history.json` 所在目录 | exe 同目录 `data/` |
| `CS_DOWNLOAD_ROOT` / `--download-root` | 下载根目录（也是「本地文件」页的根） | `<data-dir>/downloads` |
| `CS_STATIC` / `--static-dir` | 前端资源目录 | exe 同目录 `static/` |
| `CS_RCLONE_BIN` / `--rclone-bin` | rclone 可执行文件 | `rclone` / `rclone.exe` |
| `CS_RCLONE_CONFIG` / `--rclone-config` | rclone.conf 路径 | `<data-dir>/rclone.conf` |
| `CS_RCLONE_BACKEND` / `--backend` | `auto` \| `local` \| `docker` | `auto` |
| `CS_DOCKER_CONTAINER` / `--container` | docker 后端容器名 | `rclone-backend` |
| `CS_CONTAINER_RCLONE_CONFIG` | 容器内 rclone.conf 路径 | `/config/rclone/rclone.conf` |
| `CS_CONTAINER_DOWNLOAD_ROOT` | 容器内下载根路径 | `/data` |
| `PROXY_URL` / `--proxy` | 默认代理地址，`off` 关闭 | `http://127.0.0.1:7890` |
| `CONSOLE_PUBLIC_HOST` | 对外展示的主机名 | `localhost:5566` |
| `UPDATE_REPO` | 检查更新用的 GitHub 仓库（只读） | `Dlany-Cohhh/cloud-shuttle` |

**自定义代理**：设置页「代理下载 → 自定义代理地址」优先级最高，其次 `.env` / 环境变量 `PROXY_URL`。支持 `http://`、`https://`、`socks5://`，填 `off` 表示彻底直连。代理只注入到 rclone 子进程的 `HTTP_PROXY` / `HTTPS_PROXY`；Windows 访问宿主机代理请填 `http://host.docker.internal:7890` 这类可达地址（本机直跑时通常是 `http://127.0.0.1:7890`）。

---

## 三、两个平台的差异

| 项目 | Windows | Linux |
| --- | --- | --- |
| 产物 | `cloud-shuttle-rs.exe` | `cloud-shuttle-rs` |
| rclone 调用 | 本机 `rclone.exe`，子进程用 `CREATE_NO_WINDOW` 隐藏黑框 | 本机 `rclone`，或 `docker exec rclone-backend` |
| 默认后端 | `local` | `auto` → 探测到 `rclone-backend` 容器则 `docker` |
| 停止任务 | `taskkill /PID /T /F` | `kill -TERM` |
| 优雅退出 | Ctrl+C | `SIGINT` / `SIGTERM`（systemd 友好） |
| 开机自启 | `packaging/install-service.ps1`（登录时计划任务） | `packaging/cloud-shuttle-rs.service`（systemd） |
| 路径 | `PathBuf` 自动处理 `\`；API 路径统一用 `/` | 原生 `/` |
| 前端资源 | exe 同目录 `static/` | 同左 |

> 上表是**服务器版**的差异；桌面窗口版（`cloud-shuttle-desktop.exe`）不需要 `static/`、不监听端口、不启动浏览器，也不使用 `web` 模块的路由。

服务器版依赖只有 `tokio` / `axum` / `serde` / `serde_json`；桌面窗口版额外依赖固定版本的 `eframe 0.31.1`（`gui` feature，glow 走系统 OpenGL）。**全部是纯 Rust**：不链接 OpenSSL、不需要 cmake 或 C 交叉编译器，服务器版源码在 Windows 与 Linux 上都能直接编译。唯一的外部命令是「检查更新」用到的系统 `curl`（Windows 10+ 自带，缺失时自动回退 PowerShell）；下载、账号、本地文件等功能只需要 `rclone`。

---

## 四、功能对照（相对 Python / Docker 版）

| 能力 | Rust 原生版 | 说明 |
| --- | --- | --- |
| 完整 Web 控制台（Vue 3 前端、32 套主题、图表） | ✅ | 直接复用 `static/`，未改动一行前端代码 |
| 设置页 / 自定义代理地址 / 参数持久化 | ✅ | `settings.json` 字段与 Python 版兼容 |
| 下载任务：新建 / 排队 / 并发上限 / 暂停 / 继续 / 删除 / 清理 | ✅ | 每个任务一个 `rclone copy` 子进程，落盘大小估算进度 |
| 云盘浏览（列表、已下载标记）、账号列表 / 测试 / 添加 / 删除 / 改名 / 残留清理 | ✅ | 通过 `rclone listremotes` / `lsjson` / `config dump` |
| 本地文件页：列目录、文件/文件夹**单项与批量重命名**、在线播放 | ✅ | 复用 Python 版安全规则：越界 / 重名 / 软链接 / 两阶段回滚；`/media/` 支持 Range |
| 统计（速度曲线、24h 历史）、健康检查、容量查询、磁盘剩余空间保护 | ✅ | 速度历史存 `history.json`；磁盘剩余用 Win32 API / `df` 实测并缓存 60 秒。首页「存储空间」卡片的总容量沿用前端常量，本机显示的比例仅作参考 |
| 检查更新（GitHub Releases） | ⚠️ 只读 | 查询主仓库统一 Release，按 `cloud-shuttle-rs-*` 资产名列出原生包；`hasUpdate` 只在 CI 构建（写入 `CS_RELEASE_TAG`）且标签变化时为真，本地手工编译不误报。需要系统 `curl`（Windows 自动回退 PowerShell） |
| 在线一键替换程序文件 | ❌ | 原生进程不能安全覆盖自身；到 Releases 下载新 exe 覆盖后重启，数据目录不变 |
| 分享转存、回收站、云端解压、书签采集、验证码登录、配额详情、测速、目录扫描 | ❌ | 接口明确返回 `unsupported: true` 并带中文提示，不会静默失败 |
| HTTPS / TLS 终止 | ❌ | 默认只监听本机；需要公网访问请放在 Nginx / Caddy 反向代理后面 |

未实现的 action 会在 `/api` 返回 `{"unsupported": true, "error": "…"}`，前端 toast 会明确提示，不会出现「点了没反应」。

---

## 五、数据与安全

```
<exe 同目录>/
├── cloud-shuttle-rs[.exe]
├── static/                 # 前端（同一份，可直接从 Python 版仓库复制）
├── .env                    # 可选配置
└── data/
    ├── settings.json       # 设置（与 Python 版字段兼容）
    ├── jobs.json           # 任务列表
    ├── history.json        # 速度历史 / 每日下载量
    ├── rclone.conf         # 账号配置（含 token，权限请设为仅本人可读）
    └── downloads/          # 下载根目录（可用 CS_DOWNLOAD_ROOT 指到别处）
```

- 默认监听 `127.0.0.1`，**不对外暴露**；要局域网访问必须显式 `--bind 0.0.0.0`，此时请自行加反向代理与认证。
- 本地文件页严格限制在下载根目录内：拒绝 `..`、绝对路径、软链接越界；批量改名整批预检 + 两阶段临时名 + 失败回滚。
- 账号列表只回传名称 / 类型 / 邮箱，`refresh_token`、密码等绝不出现在 API 响应里。
- 进程重启会把上次遗留的 `running` 任务标为「已暂停」，点「继续」由 rclone 断点续传，不会重复下载。

---

## 六、构建与验证

```bash
cargo fmt --check && cargo clippy --all-targets -- -D warnings
cargo build --release
# Linux 交叉检查（装好 target 后，只做类型/条件编译校验，不链接）
rustup target add x86_64-unknown-linux-gnu
cargo check --target x86_64-unknown-linux-gnu
```

`scripts/build.sh` / `scripts/build.ps1` 会把编译产物和主仓库的 `static/` 一起放进 `dist/<平台>/`，可直接拷走运行。统一发布模板见 [`../docs/release-workflow.yml`](../docs/release-workflow.yml)：一个 workflow 同时产出 Python 发行包和 Windows / Linux 原生包，附到同一个 `v3.xx` Release。把它复制为 `.github/workflows/release.yml` 即可启用（需在 GitHub 网页端创建，或使用具备 workflow 权限的令牌推送）。

仓库自带两个测试（仅 Python 标准库，Windows / Linux 通用）：

```bash
# 接口冒烟：静态资源、设置读写、代理回显、本地文件改名、Range 播放、账号接口（需进程已在跑）
python scripts/smoke.py --url http://127.0.0.1:5566

# 端到端：编译「假 rclone」→ 自动起进程 → 账号/云盘 → 新建任务 →
#         排队/启动/进度/完成 → 文件落盘 → 任务详情 → 删除 → 更新降级（不需要真实网盘或网络）
python scripts/e2e.py
```

`e2e.py` 的夹具在 `scripts/fixtures/fake_rclone.rs`，纯 Rust 单文件，用 `rustc` 直接编译。

---

## 七、许可

MIT，见 [LICENSE](LICENSE)。与 Python / Docker 版同属 [Dlany-Cohhh/cloud-shuttle](https://github.com/Dlany-Cohhh/cloud-shuttle) 仓库。
