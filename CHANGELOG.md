# 云梭 CloudShuttle · 更新日志

本仓库为首次公开整理，只记录仓库化时的版本与最近五次变更；更早的迭代没有保留独立记录。
版本号对应 `download-console.py` 里的 `APP_VERSION`，也显示在页面页脚。

## v3.56（补充二）— 新增桌面原生窗口版（2026-11-10）

> 只新增 Rust 桌面 exe 资产，Python 版程序文件与 `APP_VERSION` 均未变，NAS 在线更新不会误报升级。

- 新增 `cloud-shuttle-desktop.exe`：**单文件原生窗口程序**，双击即用，不启动 HTTP 服务、不打开浏览器、不需要 `static/`、不依赖 Python / Docker；
- `rust/` 重构为共享库 + 双入口（`cloud-shuttle-rs` 服务器版行为不变 / `cloud-shuttle-desktop` 桌面版），两者共用 `api::dispatch` 全部业务逻辑与数据格式；
- 桌面版七个页面：概览、下载任务、云盘浏览、账号管理、本地文件（单项 + 批量重命名）、设置、关于；中文字体走系统字体，支持浅色 / 深色；
- 数据默认写 `%APPDATA%\CloudShuttle`，下载写「下载\CloudShuttle」；rclone 缺失时可在窗口内一键下载或手动指定；
- 统一 Release 追加桌面 exe 与 sha256，`scripts/publish_release.py` 与 `docs/release-workflow.yml` 自动附带；原生版「检查更新」现在也能列出桌面包。

## v3.56（补充）— 同一仓库发布 Rust 原生版（2026-11-10）

> 程序文件与 v3.56 完全相同，`APP_VERSION` 未变，NAS 在线更新不会提示升级；本次只是仓库结构与 Release 资产调整。

- Rust 原生版源码以 `rust/` 子目录并入本仓库（原独立仓库归档并指向这里），不再维护两套仓库 / 两套 `releases/latest`；
- **统一 Release**：同一个 `v3.xx` 同时带 `cloud-shuttle.zip` + `.sha256` 与 `cloud-shuttle-rs-*.zip|tar.gz`；Python 后端按固定资产名取包，原生版按 `cloud-shuttle-rs-*` 资产名识别，互不干扰；
- `scripts/publish_release.py` 自动附带 `rust/dist/` 里已有的原生包；`docs/release-workflow.yml` 改为单 workflow 同时构建 Python 包与 Windows / Linux 原生包；
- Rust 构建脚本新增 `-ReleaseTag` / `--release-tag`，把统一 Release 标签写进可执行文件，检查更新不会误报。

## v3.56 — 本地文件单项重命名（2026-11-09）

**新增：本地文件页可单独重命名文件夹 / 文件**

- 「本地文件」页每个文件夹行、文件行右侧增加铅笔按钮，点击输入新名字即可单独改名，与云盘页的单文件重命名对齐；
- 复用既有 `localrenamebatch` 接口，不新增后端 action：名称合法性、目标重名、源文件缺失、软链接与路径越界、两阶段临时名 + 失败回滚全部沿用；
- 改名成功后自动刷新当前目录、清掉对应勾选；旧的播放 / 下载链接会失效。

## v3.55 — Docker 一键部署 + 自定义代理地址（2026-11-09）

**新增：Linux / Windows 一键部署**

- `scripts/deploy.sh`（Linux / macOS / NAS）、`scripts/deploy.ps1` + `scripts/deploy.bat`（Windows Docker Desktop 双击即可）；
- 交互式询问下载目录、rclone 配置目录与代理地址，自动生成随机 RC 凭据、`.env`、空的 `rclone.conf`，构建启动并做健康检查，打印访问地址；
- 支持非交互参数、`--dry-run` 只生成配置，以及 `status | logs | stop | restart` 管理已部署容器；
- Windows 用 `docker-compose.windows.yml` 覆盖为 bridge + 端口映射（不依赖 host 网络），Linux 仍用 host 网络；`.gitignore` 补上脚本默认生成的目录，避免误提交 `rclone.conf`。

**新增：代理地址可自定义**

- 后端移除写死的 `MIHOMO_PROXY`，改为 `proxy_url()`：设置页 `proxyUrl` > 环境变量 `PROXY_URL` > 默认 `http://127.0.0.1:7890`；支持 http(s)/socks5，填 `off` 关闭代理；
- 设置页「代理下载」卡片新增自定义地址输入框与「当前生效」显示，保存即生效；`savesettings` 白名单与 `_norm_settings` 校验非法地址；
- 地址变化且 5573 空闲时自动按新地址重启代理守护进程，有任务在跑则等任务结束，避免中断下载；`socks5` 仅用于 rclone 下载，Python 侧请求自动直连兜底；
- 代理地址同时作用于下载任务、分享链接、对比测速与 PikPak 接口请求，不再要求一定运行 mihomo 容器。

**文档**：README 新增「一键部署」章节，`docs/DEPLOYMENT.md` 补一键部署与代理地址优先级，`docs/API.md` / `.env.example` / `examples/settings.example.json` 同步。

## v3.54 — 在线检查更新 + 项目命名（2026-11-09）

**新增：检查更新与一键升级**（设置 → 版本与更新）

- 后端新增 `updatecheck` / `updateapply` / `updatestate` / `updaterollback` 四个 action，对接 GitHub Releases `releases/latest`，结果缓存 10 分钟；
- 发行包整包 sha256 + `release-manifest.json` 逐文件 sha256 双重校验，主程序落盘前先做 Python 语法编译；成员名限制在 `UPDATE_FILES` 白名单，拒绝路径越界与超 24MB 的包；
- 替换前按时间戳备份到 `update-backups/`，支持一键回滚；原子替换保留文件权限，容器里延迟 3 秒 `docker restart`，裸机提示手动重启；
- 只替换程序文件，设置 / 任务 / 账号 / `rclone.conf` / 下载数据均不受影响；
- 新增 `scripts/build_release.py` / `scripts/publish_release.py`（本地一键构建并发布 Release）、可选 CI 模板 `docs/release-workflow.yml`、`docs/UPDATE.md`；
- 前端设置页新增「版本与更新」卡片（检查结果、发行说明、下载并更新、回滚、上次更新记录），侧栏版本旁显示「有新版本」提示；
- 版本号升至 `v3.54`，4 个更新 action 的超时单独配置；下载优先走 GitHub API 的 asset 端点（`api.github.com`），失败再回退 Release 直链，兼顾只放行 API 域名或走代理的网络。

**项目命名**：产品代号「云梭 · CloudShuttle」，仓库 `cloud-shuttle`；页面标题 / 侧栏品牌、启动横幅、README 与文档均保留「PikPak 下载器」字样，并明确「PikPak 多账号下载」的定位。技术标识（容器名、入口文件名、服务名）保持不变。

**文档**：README 补充「多主题切换」「检查更新与一键升级」两节，新增第 8 张多主题功能图与第 9 张更新卡片特写；全部 9 张截图按新品牌与 v3.54 重新生成；「检查更新与一键升级」随后精简为简要说明 + 截图，技术细节集中在 `docs/UPDATE.md`。

## Unreleased — 仓库化整理（2026-11-09）

面向上传 GitHub 的整理，**不改变功能，也不改动正在运行的 NAS 实例**：

- 剔除历史备份、运行时数据、TLS 私钥、`rclone.conf`、私有运维笔记；
- rclone RC 凭据由源码字面量改为读取 `RCLONE_RC_USER` / `RCLONE_RC_PASS` 环境变量；
- 生成采集脚本时的兜底主机地址改为 `CONSOLE_PUBLIC_HOST`（默认 `localhost:5566`）；
- 默认「NAS 主页地址」与文档字符串里的示例账号 / 邮箱改为通用占位；
- 新增 `.gitignore`、`.gitattributes`、`.dockerignore`、`.editorconfig`、`LICENSE`、`.env.example`、`docker-compose.example.yml`、`examples/settings.example.json`；
- 新增 `README.md`、`SECURITY.md`、`docs/DEPLOYMENT.md`、`docs/API.md`，`scripts/refresh-pikpak-token.sh` 路径参数化；
- README 补充为图文项目介绍：`docs/screenshots/` 下 7 张界面对应 7 个导航栏 + 1 张多主题功能图 + 1 张更新卡片特写，全部由本地 mock 渲染，账号 / 邮箱 / 文件名 / 容量均为虚拟示例数据，不含真实信息。

## v3.53 — 批量改名结果条精简（2026-11-09）

- 执行后的结果提示只保留一行「已完成 N 项改名，预览已按新文件名刷新」，去掉「窗口保持打开」及「改一下规则就能继续下一轮…」等说明文字；失败明细仍照常显示。

## v3.52 — 本地批量改名执行后保留窗口（2026-11-09）

- 本地文件页「批量改名」点击「执行改名」后弹窗不再自动关闭，可留在窗口里连续多轮改名；
- 执行后自动清空勾选、把作用范围切回「当前列表全部」，并等待本地文件列表刷新完成，预览按新文件名重算，避免拿旧名字重复提交；
- 弹窗内新增本次执行结果提示（成功项数与失败明细），底部按钮完成后由「取消」变为「关闭」，状态语改为「本轮已完成，改规则可继续下一轮」；
- 云盘预览页沿用原来的「全部成功即关闭」行为，未做改动。

## v3.51 — 账号残留空壳段的根治（2026-11-09）

- `/api accounts` 只返回带 `type` 的配置段，无 `type` 的空壳不再出现在账号列表，也不再进入「一键测试全部」；
- 新增 `purge_ghost_sections()` 与 `acctpurge`：对 5572 / 5573 两个 rcd 各清一次残留；`acctrename` / `acctdel` 结束时也会自动清理旧名；
- 账号页显示「已忽略 N 个无效残留」，「更多」菜单里可一键清理；
- 修掉了「把旧账号改名成新账号后，点一键测试旧名字又冒出来」的问题：PikPak 后端会把 `captcha_token` 回写给已删除的旧段，而两个 rcd 各持一份内存配置，任一次保存都会把它写回 `rclone.conf`。

## v3.50 — 本地文件页批量重命名（2026-11-09）

- 本地文件页新增「批量改名」，与云盘预览页共用同一套模板 / 正则 / 序号规则与预览弹窗；
- 文件夹行和所有文件行加勾选框、筛选栏加「全选」，可只改勾选项或当前列表全部；
- 后端新增 `localrenamebatch`：`../` 越界、软链接、隐藏文件、`.part` 临时名一律拒绝，整批预检后两阶段改名（先转临时名再归位），交换式改名不会撞车，失败自动回滚，单次上限 500 项；
- 页面明确提示：改的是 NAS 上的真实文件名，不进回收站，旧的播放 / 下载链接会失效。

## v3.49 — 一键测试全部 + NAS 主页地址可配置（2026-11-09）

- 账号页新增「一键测试全部」：复用 3 路并发逐号真连接，按钮显示 `测试中 x/N`，结束汇总并刷新凭证徽标，与「检查并修复全部」互斥；
- 设置页新增「NAS 主页地址」，保存后侧边栏与移动端顶栏的「返回 NAS 主页」同时生效，带「用默认」「试打开」；
- 前后端都只放行 `http://` / `https://` 绝对地址，拦截 `javascript:` 等伪协议。
