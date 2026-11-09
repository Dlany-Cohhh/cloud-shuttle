#!/usr/bin/env bash
# PikPak 多账号下载器（云梭 CloudShuttle）· Linux / macOS Docker 一键部署
#
#   bash scripts/deploy.sh                 交互式：问目录与代理地址，然后生成 .env 并启动
#   bash scripts/deploy.sh --yes           全部用默认值，不问问题
#   bash scripts/deploy.sh --proxy http://192.168.1.10:7890 --downloads /mnt/nas/downloads
#   bash scripts/deploy.sh --no-proxy      完全不走代理
#   bash scripts/deploy.sh --dry-run       只生成配置，不启动容器（可用来检查 .env）
#   bash scripts/deploy.sh status|logs|stop|restart
#
# 生成的 .env 含随机 RC 密码，已被 .gitignore 忽略，不要提交。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CMD="start"
YES=0
FORCE=0
DRY=0
NO_BUILD=0
NO_PROXY=0
DOWNLOADS=""
CONFIG_DIR=""
PROXY=""
PORT="5566"
PUBLIC_HOST=""

usage() {
  cat <<'USAGE'
用法: bash scripts/deploy.sh [命令] [选项]

命令（默认 start）:
  start     生成 .env、准备目录、构建并启动容器（默认）
  status    查看容器状态
  logs      跟随控制台日志（Ctrl+C 退出）
  stop      停止容器（数据都在宿主机目录，不会丢）
  restart   重启两个容器

选项:
  --downloads DIR     下载落盘目录（同时挂到 /data 与 /vol2/download）
  --config-dir DIR    rclone.conf 所在目录
  --proxy URL         自定义代理地址：http(s)://host:port 或 socks5://host:port
  --no-proxy          不走代理（等价于 --proxy off）
  --port N            仅写入 WEB_PORT（host 网络下服务端口固定 5566）
  --public-host HOST  固定采集脚本的回推地址，如 nas.example.com:5566
  --root DIR          项目根目录（默认脚本所在仓库）
  -y, --yes           非交互，全部用默认值
  --force             覆盖已有的 .env（默认保留并复用其中的值）
  --dry-run           只生成配置、不启动容器
  --no-build          跳过镜像构建，直接用已有镜像
  -h, --help          显示本帮助

代理地址优先级：设置页「自定义代理地址」 > 本脚本写入的 PROXY_URL > 默认 http://127.0.0.1:7890。
USAGE
}

die() { printf '\n[错误] %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }
warn() { printf '[提示] %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --downloads)   DOWNLOADS="$2"; shift 2 ;;
    --config-dir)  CONFIG_DIR="$2"; shift 2 ;;
    --proxy)       PROXY="$2"; shift 2 ;;
    --no-proxy)    NO_PROXY=1; shift ;;
    --port)        PORT="$2"; shift 2 ;;
    --public-host) PUBLIC_HOST="$2"; shift 2 ;;
    --root)        ROOT="$(cd "$2" && pwd -P)" || die "项目目录不存在: $2"; shift 2 ;;
    -y|--yes)      YES=1; shift ;;
    --force)       FORCE=1; shift ;;
    --dry-run)     DRY=1; shift ;;
    --no-build)    NO_BUILD=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    start|status|logs|stop|restart) CMD="$1"; shift ;;
    *) die "未知参数: $1（用 --help 查看用法）" ;;
  esac
done

ENV_FILE="$ROOT/.env"
COMPOSE_FILE="$ROOT/docker-compose.example.yml"
[[ -f "$COMPOSE_FILE" ]] || die "找不到 docker-compose.example.yml：$COMPOSE_FILE"
[[ -f "$ROOT/download-console.py" ]] || die "项目目录里没有 download-console.py：$ROOT"

env_get() {
  local key="$1" val=""
  if [[ -f "$ENV_FILE" ]]; then
    val="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-)" || true
  fi
  printf '%s' "$val"
}

ask() {
  local var="$1" prompt="$2" def="$3" ans=""
  if [[ $YES -eq 1 ]]; then printf -v "$var" '%s' "$def"; return 0; fi
  read -r -p "$prompt [回车默认: $def]: " ans || true
  printf -v "$var" '%s' "${ans:-$def}"
}

abspath() {
  local p="$1"
  mkdir -p "$p" || die "无法创建目录: $p"
  (cd "$p" && pwd -P)
}

rand_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 18
  else
    head -c 36 /dev/urandom 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | head -c 32 || true
  fi
}

# ── 检测 docker ────────────────────────────────────────────────────────
DC=()
if [[ $DRY -eq 0 ]]; then
  command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装 Docker / Docker Compose"
  if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
  else
    die "未找到 docker compose 插件或 docker-compose 命令"
  fi
  docker info >/dev/null 2>&1 || die "Docker 守护进程不可用（当前用户可能不在 docker 组，试试 sudo 运行）"
else
  DC=(docker compose)
fi

compose() { "${DC[@]}" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

# ── start 之外的子命令 ─────────────────────────────────────────────────
if [[ "$CMD" != "start" ]]; then
  [[ -f "$ENV_FILE" ]] || die "还没有 .env，请先运行一键部署（start）"
  case "$CMD" in
    status)  compose ps ;;
    logs)    compose logs -f download-console ;;
    stop)    compose stop ;;
    restart) compose restart download-console rclone-backend ;;
  esac
  exit 0
fi

printf '\n==========================================================\n'
printf ' PikPak 多账号下载器 · 云梭 CloudShuttle\n'
printf ' Docker 一键部署（Linux / macOS）\n'
printf '==========================================================\n'
info "项目目录：$ROOT"

if [[ -f "$ENV_FILE" && $FORCE -eq 0 ]]; then
  warn "检测到已有 .env，将沿用其中的路径与凭据（要重新生成请加 --force）"
elif [[ -f "$ENV_FILE" && $FORCE -eq 1 ]]; then
  cp -f "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
  warn "已备份原 .env 为 *.bak.*"
fi

# ── 收集参数 ───────────────────────────────────────────────────────────
DEF_DOWNLOADS="$(env_get DOWNLOAD_ROOT)"; DEF_DOWNLOADS="${DEF_DOWNLOADS:-$ROOT/downloads}"
DEF_CONFIG="$(env_get RCLONE_CONFIG_DIR)"; DEF_CONFIG="${DEF_CONFIG:-$ROOT/rclone-config}"
DEF_PROXY="$(env_get PROXY_URL)"; DEF_PROXY="${DEF_PROXY:-http://127.0.0.1:7890}"
DEF_PORT="$(env_get WEB_PORT)"; DEF_PORT="${DEF_PORT:-5566}"
DEF_PUBLIC="$(env_get CONSOLE_PUBLIC_HOST)"

[[ -n "$DOWNLOADS" ]] || ask DOWNLOADS "下载文件保存目录" "$DEF_DOWNLOADS"
[[ -n "$CONFIG_DIR" ]] || ask CONFIG_DIR "rclone.conf 所在目录" "$DEF_CONFIG"
[[ -n "$PUBLIC_HOST" ]] || PUBLIC_HOST="$DEF_PUBLIC"
if [[ "$PORT" == "5566" && -n "$DEF_PORT" ]]; then PORT="$DEF_PORT"; fi

if [[ $NO_PROXY -eq 1 ]]; then
  PROXY="off"
elif [[ -z "$PROXY" ]]; then
  ask PROXY "代理地址（回车用默认；自定义填 http(s)://host:port 或 socks5://host:port；填 off 关闭）" "$DEF_PROXY"
fi
PROXY="${PROXY:-$DEF_PROXY}"

DOWNLOADS="$(abspath "$DOWNLOADS")"
CONFIG_DIR="$(abspath "$CONFIG_DIR")"
RC_USER="$(env_get RCLONE_RC_USER)"; RC_USER="${RC_USER:-console}"
RC_PASS="$(env_get RCLONE_RC_PASS)"
if [[ -z "$RC_PASS" || "$RC_PASS" == change-me* ]]; then
  RC_PASS="$(rand_hex)"
  [[ -n "$RC_PASS" ]] || die "无法生成随机 RC 密码，请在 .env 里手工填写 RCLONE_RC_PASS"
  info "已生成随机 rclone RC 凭据"
fi
OPENLIST_PASS="$(env_get OPENLIST_ADMIN_PASSWORD)"; OPENLIST_PASS="${OPENLIST_PASS:-$(rand_hex)}"
ARIA2_SECRET="$(env_get ARIA2_RPC_SECRET)"; ARIA2_SECRET="${ARIA2_SECRET:-$(rand_hex)}"

# ── 准备目录与 rclone.conf ──────────────────────────────────────────────
if [[ ! -f "$CONFIG_DIR/rclone.conf" ]]; then
  : > "$CONFIG_DIR/rclone.conf"
  chmod 600 "$CONFIG_DIR/rclone.conf"
  warn "已创建空的 rclone.conf：$CONFIG_DIR/rclone.conf"
  warn "启动后进「账号」页，用「导入 / 采集 Token」或「重新登录」添加 PikPak 账号"
else
  warn "沿用已有 rclone.conf（含账号 refresh_token，请勿提交到仓库）"
fi

# ── 生成 .env ──────────────────────────────────────────────────────────
{
  printf '# 由 scripts/deploy.sh 生成于 %s\n' "$(date '+%F %T')"
  printf '# 此文件含 RC 密码，已被 .gitignore 忽略，切勿提交。\n'
  printf 'PROJECT_ROOT=%s\n' "$ROOT"
  printf 'DOWNLOAD_ROOT=%s\n' "$DOWNLOADS"
  printf 'RCLONE_CONFIG_DIR=%s\n' "$CONFIG_DIR"
  printf 'RCLONE_RC_USER=%s\n' "$RC_USER"
  printf 'RCLONE_RC_PASS=%s\n' "$RC_PASS"
  printf 'PROXY_URL=%s\n' "$PROXY"
  printf 'WEB_PORT=%s\n' "$PORT"
  printf 'CONSOLE_PUBLIC_HOST=%s\n' "${PUBLIC_HOST:-localhost:5566}"
  printf 'UPDATE_REPO=Dlany-Cohhh/cloud-shuttle\n'
  printf 'UPDATE_AUTO_RESTART=1\n'
  printf 'OPENLIST_ADMIN_PASSWORD=%s\n' "$OPENLIST_PASS"
  printf 'ARIA2_RPC_SECRET=%s\n' "$ARIA2_SECRET"
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
info "已写入 $ENV_FILE"
printf '   下载目录   : %s\n' "$DOWNLOADS"
printf '   配置目录   : %s\n' "$CONFIG_DIR"
printf '   代理地址   : %s\n' "$PROXY"
printf '   访问端口   : %s（host 网络固定 5566）\n' "$PORT"

UP_ARGS=(up -d)
if [[ $NO_BUILD -eq 0 ]]; then UP_ARGS+=(--build); fi
if [[ $DRY -eq 1 ]]; then
  warn "--dry-run：只生成配置，跳过容器启动"
  printf '   将要执行 : %s -f %s --env-file %s %s\n' "${DC[*]}" "$COMPOSE_FILE" "$ENV_FILE" "${UP_ARGS[*]}"
  exit 0
fi

info "构建并启动容器（首次构建镜像需要几分钟）"
compose "${UP_ARGS[@]}"

# ── 健康检查 ───────────────────────────────────────────────────────────
info "等待控制台就绪"
health() {
  local body='{"action":"version"}'
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m 2 -X POST http://127.0.0.1:5566/api \
      -H 'Content-Type: application/json' -d "$body" >/dev/null 2>&1
  elif command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY' >/dev/null 2>&1
import urllib.request
r = urllib.request.Request("http://127.0.0.1:5566/api", data=b'{"action":"version"}',
                           headers={"Content-Type": "application/json"})
urllib.request.urlopen(r, timeout=2).read()
PY
  else
    return 0   # 没有检查工具就直接给出访问提示
  fi
}
OK=0
for _ in $(seq 1 30); do
  if health; then OK=1; break; fi
  sleep 1
done

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"; HOST_IP="${HOST_IP:-127.0.0.1}"
printf '\n==========================================================\n'
if [[ $OK -eq 1 ]]; then
  printf ' 部署完成 ✓  打开 http://%s:5566/\n' "$HOST_IP"
else
  printf ' 容器已启动，但接口暂未响应；用 logs 查看：\n'
  printf '   bash scripts/deploy.sh logs\n'
  printf ' 浏览器打开 http://%s:5566/\n' "$HOST_IP"
fi
printf '\n 下一步：\n'
printf '   1. 浏览器按 Ctrl+F5 强刷一次，进「账号」页导入 PikPak 账号\n'
printf '   2. 代理地址可在「设置 → 代理下载」随时改；部署时用的是 %s\n' "$PROXY"
printf '   3. 常用命令：bash scripts/deploy.sh status | logs | stop | restart\n'
printf '   4. .env 含 RC 密码与路径，位置：%s\n' "$ENV_FILE"
printf '==========================================================\n'
