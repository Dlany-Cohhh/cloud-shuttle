#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PikPak 多账号下载器（云梭 CloudShuttle）· 批量下载控制台 v3.0 —— metacubexd 设计系统重构（sunset 暗色主题 · Vue 3 · 弹性动效）
   前端在 ./static/（Vue3 + Tailwind v4 + DaisyUI v5 全部自托管，无外网 CDN 依赖）；
   API 与 v2.2 完全兼容，任务/设置数据路径不变"""
import base64, hashlib, io, json, os, re, select, shutil, socket, ssl, stat as statmod, subprocess, sys, tarfile, threading, time
from datetime import datetime as _dt
import unicodedata, urllib.error, urllib.parse, urllib.request, uuid, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── 数据目录（v3.18 修复数据分叉）────────────────────────────────────────────
# 历史坑：早期把数据路径写死成 /vol1/@appshare/zcode/console/*.json。容器里这个路径是
# 镜像自带的独立目录（落进容器可写层），宿主机同名目录只通过 /app 绑定挂载进来，
# 两者互不相通 —— 于是任务/历史全写进了容器内，docker rm 重建即全部丢失。
# 现在统一跟随脚本所在目录（容器内 /app == 宿主机 /vol1/@appshare/zcode/console），
# 需要时可再用环境变量 CONSOLE_DATA_DIR 覆盖。
DATA_DIR = os.environ.get("CONSOLE_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
SHARE_LIMITS_FILE = os.path.join(DATA_DIR, "share_limits.json")   # v3.42 分享提取上限
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")

# ── v3.80：JSON 落盘统一走这两个函数 ───────────────────────────────────────
# 旧写法 `json.load(open(f))` / `json.dump(o, open(f, "w"))` 有三个隐患：
#   1) 文件对象不显式关闭，只在 GC 时回收；2 Hz 轮询下长期运行会堆积 fd；
#   2) 不写 encoding，落盘/读取编码取决于容器 locale（alpine 镜像默认无 locale），
#      中文任务名/设置项在极端 locale 下会 UnicodeEncodeError；
#   3) `json.dump` 后立刻 `os.replace` 没有 flush/fsync，掉电可能留下空文件。
# 现在统一 UTF-8 + flush/fsync + 原子替换，并用一把锁避免并发写互相踩 .tmp。
_JSON_LOCK = threading.Lock()


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj, indent=1, quiet=False):
    """原子写：临时文件 → fsync → os.replace。失败时按 quiet 决定是否抛出。"""
    tmp = path + ".tmp"
    try:
        with _JSON_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=indent)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
    except Exception:
        if not quiet:
            raise

# ── v3.45 HTTPS（同一端口同时接 http / https）──────────────────────────────
# 为什么需要：Chromium 在「非安全上下文」（http + 私网 IP）里不允许 media 元素
# 加载跨源资源。实测同一个 PikPak 直链：https 页面里能播，http://192.168.x 页面
# 里报 MEDIA_ELEMENT_ERROR。所以在线播放必须走 https。
# 容器只映射了 5566 一个端口，因此不另开端口：get_request 靠首字节 0x16（TLS
# 握手）判断走哪条路，同一个端口同时接受 http:// 与 https://。
TLS_DIR = os.path.join(DATA_DIR, "tls")
TLS_CERT = os.path.join(TLS_DIR, "cert.pem")
TLS_KEY = os.path.join(TLS_DIR, "key.pem")


def tls_cert_ready():
    return os.path.isfile(TLS_CERT) and os.path.isfile(TLS_KEY)


def tls_make_context():
    """有证书就建 SSLContext；没有就返回 None，服务只跑 HTTP。"""
    if not tls_cert_ready():
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        return ctx
    except Exception as e:
        print("%s TLS 证书加载失败（仅提供 HTTP）：%s" % (time.strftime("%F %T"), e))
        return None
LEGACY_DATA_DIR = "/vol1/@appshare/zcode/console"   # 仅用于首次启动时兜底迁移
# 账号 → PikPak sub/邮箱（导入时用于识别身份）。优先放代码目录(=宿主机目录)，容器重建也不丢。
ACCT_META_PATHS = (os.path.join(DATA_DIR, "accounts_meta.json"), "/app/accounts_meta.json",
                   "/vol1/@appshare/zcode/console/accounts_meta.json",
                   os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts_meta.json"))
# 控制台版本：启动横幅与前端页脚共用这一处，避免前端硬编码再次落后
APP_VERSION = "v3.81"


# ── v3.38：凭证错误脱敏 + 「需要重新登录」识别 ────────────────────────────
# PikPak 的 error_description 会把整个 RefreshToken 原样拼进报错里
# （invalid refresh token ... RefreshToken os.xxxx）。直接回显到 toast /
# 任务详情 / 日志，等于把可用凭证贴到屏幕上，所以所有外发错误文本统一过一遍掩码。
_SECRET_RE = re.compile(r"(os\.[A-Za-z0-9._\-]{16,}|[A-Za-z0-9_\-]{40,})")


def mask_secret(text):
    """疑似 token 的长串只留首尾各几位，中间标成已隐藏。"""
    def _m(m):
        v = m.group(0)
        return v[:8] + "…（已隐藏" + str(len(v) - 12) + "位）…" + v[-4:]
    return _SECRET_RE.sub(_m, str(text or ""))


RELINK_ERROR_KEYS = (
    "refreshed by other", "已被别处刷新", "invalid refresh token", "refresh_token 无效",
    "refresh_token 已失效", "invalid_grant", "invalid_token", "unauthorized",
    "no username", "账号凭据不完整", "captcha", "需重新登录", "请重新登录",
    "重新采集", "鉴权失效", "凭证已失效", "401",
)


def need_relink(text):
    """这类错误重试多少次都救不回来，只能重新在浏览器登录拿新的 refresh_token。"""
    low = str(text or "").lower()
    return any(k.lower() in low for k in RELINK_ERROR_KEYS)


def migrate_legacy_data():
    """首次用新路径启动时，如果目标文件不存在而旧路径有数据，就把旧数据搬过来。
    只在目标缺失时补齐，绝不覆盖已经存在的数据文件。返回实际迁移的文件名列表。"""
    moved = []
    try:
        if os.path.abspath(LEGACY_DATA_DIR) == os.path.abspath(DATA_DIR):
            return moved
        for name in ("jobs.json", "settings.json", "history.json", "accounts_meta.json"):
            src, dst = os.path.join(LEGACY_DATA_DIR, name), os.path.join(DATA_DIR, name)
            if os.path.isfile(src) and (not os.path.isfile(dst) or os.path.getsize(dst) == 0):
                shutil.copy2(src, dst)
                moved.append(name)
    except Exception as e:
        print("%s 旧数据迁移失败: %s" % (time.strftime("%F %T"), e))
    return moved


def _meta_path(write=False):
    for p in ACCT_META_PATHS:
        d = os.path.dirname(p)
        if write:
            if os.path.isdir(d) and os.access(d, os.W_OK):
                return p
        elif os.path.isfile(p):
            return p
    return ACCT_META_PATHS[0]
DAILY_KEEP = 90  # 保留最近 90 天下载量

# ── rclone RC 凭据（仓库版：只从环境变量读取，源码内不含任何真实密码）──────
# 必须与 docker-compose 里 rclone-backend 的 --rc-user / --rc-pass 一致。
# 未设置时服务仍能启动，但所有 rclone 调用会认证失败，请检查 .env。
RCLONE_RC_USER = os.environ.get("RCLONE_RC_USER", "")
RCLONE_RC_PASS = os.environ.get("RCLONE_RC_PASS", "")
# 生成 Windows 采集脚本时若请求没有 Host 头，用这个地址拼「回推 URL」。
# 正常浏览器请求一定带 Host；需要固定域名（如反代后）可设 CONSOLE_PUBLIC_HOST。
# v3.59：容器名与端口可随部署自定义（多套部署 / 端口冲突场景），默认值即
# docker-compose.example.yml 的约定；改 compose 时同步设这两个环境变量即可。
RCLONE_CONTAINER = os.environ.get("RCLONE_CONTAINER", "rclone-backend")
CONSOLE_PORT = int(os.environ.get("PORT") or 5566)             # 控制台 HTTP 监听端口
RC_PORT = int(os.environ.get("RC_PORT") or 5572)               # rclone 主守护进程（直连 rc）
PROXY_RC_PORT = int(os.environ.get("PROXY_RC_PORT") or 5573)   # 代理下载守护进程
CONSOLE_PUBLIC_HOST = os.environ.get("CONSOLE_PUBLIC_HOST", "localhost:5566")
RC = ["docker", "exec", RCLONE_CONTAINER, "rclone", "rc", "--rc-addr", "127.0.0.1:%d" % RC_PORT,
      "--rc-user", RCLONE_RC_USER, "--rc-pass", RCLONE_RC_PASS]
LOCK = threading.RLock()
STATE = {"diskLow": False, "diskFree": None, "diskTotal": None, "storages": [],
         "volumePairs": None, "firedStart": None, "firedPause": None}
# v3.44：正在跑的「解压」任务（云端轮询线程 / 本地解压线程）。
# job_live 用它区分「线程还在推进度」与「控制台重启过、线程已丢」。
UNZIP_ALIVE = set()
DEFAULTS = {"maxConcurrent": 2, "autoStart": "", "autoPause": "", "diskMinGB": 20,
            # v3.57 自定义存储空间：每个下载卷的容量上限（GB，十进制口径与页面显示一致）。
            # 0 = 不限，按磁盘真实容量显示与保护；>0 时该卷的「剩余」按 配额-已用 折算，
            # 配额按整卷占用计（同一卷上其他程序的数据也算在内），配额用尽触发磁盘保护。
            "diskQuotaGB": 0,
            "transfers": 6, "checkers": 8, "proxy": False, "downloadProxy": True,
            # v3.36 高级传输参数：这些值通过 rclone rc 的 _config 按任务注入，
            # 不修改 rcd 守护进程的全局配置，也不会重启容器。
            # v3.37：默认关闭。关闭时只下发基础参数，高级字段一律交给 rclone 默认值；
            # 用户在设置页打开「启用高级传输参数」后才会注入下面这些字段。
            "advancedMode": False,
            "multiThreadStreams": 8,        # --multi-thread-streams 单文件分块并发
            "multiThreadCutoffMB": 64,      # --multi-thread-cutoff 超过该大小才分块
            "orderBy": "size,descending",   # --order-by 先下大文件
            "bwLimitOn": False,             # --bwlimit 开关
            "bwLimit": "5M",                # --bwlimit 数值（off 时不注入）
            "retries": 10,                  # --retries 弱网自动重试
            "timeout": "1m",                # --timeout IO 空闲超时
            "idleTimeout": "10m",           # --contimeout 连接/空闲超时（1.75 无 --idle-timeout）
            "createEmptySrcDirs": True,     # --create-empty-src-dirs 保留空目录
            "logLevel": "INFO",             # --log-level 任务日志级别
            "logFile": "",                  # --log-file：rcd 启动参数，仅作配置记录
            "transfersClearedAt": 0,        # 「清空已完成传输记录」的时间水位
            "shareAutoCancel": False,       # v3.42 提取次数到上限自动取消分享（总开关）
            "nasHomeUrl": "http://127.0.0.1:8080/",  # v3.49 「返回 NAS 主页」跳转地址
            # v3.77 全局网络模式：direct = 下载 / 云盘 API / 封面全部直连；
            # proxy = 三者统一走同一个代理地址。选 proxy 必须填写合法地址，
            # 留空会自动回落 direct，不再悄悄套用 127.0.0.1:7890。
            "netMode": "direct",
            "proxyUrl": "",
            # 兼容字段：v3.74~v3.76 的 pkProxyMode，由 _norm_settings 与 netMode 同步；
            # 请求层已不再读取，保留只为老配置 / 老前端平滑升级。
            "pkProxyMode": "direct"}

# 设置页可选值白名单：避免把任意字符串拼进 rclone 参数
ORDER_BY_OPTIONS = ("", "size,descending", "size,ascending", "name,ascending",
                    "modtime,descending", "modtime,ascending")
LOG_LEVELS = ("INFO", "DEBUG", "NOTICE", "WARN", "ERROR", "OFF")
DUR_RE = re.compile(r"^\d+(ms|s|m|h)$")
BW_RE = re.compile(r"^\d+(\.\d+)?[KMG]?$", re.I)
# PikPak 传输配额：接口慢且按账号限流，缓存 5 分钟（与官方网页一致）
QUOTA_CACHE = {}              # remote -> (ts, result)
QUOTA_TTL = 300
QUOTA_LOCK = threading.Lock()

# ── v3.47：PikPak 会员状态判定 ─────────────────────────────────────────
# /vip/v1/quantity/list 的 base.vip_status 实测返回 "ok" / "invalid"，
# 旧代码只认 "valid"，于是把真会员一律判成非会员：配额页显示「非会员」，
# 更严重的是前端 curVip() 据此把「云端解压」（会员专属功能）也一并禁用了。
VIP_ACTIVE_STATES = ("ok", "valid", "active", "true", "1")


def pk_vip_active(base):
    """从配额接口 base 段判断会员是否有效；字段缺失或未知取值按非会员处理。"""
    b = base if isinstance(base, dict) else {}
    v = str(b.get("vip_status") or "").strip().lower()
    return v in VIP_ACTIVE_STATES

def rc(method, _timeout=120, _strict=False, _proxy=False, **kv):
    """_timeout 供持锁路径（如 jobs 轮询里的状态查询）设短超时，避免 rclone 卡住拖死接口。
    _strict=True 时额外检查返回体里的 error 字段并抛异常：rclone RC 出错是
    「HTTP 200 + {"error": ...}」，不抛异常的话调用方会把错误 dict 当正常结果继续处理
    （历史 bug：job/status 返回 job not found 时任务永远卡在 running）。默认 False，
    以保持其余 action 的既有行为。"""
    base = RC_PROXY if _proxy else RC
    args = base + [method] + ["%s=%s" % (k, v) for k, v in kv.items()]
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_timeout)
    try:
        d = json.loads(p.stdout)
    except Exception:
        raise RuntimeError("rc %s 失败: %s%s" % (method, p.stdout[:200], p.stderr[:200]))
    if _strict and isinstance(d, dict) and d.get("error"):
        raise RuntimeError("rc %s: %s" % (method, str(d["error"])[:200]))
    return d

DEFAULT_PROXY_URL = "http://127.0.0.1:7890"  # 默认 mihomo（与 rclone-backend 同为 host 网络）
# v3.55：代理地址可在「设置 → 代理」自定义，或部署时用环境变量 PROXY_URL 指定；
# 填 off / none / direct 表示完全不走代理（两条路都直连）。
PROXY_URL_RE = re.compile(r"^(?:https?|socks5h?)://[^\s\"'<>]+$", re.I)
PROXY_OFF_WORDS = ("off", "none", "direct", "disabled", "no")
SPEEDTEST_SECONDS = 5                   # 单条路径测速窗口；容器内硬超时 = 该值 + 3s


def net_mode():
    """v3.77 全局网络模式：direct | proxy。
    兼容迁移：老配置只有 pkProxyMode(follow/direct/proxy)，按语义推导；
    follow 且填了合法地址视为 proxy，填关闭词 / 留空视为 direct。"""
    st = load_settings()
    m = str(st.get("netMode") or "").strip().lower()
    if m in ("direct", "proxy"):
        return m
    old = str(st.get("pkProxyMode") or "follow").strip().lower()
    if old in ("direct", "proxy"):
        return old
    raw = (str(st.get("proxyUrl") or "").strip()
           or str(os.environ.get("PROXY_URL") or "").strip())
    return "direct" if (not raw or raw.lower() in PROXY_OFF_WORDS) else "proxy"


def proxy_url():
    """v3.77 全局生效的代理地址：所有网络请求（下载 / PikPak API / 封面 CDN）共用。
    direct 模式返回空串；proxy 模式必须有显式配置的合法地址，否则返回空串
    （不会回落到默认地址，避免「选了代理却没填地址」时静默走一个坏代理）。"""
    if net_mode() != "proxy":
        return ""
    raw = (str(load_settings().get("proxyUrl") or "").strip()
           or str(os.environ.get("PROXY_URL") or "").strip())
    if not raw or raw.lower() in PROXY_OFF_WORDS:
        return ""
    return raw if PROXY_URL_RE.match(raw) else ""


def proxy_env_args():
    """docker exec 的代理环境变量参数；直连模式返回空列表。"""
    p = proxy_url()
    return ["-e", "HTTPS_PROXY=" + p, "-e", "HTTP_PROXY=" + p] if p else []


def proxy_http_url():
    """Python urllib 只认 http(s) 代理；socks5 仅用于 rclone 下载，这里退回直连。"""
    p = proxy_url()
    return p if p.lower().startswith(("http://", "https://")) else ""


def pk_proxy_url():
    """v3.77：不再拆分作用域，PikPak API / 封面 CDN 与下载共用 proxy_url()。
    保留函数名是为了不改动 _pk_http / _thumb_fetch 的调用点。"""
    return proxy_http_url()


def _probe_url(url, proxy, timeout=8):
    """只读连通性探测：401 / 404 也算「到达了服务器」，不带任何账号凭证。"""
    req = urllib.request.Request(url, headers={"User-Agent": PK_UA})
    ph = {"http": proxy, "https": proxy} if proxy else {}
    t0 = time.time()
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler(ph)).open(req, timeout=timeout) as r:
            r.read(64)
            return True, int((time.time() - t0) * 1000), "HTTP %s" % r.status
    except urllib.error.HTTPError as e:
        return True, int((time.time() - t0) * 1000), "HTTP %s" % e.code
    except Exception as e:
        return False, int((time.time() - t0) * 1000), _friendly_net_err(e)


def _friendly_net_err(e):
    """v3.75：把 Python 原始异常翻成用户能看懂的一句话。"""
    s, low = str(e), str(e).lower()
    if "unexpected_eof" in low or "eof occurred" in low:
        return "TLS 连接被中断（代理可能未正常工作）"
    if "timed out" in low or "timeout" in type(e).__name__.lower():
        return "连接超时"
    if "refused" in low or "10061" in s:
        return "连接被拒绝（端口未监听）"
    if "getaddrinfo" in low or "name or service not known" in low:
        return "域名解析失败"
    if "certificate" in low:
        return "证书校验失败"
    if "reset by peer" in low or "errno 104" in low or "connection reset" in low:
        return "连接被对端重置（网络抖动或被限速）"
    if "broken pipe" in low or "errno 32" in low:
        return "连接被中断"
    return "%s：%s" % (type(e).__name__, s[:60])


def _probe_connect(proxy, host="api-drive.mypikpak.com", port=443, timeout=6):
    """v3.76：CONNECT + TLS 握手 + 一个 HEAD，验证代理真能转发 HTTPS。
    只到「CONNECT 200」不够：代理可以回 200 后立刻断开隧道，此时 API 请求仍会
    卡在 TLS 握手（真机实测 1 ms CONNECT 成功、5.1 s TLS 中断）。
    socks5 / https 代理不支持此探测，返回 None 跳过。"""
    u = urllib.parse.urlparse(proxy)
    if u.scheme not in ("http", ""):
        return None
    ph, pp = u.hostname or "", u.port or 80
    t0 = time.time()
    try:
        with socket.create_connection((ph, pp), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
                          % (host, port, host, port)).encode())
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 4096:
                chunk = sock.recv(512)
                if not chunk:
                    break
                buf += chunk
            status = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if " 200 " not in (" " + status + " "):
                return {"ok": False, "ms": int((time.time() - t0) * 1000),
                        "detail": "代理拒绝隧道：" + (status[:50] or "无响应")}
            try:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
                    tls.settimeout(timeout)
                    tls.sendall(("HEAD / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n"
                                 % host).encode())
                    tls.recv(256)
                return {"ok": True, "ms": int((time.time() - t0) * 1000),
                        "detail": "CONNECT + TLS 通过"}
            except Exception as e:
                return {"ok": False, "ms": int((time.time() - t0) * 1000),
                        "detail": "隧道已建立，但 TLS 握手失败：" + _friendly_net_err(e)}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "detail": _friendly_net_err(e)}


PROXY_CHECK_CACHE = {"ts": 0.0, "bad": set(), "hint": "", "summary": "", "recommend": None}


def proxy_scope():
    """v3.77：全局模式下三条路径同值；健康标记与最近一次诊断结论同步。"""
    addr = proxy_url() or "直连"
    mode = net_mode()
    ck = PROXY_CHECK_CACHE
    fresh = time.time() - ck["ts"] < 300
    bad = ck["bad"] if fresh else set()
    unhealthy = bool(bad)          # 全局：任一路径失败，三条一起标红
    return {"download": addr, "pk": addr, "thumb": addr,
            "mode": mode, "global": True,
            "configured": mode == "direct" or bool(proxy_url()),
            "downloadOk": not unhealthy, "pkOk": not unhealthy, "thumbOk": not unhealthy,
            "hint": ck["hint"] if fresh else "",
            "summary": ck["summary"] if fresh else "",
            "recommend": ck["recommend"] if fresh else None,
            "checkedAt": ck["ts"] if fresh else 0}


def api_proxy_check():
    """v3.77 只读连通性测试：按当前全局模式测主路径，代理模式另测对照与 CONNECT+TLS。
    主路径失败时把三条健康标记一起置红，避免「状态行一切正常 / 诊断网络未通过」并存。"""
    from concurrent.futures import ThreadPoolExecutor
    url = PK_API + "/drive/v1/files"
    cdn = "https://sg-thumbnail-drive.mypikpak.com/"
    mode, pxy, pk = net_mode(), proxy_url(), proxy_http_url()
    jobs = [("PikPak API · 直连", url, ""), ("封面 CDN · 直连", cdn, "")]
    if pk:
        jobs += [("PikPak API · 代理", url, pk), ("封面 CDN · 代理", cdn, pk)]

    def add(label, target, proxy):
        ok, ms, detail = _probe_url(target, proxy)
        return {"name": label, "ok": ok, "ms": ms, "detail": detail}

    def port_probe():
        u = urllib.parse.urlparse(pxy)
        host, port = u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
        t0 = time.time()
        try:
            with socket.create_connection((host, port), timeout=4):
                return {"name": "代理端口 %s:%s" % (host, port), "ok": True,
                        "ms": int((time.time() - t0) * 1000), "detail": "TCP 可连"}
        except Exception as e:
            return {"name": "代理端口 %s:%s" % (host, port), "ok": False,
                    "ms": int((time.time() - t0) * 1000), "detail": _friendly_net_err(e)}

    def connect_probe():
        r = _probe_connect(pk) if pk else None
        return ({"name": "代理 CONNECT + TLS", "ok": r["ok"], "ms": r["ms"],
                 "detail": r["detail"]} if r else None)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(add, *j) for j in jobs]
        pf = ex.submit(port_probe) if pxy else None
        cf = ex.submit(connect_probe) if pk else None
        items = [f.result() for f in futs]
        if pf:
            items.append(pf.result())
        if cf and cf.result():
            items.append(cf.result())

    def get(name):
        return next((x for x in items if x["name"] == name), None)

    direct = get("PikPak API · 直连")
    via = get("PikPak API · 代理")
    conn = get("代理 CONNECT + TLS")
    direct_ok = bool(direct and direct["ok"])
    via_ok = bool(via and via["ok"])
    conn_ok = bool(conn and conn["ok"]) if conn else via_ok
    main_ok = (via_ok and conn_ok) if mode == "proxy" else direct_ok
    alt_ok = direct_ok if mode == "proxy" else via_ok

    recommend, hint, summary = None, "", ""
    if not main_ok and alt_ok:
        recommend = "direct" if mode == "proxy" else "proxy"
        hint = ("当前代理不可用、直连正常 → 建议改用直连" if mode == "proxy"
                else "直连不通；如网络需要代理，请切换为代理模式并填写地址")
        summary = ("代理路径不通；直连 %s ms 正常" % direct["ms"] if mode == "proxy"
                   else "直连不通，请检查网络或切换代理模式")
    elif not main_ok:
        hint = "两条路径都不通，请检查网络与代理配置"
        summary = "网络检测未通过"
        if mode == "direct":
            recommend = "proxy"     # 前端会提示先填写有效代理地址
            hint = "直连不通；如网络需要代理，请切换为代理模式并填写地址"
    elif mode == "proxy" and direct_ok and via["ms"] > direct["ms"] * 1.5:
        recommend = "direct"
        hint = "直连明显快于代理 → 可改回直连加速大小统计"
        summary = "代理可用（%s ms），但直连更快（%s ms）" % (via["ms"], direct["ms"])
    elif mode == "proxy":
        hint = "代理可用；下载 / 云盘数据 / 封面统一走该代理"
        summary = "代理可用（%s ms）" % via["ms"]
    else:
        hint = "直连可用；下载 / 云盘数据 / 封面全部直连"
        summary = "直连可用（%s ms）" % direct["ms"]

    bad = set() if main_ok else {"download", "pk", "thumb"}
    PROXY_CHECK_CACHE.update(ts=time.time(), bad=bad, hint=hint,
                             summary=summary, recommend=recommend)
    return {"ok": True, "items": items, "scope": proxy_scope(), "failed": not main_ok,
            "hint": hint, "summary": summary, "recommend": recommend}


# ── 隔离的「代理下载」守护进程 ─────────────────────────────────────
# 主守护进程 5572 直连；5573 启动时注入 HTTP(S)_PROXY。两个进程各有
# 独立的 Fs 缓存 / jobid 空间 / stats 分组，因此可以逐任务选择路径，
# 不必改 compose、不必重建容器，也不会影响已运行的直连任务。
RC_PROXY = ["docker", "exec", RCLONE_CONTAINER, "rclone", "rc",
            "--rc-addr", "127.0.0.1:%d" % PROXY_RC_PORT, "--rc-user", RCLONE_RC_USER, "--rc-pass", RCLONE_RC_PASS]
PROXY_RC_STATE = {"ok": False, "ts": 0.0, "url": ""}


def proxy_rcd_cmd():
    """按当前配置的代理地址拼出 5573 的启动命令（docker exec -d）。"""
    return ["docker", "exec", "-d"] + proxy_env_args() + [
        RCLONE_CONTAINER, "rclone", "rcd", "--rc-addr", "127.0.0.1:%d" % PROXY_RC_PORT,
        "--rc-user", RCLONE_RC_USER, "--rc-pass", RCLONE_RC_PASS, "--log-level", "INFO"]


def _proxy_rc_ping():
    try:
        p = subprocess.run(RC_PROXY + ["core/version"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=6)
        return p.returncode == 0 and "version" in (p.stdout or "")
    except Exception:
        return False


def _stop_proxy_rc():
    """只停 5573，不碰主守护进程。[r] 技巧避免 pkill 匹配到执行它的 shell 自身。"""
    try:
        subprocess.run(["docker", "exec", RCLONE_CONTAINER, "sh", "-c",
                        "pkill -f '[r]clone rcd --rc-addr 127.0.0.1:%d' || true" % PROXY_RC_PORT],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except Exception:
        pass
    PROXY_RC_STATE.update(ok=False, ts=0.0, url="")


def ensure_proxy_rc(force=False):
    """确认 5573 可用；rclone-backend 容器重启或代理地址变化后按需重新拉起。"""
    now = time.time()
    wanted = proxy_url()
    if (not force and PROXY_RC_STATE["ok"] and PROXY_RC_STATE.get("url") == wanted
            and now - PROXY_RC_STATE["ts"] < 30):
        return True
    if not force and PROXY_RC_STATE["ok"] and PROXY_RC_STATE.get("url") != wanted:
        # 地址变了：有传输在跑就沿用旧进程，等任务结束再换，避免中断下载
        try:
            d = rc("core/stats", _timeout=6, _proxy=True)
            if (d or {}).get("transferring"):
                PROXY_RC_STATE["ts"] = now
                return True
        except Exception:
            pass
        _stop_proxy_rc()

    if _proxy_rc_ping():
        PROXY_RC_STATE.update(ok=True, ts=now, url=PROXY_RC_STATE.get("url") or wanted)
        return True
    try:
        subprocess.run(proxy_rcd_cmd(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except Exception:
        pass
    for _ in range(10):
        time.sleep(0.6)
        if _proxy_rc_ping():
            PROXY_RC_STATE.update(ok=True, ts=now, url=wanted)
            return True
    PROXY_RC_STATE["ok"] = False
    return False


def refresh_proxy_rc():
    """设置页改了模式/地址后调用：空闲时按新地址重启 5573，返回 (ok, 提示文字)。"""
    if net_mode() != "proxy" and _proxy_rc_ping():
        _stop_proxy_rc()
        PROXY_RC_STATE.update(ok=False, ts=0.0, url="")
        return True, "已切换为全局直连，代理守护进程已停止"
    if net_mode() != "proxy":
        PROXY_RC_STATE.update(ok=False, ts=0.0, url="")
        return True, ""
    if not _proxy_rc_ping():
        PROXY_RC_STATE.update(ok=False, ts=0.0, url="")
        return True, "代理地址已保存，首次代理下载时按新地址自动拉起"
    try:
        d = rc("core/stats", _timeout=6, _proxy=True)
        if (d or {}).get("transferring"):
            return True, "地址已保存；有代理任务在跑，等任务结束或重启容器后生效"
    except Exception:
        pass
    _stop_proxy_rc()
    time.sleep(0.4)
    try:
        subprocess.run(proxy_rcd_cmd(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except Exception:
        pass
    for _ in range(5):
        time.sleep(0.5)
        if _proxy_rc_ping():
            PROXY_RC_STATE.update(ok=True, ts=time.time(), url=proxy_url())
            return True, "代理地址已生效（%d 已按新地址重启）" % PROXY_RC_PORT
    return False, "地址已保存，但代理守护进程未就绪，首次代理下载时会自动重试"


def merged_stats(_timeout=8):
    """首页总速度 / 历史曲线需把代理守护进程的传输一并计入。
    任务行的速度与进度由 job_live 按各自守护进程的 job 分组单独读取。"""
    d = rc("core/stats", _timeout=_timeout)
    if PROXY_RC_STATE["ok"]:
        try:
            d2 = rc("core/stats", _timeout=_timeout, _proxy=True)
            d = dict(d)
            d["bytes"] = int(d.get("bytes") or 0) + int(d2.get("bytes") or 0)
            d["transferring"] = (list(d.get("transferring") or [])
                                 + list(d2.get("transferring") or []))
        except Exception:
            pass
    return d

# 「添加账号」支持的 rclone 后端（非 OAuth 类型；OAuth 型需交互授权，暂不支持网页添加）
PROVIDERS = [
    {"type": "pikpak", "label": "PikPak 网盘（一键浏览器登录采集）", "params": []},
    {"type": "webdav", "label": "OpenList / WebDAV（Alist/坚果云等）", "params": [
        {"name": "url", "label": "WebDAV 地址", "required": True, "ph": "http://NAS_IP:5244/dav"},
        {"name": "user", "label": "用户名"},
        {"name": "pass", "label": "密码", "secret": True},
        {"name": "vendor", "label": "厂商（默认 other）", "ph": "other"}]},
]

# ---- PikPak 令牌 ----
# 注意：rclone 的 pikpak 后端【没有】 refresh_token 这个配置项（rclone config providers 可查），
# 真正用于鉴权的是 OAuth 的 token 字段（一整段 JSON）。所以只写 refresh_token 是无效的，
# 必须先用 refresh_token 换出 access_token，再整段写回 token。
PIKPAK_TOKEN_URL = "https://user.mypikpak.net/v1/auth/token"
PIKPAK_SIGNIN_URL = "https://user.mypikpak.net/v1/auth/signin"
PIKPAK_CAPTCHA_URL = "https://user.mypikpak.net/v1/shield/captcha/init"
PIKPAK_CLIENT_ID = "YUMx5nI8ZU8Ap8pm"
PK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def rc_ok(method, _proxy=False, **kv):
    """rc 的包装：rclone 的 RC 出错时是 200 + {"error":...}，不会抛异常"""
    r = rc(method, _proxy=_proxy, **kv)
    if isinstance(r, dict) and r.get("error"):
        raise RuntimeError(str(r.get("error"))[:300])
    return r


def cloud_list(name, sub="", retry=True, proxy=False):
    """列云端目录，兼容 PikPak 偶发「第一次返回空列表」。
    返回 (list, err)：err 非空 = 接口调用失败；list 为空且 err 为空 = 目录确实为空。"""
    err = ""
    for i in range(2 if retry else 1):
        try:
            lst = (rc_ok("operations/list", _proxy=proxy, fs=name + ":", remote=sub) or {}).get("list") or []
            if lst or not retry or i:
                return lst, ""
            time.sleep(1.5)          # 空结果先别急着判「源目录已空」
        except Exception as e:
            err = str(e)[:150]
            if not retry or i:
                return [], err
            time.sleep(1.5)
    return [], err


def cloud_precheck(name, sub="", proxy=False):
    """预检下载源：sub 既可能是目录，也可能是单个文件。

    rclone 的 operations/list 只能列目录，传单个文件路径会回
    「error in ListJSON: directory not found」—— 历史上 start 因此把
    所有单文件下载挡在预检阶段。这里在遇到该类错误时回退为
    「列父目录 + 按 Name 找同名条目」来判定目标存在。

    返回 (list, err, is_file)：list 非空 = 源可用；err 非空 = 调用失败或目标不存在。
    """
    sub = str(sub or "").strip("/")

    def _once(pxy):
        if not sub:
            lst, err = cloud_list(name, "", proxy=pxy)
            return lst, err, False
        lst, err = cloud_list(name, sub, retry=False, proxy=pxy)
        if err:
            low = err.lower()
            if not any(k in low for k in ("directory not found", "not a directory", "is a file")):
                return [], err, False
            parent, _, leaf = sub.rpartition("/")
            plist, perr = cloud_list(name, parent, retry=False, proxy=pxy)
            if perr:
                return [], err, False
            hit = next((x for x in plist if str(x.get("Name") or "") == leaf), None)
            if not hit:
                return [], "云端找不到「%s」（父目录里没有这个名字）" % sub, False
            return [hit], "", True
        if not lst:
            lst, err = cloud_list(name, sub, proxy=pxy)   # PikPak 偶发空列表，复核一次
        return lst, err, False

    result = _once(proxy)
    # 代理守护进程 5573 可能尚未拉起、或代理链路偶发被重置。预检只负责确认
    # 「源是否存在」，用直连复核一次即可，不改变任务实际下载路径（由 job.proxy
    # 决定，launch_job 里仍会确保 5573 可用）。
    if proxy and result[1]:
        fallback = _once(False)
        if not fallback[1]:
            return fallback
    return result


def pikpak_token_full(rt):
    """用 refresh_token 换 access_token。
       返回 (token 字段的 JSON 字符串, 轮换后的 refresh_token, 账号 sub, access_token)
       失败抛 RuntimeError，带可读原因。"""
    rt = str(rt or "").strip()
    if not rt:
        raise RuntimeError("refresh_token 为空")
    body = json.dumps({"client_id": PIKPAK_CLIENT_ID, "grant_type": "refresh_token",
                       "refresh_token": rt}).encode()
    req = urllib.request.Request(PIKPAK_TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": PK_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        # v3.38：PikPak 会把完整 RefreshToken 塞进 error_description，必须先脱敏再外发
        code, desc = "", mask_secret(raw[:250])
        try:
            j = json.loads(raw)
            code = str(j.get("error_code") or j.get("code") or "")
            desc = mask_secret(str(j.get("error_description") or j.get("error") or raw)[:250])
        except Exception:
            pass
        low = desc.lower()
        if "refreshed by other" in low:
            raise RuntimeError("这个 refresh_token 已被别处刷新过（%s）。请在浏览器重新登录该账号后再采集" % desc)
        if code == "4126" or "invalid" in low:
            raise RuntimeError("refresh_token 无效，PikPak 不认（%s）。请确认复制完整，或在该账号的浏览器里重新登录后再采集" % desc)
        if code == "9" or "captcha" in desc.lower():
            raise RuntimeError("PikPak 要求人工验证码（%s）。需要在 PikPak 客户端/网页上先登录一次该账号" % desc)
        if "invalid_grant" in desc or code in ("2", "3"):
            raise RuntimeError("refresh_token 已失效（%s）。请重新登录该 PikPak 账号后重新提取" % desc)
        raise RuntimeError("PikPak 拒绝换发 token（HTTP %s, code=%s）：%s" % (e.code, code or "-", desc))
    except Exception as e:
        raise RuntimeError("连接 PikPak 令牌接口失败：%s" % mask_secret(str(e)[:150]))
    at = str(d.get("access_token") or "")
    if not at:
        raise RuntimeError("PikPak 未返回 access_token：%s" % mask_secret(json.dumps(d, ensure_ascii=False)[:150]))
    newrt = str(d.get("refresh_token") or rt)
    try:
        exp_s = int(d.get("expires_in") or 3600)
    except Exception:
        exp_s = 3600
    expiry = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + max(60, exp_s - 60)))
    tok = {"access_token": at, "token_type": d.get("token_type") or "Bearer",
           "refresh_token": newrt, "expiry": expiry}
    return json.dumps(tok, ensure_ascii=False), newrt, str(d.get("sub") or ""), at


def pikpak_token_json(rt):
    """兼容旧调用：只要 (token JSON, 新 refresh_token)"""
    tok, newrt, _sub, _at = pikpak_token_full(rt)
    return tok, newrt


def pikpak_me(access_token):
    """用 access_token 查 /v1/user/me，返回 (sub, email)；失败返回 ("", "")"""
    at = str(access_token or "")
    if not at:
        return "", ""
    try:
        req = urllib.request.Request("https://user.mypikpak.net/v1/user/me",
                                     headers={"Authorization": "Bearer " + at, "User-Agent": PK_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        return str(d.get("sub") or ""), str(d.get("email") or "")
    except Exception:
        return "", ""


def pikpak_signin(username, password, device_id=None):
    """账号密码直登换 token。返回 (token JSON, 新 refresh_token, 实际使用的 device_id)

    PikPak 的 signin 接口强制 captcha：先申请 captcha_token，再带 X-Captcha-Token 登录。
    风控判定为 review 时会返回 4002 captcha_invalid（要求人工过验证码），
    这种情况下服务器端无法自动完成，需要改用浏览器环境提取。"""
    user = str(username or "").strip()
    pw = str(password or "")
    if not user or not pw:
        raise RuntimeError("账号和密码都不能为空")
    did = str(device_id or "").strip() or uuid.uuid4().hex
    try:
        cap_body = json.dumps({"client_id": PIKPAK_CLIENT_ID,
                               "action": "POST:/v1/auth/signin",
                               "device_id": did,
                               "meta": {"email": user, "password": pw}}).encode()
        cap_req = urllib.request.Request(PIKPAK_CAPTCHA_URL, data=cap_body, method="POST",
                                        headers={"Content-Type": "application/json",
                                                 "User-Agent": PK_UA})
        with urllib.request.urlopen(cap_req, timeout=25) as r:
            cap = json.loads(r.read().decode("utf-8", "ignore"))
        cap_token = str(cap.get("captcha_token") or "")
    except Exception as e:
        raise RuntimeError("申请 captcha_token 失败：%s" % mask_secret(str(e)[:120]))
    if not cap_token:
        raise RuntimeError("PikPak 未返回 captcha_token，无法登录")
    body = json.dumps({"client_id": PIKPAK_CLIENT_ID, "grant_type": "password",
                       "username": user, "password": pw}).encode()
    req = urllib.request.Request(PIKPAK_SIGNIN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": PK_UA,
                                          "X-Captcha-Token": cap_token,
                                          "X-Device-Id": did})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        code, desc = "", mask_secret(raw[:160])
        try:
            j = json.loads(raw)
            code = str(j.get("error_code") or j.get("code") or "")
            desc = mask_secret(str(j.get("error_description") or j.get("error") or raw)[:160])
        except Exception:
            pass
        if code == "4002" or "captcha" in desc.lower():
            raise RuntimeError("PikPak 要求人工验证码（code=%s）。账号密码无法在服务器端全自动登录，"
                               "请用「⚡ 全自动获取」从本机浏览器提取，或先在浏览器登录该账号" % (code or "-"))
        if "password" in desc.lower() or "account" in desc.lower() or code in ("2", "3", "4001"):
            raise RuntimeError("账号或密码不正确（code=%s）" % (code or "-"))
        raise RuntimeError("登录失败（HTTP %s, code=%s）：%s" % (e.code, code or "-", desc))
    except Exception as e:
        raise RuntimeError("连接 PikPak 登录接口失败：%s" % mask_secret(str(e)[:120]))
    at = str(d.get("access_token") or "")
    if not at:
        raise RuntimeError("PikPak 未返回 access_token：%s" % mask_secret(json.dumps(d, ensure_ascii=False)[:150]))
    newrt = str(d.get("refresh_token") or "")
    if not newrt:
        raise RuntimeError("PikPak 未返回 refresh_token")
    try:
        exp_s = int(d.get("expires_in") or 3600)
    except Exception:
        exp_s = 3600
    expiry = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + max(60, exp_s - 60)))
    tok = {"access_token": at, "token_type": d.get("token_type") or "Bearer",
           "refresh_token": newrt, "expiry": expiry}
    return json.dumps(tok, ensure_ascii=False), newrt, did


def verify_list(name):
    """真正实例化该 remote 并列根目录，返回 (ok, 错误信息)"""
    try:
        rc_ok("operations/list", fs=name + ":", remote="")
        return True, ""
    except Exception as e:
        return False, str(e)[:250]


def norm_acct(n):
    """账号名归一化，用于把 Token 面板的环境名对齐到控制台账号名
    edge-分类3(bai) -> 分类3bai ; edge-合集 -> 合集"""
    s = re.sub(r"^edge-", "", str(n or "").strip(), flags=re.I)
    s = re.sub(r"[^\w\u4e00-\u9fff-]", "", s)
    return s.lower()


# ---------- 账号身份表：控制台账号名 ↔ PikPak 身份(sub/邮箱) ----------
# rclone 的 pikpak 配置里没有 email 字段（写未知参数有风险），所以身份单独存这个 JSON。
# 有了它就【不再依赖哪台电脑的浏览器环境名】：换电脑后 token 靠 sub 自动对上号。
def load_meta():
    d = _read_json(_meta_path(), {})
    return d if isinstance(d, dict) else {}


def save_meta(m):
    try:
        _write_json(_meta_path(write=True), m, quiet=True)
    except Exception:
        pass


def meta_set(name, sub="", email=""):
    if not name:
        return
    with LOCK:
        m = load_meta()
        e = m.get(name) if isinstance(m.get(name), dict) else {}
        if sub:
            e["sub"] = sub
        if email:
            e["email"] = email
        if not e:
            return
        e["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        m[name] = e
        save_meta(m)


# ---------- PikPak 凭证生命周期 ----------
# PikPak 的 refresh_token 是「一次性轮换凭证」：每次续期会换回一份新的，旧的立即作废；
# 长时间不活动（约 7~30 天）、换 IP / 换设备 / 改密码 / 风控也会直接吊销。
# rclone 的 pikpak 后端只维护 token 字段（一整段 OAuth JSON），续期时把轮换后的新
# refresh_token 写回 token 内部；但控制台早期额外镜像的顶层 refresh_token 字段
# rclone 不会更新。继续读顶层就会拿一份已作废的旧凭证去换发，轻则 invalid_grant，
# 重则把账号凭证写坏。因此一律以 token 内部为唯一真源。
CRED_STALE_DAYS = 20          # 距上次成功验证超过这个天数就预警（保守取 7~30 天下限之上）
CRED_KEEPALIVE_HOURS = 12    # 保活间隔：让 rclone 自己续期，避免长期闲置导致 rt 过期


def cfg_token_obj(cfg):
    try:
        d = json.loads(str((cfg or {}).get("token") or "{}"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def cfg_rt(cfg):
    """refresh_token 单一真源：优先取 rclone 轮换后写回的 token.refresh_token。"""
    cfg = cfg or {}
    rt = str(cfg_token_obj(cfg).get("refresh_token") or "").strip()
    return rt or str(cfg.get("refresh_token") or "").strip()


def meta_patch(name, fields):
    """给身份表追加任意字段（lastOk / lastErr / cred 等），保留已有 sub / email。"""
    if not name or not isinstance(fields, dict):
        return
    with LOCK:
        m = load_meta()
        e = m.get(name) if isinstance(m.get(name), dict) else {}
        e.update(fields)
        e["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        m[name] = e
        save_meta(m)


def sync_rt_mirror(name, cfg=None):
    """把 token 内轮换后的新 refresh_token 同步到顶层镜像，避免两份凭证分叉。"""
    try:
        c = cfg if isinstance(cfg, dict) else (rc("config/get", name=name) or {})
        tok_rt = str(cfg_token_obj(c).get("refresh_token") or "").strip()
        top_rt = str(c.get("refresh_token") or "").strip()
        if not tok_rt or tok_rt == top_rt:
            return False
        rc("config/update", name=name, type="pikpak",
           parameters=json.dumps({"refresh_token": tok_rt}, ensure_ascii=False), obscure="true")
        return True
    except Exception:
        return False


def purge_ghost_sections(names=None):
    """v3.51 清掉 rclone 里「没有 type」的空壳配置段。

    成因：pikpak 后端会把 captcha_token 回写到配置段；账号改名 / 删除后，
    旧名字仍可能被回写，凭空生成一个只有 captcha_token、没有 type 的段。
    rclone 的两个 rcd 进程（5572 直连 / 5573 代理）各持有一份内存配置，
    任何一次 SaveConfig 都会把这份残留原样写回 rclone.conf，于是「删掉或改名后
    一点测试，旧账号又原样冒出来」。这类段 rclone 无法实例化，也不该进账号列表。
    只清真空壳（没有 type，也没有 token / refresh_token / username），避免误伤。
    """
    removed = []
    for proxy in (False, True):
        try:
            conf = rc("config/dump", _proxy=proxy, _timeout=20)
        except Exception:
            continue
        if not isinstance(conf, dict):
            continue
        for k, v in list(conf.items()):
            if not isinstance(v, dict) or str(v.get("type") or "").strip():
                continue
            if names is not None and k not in names:
                continue
            if v.get("token") or v.get("refresh_token") or v.get("username"):
                continue
            try:
                rc("config/delete", name=k, _proxy=proxy, _timeout=20)
                if k not in removed:
                    removed.append(k)
            except Exception:
                pass
    return removed

def cred_health(name, cfg, meta=None):
    """纯本地评估凭证健康度：不发起网络请求、不轮换 token、不回传任何凭证内容。"""
    mm = load_meta() if meta is None else meta
    m = mm.get(name) if isinstance(mm.get(name), dict) else {}
    cfg = cfg or {}
    tok = cfg_token_obj(cfg)
    has_rt = bool(cfg_rt(cfg))
    top_rt = str(cfg.get("refresh_token") or "").strip()
    tok_rt = str(tok.get("refresh_token") or "").strip()
    drift = bool(tok_rt and top_rt and tok_rt != top_rt)
    expiry = str(tok.get("expiry") or "")
    try:
        last_ok = int(m.get("lastOk") or 0)
    except Exception:
        last_ok = 0
    last_err = str(m.get("lastErr") or "")
    days = int((time.time() - last_ok) / 86400) if last_ok else None
    low = last_err.lower()
    # v3.38：no username 也是「凭证缺失、rclone 回退到直登」的表现，
    # 以前不在这张表里，所以会出现绿徽标「凭证正常」+ 红字 no username 的自相矛盾。
    cred_bad = any(k in low for k in ("invalid_grant", "invalid_token", "unauthorized",
                                      "refreshed", "失效", "captcha", "验证", "401",
                                      "no username", "no auth", "credential", "password"))
    if last_err and cred_bad:
        state, note = "dead", "最近一次验证失败，需要重新登录采集：" + last_err[:80]
    elif not has_rt and not tok.get("access_token"):
        state, note = "dead", "没有可用的 refresh_token / access_token，请重新登录采集"
    elif drift:
        state, note = "warn", "凭证已被 rclone 轮换，控制台镜像尚未同步，建议点「测试」"
    elif days is None:
        state, note = "unknown", "尚未验证过凭证，点「测试」或等待自动保活"
    elif days >= CRED_STALE_DAYS:
        state, note = "warn", "已 %d 天未成功验证，refresh_token 可能已过期" % days
    else:
        state, note = "ok", "凭证正常（%s已成功验证）" % ("今天" if days == 0 else "%d 天前" % days)
    return {"state": state, "note": note, "days": days, "lastOk": last_ok,
            "lastErr": last_err[:120], "expiry": expiry, "drift": drift, "hasRt": has_rt}


def cred_keepalive_once(names=None):
    """跑一轮保活。故意只调 rclone 的 operations/list：access_token 过期时由 rclone
    用自己的 refresh_token 续期并落盘，它是唯一持有者，不会和别的客户端互相顶掉。
    绝不在这里直接调 /v1/auth/token —— 那会让旧凭证立刻作废，并发时极易写坏账号。"""
    try:
        conf = rc("config/dump", _timeout=30)
    except Exception as e:
        return [{"name": "-", "ok": False, "msg": "读取配置失败：" + str(e)[:120]}]
    targets = [n for n, v in conf.items() if str((v or {}).get("type")) == "pikpak"]
    if names:
        want = {str(x) for x in names}
        targets = [n for n in targets if n in want]
    out = []
    for n in sorted(targets):
        try:
            ok, err = pikpak_verify(n)
            if ok:
                meta_patch(n, {"lastOk": int(time.time()), "lastErr": "", "cred": "ok"})
            else:
                meta_patch(n, {"lastErr": str(err)[:200], "lastCheck": int(time.time())})
            sync_rt_mirror(n)
            out.append({"name": n, "ok": ok, "msg": (err or "连接正常")[:120]})
        except Exception as e:
            out.append({"name": n, "ok": False, "msg": str(e)[:120]})
        time.sleep(0.4)
    return out


def cred_keepalive_loop():
    """后台保活：启动后先等 3 分钟避开启动期的任务调度，之后每 12 小时一轮。"""
    time.sleep(180)
    while True:
        try:
            cred_keepalive_once()
        except Exception:
            pass
        time.sleep(CRED_KEEPALIVE_HOURS * 3600)


def parse_import_text(txt):
    """解析粘贴导入的文本 → [{env, refresh_token}]。支持：
         demo-account   os.xxxxxx          （账号名 + 空格/制表符/|/逗号 + token）
         demo@example.com  os.xxxxxx   （邮箱当名字也行）
         os.xxxxxx                       （只有 token，靠 sub/邮箱/新建来对号）
       也接受旧面板导出的 JSON（[{"env":..,"refresh_token":..}] 或 {"rows":[..]}）"""
    raw = str(txt or "").strip()
    if not raw:
        return []
    if raw[0] in "[{":
        try:
            d = json.loads(raw)
            arr = d if isinstance(d, list) else (d.get("rows") or d.get("items") or [])
            out = []
            for x in arr:
                if isinstance(x, dict) and str(x.get("refresh_token") or "").strip():
                    out.append({"env": str(x.get("env") or x.get("name") or "")[:64],
                                "sub": str(x.get("sub") or ""),
                                "email": str(x.get("email") or ""),
                                "refresh_token": str(x["refresh_token"]).strip()})
            return out[:60]
        except Exception:
            pass
    items = []
    for line in raw.splitlines():
        s = line.strip().strip(",;")
        if not s or s.startswith("#") or s.startswith("//") or s.startswith("|"):
            continue
        s = s.replace("\t", " ").replace("：", " ")
        parts = [p.strip().strip("`\"'") for p in re.split(r"[\s|,;]+", s) if p.strip()]
        tok, rest = "", []
        for p in parts:
            if not tok and len(p) >= 25 and re.match(r"^[A-Za-z0-9_.\-]+$", p):
                tok = p
            else:
                rest.append(p)
        if not tok:
            continue
        items.append({"env": " ".join(rest).strip()[:64], "refresh_token": tok})
    return items[:60]


def import_items(items, create=False, dry=False):
    """把一批 {env, sub, email, refresh_token} 写进 rclone 配置并实测。
    匹配顺序：PikPak sub → 邮箱 → 环境名归一化 →（换发后用真实 sub 校正）→ 新建。
    安全要点：token 换发返回的 sub 才是权威身份。若采集到的身份标记与 token 实际身份
    不一致，一律以 token 真实身份为准，绝不把 A 的凭证写进 B 的配置。"""
    try:
        conf = rc_ok("config/dump")
    except Exception as e:
        return {"error": str(e)[:150]}
    pik = {k: (v or {}) for k, v in conf.items() if str((v or {}).get("type")) == "pikpak"}
    by_name = {norm_acct(k): k for k in pik}
    by_sub, by_mail = {}, {}
    meta = load_meta()
    for k, v in meta.items():
        if k not in pik or not isinstance(v, dict):
            continue
        if v.get("sub"):
            by_sub[str(v["sub"])] = k
        if v.get("email"):
            by_mail[str(v["email"]).lower()] = k
    # 先过滤非法元素再截断，避免列表里混入垃圾就少处理一条
    valid = [it for it in items if isinstance(it, dict)][:60]
    results = []
    for it in valid:
        env = str(it.get("env") or it.get("name") or "").strip()
        rt = str(it.get("refresh_token") or "").strip()
        sub = str(it.get("sub") or "").strip()
        mail = str(it.get("email") or "").strip().lower()
        target = (by_sub.get(sub, "") if sub else "") or (by_mail.get(mail, "") if mail else "")
        if not target and env:
            target = by_name.get(norm_acct(env), "")
        if target and target not in pik:
            target = ""
        label = env or sub or mail or "-"
        if not rt:
            results.append({"env": label, "target": target or "-", "action": "更新" if target else "新建",
                            "ok": False, "msg": "这条没有 refresh_token"})
            continue
        if dry:
            # 预演必须和真实执行一致：未开启自动新建时不能谎报「将新建」
            if target:
                act, ok, msg = "更新", True, "将更新"
            elif create:
                act, ok, msg = "新建", True, "将新建（自动新建已开启）"
            else:
                act, ok, msg = "无匹配", False, "控制台没有对应账号（勾选「自动新建」可自动创建）"
            results.append({"env": label, "target": target or "-", "action": act,
                            "ok": ok, "msg": msg})
            continue
        try:
            tok_json, newrt, sub2, at2 = pikpak_token_full(rt)
        except RuntimeError as e:
            msg = str(e)
            rotated = ("refreshed by other" in msg.lower()
                       or ("已被别处刷新" in msg and "invalid" not in msg.lower()))
            if target and rotated:
                # 送来的 token 已被别处轮换，但控制台手里那份可能还能用
                ok_now, err_now = pikpak_verify(target)
                if ok_now:
                    results.append({"env": label, "target": target, "action": "跳过", "ok": True,
                                    "msg": "控制台凭证正常，无需更新（这份 token 已被别处轮换）"})
                else:
                    results.append({"env": label, "target": target, "action": "更新", "ok": False,
                                    "msg": "token 已过期且控制台凭证也失效，请在浏览器重新登录该账号后再采集"})
                continue
            if not target:
                msg = ("无法识别是哪个账号（来源：%s），且这份 token 已失效 —— "
                       "请在该浏览器环境里重新登录 PikPak 后再采集" % label)
            results.append({"env": label, "target": target or "-", "action": "更新" if target else "识别失败",
                            "ok": False, "msg": msg[:180]})
            continue

        # ---- 身份防错配：换发拿到的真实 sub 才是权威 ----
        note = ""
        real_target = by_sub.get(sub2, "") if sub2 else ""
        if real_target and real_target in pik:
            if target and target != real_target:
                note = "采集到的身份标记与 token 实际身份不一致，已按 token 真实身份改写到 %s；" % real_target
            target = real_target
        elif sub2 and target:
            known = str((meta.get(target) or {}).get("sub") or "")
            if known and known != sub2:
                results.append({"env": label, "target": target, "action": "拒绝", "ok": False,
                                "msg": "这个 token 属于另一个 PikPak 账号（身份不匹配），已拒绝写入 %s，避免覆盖已有凭证" % target})
                continue
            by_sub.setdefault(sub2, target)
        elif sub2:
            by_sub.setdefault(sub2, "")

        if not target:
            if not create:
                results.append({"env": label, "target": "-", "action": "无匹配", "ok": False,
                                "msg": "控制台没有对应账号（勾选「自动新建」可自动创建）"})
                continue
            base = re.sub(r"[^\w\u4e00-\u9fff-]", "", norm_acct(env)) if env else ""
            # 采集器传来的 env-xxxx / clean-login 都只是来源标记，不能拿来当账号名
            if re.match(r"^(?:env-?[0-9a-f]{6,12}|clean-login|pikpak-login)$", base, re.I):
                base = ""
            # 优先用采集到的邮箱命名（可读、稳定），换发接口拿不到邮箱时再兜底
            if not base and mail:
                base = re.sub(r"[^\w\u4e00-\u9fff-]", "", mail.split("@")[0])
            if not base:
                _s, m2 = pikpak_me(at2)
                if m2:
                    base = re.sub(r"[^\w\u4e00-\u9fff-]", "", m2.split("@")[0])
            target = (base or ("pikpak-" + uuid.uuid4().hex[:6]))[:64]
        act = "更新" if target in pik else "新建"
        params = {"token": tok_json, "refresh_token": newrt}
        try:
            rc("config/create" if act == "新建" else "config/update", name=target, type="pikpak",
               parameters=json.dumps(params, ensure_ascii=False), obscure="true")
        except Exception:
            pass  # 后端实例化校验报错时配置通常已落盘，以实测为准
        ok, err = pikpak_verify(target)
        if ok:
            sid, mail2 = sub2, ""
            if not sub2 or sub2 not in by_sub:
                sid, mail2 = pikpak_me(at2)      # 顺手记住身份，下次换电脑也能自动对号
            meta_set(target, sub2 or sid, mail2 or mail)
            meta_patch(target, {"lastOk": int(time.time()), "lastErr": "", "cred": "ok"})
            meta.setdefault(target, {})
            pik[target] = pik.get(target) or {}
            if sub2 or sid:
                by_sub.setdefault(sub2 or sid, target)
            if mail2 or mail:
                by_mail.setdefault((mail2 or mail).lower(), target)
        # 轮换后的新 refresh_token 只留在后端，绝不回传到浏览器（结果页只需要一个布尔）
        results.append({"env": label, "target": target, "action": act, "ok": ok,
                        "rtUpdated": bool(ok and newrt != rt),
                        "msg": (note + (err or "已%s并连通" % act))[:180]})
    return {"results": results, "okCount": sum(1 for r in results if r["ok"]), "total": len(results)}


def pikpak_verify(name, allow_repair=True):
    """pikpak 实测连接；captcha 风控自动换 device_id，瞬时网络抖动再重试一次。
    批量验证时 PikPak 偶尔会返回一次连接失败，单次失败不足以判定账号失效。"""
    ok, err = verify_list(name)
    if ok or not allow_repair:
        return ok, err
    note = ""
    if "captcha" in err.lower():
        try:
            rc("config/update", name=name, type="pikpak",
               parameters=json.dumps({"device_id": uuid.uuid4().hex, "captcha_token": ""}))
        except Exception:
            pass
        note = "已自动更换 device_id 后"
        ok, err = verify_list(name)
        if ok:
            return True, note + "连通（原 device_id 被 PikPak 风控）"
    time.sleep(0.6)
    ok2, err2 = verify_list(name)
    if ok2:
        return True, note + "重试后连通" if note else "首次连接瞬时失败，重试后连通"
    return False, err2 or err


def rclone_cli(args, proxy=False, timeout=120):
    """一次性 rclone CLI 进程（可带代理环境变量），与 rc 守护进程共用同一份配置"""
    cmd = ["docker", "exec"]
    if proxy:
        cmd += proxy_env_args()
    cmd += [RCLONE_CONTAINER, "rclone"] + args
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()

def load_jobs():
    d = _read_json(JOBS_FILE, [])
    return d if isinstance(d, list) else []

def save_jobs(jobs):
    _write_json(JOBS_FILE, jobs)

def _truthy(v):
    """布尔字段兼容 0/1、true/false 字符串，避免 "false" 被 bool() 判成 True。"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _norm_settings(raw):
    """统一钳制设置：老 settings.json 缺字段、或前端传了 null / 越界值都不能拖垮接口。
    每个字段独立兜底，避免旧版 try 里一处异常就把全部设置回滚成默认值。"""
    d = dict(DEFAULTS)
    d.update(raw or {})
    def num(key, lo, hi, cast=int):
        try:
            v = cast(d.get(key))
        except Exception:
            v = cast(DEFAULTS[key])
        d[key] = max(lo, min(hi, v))
    num("maxConcurrent", 1, 6)
    num("diskMinGB", 0, 10000, float)
    num("diskQuotaGB", 0, 10000000, float)
    num("transfers", 1, 12)
    num("checkers", 1, 16)
    num("multiThreadStreams", 1, 32)
    num("multiThreadCutoffMB", 1, 1024)
    num("retries", 0, 100)
    num("transfersClearedAt", 0, 4102444800, float)
    d["proxy"] = bool(d.get("proxy"))
    d["downloadProxy"] = bool(d.get("downloadProxy", True))
    d["advancedMode"] = _truthy(d.get("advancedMode", False))
    d["shareAutoCancel"] = _truthy(d.get("shareAutoCancel", False))
    # v3.77 全局网络模式：老配置没有 netMode 时按 pkProxyMode / proxyUrl 迁移。
    # 注意必须看 raw 而不是 d —— d 已被 DEFAULTS 补成 direct，会盖掉迁移判断。
    m = str((raw or {}).get("netMode") or "").strip().lower()
    if m not in ("direct", "proxy"):
        old = str(d.get("pkProxyMode") or "follow").strip().lower()
        if old in ("direct", "proxy"):
            m = old
        else:
            raw = str(d.get("proxyUrl") or "").strip()
            m = "direct" if (not raw or raw.lower() in PROXY_OFF_WORDS) else "proxy"
    d["netMode"] = m
    d["pkProxyMode"] = m          # 兼容镜像，请求层不再读取
    d["bwLimitOn"] = bool(d.get("bwLimitOn"))
    d["createEmptySrcDirs"] = bool(d.get("createEmptySrcDirs", True))
    d["autoStart"] = str(d.get("autoStart") or "")[:5]
    d["autoPause"] = str(d.get("autoPause") or "")[:5]
    d["orderBy"] = str(d.get("orderBy") or "")
    if d["orderBy"] not in ORDER_BY_OPTIONS:
        d["orderBy"] = DEFAULTS["orderBy"]
    d["logLevel"] = str(d.get("logLevel") or "INFO").upper()
    if d["logLevel"] not in LOG_LEVELS:
        d["logLevel"] = "INFO"
    for k in ("timeout", "idleTimeout"):
        v = str(d.get(k) or DEFAULTS[k]).strip()
        d[k] = v if DUR_RE.match(v) else DEFAULTS[k]
    v = str(d.get("bwLimit") or DEFAULTS["bwLimit"]).strip()
    d["bwLimit"] = v if BW_RE.match(v) else DEFAULTS["bwLimit"]
    d["logFile"] = str(d.get("logFile") or "")[:200]
    # v3.49：主页地址会直接写进 <a href>，只放行 http(s) 绝对地址，挡掉 javascript: / data: 等伪协议
    v = str(d.get("nasHomeUrl") or DEFAULTS["nasHomeUrl"]).strip()[:300]
    if not re.match(r"^https?://[^\s<>\x22\x27]+$", v, re.I):
        v = DEFAULTS["nasHomeUrl"]
    d["nasHomeUrl"] = v
    # v3.55：代理地址会拼进 docker exec 的 -e 参数，只放行 http(s)/socks5 绝对地址；
    # 空串或 off 表示交给环境变量 / 直连，非法值一律清空，不写进命令行。
    v = str(d.get("proxyUrl") or "").strip()[:200]
    if v and not (v.lower() in PROXY_OFF_WORDS or PROXY_URL_RE.match(v)):
        v = ""
    d["proxyUrl"] = v
    return d


def load_settings():
    s = _read_json(SETTINGS_FILE, {})
    return _norm_settings(s if isinstance(s, dict) else {})

def save_settings(s):
    _write_json(SETTINGS_FILE, s)

def basename_of(path):
    p = path.rstrip("/").split("/")[-1] if path.strip("/") else path.split(":")[0]
    return p or path.split(":")[0]

def dedupe_dest(dest, fs=None):
    """目标目录重名避让：列出 fs 根下已有目录名，撞名就追加 -2 / -3。
    v3.57 支持 fs 参数（多存储空间后每个卷各查各的根）。"""
    fs = fs or RCLONE_DATA_ROOT
    try:
        r = rc("operations/list", fs=fs, remote="")
        names = {e.get("Name", "") for e in r.get("list", [])}
    except Exception:
        return dest
    if dest not in names:
        return dest
    i = 2
    while "%s-%d" % (dest, i) in names:
        i += 1
    return "%s-%d" % (dest, i)

def _job_config(s):
    """把设置页的高级参数翻译成 rclone rc 的 `_config`（Go 内部字段名，驼峰）。

    rclone 的 job.NewJob 会执行 rc.AddConfig(ctx, in)，把 `_config` 覆盖到**这一次任务**
    的 fs.Config，不会污染 rcd 守护进程的全局配置，也不需要重启容器。已用
    `rclone rc options/local _config=...` 在 v1.75.2 上逐字段验证过。
    注意两处架构限制：--log-file 属于 log 块（进程级启动参数，无法按任务注入），
    rclone 1.75 也没有 --idle-timeout，改用等价的 ConnectTimeout 覆盖连接空闲。
    v3.37：advancedMode=False 时只保留基础并发数，其余字段一律不注入，
    让 rclone 用自身默认值启动（设置页的高级参数值仍保留，随时可以再打开）。"""
    cfg = {
        "Transfers": int(s["transfers"]),
        "Checkers": int(s["checkers"]),
    }
    if not s.get("advancedMode", False):
        return cfg
    cfg.update({
        "MultiThreadStreams": int(s["multiThreadStreams"]),
        "MultiThreadCutoff": int(s["multiThreadCutoffMB"]) * 1048576,
        "Retries": int(s["retries"]),
        "Timeout": s["timeout"],
        "ConnectTimeout": s["idleTimeout"],
        "LogLevel": s["logLevel"],
    })
    if s["orderBy"]:
        cfg["OrderBy"] = s["orderBy"]
    if s["bwLimitOn"] and s["bwLimit"] and s["bwLimit"].lower() != "off":
        cfg["BwLimit"] = s["bwLimit"]
    return cfg


# ── 下载过滤（v3.43）───────────────────────────────────────────────
# 前端只传「人话」：跳过 0 字节、最小/最大大小、仅含扩展名、排除扩展名、排除关键词；
# 后端翻译成 rclone 的 filter.Opt，通过 rc 的 `_filter` 参数按任务注入 ——
# rclone 在 job.NewJob 里 AddConfig，只作用于这一次任务，不改守护进程全局配置。
# 已在 rclone v1.75.2 上验证 MinSize / MaxSize / IncludeRule / ExcludeRule 可用。
FILTER_SIZE_RE = re.compile(r"^\d+(?:\.\d+)?[bBkKmMgGtT]?$")
FILTER_MAX_RULES = 24


def _filter_parts(v):
    """接受字符串（逗号/顿号/分号分隔）或数组，返回去空的片段。"""
    if isinstance(v, str):
        raw = re.split(r"[,，、;；\n]+", v)
    elif isinstance(v, (list, tuple)):
        raw = [str(x) for x in v]
    else:
        return []
    return [x.strip().strip("'\"").strip() for x in raw if str(x).strip()]


def _filter_ext_rules(v):
    """扩展名 → rclone glob：mkv / .mkv / *.mkv 归一化成 *.mkv。"""
    out, seen = [], set()
    for s in _filter_parts(v):
        s = s.lstrip("*").lstrip(".").strip().lower()
        if not s or "/" in s or "\\" in s or len(s) > 20:
            continue
        rule = "*." + s
        if rule not in seen:
            seen.add(rule)
            out.append(rule)
    return out[:FILTER_MAX_RULES]


def _filter_kw_rules(v):
    """关键词 → rclone glob：sample 归一化成 *sample*（不区分大小写由 rclone 处理）。"""
    out, seen = [], set()
    for s in _filter_parts(v):
        if not s or len(s) > 50:
            continue
        rule = "*" + s + "*"
        if rule.lower() not in seen:
            seen.add(rule.lower())
            out.append(rule)
    return out[:FILTER_MAX_RULES]


def norm_download_filter(f):
    """规范前端过滤条件；没有任何有效规则时返回 {}（= 不加过滤，行为同旧版）。"""
    if not isinstance(f, dict):
        return {}
    out = {}
    for k in ("minSize", "maxSize"):
        v = str(f.get(k) or "").strip()
        if v and FILTER_SIZE_RE.match(v):
            out[k] = v
    if f.get("skipEmpty"):
        out["skipEmpty"] = True
    for k in ("incExt", "excExt", "excKw"):
        rules = _filter_kw_rules(f.get(k)) if k == "excKw" else _filter_ext_rules(f.get(k))
        if rules:
            out[k] = rules
    return out


def build_filter_opt(f):
    """过滤条件 → rclone filter.Opt（dict）。无规则返回 None = 不注入 _filter。"""
    if not f:
        return None
    opt = {}
    mn = str(f.get("minSize") or "")
    mx = str(f.get("maxSize") or "")
    if f.get("skipEmpty") and not mn:
        mn = "1"                      # 0 字节多半是占位文件或断流留下的空壳
    if mn:
        opt["MinSize"] = mn
    if mx:
        opt["MaxSize"] = mx
    inc = list(f.get("incExt") or [])
    exc = list(f.get("excExt") or []) + list(f.get("excKw") or [])
    if inc:
        opt["IncludeRule"] = inc
    if exc:
        opt["ExcludeRule"] = exc
    return opt or None


def describe_filter(f):
    """把过滤条件写成一句人话，任务卡片 / 详情弹窗直接显示，避免用户忘了自己加过规则。"""
    if not f:
        return ""
    bits = []
    if f.get("skipEmpty"):
        bits.append("跳过 0 字节")
    if f.get("minSize"):
        bits.append("≥ %s" % f["minSize"])
    if f.get("maxSize"):
        bits.append("≤ %s" % f["maxSize"])
    if f.get("incExt"):
        bits.append("仅 " + "/".join(x[2:] for x in f["incExt"]))
    if f.get("excExt"):
        bits.append("排除 " + "/".join(x[2:] for x in f["excExt"]))
    if f.get("excKw"):
        bits.append("排除关键词 " + "/".join(x[1:-1] for x in f["excKw"]))
    return " · ".join(bits)


def launch_job(job, s):
    proxy = bool(job.get("proxy"))
    if proxy and not ensure_proxy_rc():
        job["state"] = "error"
        job["error"] = "代理守护进程（%d）启动失败，请检查代理客户端与 %s 容器" % (PROXY_RC_PORT, RCLONE_CONTAINER)
        return False
    move = job["mode"] == "move"
    if job.get("single_file"):
        # sync/copy 的 srcFs/dstFs 都必须是目录，单文件必须用专用接口；
        # src_dir / dst_dir / leaf 由 start 在预检判定为文件后写进 job。
        method = "operations/movefile" if move else "operations/copyfile"
        kv = dict(srcFs=job["src_dir"], srcRemote=job["leaf"],
                  dstFs=job["dst_dir"], dstRemote=job["leaf"], _async="true")
    else:
        method = "sync/move" if move else "sync/copy"
        kv = dict(srcFs=job["src"], dstFs=job["dest"], _async="true",
                  transfers=max(1, int(s["transfers"])), checkers=max(1, int(s["checkers"])))
        if move:
            kv["deleteEmptySrcDirs"] = "true"
        # 保留空目录也是高级参数：关闭高级模式时不传，交给 rclone 默认值
        if s.get("advancedMode", False):
            kv["createEmptySrcDirs"] = "true" if s.get("createEmptySrcDirs", True) else "false"
    # 单文件与目录任务都按同一套基础参数注入（--multi-thread-streams 对单文件最有用）
    kv["_config"] = json.dumps(_job_config(s))
    # v3.43 下载过滤：只作用于目录任务。单文件是用户明确点下的那一个，
    # 再被过滤掉只会让人以为「点了没反应」，所以这里直接不注入。
    fopt = build_filter_opt(job.get("filter") or {})
    if fopt and not job.get("single_file"):
        kv["_filter"] = json.dumps(fopt, ensure_ascii=False)
    try:
        r = rc(method, _proxy=proxy, **kv)
        jid = r.get("jobid")
        if not jid:
            raise RuntimeError(json.dumps(r)[:150])
        job["jobid"] = jid
        job["started_ts"] = time.time()   # 本次运行起点（epoch），供日志窗口解析已完成传输
        job["state"] = "running"
        job["error"] = ""        # 清掉上次「源目录已空」等残留提示，避免误导
        job["curfile"] = ""
        return True
    except Exception as e:
        job["state"] = "error"
        job["error"] = "启动失败: %s" % str(e)[:150]
        return False

def track_transferred(job, transferring):
    """core/stats 不提供『已完成文件』历史，只能靠轮询 transferring 列表的差异推断：
    某个文件上一轮还在传输、这一轮消失了，即视为本次运行已完成（或任务结束）。
    用于统计页『本次运行已完成传输』展示。按文件名去重，保证每个文件只记一次。"""
    tr_list = job.setdefault("transferred", [])
    seen = {t.get("name") for t in tr_list}
    cur = {}
    for t in (transferring or []):
        if isinstance(t, dict) and t.get("name"):
            cur[t["name"]] = int(t.get("size") or t.get("bytes") or 0)
    # 上一轮在传、这一轮不在了 -> 完成，记录一次（finished 分支传 [] 即全部冲刷为已完成）
    for name, size in (job.get("_xf") or {}).items():
        if name not in cur and name not in seen:
            tr_list.append({"name": name, "size": size})
            seen.add(name)
    job["_xf"] = cur

# 上游明确拒绝、重试也解决不了的业务错误。命中后必须让任务脱离 running，
# 否则 job_live 每轮都去问 job/status，任务会永远显示「运行中」却 0 速度、0 字节。
# 瞬时网络抖动（TLS handshake / EOF / i/o timeout / connection reset）不在此列，
# 保持 running 让 rclone 自行重试。
# 元组：(错误关键字, 终态, 面向用户的提示)
FATAL_JOB_ERRORS = (
    ("daily_download_size_exceeded", "stopped", "PikPak 当日下载额度已用尽，额度恢复后点「继续」续传"),
    ("download_size_exceeded", "stopped", "PikPak 下载额度已用尽，额度恢复后点「继续」续传"),
    ("storage_quota_exceeded", "stopped", "存储空间不足，处理后点「继续」续传"),
    ("captcha", "error", "PikPak 触发人工验证风控，请到账号页点该账号的「重新登录」，在官方登录页过完安全验证后重新采集 Token"),
    ("unauthorized", "error", "账号鉴权失效，请到账号页点该账号的「重新登录」重新采集 Token"),
    ("invalid_grant", "error", "账号凭证已失效，请到账号页点该账号的「重新登录」重新采集 Token"),
    ("invalid_token", "error", "账号凭证已失效，请到账号页点该账号的「重新登录」重新采集 Token"),
    # v3.47：旧文案「请在账号页补全用户名」指向了一个并不存在的入口 ——
    # acctadd / import_items 都刻意只写 token+refresh_token，用户名密码会被 pop 掉，
    # 用户照着做也修不好。真实原因是 token 失效后 rclone 回退直登，配置里却没账号密码。
    ("no username", "error", "账号 Token 已失效（rclone 回退直登时缺账号密码），请到账号页点该账号的「重新登录」重新采集"),
    ("account_not_found", "error", "云端账号不存在，请检查账号配置"),
)

# v3.47：凭证类终态要把对应账号立刻标成「需重新登录」（账号徽标变红 + 行内出按钮）。
# 以前这里只列 4 个关键字，漏了 no username / no auth / credential / password，
# 于是排队任务明明报 no username，账号页却还显示「凭证正常」，用户找不到入口。
CRED_FATAL_KEYS = (
    "invalid_grant", "invalid_token", "unauthorized", "captcha",
    "no username", "no password", "no auth", "credential",
    "账号凭据", "账号凭证", "鉴权失效",
)


def _fatal_job_error(msg):
    """返回 (state, 友好提示+原始错误摘要)；非不可恢复错误返回 None。"""
    raw = str(msg or "")
    low = raw.lower()
    for key, state, hint in FATAL_JOB_ERRORS:
        if key in low:
            return state, "%s（原始：%s）" % (hint, mask_secret(raw[:120]))
    return None


# ── PikPak 原生「分享转存」（v3.34）────────────────────────────────────
# 为什么不用 rclone 搬：PikPak 的 backend features 里 ServerSideAcrossConfigs=false，
# 跨账号只能下载再上传（吃 20GB/日下载配额）。而网页端自己的「转存」是纯服务端
# 操作：源账号建分享 → 目标账号 restore，秒级完成、不消耗任何流量配额。
# 这套是网页私有 API，需要 5 分钟有效期的 captcha token；签名算法是
# 客户端ID+版本+包名+设备ID+时间戳 逐轮拼 salt 做 md5（见 _pk_sign）。
# 两个已知行为：
#   · restore 的 parent_id 会被忽略，内容固定落在目标账号的「Pack From Shared」；
#   · 同账号内移动是服务端操作（DirMove=true），所以再用 rclone moveto 归位，零成本。
PK_API = "https://api-drive.mypikpak.com"
PK_CAPTCHA_URL = "https://user.mypikpak.net/v1/shield/captcha/init"
PK_CLIENT_VERSION = "2.0.0"
PK_PACKAGE = "mypikpak.com"
PK_SALT = ["C9qPpZLN8ucRTaTiUMWYS9cQvWOE", "+r6CQVxjzJV6LCV", "F", "pFJRC",
           "9WXYIDGrwTCz2OiVlgZa90qpECPD6olt", "/750aCr4lm/Sly/c", "RB+DT/gZCrbV", "",
           "CyLsf7hdkIRxRm215hl", "7xHvLi2tOYP0Y92b", "ZGTXXxu8E/MIWaEDB+Sm/", "1UI3",
           "E7fP5Pfijd+7K+t6Tg/NhuLq0eEUVChpJSkrKxpO", "ihtqpG6FMt65+Xk+tWUH2",
           "NhXXU9rg4XXdzo7u5o"]
PK_CACHE = {}
PK_LOCK = threading.Lock()


def _pk_sign(dev, ts):
    s = PIKPAK_CLIENT_ID + PK_CLIENT_VERSION + PK_PACKAGE + str(dev) + str(ts)
    for salt in PK_SALT:
        s = hashlib.md5((s + salt).encode()).hexdigest()
    return "1." + s


def _pk_http(url, body=None, headers=None, method="GET", timeout=45, proxy=True):
    """先走 mihomo，失败再直连（两个方向都可能被 PikPak 拦，互为兜底）。

    v3.81：proxy 除 True/False 外还接受一个具体的代理地址字符串，只在本次请求生效，
    用于「分享解析单独走代理、下载仍直连」的双路尝试。显式指定地址时失败**不**
    静默回落直连 —— 否则调用方会误以为代理试过了，其实又走了直连。
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    handlers = []
    p = proxy if isinstance(proxy, str) else (pk_proxy_url() if proxy else "")
    if p:
        handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        if proxy is True:
            return _pk_http(url, body, headers, method, timeout, proxy=False)
        return 0, str(e)


def _pk_cfg(name):
    d = rc("config/dump", _timeout=15)
    c = d.get(name)
    if not isinstance(c, dict):
        raise RuntimeError("账号 %s 不在 rclone 配置里" % name)
    tok = c.get("token") or "{}"
    if isinstance(tok, str):
        try:
            tok = json.loads(tok)
        except Exception:
            tok = {}
    return c, tok


def _rfc3339_ts(v):
    """RFC3339 时间串 → epoch 秒（v3.61）。rclone 写回的 expiry 可能带小数秒或
    时区偏移，老版本直接字符串比较，非 UTC 时区下会误判 token 未过期。
    解析失败返回 None，由调用方退回旧的字典序比较兜底。"""
    txt = str(v or "").strip()
    if not txt:
        return None
    iso = txt[:-1] + "+00:00" if txt.endswith(("Z", "z")) else txt
    try:
        return _dt.fromisoformat(iso).timestamp()
    except Exception:
        return None

def _pk_token(name):
    """access_token + device_id。快过期时先让 rclone 自己刷一次——rclone 会把
    轮换后的 refresh_token 写回配置，避免我们自己换发导致两边凭证分叉。"""
    c, tok = _pk_cfg(name)
    at = str(tok.get("access_token") or "")
    exp = str(tok.get("expiry") or "")
    exp_ts = _rfc3339_ts(exp)
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 150))
    # 优先按时间戳判断；expiry 无法解析时退回旧字典序比较（总比不检查强）
    expired = (exp_ts is not None and exp_ts < time.time() + 150) or               (exp_ts is None and exp and exp < soon)
    if (not at) or expired:
        try:
            subprocess.run(["docker", "exec", RCLONE_CONTAINER, "rclone", "about", name + ":"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        except Exception:
            pass
        c, tok = _pk_cfg(name)
        at = str(tok.get("access_token") or "")
        if not at:
            raise RuntimeError("账号 %s 取不到 access_token，请到账号页重新导入" % name)
    return at, str(c.get("device_id") or "")


def _pk_session(name, action):
    """按 action 缓存 captcha token（有效期 5 分钟，这里保守用 4 分钟）。"""
    key = (name, action)
    now = time.time()
    with PK_LOCK:
        ent = PK_CACHE.get(key)
        if ent and ent["exp"] > now:
            return ent
    at, dev = _pk_token(name)
    ts = str(int(now * 1000))
    body = {"action": action, "captcha_token": "", "client_id": PIKPAK_CLIENT_ID,
            "device_id": dev,
            "meta": {"captcha_sign": _pk_sign(dev, ts), "client_version": PK_CLIENT_VERSION,
                     "package_name": PK_PACKAGE, "timestamp": ts, "user_id": ""}}
    code, raw = _pk_http(PK_CAPTCHA_URL, body,
                         {"Content-Type": "application/json", "User-Agent": PK_UA}, "POST")
    try:
        cap = str(json.loads(raw).get("captcha_token") or "")
    except Exception:
        cap = ""
    if not cap:
        raise RuntimeError("取 PikPak 验证码失败（HTTP %s）：%s" % (code, raw[:120]))
    ent = {"at": at, "dev": dev, "cap": cap, "exp": now + 240}
    with PK_LOCK:
        PK_CACHE[key] = ent
    return ent


def pk_api(name, method, path, body=None, _retry=True, _timeout=45, _proxy=True):
    action = "%s:%s" % (method, path.split("?")[0])
    s = _pk_session(name, action)
    h = {"Content-Type": "application/json", "Authorization": "Bearer " + s["at"],
         "x-device-id": s["dev"], "x-captcha-token": s["cap"], "User-Agent": PK_UA}
    code, raw = _pk_http(PK_API + path, body, h, method, timeout=_timeout, proxy=_proxy)
    try:
        d = json.loads(raw)
    except Exception:
        d = {"_raw": raw[:300]}
    if _retry and isinstance(d, dict) and d.get("error") in ("captcha_invalid", "captcha_expired"):
        with PK_LOCK:
            PK_CACHE.pop((name, action), None)
        return pk_api(name, method, path, body, _retry=False, _timeout=_timeout, _proxy=_proxy)
    return code, d


def _pk_err(code, d):
    d = d or {}
    return str(d.get("error_description") or d.get("error") or d.get("_raw") or
               ("HTTP %s" % code))[:160]


def pk_walk(name, path):
    """云端路径 -> 文件夹 id（逐级查找，中间层必须已存在）"""
    pid = ""
    for seg in [p for p in str(path or "").strip("/").split("/") if p]:
        code, d = pk_api(name, "GET",
                         "/drive/v1/files?parent_id=%s&limit=200&phase=PHASE_TYPE_COMPLETE&thumbnail_size=SIZE_SMALL"
                         % urllib.parse.quote(pid))
        if code >= 400:
            raise RuntimeError(_pk_err(code, d))
        hit = next((f for f in (d.get("files") or [])
                    if f.get("name") == seg and f.get("kind") == "drive#folder"), None)
        if not hit:
            raise RuntimeError("账号 %s 里找不到目录「%s」" % (name, seg))
        pid = hit["id"]
    return pid


def pk_file(name, path):
    """云端文件路径 -> 文件条目（id/size/name）。
    与 pk_walk 的区别：末段必须是文件，parent_id 由前缀逐级解析。"""
    p = str(path or "").strip("/")
    parent, _, leaf = p.rpartition("/")
    pid = pk_walk(name, parent) if parent else ""
    code, d = pk_api(name, "GET",
                     "/drive/v1/files?parent_id=%s&limit=200&phase=PHASE_TYPE_COMPLETE&thumbnail_size=SIZE_SMALL"
                     % urllib.parse.quote(pid))
    if code >= 400:
        raise RuntimeError(_pk_err(code, d))
    hit = next((f for f in (d.get("files") or [])
                if f.get("name") == leaf and f.get("kind") != "drive#folder"), None)
    if not hit:
        raise RuntimeError("账号 %s 里找不到文件「%s」" % (name, p))
    return hit


def _pk_ls(name, path):
    pid = pk_walk(name, path) if str(path or "").strip("/") else ""
    code, d = pk_api(name, "GET",
                     "/drive/v1/files?parent_id=%s&limit=200&phase=PHASE_TYPE_COMPLETE&thumbnail_size=SIZE_SMALL"
                     % urllib.parse.quote(pid))
    if code >= 400:
        raise RuntimeError(_pk_err(code, d))
    return d.get("files") or []


def _pk_ls_all(name, path, max_pages=3, limit=500):
    """列目录（翻页版）。Pack From Shared 这类目录条目多，默认一页会漏掉新文件。"""
    pid = pk_walk(name, path) if str(path or "").strip("/") else ""
    out, token, seen = [], "", set()
    for _ in range(max_pages):
        q = ("/drive/v1/files?parent_id=%s&limit=%d&phase=PHASE_TYPE_COMPLETE&thumbnail_size=SIZE_SMALL"
             % (urllib.parse.quote(pid), limit))
        if token:
            q += "&page_token=" + urllib.parse.quote(token)
        code, d = pk_api(name, "GET", q)
        if code >= 400:
            raise RuntimeError(_pk_err(code, d))
        out.extend(d.get("files") or [])
        token = str(d.get("next_page_token") or "")
        if not token or token in seen:
            break
        seen.add(token)
    return out


# v3.40 缩略图元数据缓存：只在「缩略图视图」下请求，且按目录缓存 90 秒，
# 避免用户来回切页时反复打 PikPak 私有 API 触发 captcha 风控。
THUMB_LOCK = threading.Lock()
THUMB_CACHE = {}
THUMB_TTL = 90


def _pk_thumb_map(name, path):
    """目录条目的封面 / MIME 映射（按文件名索引）。

    失败一律返回空 dict：非 PikPak 账号、凭证失效、captcha 拦截、目录层级过深
    都不会影响 dirs 的正常工作，前端会回退到文件类型图标。
    """
    key = (name, str(path or "").strip("/"))
    now = time.time()
    with THUMB_LOCK:
        ent = THUMB_CACHE.get(key)
        if ent and ent["exp"] > now:
            return ent["map"]
    out = {}
    try:
        c, _ = _pk_cfg(name)
        if str(c.get("type") or "") != "pikpak":
            return {}
        for f in _pk_ls_all(name, key[1], max_pages=5, limit=200):
            nm = str(f.get("name") or "")
            if not nm:
                continue
            u = str(f.get("thumbnail_link") or "")
            # 只放行 PikPak 自家 CDN 的 https 链接，避免外部返回值被直接塞进 <img src>
            host = urllib.parse.urlparse(u).netloc.lower() if u.startswith("https://") else ""
            if not (host == "mypikpak.com" or host.endswith(".mypikpak.com")):
                u = ""
            out[nm] = {"thumb": u,
                       "mime": str(f.get("mime_type") or ""),
                       "kind": "folder" if f.get("kind") == "drive#folder" else "file"}
    except Exception:
        return {}          # 失败不缓存，下次切目录还能重试
    with THUMB_LOCK:
        THUMB_CACHE[key] = {"map": out, "exp": now + THUMB_TTL}
    return out


def _thumb_fetch(url):
    """取 PikPak CDN 封面（二进制）：先 mihomo，失败直连。

    NAS 能访问 CDN，但用户浏览器不一定能（代理/区域限制）。走同源 /thumb
    后，前端 <img> 只需访问内网，封面成功率不受用户网络环境影响。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": PK_UA,
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
    for use_proxy in (True, False):
        try:
            p = pk_proxy_url() if use_proxy else ""
            ph = {"http": p, "https": p} if p else {}
            with urllib.request.build_opener(urllib.request.ProxyHandler(ph)).open(req, timeout=15) as r:
                ct = str(r.headers.get("Content-Type") or "image/jpeg")
                data = r.read(2 * 1024 * 1024)
                if ct.startswith("image/") and data:
                    return data, ct
                return b"", ""
        except Exception:
            continue
    return b"", ""


# ══════════════ v3.41 外部分享链接：解析 / 透视 / 转存 ══════════════
# 与 _share_worker（自己账号建分享 → 另一个账号 restore）不同，这里的源是
# 别人的分享链接，不需要建分享、也不需要删分享，只需要 token + restore + 归位。

def parse_share_input(text, pass_code=""):
    """从粘贴的任意文本里提取 (share_id, pass_code, err)。

    支持 https://mypikpak.com/s/XXXX?pwd=abcd、中文「提取码：abcd」、
    「链接 提取码」整段复制文本，以及直接贴 8~128 位分享 ID。
    """
    t = str(text or "").strip()
    if not t:
        return "", "", "请粘贴 PikPak 分享链接或分享 ID"
    m = re.search(
        r"(?:https?://)?(?:www\.)?(?:mypikpak\.com|app\.mypikpak\.com|"
        r"drive\.mypikpak\.com|mypikpak\.net|pikpak\.me)/s/"
        r"([A-Za-z0-9_-]{8,128})(?=[^A-Za-z0-9_-]|$)", t, re.I)
    sid = m.group(1) if m else (t if re.fullmatch(r"[A-Za-z0-9_-]{8,128}", t) else "")
    if not sid:
        return "", "", "没识别出分享链接（支持 https://mypikpak.com/s/xxxx 或直接贴分享 ID）"
    code = str(pass_code or "").strip()
    if not code:
        m2 = re.search(r"[?&#](?:s_code|pass_code|passcode|pass|pwd)=([A-Za-z0-9]{4,10})(?=&|#|\s|$)", t, re.I)
        if not m2:
            m2 = re.search(r"#([A-Za-z0-9]{4,10})(?=$|[^A-Za-z0-9])", t)   # 链接#提取码
        if not m2:
            m2 = re.search(r"(?:提取码|提取碼|访问码|訪問碼|密码|密碼|口令)\s*[:：]?\s*([A-Za-z0-9]{4,10})", t)
        if m2:
            code = m2.group(1)
    return sid, code, ""


def _share_payload(d):
    """PikPak 有些响应把主体包在 data 里，统一摊平。"""
    if isinstance(d, dict) and isinstance(d.get("data"), dict) and "files" not in d:
        return d["data"]
    return d or {}


_SHARE_STATUS_HINT = {
    "EXPIRED": "分享已过期，请让分享者重新生成链接",
    "DELETED": "分享里的文件已被删除",
    "NOT_FOUND": "PikPak 找不到这个分享（链接 ID 可能有误，或分享已被彻底移除）",
    "PROHIBITED": "PikPak 拒绝当前网络出口访问：多为出口 IP 所在地区受限。"
                  "请在「设置 → 代理」里填一个可用代理地址（无需切换全局模式，"
                  "下载仍走直连），解析会自动改走代理重试；也可用手机流量打开链接对照",
}


def _share_fail(st, text="", via=""):
    """按 share_status 分类给可操作的提示，并带上服务端返回的官方说明。"""
    st = str(st or "").strip() or "UNKNOWN"
    hint = _SHARE_STATUS_HINT.get(st.upper(), "PikPak 返回了未识别的分享状态")
    bits = ["分享不可用（%s）：%s" % (st[:60], hint)]
    if text:
        bits.append("服务端说明：" + str(text)[:120])
    if via:
        bits.append("本次请求走：" + via)
    return "；".join(bits)


def pk_share_info(name, sid, pcode, proxy=True, via=""):
    """GET /drive/v1/share：拿 pass_code_token 与分享元信息（只读）。

    v3.81：proxy 可传具体地址字符串，供 shareparse 做「直连 → 代理」双路尝试；
    via 只用于把实际走的路径写进错误提示，避免用户以为代理生效了其实没有。
    """
    # 对齐 alist / pikpakcli 的 getSharePassToken：这两个参数不是可选的装饰，
    # 缺了它们 PikPak 会回一份不带 pass_code_token 的精简响应（表现为「需要提取码」）。
    q = ("/drive/v1/share?share_id=%s&pass_code=%s&thumbnail_size=SIZE_MEDIUM&limit=100"
         % (urllib.parse.quote(sid), urllib.parse.quote(pcode or "")))
    code, d = pk_api(name, "GET", q, _proxy=proxy)
    if code >= 400:
        raise RuntimeError(_pk_err(code, d))
    x = _share_payload(d)
    st = str(x.get("share_status") or "")
    s_text = str(x.get("share_status_text") or "")
    if st and st != "OK":
        raise RuntimeError(_share_fail(st, s_text, via))
    tok = str(x.get("pass_code_token") or "")
    if not tok:
        # 带上服务端状态与响应字段名，便于区分「真的要提取码」与「接口没给 token」
        keys = ",".join(sorted(x.keys())[:14]) if isinstance(x, dict) else ""
        raise RuntimeError("需要提取码，或提取码不正确（请在上方填写后重新解析）"
                           + ("；服务端状态：" + (st or "空"))
                           + ("；服务端说明：" + s_text[:120] if s_text else "")
                           + ("；响应字段：" + keys[:160] if keys else "")
                           + ("；本次请求走：" + via if via else ""))
    return {"shareId": sid, "passCode": pcode, "passCodeToken": tok,
            "title": str(x.get("title") or x.get("share_title") or x.get("name") or "")[:120],
            "shareStatus": st, "shareStatusText": s_text, "via": via,
            "shareUser": str((x.get("share_user") or {}).get("nickname")
                             if isinstance(x.get("share_user"), dict)
                             else (x.get("share_user_name") or x.get("nickname") or ""))[:60]}


def pk_share_detail(name, sid, token, parent_id="", page_token="", limit=200, proxy=True):
    """GET /drive/v1/share/detail：列分享内某一层（只读，可翻页/进子目录）。
    必须与 pk_share_info 用同一条网络路径，否则换出口后 token 可能不通用。"""
    q = ("/drive/v1/share/detail?share_id=%s&pass_code_token=%s&limit=%d"
         "&thumbnail_size=SIZE_MEDIUM&order=3&folders_first=true"
         % (urllib.parse.quote(sid), urllib.parse.quote(token), limit))
    if parent_id:
        q += "&parent_id=" + urllib.parse.quote(parent_id)
    if page_token:
        q += "&page_token=" + urllib.parse.quote(page_token)
    code, d = pk_api(name, "GET", q, _proxy=proxy)
    if code >= 400:
        raise RuntimeError(_pk_err(code, d))
    x = _share_payload(d)
    out = []
    for f in (x.get("files") or []):
        nm = str(f.get("name") or "")
        if not nm:
            continue
        kind = str(f.get("kind") or "").lower()
        try:
            size = int(f.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        thumb = str(f.get("thumbnail_link") or "")
        host = urllib.parse.urlparse(thumb).netloc.lower() if thumb.startswith("https://") else ""
        if not (host == "mypikpak.com" or host.endswith(".mypikpak.com")):
            thumb = ""
        out.append({"id": str(f.get("id") or f.get("file_id") or ""),
                    "name": nm, "isdir": "folder" in kind,
                    "size": size, "mime": str(f.get("mime_type") or ""),
                    "thumb": thumb})
    return out, str(x.get("next_page_token") or "")


def pk_share_delete(name, share_id):
    """取消自己建的分享：官方网页端用 POST share:batchDelete，DELETE 单条已会 404。

    v3.34~v3.40 的 _share_worker 只调 DELETE，实测返回 404 —— 用完即删其实一直没删掉。
    """
    if not share_id:
        return False
    try:
        code, d = pk_api(name, "POST", "/drive/v1/share:batchDelete", {"ids": [str(share_id)]})
        if code < 400 and not (d or {}).get("error"):
            return True
    except Exception:
        pass
    try:
        code, d = pk_api(name, "DELETE", "/drive/v1/share/" + urllib.parse.quote(str(share_id)))
        return code < 400 and not (d or {}).get("error")
    except Exception:
        return False


# ══════════════ v3.42 我的分享管理 / 提取上限自动取消 ══════════════
SHARE_LIMITS_LOCK = threading.Lock()
MYSHARES_TTL = 60
MYSHARES_CACHE = {}       # remote -> (ts, rows)
FOLDERCOVERS_CACHE = {}   # (remote, fileId) -> (ts, coverUrl)；v3.80 封面补齐缓存


def load_share_limits():
    d = _read_json(SHARE_LIMITS_FILE, {})
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("limits"), dict):
        d["limits"] = {}
    if not isinstance(d.get("log"), list):
        d["log"] = []
    return d


def save_share_limits(d):
    _write_json(SHARE_LIMITS_FILE, d)


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# v3.44 云盘整理三件套：批量重命名 / 回收站 / 云端解压
#   · 批量重命名：复用 rclone operations/movefile（与单条重命名完全同一条路径），
#     冲突检测放在服务端做 —— 前端只负责按模板/正则/序号算出「新名字」并预览。
#   · 回收站：PikPak 的 trash 是服务端概念，rclone 不暴露，只能走私有 API：
#     列表 filters={"trashed":{"eq":true}}&parent_id=*、恢复 files:batchUntrash、
#     彻底删除 files:batchDelete。三者都按「当前账号内的文件 id」操作，跨账号无效。
#   · 云端解压：/decompress/v1/list 预览（免费账号也能用）、/decompress/v1/decompress
#     提交（PikPak 判定为会员功能）。非会员时如实回传，前端引导走 NAS 本地解压。
# ---------------------------------------------------------------------------
RENAME_MAX_ITEMS = 500
TRASH_PAGE_LIMIT = 100
TRASH_MAX_PAGES = 5
TRASH_MAX_IDS = 500
UNZIP_PREVIEW_MAX = 300
ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2",
                ".tbz2", ".tar.xz", ".txz", ".gz", ".bz2", ".xz")
UNZIP_PREMIUM_HINTS = ("premium", "need_more_quota", "vip", "member", "会员")
UNZIP_PASSWORD_HINTS = ("password", "pass_word", "pwd", "encrypted", "密码")


def is_archive_name(name):
    low = str(name or "").lower()
    return any(low.endswith(e) for e in ARCHIVE_EXTS)


def _bad_entry_name(n):
    """单层文件名合法性：不允许空、路径分隔符、控制字符与 ..（与 cloudmv 同规则）"""
    s = str(n or "").strip()
    if not s or len(s) > 180 or s in (".", ".."):
        return True
    if "/" in s or "\\" in s:
        return True
    return any(ord(ch) < 32 for ch in s)


def _cloud_join(folder, name):
    f = str(folder or "").strip("/")
    return (f + "/" + name) if f else name


def rename_batch(remote, folder, items):
    """批量重命名云端文件。

    items: [{"from": 旧名, "to": 新名}]，只接受单层名称（同目录内改名）。
    先整体校验再动手：任何冲突都不执行，避免改到一半停下留一地半成品。
    """
    if not remote:
        return {"error": "缺少参数：remote（请选择账号）"}
    clean, failed = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        a = str(it.get("from") or "").strip()
        b = str(it.get("to") or "").strip()
        if _bad_entry_name(a) or _bad_entry_name(b):
            failed.append({"name": a or "?", "error": "名称不合法（不能含 / 或为空）"})
            continue
        if a == b:
            continue                     # 没变化，跳过不计失败
        clean.append((a, b))
    if not clean:
        return {"ok": 0, "failed": failed, "createdText": "没有需要改名的项"}
    targets = [b for _, b in clean]
    dup = sorted({t for t in targets if targets.count(t) > 1})
    if dup:
        return {"error": "新名字重复：%s" % "、".join(dup[:3]), "conflicts": dup}
    try:
        lst = rc("operations/list", fs=remote + ":", remote=folder, _timeout=30, _strict=True)
        existing = {str(x.get("Name") or "") for x in (lst.get("list") or [])}
    except Exception as e:
        return {"error": "读取当前目录失败：%s" % mask_secret(str(e))[:150]}
    srcs = {a for a, _ in clean}
    blocked = [t for t in targets if t in existing and t not in srcs]
    if blocked:
        return {"error": "目标名称已存在：%s" % "、".join(blocked[:3]),
                "conflicts": blocked}

    ok = 0
    tmp = {}
    # 交换式改名（A→B 且 B 也在改）时先挪到临时名，否则第一条就会撞到还没让位的 B
    risky = [i for i, (_, b) in enumerate(clean) if b in srcs]
    for i in risky:
        a = clean[i][0]
        t = "__ren_tmp_%d_%d" % (i, int(time.time() * 1000) % 1000000)
        try:
            rc("operations/movefile", _strict=True,
               srcFs=remote + ":", srcRemote=_cloud_join(folder, a),
               dstFs=remote + ":", dstRemote=_cloud_join(folder, t))
            tmp[i] = t
        except Exception as e:
            failed.append({"name": a, "error": "临时改名失败：%s" % mask_secret(str(e))[:90]})
    for i, (a, b) in enumerate(clean):
        if i in risky and i not in tmp:
            continue
        src = tmp.get(i, a)
        try:
            rc("operations/movefile", _strict=True,
               srcFs=remote + ":", srcRemote=_cloud_join(folder, src),
               dstFs=remote + ":", dstRemote=_cloud_join(folder, b))
            ok += 1
        except Exception as e:
            # v3.61：第二步失败时把临时名挪回原名，云端不再留 __ren_tmp_* 孤儿
            # （与本地版 rename_batch 的回滚对称；回滚也失败则明确告知手动处理）
            if src != a:
                try:
                    rc("operations/movefile", _strict=True,
                       srcFs=remote + ":", srcRemote=_cloud_join(folder, src),
                       dstFs=remote + ":", dstRemote=_cloud_join(folder, a))
                except Exception:
                    failed.append({"name": a, "error": "改名失败，且临时名 %s 回滚失败（请手动改回）：%s"
                                   % (src, mask_secret(str(e))[:90])})
                    continue
            failed.append({"name": a, "error": mask_secret(str(e))[:120]})
    return {"ok": ok, "failed": failed, "total": len(clean)}


LOCAL_SIZE_DEADLINE = 55        # 本地目录统计软时限（秒），前端超时 60s
LOCAL_SIZE_MAX_FILES = 200000   # 条目数上限

def _local_dir_size(real_path, deadline):
    """本地目录递归求和（v3.64）。只统计普通文件，跳过隐藏/临时文件与软链接。
    超时或条目超限抛 RuntimeError（调用方转错误响应）。"""
    total, count = 0, 0
    for cur, dnames, fnames in os.walk(real_path, followlinks=False):
        dnames[:] = [x for x in dnames if not _junk_name(x)]
        for fn in fnames:
            if _junk_name(fn):
                continue
            try:
                st = os.stat(os.path.join(cur, fn))
            except OSError:
                continue
            if statmod.S_ISREG(st.st_mode):
                total += st.st_size
                count += 1
                if count > LOCAL_SIZE_MAX_FILES:
                    raise RuntimeError("目录条目超过 %d 个，已停止统计" % LOCAL_SIZE_MAX_FILES)
        if time.time() > deadline:
            raise RuntimeError("目录太大，统计超时")
    return {"bytes": total, "count": count}

PK_SIZE_DEADLINE = 110          # 目录大小统计的软时限（秒），前端超时 150s
PK_SIZE_MAX_FILES = 20000       # 条目数上限，防失控
PK_SIZE_MAX_DEPTH = 10          # 嵌套深度上限

def _pk_folder_size(name, folder_id, deadline, depth=0, seen=None):
    """递归统计 PikPak 文件夹总字节数（v3.62，逻辑对齐 PikPak_Enhancement_Master）：
    逐层 /drive/v1/files?parent_id=<id> 分页拉全。文件夹对象自带 size 时直接采信
    （>0 免递归，与参考脚本 getShareParseFolderSummary 一致），为 0 才下钻求和。
    深度 / 条目数 / 时限三重上限，超限抛 RuntimeError 由调用方兜底。"""
    fid = str(folder_id or "")
    if seen is None:
        seen = set()
    key = fid or "__root__"
    if key in seen or depth > PK_SIZE_MAX_DEPTH:
        return 0
    seen.add(key)
    total, next_tok, files_seen = 0, "", 0
    while True:
        if time.time() > deadline:
            raise RuntimeError("目录太大，统计超时（上限 %d 秒）" % PK_SIZE_DEADLINE)
        q = ("/drive/v1/files?parent_id=%s&limit=1000&phase=PHASE_TYPE_COMPLETE"
             "&thumbnail_size=SIZE_SMALL%s" % (urllib.parse.quote(fid),
                                               ("&page_token=" + urllib.parse.quote(next_tok)) if next_tok else ""))
        code, d = pk_api(name, "GET", q)
        if code >= 400:
            raise RuntimeError(_pk_err(code, d))
        for e in (d.get("files") or []):
            files_seen += 1
            if files_seen > PK_SIZE_MAX_FILES:
                raise RuntimeError("目录条目超过 %d 个，已停止统计" % PK_SIZE_MAX_FILES)
            try:
                sz = int(e.get("size") or 0)
            except Exception:
                sz = 0
            if e.get("kind") == "drive#folder":
                # PikPak 有时直接给出文件夹累计大小（>0 采信，免递归）
                total += sz if sz > 0 else _pk_folder_size(name, str(e.get("id") or ""), deadline, depth + 1, seen)
            else:
                total += max(0, sz)
        next_tok = str(d.get("next_page_token") or "")
        if not next_tok:
            break
    return total

def pk_trash_list(name, max_pages=TRASH_MAX_PAGES, limit=TRASH_PAGE_LIMIT):
    """回收站列表。parent_id=* 表示不限父目录；某些账号的陈旧条目只会出现在
    未过滤列表里，但真实回收站内容这条查询一直准确（已用真实数据核对）。"""
    flt = urllib.parse.quote(json.dumps({"trashed": {"eq": True}}, separators=(",", ":")))
    out, token, seen = [], "", set()
    for _ in range(max_pages):
        q = ("/drive/v1/files?thumbnail_size=SIZE_SMALL&limit=%d&parent_id=*&with_audit=true"
             "&filters=%s&_t=%d" % (limit, flt, int(time.time() * 1000)))
        if token:
            q += "&page_token=" + urllib.parse.quote(token)
        code, d = pk_api(name, "GET", q)
        if code >= 400:
            raise RuntimeError(_pk_err(code, d))
        out.extend(d.get("files") or [])
        token = str(d.get("next_page_token") or "")
        if not token or token in seen:
            break
        seen.add(token)
    return out


def map_trash(f):
    """回收站条目 → 前端字段（只留展示需要的，id 用于恢复/彻底删除）"""
    return {"id": str(f.get("id") or ""),
            "name": str(f.get("name") or ""),
            "isdir": f.get("kind") == "drive#folder",
            "size": _as_int(f.get("size")),
            "created": str(f.get("created_time") or "")}


def pk_batch_ids(name, op, ids):
    """按 id 批量操作：batchTrash(进回收站) / batchUntrash(恢复) / batchDelete(彻底删除)。
    PikPak 单次上限 100，超过就切块，返回成功条数。"""
    uniq = []
    for i in ids:
        s = str(i or "").strip()
        if s and s not in uniq:
            uniq.append(s)
    if not uniq:
        return {"ok": 0, "chunks": 0}
    if len(uniq) > TRASH_MAX_IDS:
        raise RuntimeError("一次最多处理 %d 项" % TRASH_MAX_IDS)
    done, tasks = 0, []
    for i in range(0, len(uniq), 100):
        chunk = uniq[i:i + 100]
        code, d = pk_api(name, "POST", "/drive/v1/files:" + op, {"ids": chunk})
        if code >= 400 or (isinstance(d, dict) and d.get("error")):
            raise RuntimeError(_pk_err(code, d))
        done += len(chunk)
        tid = str((d or {}).get("task_id") or "")
        if tid:
            tasks.append(tid)
    return {"ok": done, "chunks": len(tasks), "taskIds": tasks}


def _pk_file_id(name, path):
    """云端路径 → (id, 条目)。目录走 pk_walk，文件走 pk_file。"""
    p = str(path or "").strip("/")
    try:
        return pk_walk(name, p), {"kind": "drive#folder", "name": p.rsplit("/", 1)[-1]}
    except Exception:
        f = pk_file(name, p)
        return str(f.get("id") or ""), f


def cloud_hard_delete(remote, path, isfile):
    """彻底删除（不进回收站）。rclone 的 delete/purge 在 PikPak 上等同「移入回收站」，
    真正的永久删除只有 files:batchDelete。"""
    p = str(path or "").strip("/")
    if not p:
        return {"error": "缺少参数：path"}
    if remote not in pikpak_remotes():
        return {"error": "彻底删除依赖 PikPak 回收站接口，该账号不是 PikPak 类型"}
    if isfile:
        try:
            f = pk_file(remote, p)
            fid = str(f.get("id") or "")
        except Exception as e:
            return {"error": mask_secret(str(e))[:150]}
    else:
        try:
            fid = pk_walk(remote, p)
        except Exception as e:
            return {"error": mask_secret(str(e))[:150]}
    if not fid:
        return {"error": "在账号 %s 里找不到「%s」" % (remote, p)}
    try:
        r = pk_batch_ids(remote, "batchDelete", [fid])
    except Exception as e:
        return {"error": "彻底删除失败：%s" % mask_secret(str(e))[:150]}
    return {"ok": True, "hard": True, "deleted": r.get("ok")}


def _pk_gcid(name, fid):
    """解压接口要 gcid；PikPak 详情里字段名是 hash（不是 gcid），三个都试。"""
    code, d = pk_api(name, "GET", "/drive/v1/files/" + urllib.parse.quote(str(fid)))
    if code >= 400:
        raise RuntimeError(_pk_err(code, d))
    return str(d.get("gcid") or d.get("hash") or d.get("md5_checksum") or ""), d


# ── v3.45 在线播放 ────────────────────────────────────────────────────────
# 播放链路实测（真实账号，2026-11-07）：
#   · GET /drive/v1/files/{id} → web_content_link 自带 expire 签名、免 token、
#     支持 Range/206，浏览器 <video src> 可直接拉，本机实测 1163 KB/s
#   · medias[].link.url 是「播放链路」，但必须带 Authorization: Bearer <JWT>；
#     <video> 发不了自定义头，且转码版一律 HEVC（Chrome 解不了）→ 不采用
#   · 走 NAS 中转：PikPak 45 KB/s、webdav(openlist→百度网盘) 79 KB/s，放视频必卡
CLOUD_VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".ogv", ".mkv",
                    ".avi", ".flv", ".ts", ".m2ts", ".wmv"}
CLOUD_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus",
                    ".wav", ".flac", ".ape"}
# 浏览器能解封装的容器。.mkv 不在官方支持列表，但 Chromium 自带 matroska
# demuxer，h264+aac 实测可播；播不了时前端退化成「仅下载」，不假装成功。
CLOUD_PLAY_OK = {".mp4", ".m4v", ".mov", ".webm", ".ogv", ".mkv"}
CLOUD_HEVC = {"hevc", "h265", "hvc1", "hev1"}


def cloud_media_kind(filename):
    """云端文件名 → video / audio / ""（不是音视频）。"""
    ext = os.path.splitext(str(filename or ""))[1].lower()
    if ext in CLOUD_VIDEO_EXTS:
        return "video"
    if ext in CLOUD_AUDIO_EXTS:
        return "audio"
    return ""


def cloud_playable(filename):
    """浏览器能不能直接解。注意这只判容器，编码要问服务端详情才准。"""
    ext = os.path.splitext(str(filename or ""))[1].lower()
    return ext in CLOUD_PLAY_OK or ext in CLOUD_AUDIO_EXTS


def pk_play_info(remote, path):
    """PikPak 文件 → 可直接喂给 <video> 的签名直链 + 元信息。"""
    try:
        f = pk_file(remote, path)
    except Exception as e:
        return {"error": "找不到这个文件：%s" % mask_secret(str(e))[:120]}
    fid = str((f or {}).get("id") or "")
    if not fid:
        return {"error": "取不到文件 id，可能路径写错了"}
    try:
        code, d = pk_api(remote, "GET",
                         "/drive/v1/files/" + urllib.parse.quote(fid))
    except Exception as e:
        return {"error": "读取播放地址失败：%s" % mask_secret(str(e))[:150]}
    if code >= 400:
        return {"error": "读取播放地址失败：%s" % mask_secret(_pk_err(code, d))[:150]}
    d = d or {}
    url = str(d.get("web_content_link") or "")
    if not url.startswith("https://"):
        return {"error": "这个文件暂时没有可播放的直链（可能仍在转码、已损坏或不在可播状态）"}

    # 元信息：优先取 Original（原画），没有就取第一个带 video 的版本
    vid = {}
    for m in (d.get("medias") or []):
        v = m.get("video") or {}
        if v:
            vid = v
            if m.get("is_origin"):
                break

    m = re.search(r"[?&]expire=(\d+)", url)
    expire = int(m.group(1)) if m else 0
    now = int(time.time())
    vcodec = str(vid.get("video_codec") or "").lower()
    acodec = str(vid.get("audio_codec") or "").lower()
    codec_ok = vcodec not in CLOUD_HEVC
    ext = os.path.splitext(path)[1].lower()

    hint = ""
    if not codec_ok:
        hint = "该视频是 %s(H.265) 编码，Chrome 无法解码；请用 Edge 并装 HEVC 扩展，或下载后播放。" % (
            vcodec.upper())
    elif ext == ".mkv":
        hint = "mkv 容器不是浏览器官方支持的格式，多数情况能播；若黑屏无声，请下载后用本机播放器打开。"

    return {"ok": True, "mode": "direct", "url": url,
            "expire": expire,
            "expiresIn": max(0, expire - now) if expire else 0,
            "name": str(d.get("name") or str(path).rsplit("/", 1)[-1]),
            "size": _as_int(d.get("size")),
            "kind": cloud_media_kind(path),
            "codecOk": codec_ok, "videoCodec": vcodec, "audioCodec": acodec,
            "duration": float(vid.get("duration") or 0),
            "width": _as_int(vid.get("width")),
            "height": _as_int(vid.get("height")),
            "bitrate": _as_int(vid.get("bit_rate")),
            # 转码版存在但前端用不了（HEVC + 需要 Authorization 头），只做提示
            "transcodes": max(0, len(d.get("medias") or []) - 1),
            "hint": hint}


def cloud_play_info(remote, path):
    """云端文件 → 播放信息。只有 PikPak 有免鉴权的签名直链，其它后端如实说明限制。

    直链直接下发：前端只有在 https 安全上下文里才用得上（见 TLS_DIR 注释）。
    试过让服务端 302 过去（同源入口），Chromium 的 media 加载器会直接
    loadingFailed（连请求都不发），所以 302 方案行不通，只能给真实地址。"""
    path = str(path or "").strip("/")
    if not remote or not path:
        return {"error": "缺少 remote 或 path"}
    kind = cloud_media_kind(path)
    if not kind:
        return {"error": "这个文件不是音视频，无法在线播放", "playable": False}
    conf = rc("config/dump") or {}
    typ = str((conf.get(remote) or {}).get("type") or "")
    if not typ:
        return {"error": "账号不存在：%s" % remote, "playable": False}
    if typ != "pikpak":
        return {"playable": False, "kind": kind, "remoteType": typ, "mode": "none",
                "error": "「%s」是 %s 类型，没有免鉴权的播放直链；只能经 NAS 中转，"
                         "实测这类网盘常在 100 KB/s 以下（放视频会一直缓冲）。"
                         "建议先下载到 NAS 或本机再看。" % (remote, typ)}
    return pk_play_info(remote, path)


def _unzip_status_text(d):
    return str((d or {}).get("status_text") or (d or {}).get("msg") or
               (d or {}).get("error_description") or (d or {}).get("status") or "")


def unzip_preview(remote, path, password=""):
    """预览压缩包内容。免费账号也能列目录，所以「能不能解开」要等提交时才见分晓。"""
    if remote not in pikpak_remotes():
        return {"error": "云端解压只支持 PikPak 账号", "unsupported": True}
    try:
        f = pk_file(remote, path)
    except Exception as e:
        return {"error": mask_secret(str(e))[:150]}
    try:
        gcid, _det = _pk_gcid(remote, f.get("id"))
    except Exception as e:
        return {"error": "读取文件详情失败：%s" % mask_secret(str(e))[:150]}
    code, d = pk_api(remote, "POST", "/decompress/v1/list",
                     {"gcid": gcid, "file_id": str(f.get("id") or ""),
                      "password": str(password or ""), "path": ""})
    if code >= 400:
        return {"error": "读取压缩包失败：%s" % mask_secret(_pk_err(code, d))[:150]}
    status = str((d or {}).get("status") or "")
    text = _unzip_status_text(d)
    low = (status + " " + text).lower()
    if status and status.upper() != "OK":
        need_pwd = any(h in low for h in UNZIP_PASSWORD_HINTS)
        return {"error": ("压缩包需要密码，或密码不正确" if need_pwd else text or status),
                "needPassword": need_pwd,
                "premium": any(h in low for h in UNZIP_PREMIUM_HINTS)}
    entries = []
    for x in (d.get("files") or [])[:UNZIP_PREVIEW_MAX]:
        entries.append({"path": str(x.get("path") or x.get("name") or ""),
                        "size": _as_int(x.get("size")),
                        "isdir": bool(x.get("is_dir") or x.get("folder") or
                                      str(x.get("path") or "").endswith("/"))})
    return {"ok": True, "entries": entries,
            "more": len(d.get("files") or []) > UNZIP_PREVIEW_MAX,
            "fileSize": _as_int((d or {}).get("file_size")),
            "gcid": gcid, "fileId": str(f.get("id") or ""),
            "title": str((d or {}).get("title") or f.get("name") or "")}


def unzip_start_cloud(remote, path, password=""):
    """提交云端解压任务（异步），进度由 _unzip_watch_cloud 轮询写盘。"""
    if remote not in pikpak_remotes():
        return {"error": "云端解压只支持 PikPak 账号", "unsupported": True}
    try:
        f = pk_file(remote, path)
        gcid, _ = _pk_gcid(remote, f.get("id"))
    except Exception as e:
        return {"error": mask_secret(str(e))[:150]}
    code, d = pk_api(remote, "POST", "/decompress/v1/decompress",
                     {"file_id": str(f.get("id") or ""), "files": [],
                      "password": str(password or ""), "default_parent": True,
                      "gcid": gcid})
    if code >= 400:
        return {"error": "提交解压失败：%s" % mask_secret(_pk_err(code, d))[:150]}
    status = str((d or {}).get("status") or "")
    text = _unzip_status_text(d)
    low = (status + " " + text).lower()
    premium = any(h in low for h in UNZIP_PREMIUM_HINTS)
    if premium:
        return {"error": "该账号不是 PikPak 会员，云端解压不可用（官方限制）。" +
                         "可以先把压缩包下载到 NAS，再用「本地文件 · 解压」。",
                "premium": True}
    if status and status.upper() != "OK":
        need_pwd = any(h in low for h in UNZIP_PASSWORD_HINTS)
        return {"error": ("密码不正确或无密码" if need_pwd else text or status),
                "needPassword": need_pwd}
    tid = str((d or {}).get("task_id") or "")
    if not tid:
        return {"error": "解压接口没有返回任务 id：%s" % mask_secret(json.dumps(d, ensure_ascii=False))[:120]}
    name = str(f.get("name") or path)
    job = {"id": int(time.time() * 1000), "kind": "unzip", "engine": "cloud",
           "remote": remote, "subpath": str(path or "").strip("/"),
           "name": name, "dest": "云盘同目录", "mode": "unzip",
           "state": "running", "started": time.strftime("%m-%d %H:%M"),
           "bytes": 0, "total": 0, "speed": 0, "eta": None, "error": "",
           "taskId": tid, "pctNum": 0, "phase": "已提交",
           "password": mask_secret(password) if password else ""}
    with LOCK:
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)
    UNZIP_ALIVE.add(job["id"])
    threading.Thread(target=_unzip_watch_cloud, args=(job["id"],), daemon=True).start()
    return {"job": job}


def _job_patch(job_id, **kw):
    with LOCK:
        jobs = load_jobs()
        hit = None
        for j in jobs:
            if j.get("id") == job_id:
                j.update(kw)
                hit = j
                break
        if hit is not None:
            save_jobs(jobs)
    return hit


def _unzip_watch_cloud(job_id):
    """轮询 /decompress/v1/progress，最多 1 小时（3s 一次）。"""
    try:
        for _ in range(1200):
            time.sleep(3)
            with LOCK:
                job = next((j for j in load_jobs() if j.get("id") == job_id), None)
            if not job or job.get("state") != "running":
                return
            tid = str(job.get("taskId") or "")
            try:
                code, p = pk_api(job["remote"], "GET",
                                 "/decompress/v1/progress?task_id=" + urllib.parse.quote(tid))
            except Exception as e:
                _job_patch(job_id, state="error",
                           error="读取解压进度失败：%s" % mask_secret(str(e))[:120])
                return
            if code >= 400:
                _job_patch(job_id, state="error", error=mask_secret(_pk_err(code, p))[:150])
                return
            phase = str((p or {}).get("phase") or "")
            if phase == "PHASE_TYPE_COMPLETE":
                _job_patch(job_id, state="done", pctNum=100, phase="", error="")
                return
            if phase == "PHASE_TYPE_ERROR":
                _job_patch(job_id, state="error", phase="",
                           error=mask_secret(str((p or {}).get("message") or
                                                 (p or {}).get("status_text") or "解压失败"))[:160])
                return
            try:
                v = float((p or {}).get("progress") or 0)
            except Exception:
                v = 0.0
            _job_patch(job_id, pctNum=max(0.0, min(99.0, v)), phase="解压中")
    finally:
        UNZIP_ALIVE.discard(job_id)


def pikpak_remotes():
    try:
        conf = rc("config/dump", _timeout=15) or {}
        return [k for k, v in conf.items() if str((v or {}).get("type")) == "pikpak"]
    except Exception:
        return []


def pk_share_list(name, max_pages=4, limit=100, force=False):
    """列某个账号「我的分享」，带 60 秒缓存（自动取消巡检每 5 分钟才跑一次）。"""
    now = time.time()
    if not force:
        with SHARE_LIMITS_LOCK:
            ent = MYSHARES_CACHE.get(name)
            if ent and now - ent[0] < MYSHARES_TTL:
                return ent[1]
    out, token, seen = [], "", set()
    for _ in range(max_pages):
        q = "/drive/v1/share/list?limit=%d&thumbnail_size=SIZE_SMALL&_t=%d" % (
            limit, int(now * 1000))
        if token:
            q += "&page_token=" + urllib.parse.quote(token)
        code, d = pk_api(name, "GET", q)
        if code >= 400:
            raise RuntimeError(_pk_err(code, d))
        x = d if isinstance(d, dict) else {}
        data = x.get("data")
        rows = data if isinstance(data, list) else (x.get("shares") or [])
        out.extend(rows)
        token = str(x.get("next_page_token") or "")
        if not token or token in seen:
            break
        seen.add(token)
    with SHARE_LIMITS_LOCK:
        MYSHARES_CACHE[name] = (now, out)
    return out


def map_my_share(name, s):
    """把私有 API 的分享条目映射成前端字段（字段名已按真实响应核对）。"""
    return {"remote": name,
            "shareId": str(s.get("share_id") or s.get("id") or ""),
            "title": str(s.get("title") or "")[:160],
            "status": str(s.get("share_status") or ""),
            "statusText": str(s.get("share_status_text") or ""),
            "shareTo": str(s.get("share_to") or ""),
            "passCode": str(s.get("pass_code") or ""),
            "views": _as_int(s.get("view_count")),
            "restores": _as_int(s.get("restore_count")),
            "restoreLimit": _as_int(s.get("restore_limit")),
            "expirationDays": str(s.get("expiration_days") or ""),
            "expirationLeft": str(s.get("expiration_left") or ""),
            "created": str(s.get("create_time") or ""),
            "url": str(s.get("share_url") or ""),
            "fileNum": _as_int(s.get("file_num")),
            "fileSize": _as_int(s.get("file_size"))}


def _share_limit_key(remote, sid):
    return "%s|%s" % (remote, sid)


def _share_auto_cancel_tick():
    """达到用户设定的提取次数上限 → 自动取消分享。由 scheduler 每 5 分钟调用一次。

    注意：只在总开关打开且有规则时才打 API；删除动作会记入 log 供前端展示。
    """
    if not load_settings().get("shareAutoCancel"):
        return
    with SHARE_LIMITS_LOCK:
        d = load_share_limits()
        limits = dict(d.get("limits") or {})
    if not limits:
        return
    by_remote = {}
    for key, ent in limits.items():
        if isinstance(ent, dict):
            by_remote.setdefault(str(ent.get("remote") or ""), []).append((key, ent))
    changed = False
    for remote, items in by_remote.items():
        if not remote:
            continue
        try:
            rows = pk_share_list(remote, force=True)
        except Exception as e:
            print("%s 分享巡检跳过 %s: %s" % (time.strftime("%F %T"), remote, str(e)[:100]))
            continue
        idx = {str(x.get("share_id") or ""): x for x in rows}
        for key, ent in items:
            sid, lim = str(ent.get("shareId") or ""), _as_int(ent.get("limit"))
            if lim <= 0:
                continue
            row = idx.get(sid)
            # 分享已删/失效 → 规则失效，清掉，避免每 5 分钟白查一次
            if row is None or str(row.get("share_status") or "") not in ("", "OK"):
                with SHARE_LIMITS_LOCK:
                    cur = load_share_limits()
                    cur["limits"].pop(key, None)
                    save_share_limits(cur)
                changed = True
                continue
            restores = _as_int(row.get("restore_count"))
            if restores < lim:
                continue
            ok = pk_share_delete(remote, sid)
            with SHARE_LIMITS_LOCK:
                cur = load_share_limits()
                cur["limits"].pop(key, None)
                cur["log"].insert(0, {
                    "at": time.strftime("%m-%d %H:%M"), "remote": remote,
                    "shareId": sid, "title": str(row.get("title") or "")[:80],
                    "restores": restores, "limit": lim, "ok": bool(ok)})
                cur["log"] = cur["log"][:50]
                save_share_limits(cur)
            changed = True
            print("%s 分享达上限自动取消 %s:%s (%d/%d) ok=%s"
                  % (time.strftime("%F %T"), remote, sid, restores, lim, ok))
    return changed


def build_share_restore_payload(sid, token, file_ids, ancestor_ids=None):
    """POST /drive/v1/share/restore 的请求体（抽成纯函数便于白盒验证）。

    ancestor_ids：勾选的是分享子目录里的条目时必须带上层级路径，
    否则 PikPak 无法定位这些 id（增强脚本同样处理）。
    """
    ids = [str(x) for x in (file_ids or []) if x]
    anc = [str(x) for x in (ancestor_ids or []) if x]
    body = {"share_id": str(sid or ""), "pass_code_token": str(token or ""),
            "file_ids": ids, "params": {"trace_file_ids": ",".join(ids)}}
    if anc:
        body["ancestor_ids"] = anc
    return body


def _pk_wait_task(name, tid, timeout=600, sleep=2):
    end = time.time() + timeout
    while time.time() < end:
        code, d = pk_api(name, "GET", "/drive/v1/tasks/" + urllib.parse.quote(str(tid)))
        ph = str((d or {}).get("phase") or "")
        if ph == "PHASE_TYPE_COMPLETE":
            return True, str((d or {}).get("message") or "")
        if ph in ("PHASE_TYPE_ERROR", "PHASE_TYPE_CANCELED"):
            return False, str((d or {}).get("message") or ph)
        time.sleep(sleep)
    return False, "等待任务超时"


def _pk_pass():
    return (uuid.uuid4().hex[:6] or "a1b2c3").lower()


# ---------------------------------------------------------------------------
# 跨账号转移（v3.34）
#   PikPak 的 backend features 里 ServerSideAcrossConfigs=false，两个账号之间
#   不存在服务端直传；`rclone copy A:源 B:目标` 的真实动作是
#   「从 A 读出 → 经 NAS 上的 rclone 进程内存 → 直接推给 B」的流式中转：
#   不落盘、不占 NAS 空间，但确实消耗 A 账号的下载流量配额。
#   两个必须守住的地方：
#     1) 目标已有同名文件时 PikPak 会自动改名成 xxx(1).bin，rclone 随即报
#        "uploaded file name mismatch" 整批失败 → 一律 --ignore-existing 跳过；
#     2) move 只能在 check --size-only 全部对上之后才 purge 源，否则宁可不删。
# ---------------------------------------------------------------------------
XFER = {}                    # job_id -> {"proc": Popen, "marker": str}
XSHARE = set()               # 原生转存任务：只登记在跑的 job_id（没有子进程可管）
XFER_LOCK = threading.Lock()
XFER_TRANSFERS = 2           # 单任务并发文件数：PikPak 上传吃连接，2 比 6 稳
XFER_CHECKERS = 4
XFER_PHASES = {"scan": "扫描源目录", "copy": "传输中", "check": "校验一致性", "purge": "删除源文件"}


def _xfer_cmd(job, argv):
    """docker exec 进 rclone-backend 执行；代理用环境变量注入，不动全局配置。"""
    cmd = ["docker", "exec"]
    if job.get("proxy"):
        cmd += proxy_env_args()
    return cmd + [RCLONE_CONTAINER, "rclone"] + list(argv)


def _xfer_run(job, argv, timeout=1800):
    """同步跑一条 rclone 子命令，返回 (code, stdout, stderr)。"""
    try:
        p = subprocess.run(_xfer_cmd(job, argv), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:
        return 1, "", str(e)


def _xfer_fetch(job_id):
    with LOCK:
        return next((dict(j) for j in load_jobs() if j.get("id") == job_id), None)


def _xfer_patch(job_id, **fields):
    with LOCK:
        jobs = load_jobs()
        for j in jobs:
            if j.get("id") == job_id:
                j.update(fields)
                save_jobs(jobs)
                return dict(j)
    return None


def _xfer_kill(job):
    with XFER_LOCK:
        ent = XFER.get(job["id"])
    if not ent:
        return
    try:
        ent["proc"].terminate()
    except Exception:
        pass
    # docker exec 的客户端进程被杀，容器内的 rclone 未必跟着退，再用唯一 UA 精确补刀
    try:
        subprocess.run(["docker", "exec", RCLONE_CONTAINER, "pkill", "-f", ent["marker"]],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except Exception:
        pass


def _xfer_worker(job_id):
    job = _xfer_fetch(job_id)
    if not job:
        return

    def bail(state, msg):
        _xfer_patch(job_id, state=state, error=str(msg)[:300], phase="",
                    speed=0, eta=None, curfile="")

    def finish(err=""):
        # v3.57：total 取 jobs.json 里的最新值（扫描阶段已回填真实字节数）；
        # job 变量是启动时的快照，目录任务启动时 total=0，直接用会把统计覆盖回 0
        cur = _xfer_fetch(job_id) or {}
        t = cur.get("total") or job.get("total") or 0
        _xfer_patch(job_id, state="done", phase="", speed=0, eta=None, curfile="",
                    bytes=t, total=t, error=err)

    _xfer_patch(job_id, state="running", phase="scan", error="", bytes=0,
                speed=0, eta=None, curfile="", started_ts=time.time())

    # 1) 扫描源：拿到总字节数，进度条才有分母（rclone copy 自己的 totalBytes 也行，
    #    但大目录要等整轮 list 完才出现，先用 size 给个准数）
    code, out, err = _xfer_run(job, ["size", job["src"], "--json"], timeout=900)
    cnt = byt = 0
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
                cnt, byt = int(d.get("count") or 0), int(d.get("bytes") or 0)
                break
            except Exception:
                continue
    if cnt == 0 and byt == 0:
        fatal = _fatal_job_error(err or out)
        return bail(*(fatal or ("error", "扫描源目录失败：%s" % (err or out)[:150])))
    _xfer_patch(job_id, total=byt, srcCount=cnt, phase="copy")

    # 2) 传输：--ignore-existing 跳过目标已有的同名文件（PikPak 不会覆盖，只会改名报错）
    marker = "xferid%d" % int(job_id)
    argv = ["copy", job["src"], job["dest"],
            "--ignore-existing", "--create-empty-src-dirs",
            "--stats", "1s", "--use-json-log", "-v",
            "--transfers", str(XFER_TRANSFERS), "--checkers", str(XFER_CHECKERS),
            "--user-agent", marker]
    try:
        proc = subprocess.Popen(_xfer_cmd(job, argv), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
    except Exception as e:
        return bail("error", "启动 rclone 失败：%s" % e)
    with XFER_LOCK:
        XFER[job_id] = {"proc": proc, "marker": marker}

    errs, last = [], 0.0
    try:
        for line in proc.stdout:
            line = (line or "").strip()
            if not line:
                continue
            if not line.startswith("{"):
                if "ERROR" in line or "CRITICAL" in line:
                    errs.append(line[:160])
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("level") in ("error", "critical") and o.get("msg"):
                errs.append(str(o["msg"])[:160])
            st = o.get("stats")
            if not st:
                continue
            now = time.time()
            if now - last < 1.0:      # 日志里有多种 stats 行，限流到每秒一次写盘
                continue
            last = now
            tr = st.get("transferring") or []
            _xfer_patch(job_id, bytes=int(st.get("bytes") or 0),
                        total=int(st.get("totalBytes") or 0) or job.get("total") or 0,
                        speed=st.get("speed") or 0, eta=st.get("eta"),
                        files=len(tr), curfile=(tr[0].get("name", "") if tr else ""))
    except Exception as e:
        errs.append(str(e)[:160])
    code = proc.wait()
    with XFER_LOCK:
        XFER.pop(job_id, None)

    cur = _xfer_fetch(job_id) or {}
    if cur.get("state") == "stopped":     # 用户点了暂停：不要覆盖成其它终态
        _xfer_patch(job_id, speed=0, eta=None, phase="", curfile="")
        return
    if code != 0:
        msg = " ｜ ".join(dict.fromkeys(errs))[:280] or ("rclone 退出码 %s" % code)
        fatal = _fatal_job_error(msg)
        return bail(*(fatal or ("error", msg)))

    # 3) 校验：逐文件比大小，确认目标侧真的齐了（--size-only 只列目录，不重传）
    _xfer_patch(job_id, phase="check", speed=0, eta=None, curfile="")
    c2, o2, e2 = _xfer_run(job, ["check", job["src"], job["dest"],
                                 "--size-only", "--one-way", "-v"], timeout=3600)
    blob = (o2 or "") + (e2 or "")
    mismatch = blob.count("sizes differ") + blob.count("not found")
    if c2 != 0:
        tail = ((blob.strip().splitlines() or [""])[-1])[:150]
        if mismatch:
            if job.get("mode") == "move":
                return bail("error", "有 %d 个文件在目标账号不一致（同名但大小不同），"
                                     "为安全没有删除源文件，请先处理冲突再继续" % mismatch)
            return finish("%d 个同名文件已跳过（目标已有但大小不同，未覆盖）" % mismatch)
        if job.get("mode") == "move":
            return bail("error", "已复制，但校验没能完成，为安全没有删除源文件：%s" % tail)
        return finish("已复制；校验未能完成：%s" % tail)

    # 4) move 才删源：必须已经校验通过
    if job.get("mode") == "move":
        _xfer_patch(job_id, phase="purge")
        c3, o3, e3 = _xfer_run(job, ["purge", job["src"]], timeout=1800)
        if c3 != 0:
            tail = (((o3 or "") + (e3 or "")).strip().splitlines() or ["未知错误"])[-1][:150]
            return bail("error", "已复制且校验通过，但删除源目录失败（源仍在）：%s" % tail)
    finish()


def _share_worker(job_id):
    """原生转存：源账号建分享 → 目标账号 restore → 归位 →（move 时）删源。
    全程服务端搬运，不占 NAS 带宽、不消耗下载配额。"""
    job = _xfer_fetch(job_id)
    if not job:
        return
    src, dst = job["remote"], job.get("dstRemote") or ""
    src_path = str(job.get("subpath") or "").strip("/")
    dst_path = str(job.get("dstPath") or "").strip("/")
    leaf = src_path.split("/")[-1] if src_path else ""
    one_file = bool(job.get("single_file"))
    fsiz = int(job.get("total") or 0) if one_file else 0

    def setp(**kw):
        _xfer_patch(job_id, **kw)

    def bail(state, msg):
        setp(state=state, error=str(msg)[:300], phase="", speed=0, eta=None, curfile="")

    setp(state="running", phase="share", error="", started_ts=time.time(),
         bytes=0, total=fsiz, speed=0, eta=None,
         curfile="正在读取源文件" if one_file else "正在读取源文件夹")
    with XFER_LOCK:
        XSHARE.add(job_id)          # 让 job_live 知道它是在跑的，不是重启遗留
    share_id = ""
    tgt = "%s:%s" % (dst, (dst_path + "/" + leaf) if dst_path else leaf)

    def probe_bytes(p):
        """目标路径上已有多少字节：>0 存在，==0 路径存在但为空，<0 不存在。"""
        c, o, e = _xfer_run(job, ["size", p, "--json"], timeout=600)
        for ln in reversed((o or "").splitlines()):
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    d0 = json.loads(ln)
                except Exception:
                    continue
                if int(d0.get("count") or 0) > 0:
                    return int(d0.get("bytes") or 0)
                return 0
        return -1

    def mb(n):
        return "%.1f MB" % ((n or 0) / 1048576.0)

    try:
        # 文件夹和单文件都只要一个 file_id，后续调用完全一致
        sid = pk_file(src, src_path)["id"] if one_file else pk_walk(src, src_path)

        # PikPak 不会覆盖同名内容、只会改名，moveto 在这种情况会「静默成功但文件没动」，
        # 所以在建分享之前先探目标，避免白跑一趟还在分享区留垃圾。
        setp(curfile="正在检查目标是否已有同名内容")
        have = probe_bytes(tgt)
        if have > 0 and (not one_file or have == fsiz):
            if job.get("mode") == "move":
                return bail("error", "目标已有同名%s（大小一致），为避免误删源已跳过；"
                                     "确认无误后请手动删除源" % ("文件" if one_file else "文件夹"))
            setp(state="done", phase="", speed=0, eta=None, curfile="", error="",
                 total=(fsiz or 1), bytes=(fsiz or 1), note="目标已有同名内容，未重复转存")
            return
        if have > 0:
            return bail("error", "目标已有同名文件但大小不同（目标 %s / 源 %s）。"
                                 "PikPak 不会覆盖只会改名，请先改名或删掉目标文件再试"
                        % (mb(have), mb(fsiz)))
        setp(curfile="正在创建分享链接")

        # 建分享：加密链接 + 随机提取码，1 天有效，用完立刻删除
        pcode = _pk_pass()
        code, d = pk_api(src, "POST", "/drive/v1/share", {
            "file_ids": [sid], "share_to": "encryptedlink", "expiration_days": 1,
            "pass_code_option": "REQUIRED", "custom_pass_code": pcode, "limit_count": 5})
        share_id = str((d or {}).get("share_id") or "")
        if code >= 400 or not share_id:
            return bail("error", "建分享失败：%s" % _pk_err(code, d))
        setp(shareId=share_id, phase="restore", curfile="正在转存到目标账号")

        code, det = pk_api(dst, "GET", "/drive/v1/share?share_id=%s&pass_code=%s"
                           % (urllib.parse.quote(share_id), urllib.parse.quote(pcode)))
        pct = str((det or {}).get("pass_code_token") or "")
        if code >= 400 or not pct:
            return bail("error", "目标账号读分享失败：%s" % _pk_err(code, det))

        code, rs = pk_api(dst, "POST", "/drive/v1/share/restore", {
            "share_id": share_id, "pass_code_token": pct, "file_ids": [],
            "params": {"trace_file_ids": sid}})
        if code >= 400 or (rs or {}).get("error"):
            return bail("error", "转存被拒：%s" % _pk_err(code, rs))
        tid = str((rs or {}).get("restore_task_id") or (rs or {}).get("task_id") or "")
        if tid:
            ok, msg = _pk_wait_task(dst, tid)
            if not ok:
                return bail("error", "转存任务未完成：%s" % msg)

        # restore 的 parent_id 会被忽略，内容固定落在「Pack From Shared」，重名时会自动加 (1)。
        # 不能用「列表前后差集」定位：该目录条目多、接口分页，新文件常常不在第一页；
        # 改成按名字匹配 + created_time 取最新（刚转存的那份一定是最新的）。
        def same_name(n):
            base = re.sub(r"\(\d+\)(?=\.[^.]*$|$)", "", str(n or ""))
            return base == leaf

        landed, lerr = None, ""
        for _ in range(20):        # 任务 Completed 后文件有时还要几十秒才可列
            try:
                items = _pk_ls_all(dst, "Pack From Shared", max_pages=3)
                lerr = ""
            except Exception as ex:
                items, lerr = [], str(ex)[:120]
            hit = [f for f in items if same_name(f.get("name"))]
            if hit:
                hit.sort(key=lambda f: str(f.get("created_time") or ""), reverse=True)
                landed = hit[0].get("name")
                break
            time.sleep(3)
        if not landed:
            return bail("error", "转存已完成，但目标账号「Pack From Shared」里没找到「%s」%s"
                        % (leaf, ("（读取分享区失败：%s）" % lerr) if lerr else "（可直接重试）"))

        # 归位：同账号内移动是服务端操作，零成本
        setp(phase="move", curfile="正在移动到目标目录")
        srcp = "%s:Pack From Shared/%s" % (dst, landed)
        dstp = tgt
        c2, o2, e2 = _xfer_run(job, ["moveto", srcp, dstp, "--stats", "0"])

        # moveto 在同名冲突时可能返回 0 但什么都没动；而且 PikPak 的目录列表有缓存，
        # 不能用「分享区里还在不在」判成败（会误判），只认目标侧实际字节数。
        setp(curfile="正在复核目标文件")
        got, moved_ok = -1, False
        for _ in range(8):
            got = probe_bytes(tgt)
            # 单文件必须字节数完全对上；文件夹只要路径真的存在（空目录也算）
            moved_ok = (got == fsiz) if one_file else (got >= 0)
            if moved_ok:
                break
            time.sleep(3)
        still = False
        if not moved_ok:
            try:
                still = landed in {f.get("name") for f in _pk_ls_all(dst, "Pack From Shared", max_pages=3)}
            except Exception:
                pass
        if c2 != 0 or not moved_ok:
            # 目标侧没拿到东西：清掉刚转存出来的副本，别在分享区堆垃圾
            try:
                _xfer_run(job, ["deletefile", srcp] if one_file else ["purge", srcp], timeout=600)
            except Exception:
                pass
            if c2 != 0:
                tail = ((o2 + e2).strip().splitlines() or [""])[-1][:140]
                return bail("error", "已转存，但移动到目标目录失败（副本已回滚）：%s" % tail)
            if still:
                return bail("error", "转存内容还停在「Pack From Shared/%s」，没能搬进目标目录，"
                                     "请检查目标目录是否已有同名内容后重试" % landed)
            return bail("error", "移动到目标目录后没查到文件（目标 %s / 源 %s），副本已回滚"
                        % (mb(got if got > 0 else 0), mb(fsiz)))

        if job.get("mode") == "move":
            setp(phase="purge", curfile="正在删除源文件" if one_file else "正在删除源文件夹")
            # 单文件用 deletefile，文件夹用 purge（会连空目录一起清掉）
            c3, o3, e3 = _xfer_run(job, ["deletefile", job["src"]] if one_file
                                   else ["purge", job["src"]])
            if c3 != 0:
                return bail("error", "已转存并归位，但删除源%s失败（源仍在）：%s"
                            % ("文件" if one_file else "文件夹",
                               ((o3 + e3).strip().splitlines() or ["未知错误"])[-1][:140]))
        setp(state="done", phase="", speed=0, eta=None, curfile="", error="",
             total=(fsiz or 1), bytes=(fsiz or 1))
    except Exception as e:
        bail("error", str(e)[:250])
    finally:
        with XFER_LOCK:
            XSHARE.discard(job_id)
        if share_id:
            try:
                pk_share_delete(src, share_id)
            except Exception:
                pass


def _sharesave_worker(job_id):
    """外部分享转存：restore → 等任务 → 在「Pack From Shared」找新增项 → 归位。

    复用 kind="xfer" 的任务渲染与 XSHARE 存活登记；不建分享、不删分享、
    不过 NAS 带宽，全程 PikPak 服务端完成。
    """
    job = _xfer_fetch(job_id)
    if not job:
        return
    name = str(job.get("remote") or "")
    sid = str(job.get("shareId") or "")
    tok = str(job.get("passCodeToken") or "")
    ids = job.get("fileIds") or []
    dst_path = str(job.get("dstPath") or "").strip("/")

    def setp(**kw):
        _xfer_patch(job_id, **kw)

    def stopped():
        j = _xfer_fetch(job_id)
        return bool(j and j.get("state") == "stopped")

    def bail(msg):
        setp(state="error", error=str(msg)[:300], phase="", speed=0, eta=None, curfile="")

    setp(state="running", phase="restore", error="", started_ts=time.time(),
         bytes=0, total=0, speed=0, eta=None, curfile="正在提交转存请求")
    with XFER_LOCK:
        XSHARE.add(job_id)
    try:
        # restore 的 parent_id 会被忽略，内容固定落在「Pack From Shared」；
        # 用「转存前后 id 差集」定位新增项，比按名字猜准确（重名会自动加 (1)）。
        try:
            before = {str(f.get("id")) for f in
                      _pk_ls_all(name, "Pack From Shared", max_pages=5, limit=200)}
        except Exception:
            before = set()

        code, rs = pk_api(name, "POST", "/drive/v1/share/restore",
                          build_share_restore_payload(sid, tok, ids,
                                                      job.get("ancestorIds") or []))
        if code >= 400 or (rs or {}).get("error"):
            return bail("转存被拒：%s" % _pk_err(code, rs))
        if stopped():
            return

        tid = str((rs or {}).get("restore_task_id") or (rs or {}).get("task_id") or "")
        if tid:
            setp(phase="restore", curfile="等待 PikPak 完成转存")
            ok, msg = _pk_wait_task(name, tid, timeout=900, sleep=3)
            if not ok:
                return bail("转存任务未完成：%s" % msg)
        if stopped():
            return

        setp(phase="move", curfile="正在定位转存内容")
        added = []
        for _ in range(20):
            try:
                cur = _pk_ls_all(name, "Pack From Shared", max_pages=5, limit=200)
            except Exception:
                cur = []
            added = [f for f in cur if str(f.get("id")) not in before]
            if added:
                break
            time.sleep(3)
        if not added:
            return bail("转存已完成，但没在「Pack From Shared」找到新增内容"
                        "（分享可能为空、内容已存在，或 PikPak 列表有延迟，可直接重试）")

        landed, total = [], 0
        for f in added:
            if stopped():
                return
            nm = str(f.get("name") or "")
            if not nm:
                continue
            try:
                total += int(f.get("size") or 0)
            except (TypeError, ValueError):
                pass
            srcp = "%s:Pack From Shared/%s" % (name, nm)
            dstp = "%s:%s" % (name, (dst_path + "/" + nm) if dst_path else nm)
            setp(curfile="正在归位 " + nm[:40])
            c, o, e = _xfer_run(job, ["moveto", srcp, dstp, "--stats", "0"])
            if c == 0:
                landed.append(nm)
        if not landed:
            return bail("已转存 %d 项，但归位到目标目录全部失败，请检查目标目录是否已有同名内容"
                        % len(added))
        setp(state="done", phase="", speed=0, eta=None, curfile="", error="",
             total=(total or 1), bytes=(total or 1),
             note="已转存 %d 项到 %s:%s" % (len(landed), name, dst_path or "/"))
    except Exception as e:
        bail(str(e)[:250])
    finally:
        with XFER_LOCK:
            XSHARE.discard(job_id)


def job_live(job):
    if job.get("kind") == "unzip":
        # 解压任务由各自的线程推进度（云端轮询 / 本地解压），这里不管进度，
        # 只兜底一种情况：控制台重启过 → 线程没了但状态还停在 running。
        if job.get("state") == "running" and job["id"] not in UNZIP_ALIVE:
            job["state"] = "stopped"
            job["phase"] = ""
            job["curfile"] = ""
            job["error"] = "控制台重启过，解压已中断（重新提交即可，已解出的文件会保留）"
        return
    if job.get("kind") == "xfer":
        # 跨账号转移由独立子进程承担，进度由工作线程自己写盘。
        # 这里只兜底一种情况：控制台重启过 → 进程没了但状态还停在 running。
        with XFER_LOCK:
            alive = job["id"] in XFER or job["id"] in XSHARE
        if job.get("state") == "running" and not alive:
            job["state"] = "stopped"
            job["speed"] = 0
            job["eta"] = None
            job["phase"] = ""
            job["curfile"] = ""
            job["error"] = "控制台重启过，转移已中断（点「继续」会跳过已传完的文件接着传）"
        return
    jid = job.get("jobid")
    if not jid or job["state"] not in ("running",):
        return
    pxy = bool(job.get("proxy"))
    try:
        st = rc("job/status", _timeout=8, _strict=True, _proxy=pxy, jobid=jid)
        job["_miss"] = 0          # 连续失败计数归零（仅 job not found 时累加）
        if st.get("finished"):
            # 完成时补取一次统计，把最终 bytes/total 落盘（否则秒完成/重启后永远是 0/0）
            try:
                s = rc("core/stats", _timeout=8, _proxy=pxy, group="job/%d" % jid)
                job["bytes"] = s.get("bytes", 0)
                job["total"] = s.get("totalBytes", 0)
                job["speed"] = s.get("speed", 0)
                tr = s.get("transferring") or []
                job["files"] = len(tr)
                job["curfile"] = (tr[0].get("name", "") if tr else "")
                track_transferred(job, [])
            except Exception:
                pass
            if st.get("success"):
                job["state"] = "done"
                if not job.get("total"):
                    # v3.43：带过滤的任务 0 字节多半是「全被过滤掉了」，提示要说清是规则造成的
                    if job.get("filterText"):
                        job["error"] = "本次未传输数据：过滤规则可能跳过了全部文件（%s），或目标已存在相同文件" % job["filterText"]
                    else:
                        job["error"] = "0 字节传输：源目录为空，或目标已存在相同文件（无变化）"
            else:
                raw_err = st.get("error") or "未知错误"
                fatal = _fatal_job_error(raw_err)
                job["state"], job["error"] = fatal or ("error", str(raw_err)[:200])
                # 凭证类终态要立刻把对应账号标成「需重新登录」，不用等下一轮保活才发现
                if any(k in raw_err.lower() for k in CRED_FATAL_KEYS):
                    try:
                        src = str(job.get("src") or "")
                        remote = src.split(":", 1)[0] if ":" in src else ""
                        if remote:
                            meta_patch(remote, {"lastErr": "任务失败：" + str(raw_err)[:160],
                                                "lastCheck": int(time.time())})
                    except Exception:
                        pass
            return
        s = rc("core/stats", _timeout=8, _proxy=pxy, group="job/%d" % jid)
        job["bytes"] = s.get("bytes", 0)
        job["total"] = s.get("totalBytes", 0)
        job["speed"] = s.get("speed", 0)
        job["eta"] = s.get("eta")
        tr = s.get("transferring") or []
        job["files"] = len(tr)
        job["curfile"] = (tr[0].get("name", "") if tr else "")
        track_transferred(job, tr)
    except Exception as e:
        msg = str(e)
        if "job not found" in msg or "couldn't find job" in msg:
            # rclone 的 job/status 只保留已完成 job 约 1 分钟。浏览器关闭后前端
            # 轮询停止，会错过 finished；此时 stats 分组仍保留最终字节数，
            # 先据此补判「已下载完成」，避免误报成「守护进程重启、任务中断」。
            try:
                s = rc("core/stats", _timeout=8, _proxy=pxy, group="job/%d" % jid)
                got = int(s.get("bytes") or 0)
                total = int(s.get("totalBytes") or 0)
                if total > 0 and got >= total and not (s.get("transferring") or []):
                    job["bytes"] = got
                    job["total"] = total
                    job["speed"] = s.get("speed", 0)
                    job["eta"] = None
                    job["curfile"] = ""
                    job["state"] = "done"
                    job["error"] = ""
                    job["_miss"] = 0
                    return
            except Exception:
                pass
            # 连续 3 次都查不到且 stats 无法证明完成，才判定为中断。
            # 一旦置 stopped，job_live 不再发 job/status，日志刷屏随之停止。
            n = int(job.get("_miss") or 0) + 1
            job["_miss"] = n
            if n >= 3:
                job["state"] = "stopped"
                job["error"] = "守护进程重启过，任务已中断（点继续即可续传）"
                job["_miss"] = 0
        else:
            job["_miss"] = 0
            fatal = _fatal_job_error(msg)
            if fatal:
                # rclone 侧异步任务可能还在空转重试，尽力停掉（停不掉也不影响状态判定）
                try:
                    rc("job/stop", jobid=jid, _timeout=5, _proxy=pxy)
                except Exception:
                    pass
                job["state"], job["error"] = fatal
            else:
                job["error"] = msg[:200]

def stop_job_entry(j):
    if j.get("kind") == "unzip":
        # 只能停「显示」：PikPak 侧不提供取消解压，本地解压也没有安全中断点
        j["state"] = "stopped"
        j["phase"] = ""
        j["curfile"] = ""
        j["error"] = j.get("error") or "已暂停显示（服务端解压可能仍在继续）"
        return
    if j.get("kind") == "xfer":
        _xfer_kill(j)
        j["state"] = "stopped"
        j["speed"] = 0
        j["eta"] = None
        j["phase"] = ""
        j["curfile"] = ""
        return
    if j["state"] == "running":
        try:
            rc("job/stop", jobid=j["jobid"], _proxy=bool(j.get("proxy")))
        except Exception:
            pass
    j["state"] = "stopped"

# ── v3.57 存储空间自动识别 ────────────────────────────────────────────
# 旧版把下载目录写死成 /vol2/download、磁盘总量写死在前端（926.4 GB）。
# 现在启动时用 docker inspect 把「控制台」与「rclone-backend」两边的挂载按宿主
# Source 路径配对：同一个宿主目录在两个容器里的挂载点即构成一个「存储空间」，
# 挂了几个就识别几个，容量全部实时读取，不再有任何写死的数字。
# docker.sock 不可用等异常情况回退到内置的单卷约定，行为与旧版完全一致。
# v3.80：两个挂载点都改成可用环境变量覆盖 —— 群晖/威联通/rootless 经常不给
# docker.sock，或者用户把下载目录挂到 /downloads 而不是 /vol2/download。此前这类
# 部署只能靠 docker inspect 识别，sock 一不可用就整块功能失效（容量显示 0、
# 本地文件页空白）。现在 CONSOLE_DOWNLOAD_ROOT / RCLONE_DATA_ROOT 直接给出约定值。
# 注意别和 compose 里的 DOWNLOAD_ROOT（宿主机路径）混：那是 volume 左侧，这是容器内右侧。
RCLONE_DATA_ROOT = (os.environ.get("RCLONE_DATA_ROOT") or "/data").strip().rstrip("/") or "/data"
DOWNLOAD_ROOT = (os.environ.get("CONSOLE_DOWNLOAD_ROOT") or "").strip().rstrip("/") or "/vol2/download"
LEGACY_VOLUME = {"rclone": RCLONE_DATA_ROOT, "console": DOWNLOAD_ROOT, "host": ""}


def _self_container_candidates():
    """控制台自身容器名的候选，按优先级：显式配置 → $HOSTNAME（Docker 默认把容器
    短 ID 设成 hostname，`docker inspect` 同样认）→ 历史默认名。改过
    container_name 又忘了设 UPDATE_CONTAINER 时，靠 HOSTNAME 仍能识别多卷。"""
    out = []
    for n in (os.environ.get("UPDATE_CONTAINER"), os.environ.get("HOSTNAME"), "download-console"):
        n = str(n or "").strip()
        if n and n not in out:
            out.append(n)
    return out


def _inspect_mounts(container):
    """docker inspect 拿一个容器的 bind 挂载，返回 [(宿主Source, 容器内Destination)]"""
    pr = subprocess.run(["docker", "inspect", container,
                         "--format", "{{json .Mounts}}"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    out = []
    for m in json.loads(pr.stdout or "[]"):
        src, dst = str(m.get("Source") or ""), str(m.get("Destination") or "")
        if src and dst:
            out.append((src, dst))
    return out

def storage_pairs():
    """识别可用下载卷：宿主 Source 相同的 (控制台挂载点, rclone挂载点) 配对。
    rclone=/data 的主卷排最前（历史 dest 约定）；识别失败回退内置单卷。"""
    try:
        rmap = {}
        for src, dst in _inspect_mounts(RCLONE_CONTAINER):
            rmap.setdefault(src, dst)
        self_mounts = []
        for cand in _self_container_candidates():
            try:
                self_mounts = _inspect_mounts(cand)
            except Exception:
                self_mounts = []
            if self_mounts:
                break
        pairs, seen = [], set()
        for src, cdst in self_mounts:
            rdst = rmap.get(src)
            if rdst and rdst != "/" and rdst not in seen and os.path.isdir(cdst):
                seen.add(rdst)
                pairs.append({"rclone": rdst.rstrip("/"), "console": cdst.rstrip("/") or "/", "host": src})
        if pairs:
            pairs.sort(key=lambda p: (p["rclone"] != RCLONE_DATA_ROOT, p["rclone"]))
            return pairs
    except Exception:
        pass
    return [dict(LEGACY_VOLUME)]

def refresh_storages(s=None):
    """重算每个存储空间的容量（真实值 + 可选配额折算），写入 STATE["storages"]。
    挂载关系在容器生命周期内不变，只识别一次；容量每轮 scheduler 刷新。"""
    if not STATE.get("volumePairs"):
        STATE["volumePairs"] = storage_pairs()
    s = s or load_settings()
    quota = float(s.get("diskQuotaGB") or 0)
    disk_min = float(s.get("diskMinGB") or 0)
    stors = []
    for i, p in enumerate(STATE["volumePairs"], 1):
        try:
            du = shutil.disk_usage(p["console"])
        except OSError:
            continue
        total = du.total / 1073741824.0
        free = du.free / 1073741824.0
        if quota > 0:
            # 配额只做逻辑上限：显示与保护按 配额-整卷已用 折算，物理剩余仍由解压预检兜底
            used = total - free
            total = min(total, quota)
            free = max(0.0, min(free, quota - used))
        stors.append({"name": "存储空间 %d" % i,
                      "rclone": p["rclone"], "console": p["console"], "host": p.get("host", ""),
                      "totalGB": round(total, 1), "freeGB": round(free, 1),
                      "usedPct": round(min(100.0, (total - free) / total * 100.0), 1) if total > 0 else 0.0,
                      "low": free < disk_min})
    STATE["storages"] = stors
    return stors

def storages():
    return STATE.get("storages") or []

def primary_root():
    """主卷（rclone=/data，兼容旧任务）在 storage_pairs 里排第一。"""
    rs = local_roots()
    return rs[0] if rs else LEGACY_VOLUME["console"]

def local_roots():
    return [p["console"] for p in (STATE.get("volumePairs") or [LEGACY_VOLUME])] or [LEGACY_VOLUME["console"]]

def volume_effective_free(pair, s=None):
    """一个卷的有效剩余（GB）：物理剩余与配额剩余取小；卷不可统计时返回 None"""
    try:
        free = shutil.disk_usage(pair["console"]).free / 1073741824.0
    except OSError:
        return None
    quota = float((s or load_settings()).get("diskQuotaGB") or 0)
    if quota > 0:
        used = shutil.disk_usage(pair["console"]).total / 1073741824.0 - free
        free = min(free, quota - used)
    return max(0.0, free)

def dest_volume(dest):
    """rclone 侧路径（/data/xxx、/mnt/disk2/xxx…）→ 所属卷 pair；找不到回退主卷"""
    d = str(dest or "").rstrip("/") or "/"
    best = None
    for p in (STATE.get("volumePairs") or [LEGACY_VOLUME]):
        r = p["rclone"]
        if d == r or d.startswith(r + "/"):
            if best is None or len(r) > len(best["rclone"]):
                best = p
    return best or (STATE.get("volumePairs") or [LEGACY_VOLUME])[0]

def job_volume(job):
    """任务 → 所属卷。下载/URL 任务的 dest 是 rclone 路径；本地解压的 dest 是控制台路径。"""
    lp = str(job.get("dest") or "")
    for p in (STATE.get("volumePairs") or [LEGACY_VOLUME]):
        if lp == p["console"] or lp.startswith(p["console"].rstrip("/") + "/"):
            return p
    return dest_volume(lp)

def disk_free_gb():
    """全局展示口径：所有存储空间里最紧的剩余值。"""
    fr = [st["freeGB"] for st in storages()]
    if fr:
        return min(fr)
    try:
        return round(shutil.disk_usage(LEGACY_VOLUME["console"]).free / 1073741824.0, 1)
    except Exception:
        return None

def local_dest(dest):
    """rclone 侧目标路径 → 本容器挂载路径。
    v3.57：按自动识别的卷映射（rclone 根 → 控制台根，最长前缀优先）；
    映射不到时回退内置约定 /data/xxx → /vol2/download/xxx。"""
    d = str(dest or "")
    best = None
    for p in (STATE.get("volumePairs") or [LEGACY_VOLUME]):
        r = p["rclone"]
        if r and (d == r or d.startswith(r + "/")):
            if best is None or len(r) > len(best["rclone"]):
                best = p
    if best:
        tail = d[len(best["rclone"]):].lstrip("/")
        return best["console"].rstrip("/") + ("/" + tail if tail else "")
    if d.startswith(RCLONE_DATA_ROOT.rstrip("/") + "/"):
        return DOWNLOAD_ROOT.rstrip("/") + "/" + d[len(RCLONE_DATA_ROOT.rstrip("/")) + 1:]
    return None

def completed_transfers(since_ts):
    """从 rclone-backend 日志解析本次运行已完成的文件。
    关键：rclone 落盘会保留源文件 mtime，无法用文件时间区分「本次新下」与「历史已有」，
    唯一可靠来源是日志里的 `... INFO : <name>: Copied (new)/(replaced existing)` 行。"""
    try:
        secs = float(since_ts or 0)
    except Exception:
        secs = 0
    if secs <= 0:
        secs = time.time() - 21600
    # docker 日志时间基准是 UTC（rclone-backend 容器 TZ=UTC），宿主是 CST+8；
    # localtime 拼无时区字符串会让 --since 偏 8 小时（落到未来 → 永远取不到日志）。
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))
    try:
        pr = subprocess.run(["docker", "logs", RCLONE_CONTAINER, "--since", since],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
        txt = (pr.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return []
    pat = re.compile(r"INFO\s*:\s*(.+?):\s*Copied \(")
    names, seen = [], set()
    for ln in txt.splitlines():
        m = pat.search(ln)
        if m:
            n = m.group(1).strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)
    if not names:
        return []
    dests = []
    try:
        for j in load_jobs():
            d = local_dest(j.get("dest") or "")
            if d:
                dests.append(d)
    except Exception:
        pass
    out = []
    for n in names:
        size = 0
        for d in dests:
            try:
                s = os.path.getsize(os.path.join(d, n))
                if s:
                    size = s
                    break
            except OSError:
                pass
        out.append({"name": n, "size": size, "bytes": size})
    return out

SPD_LOCK = threading.Lock()   # stats 请求线程与 scheduler 采样线程并发调用，读改写必须串行

def instant_speed(cur):
    """rclone core/stats 的 speed 是"进程启动至今的平均速度"，连续跑几天后几乎不动，
    历史图上就是一条 6.99 MiB/s 的直线。改为用两次采样之间的字节差算瞬时速度，
    再做一次指数平滑压抖动。返回 (速度B/s, 区间内新增字节)。"""
    now = time.time()
    with SPD_LOCK:                    # v3.57：加专用小锁，避免两条线程交错污染 EMA
        pb, pt = STATE.get("spdBytes"), STATE.get("spdTime")
        STATE["spdBytes"], STATE["spdTime"] = cur, now
        if pb is None or pt is None:
            return 0.0, 0
        if cur < pb:                      # rclone 重启，bytes 归零
            STATE["spdEma"] = 0.0
            return 0.0, 0
        dt, delta = now - pt, cur - pb
        if dt < 0.3:                      # 两次轮询挨太近，用上次的平滑值
            return float(STATE.get("spdEma") or 0.0), 0
        raw = delta / dt
        ema = STATE.get("spdEma")
        ema = raw if not ema else ema * 0.6 + raw * 0.4
        STATE["spdEma"] = ema
        return round(ema, 1), delta


def load_history():
    h = _read_json(HISTORY_FILE, None)
    if not isinstance(h, dict):
        return {"samples": [], "daily": {}}
    h.setdefault("samples", [])
    h.setdefault("daily", {})
    return h

def save_history(h):
    try:
        _write_json(HISTORY_FILE, h, indent=None, quiet=True)
    except Exception:
        pass

def sample_history():
    """调度器每 20s 采样一次全局速度与增量，落盘供历史图表使用"""
    try:
        st = merged_stats(_timeout=8)
        cur = int(st.get("bytes", 0) or 0)
        now = int(time.time())
        h = load_history()
        prev = STATE.get("histBytes")
        if prev is not None and cur >= prev:
            day = time.strftime("%F")
            h["daily"][day] = h.get("daily", {}).get(day, 0) + (cur - prev)
        STATE["histBytes"] = cur
        spd, _ = instant_speed(cur)        # 历史点也用瞬时值，不再是一条直线
        h["samples"].append([now, round(spd, 1)])   # 单位 B/s，前端除以 1048576 显示 MiB/s
        cutoff = now - 26 * 3600
        if h["samples"] and h["samples"][0][0] < cutoff:
            h["samples"] = [s for s in h["samples"] if s[0] >= cutoff]
        while len(h["daily"]) > DAILY_KEEP:
            h["daily"].pop(min(h["daily"]), None)
        save_history(h)
    except Exception:
        pass

def poll_jobs_live():
    """后端兜底轮询任务终态：不再依赖浏览器一直开着。
    与 jobs action 同样在 LOCK 内执行，保证 jobs.json 读写串行。"""
    with LOCK:
        jobs = load_jobs()
        before = json.dumps(jobs, ensure_ascii=False, sort_keys=True)
        for j in jobs:
            if j["state"] == "running":
                job_live(j)
        if json.dumps(jobs, ensure_ascii=False, sort_keys=True) != before:
            save_jobs(jobs)


def scheduler():
    """后台调度线程：并发上限、排队晋级、磁盘保护、定时启停（每 20 秒）"""
    while True:
        try:
            with LOCK:
                s = load_settings()
                jobs = load_jobs()
                # v3.57：每轮刷新存储空间容量（含配额折算）；磁盘保护按任务的目标卷判断，
                # 一个卷满了只停目标在该卷的任务，别的卷照常下载。
                stors = refresh_storages(s)
                frs = [st["freeGB"] for st in stors]
                if frs:
                    STATE["diskFree"] = min(frs)
                    tight = min(stors, key=lambda st: st["freeGB"])
                    STATE["diskTotal"] = tight["totalGB"]
                disk_min = float(s["diskMinGB"] or 0)
                STATE["diskLow"] = any(st["low"] for st in stors)
                def vol_low(j):
                    vf = volume_effective_free(job_volume(j), s)
                    return vf is not None and vf < disk_min
                running = [j for j in jobs if j["state"] == "running"]
                changed = False   # 只在任务状态真的变化时才写盘，避免每 20s 无谓 I/O
                for j in running:
                    if vol_low(j):
                        stop_job_entry(j)
                        j["error"] = "磁盘空间保护触发，已自动暂停"
                        changed = True
                for j in jobs:
                    if len(running) >= s["maxConcurrent"]:
                        break
                    if j["state"] == "queued" and not vol_low(j) and launch_job(j, s):
                        running.append(j)
                        changed = True
                if s["autoStart"] and s["autoPause"]:
                    now = time.strftime("%H:%M")
                    today = time.strftime("%F")
                    a, b = s["autoStart"], s["autoPause"]
                    inwin = (a <= now < b) if a <= b else (now >= a or now < b)
                    if inwin and STATE.get("firedStart") != today:
                        STATE["firedStart"] = today
                        STATE["firedPause"] = None
                        n = 0
                        for j in jobs:
                            if j["state"] in ("stopped", "error"):
                                j["state"] = "queued"
                                j["error"] = ""
                                n += 1
                        if n:
                            changed = True
                            print("%s 定时启动: %d 个任务进入队列" % (time.strftime("%F %T"), n))
                    if not inwin and STATE.get("firedPause") != today:
                        STATE["firedPause"] = today
                        if running:
                            for j in running:
                                stop_job_entry(j)
                            changed = True
                            print("%s 定时暂停: 已停止 %d 个任务" % (time.strftime("%F %T"), len(running)))
                if changed:
                    save_jobs(jobs)
            # 历史采样要请求 rclone RC，放到锁外执行：rclone 卡住时不再阻塞所有 API
            sample_history()
            poll_jobs_live()
            # v3.42：分享提取上限巡检（5 分钟一次，放在锁外，不阻塞任何 API）
            try:
                if time.time() - float(STATE.get("shareCheckAt") or 0) > 300:
                    STATE["shareCheckAt"] = time.time()
                    _share_auto_cancel_tick()
            except Exception as e:
                print("%s 分享巡检异常: %s" % (time.strftime("%F %T"), str(e)[:120]))
        except Exception as e:
            print("%s scheduler异常: %s" % (time.strftime("%F %T"), e))
        time.sleep(20)

# ---------------------------------------------------------------------------
# 本地文件页（v3.15）：只读浏览所有已识别的存储空间，在线播放 + 下载到本机
#   · 只开放 GET/HEAD 读取，不提供写/删接口，避免误操作破坏下载成果
#   · 所有路径经 realpath 校验，禁止 ../ 与符号链接跳出下载卷
#   · v3.57：/vol2/download 单卷 → 多卷（顶层空路径先列存储空间，路径可为绝对路径）
# ---------------------------------------------------------------------------
MEDIA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".ogv": "video/ogg", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".flv": "video/x-flv", ".ts": "video/mp2t",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".ogg": "audio/ogg", ".flac": "audio/flac",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".pdf": "application/pdf",
    ".srt": "text/plain; charset=utf-8", ".ass": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".nfo": "text/plain; charset=utf-8",
}
PLAYABLE_VIDEO = {".mp4", ".m4v", ".webm", ".ogv", ".mov"}
PLAYABLE_AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
PLAYABLE_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
BROWSER_OK = PLAYABLE_VIDEO | PLAYABLE_AUDIO | PLAYABLE_IMAGE | {".pdf"}
MAX_LISTING = 2000


def local_kind(ext):
    if ext in PLAYABLE_VIDEO or ext in (".mkv", ".avi", ".flv", ".ts", ".m3u8"):
        return "video"
    if ext in PLAYABLE_AUDIO:
        return "audio"
    if ext in PLAYABLE_IMAGE:
        return "image"
    if ext == ".pdf":
        return "pdf"
    return "other"


def safe_local_path(rel):
    """本地文件页路径 → 绝对路径。任何越界（../ 或软链接跳出下载卷）一律拒绝。
    v3.57 支持两种写法：绝对路径（/vol2/download/kz，须落在任一存储空间内），
    以及相对主卷的旧格式（kz/kz/0501.mp4，老链接与一致性校验沿用）。"""
    parts = [p for p in str(rel or "").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("路径越界")
    if str(rel or "").startswith("/"):
        if not parts:
            raise ValueError("路径越界")
        full = os.path.realpath(os.sep + os.sep.join(parts))
        for r in local_roots():
            root = os.path.realpath(r)
            if full == root or full.startswith(root + os.sep):
                return full
        raise ValueError("路径越界")
    root = os.path.realpath(primary_root())
    full = os.path.realpath(os.path.join(root, *parts))
    if full != root and not full.startswith(root + os.sep):
        raise ValueError("路径越界")
    return full


def media_local_path(rel):
    """`/media/<rel>` 里的 <rel> → 绝对路径（v3.79）。

    v3.57 起本地文件页按「控制台绝对路径」导航（/vol2/download/kz），前端据此
    拼出的播放地址是 `/media/vol2/download/kz/x.mp4`。而 /media/ 之后的部分天然
    没有前导斜杠，safe_local_path 会把它当「相对主卷」，去找
    `/vol2/download/vol2/download/kz/x.mp4` —— 必然 404。
    后果：所有本地视频都是黑屏卡在 0:00（video 收 404 直接 error），下载到本机
    与 M3U 导出拿到的是同一批坏链接。
    这里先把 <rel> 按卷根补回前导斜杠，再交给 safe_local_path 做越界校验。
    """
    r = str(rel or "").lstrip("/")
    if r:
        for root in local_roots():
            root = str(root or "").rstrip("/")
            bare = root.lstrip("/")
            if bare and (r == bare or r.startswith(bare + "/")):
                return safe_local_path("/" + r)
    return safe_local_path(rel)


# ---------------------------------------------------------------------------
# v3.44 本地（NAS 侧）解压：PikPak 云端解压要会员，这条路径不需要任何会员，
#   直接对 /vol2/download 里已下载的压缩包动手。
#   · zip 用标准库（能按 cp437 还原 GBK/UTF-8 文件名，Linux 的 unzip 常把中文糊掉）
#   · tar 系列手工遍历条目，顺便挡掉符号链接与 ../ 越界
#   · 7z / rar 需要额外二进制，NAS 上没有 → 明确告知，不做半吊子实现
# ---------------------------------------------------------------------------
LOCAL_UNZIP_MAX_FILES = 20000
LOCAL_UNZIP_MAX_BYTES = 200 * 1024 ** 3


def local_archive_kind(name):
    low = str(name or "").lower()
    if low.endswith(".zip"):
        return "zip"
    if re.search(r"\.(tar|tar\.gz|tgz|tar\.bz2|tbz2|tar\.xz|txz)$", low):
        return "tar"
    if low.endswith(".7z"):
        return "7z"
    if low.endswith(".rar"):
        return "rar"
    return ""


def archive_stem(name):
    low = str(name or "").lower()
    for suf in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".tar"):
        if low.endswith(suf):
            return str(name)[:-len(suf)]
    return os.path.splitext(str(name))[0]


def fmt_bytes(n):
    v = float(n or 0)
    return ("%.2f GB" % (v / 1073741824)) if v >= 1073741824 else (
           ("%.1f MB" % (v / 1048576)) if v >= 1048576 else ("%.0f KB" % (v / 1024)))


def _zip_member_name(info):
    """压缩包内文件名解码：没置 UTF-8 标志的名字按 cp437 落地，中文包会变乱码。"""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except Exception:
        return name
    for enc in ("utf-8", "gbk", "big5"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return name


def _safe_join(root, rel):
    """解压落地路径：拒绝 ../ 与绝对路径，符号链接也会被 realpath 挡下。"""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, *parts)) if parts else base
    if full != base and not full.startswith(base + os.sep):
        return None
    return full


def local_unzip_start(rel, password="", delete_after=False):
    try:
        src = safe_local_path(rel)
    except ValueError as e:
        return {"error": mask_secret(str(e))}
    if not os.path.isfile(src):
        return {"error": "文件不存在或已被移动"}
    base = os.path.basename(src)
    kind = local_archive_kind(base)
    if kind in ("7z", "rar"):
        return {"error": "NAS 上没有 7z / unrar，暂时只能解 zip 与 tar 系列；可以先把文件下载到本机再解压"}
    if not kind:
        return {"error": "只支持 zip / tar / tar.gz / tgz / tar.bz2 / tar.xz"}
    dest = os.path.join(os.path.dirname(src), archive_stem(base))
    # v3.57：越界校验按「任一存储空间」判断（rel 可以是第二卷的绝对路径）
    rdest = os.path.realpath(dest)
    if not any(rdest == os.path.realpath(r) or rdest.startswith(os.path.realpath(r) + os.sep)
               for r in local_roots()):
        return {"error": "解压目标越界"}
    try:
        free = shutil.disk_usage(os.path.dirname(src)).free
    except OSError:
        free = 0
    job = {"id": int(time.time() * 1000), "kind": "unzip", "engine": "local",
           "remote": "NAS", "subpath": str(rel or "").strip("/"),
           "name": base, "mode": "unzip", "state": "running",
           "started": time.strftime("%m-%d %H:%M"), "bytes": 0, "total": 0,
           "speed": 0, "eta": None, "error": "", "pctNum": 0, "phase": "准备解压",
           "dest": os.path.dirname(src).rstrip("/") + "/" + os.path.basename(dest),
           "freeBytes": free}
    with LOCK:
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)
    UNZIP_ALIVE.add(job["id"])
    threading.Thread(target=_local_unzip_worker,
                     args=(job["id"], src, dest, str(password or ""), bool(delete_after), kind),
                     daemon=True).start()
    return {"job": job}


def _local_unzip_worker(job_id, src, dest, password, delete_after, kind):
    files, skipped = 0, 0
    try:
        os.makedirs(dest, exist_ok=True)
        if kind == "zip":
            with zipfile.ZipFile(src) as z:
                infos = z.infolist()
                if len(infos) > LOCAL_UNZIP_MAX_FILES:
                    raise RuntimeError("压缩包内条目过多（%d），超过上限 %d" % (len(infos), LOCAL_UNZIP_MAX_FILES))
                total = sum(max(0, i.file_size) for i in infos) or 1
                if total > LOCAL_UNZIP_MAX_BYTES:
                    raise RuntimeError("解压后体积超过上限 200 GB")
                free = shutil.disk_usage(dest).free
                if total > free * 0.92:
                    raise RuntimeError("磁盘剩余空间不足（需要 %s，剩余 %s）" % (
                        fmt_bytes(total), fmt_bytes(free)))
                pwd = password.encode("utf-8") if password else None
                done = 0
                for n, info in enumerate(infos):
                    name = _zip_member_name(info)
                    target = _safe_join(dest, name)
                    if not target:
                        skipped += 1
                        continue
                    if name.endswith("/"):
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    try:
                        with z.open(info, pwd=pwd) as fp, open(target, "wb") as out:
                            shutil.copyfileobj(fp, out, 1 << 20)
                    except RuntimeError as e:
                        msg = str(e)
                        if "password" in msg.lower() or "encrypted" in msg.lower():
                            raise RuntimeError("密码不正确，或该压缩包需要密码" if password
                                               else "压缩包已加密，请输入密码")
                        raise
                    files += 1
                    done += max(0, info.file_size)
                    if n % 5 == 0:
                        _job_patch(job_id, pctNum=min(99.0, done * 100.0 / total),
                                   curfile=name, phase="解压中")
        else:
            with tarfile.open(src) as t:
                members = t.getmembers()
                if len(members) > LOCAL_UNZIP_MAX_FILES:
                    raise RuntimeError("压缩包内条目过多（%d），超过上限 %d" % (len(members), LOCAL_UNZIP_MAX_FILES))
                total = sum(max(0, m.size) for m in members) or 1
                if total > LOCAL_UNZIP_MAX_BYTES:
                    raise RuntimeError("解压后体积超过上限 200 GB")
                free = shutil.disk_usage(dest).free
                if total > free * 0.92:
                    raise RuntimeError("磁盘剩余空间不足（需要 %s，剩余 %s）" % (
                        fmt_bytes(total), fmt_bytes(free)))
                done = 0
                for n, m in enumerate(members):
                    target = _safe_join(dest, m.name)
                    if not target:
                        skipped += 1
                        continue
                    if m.isdir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    if not m.isfile():
                        skipped += 1        # 符号链接/设备文件不落地
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    fp = t.extractfile(m)
                    if not fp:
                        skipped += 1
                        continue
                    with fp, open(target, "wb") as out:
                        shutil.copyfileobj(fp, out, 1 << 20)
                    files += 1
                    done += max(0, m.size)
                    if n % 5 == 0:
                        _job_patch(job_id, pctNum=min(99.0, done * 100.0 / total),
                                   curfile=m.name, phase="解压中")
        note = ""
        if delete_after and files:
            try:
                os.remove(src)
                note = " · 原压缩包已删除"
            except OSError:
                note = " · 原压缩包删除失败"
        _job_patch(job_id, state="done", pctNum=100, phase="", files=files,
                   skipped=skipped, curfile="",
                   error=("已解压 %d 个文件" % files) + (("，跳过 %d 个越界/链接条目" % skipped) if skipped else "") + note)
    except Exception as e:
        _job_patch(job_id, state="error", phase="", curfile="",
                   error=mask_secret(str(e))[:200])
    finally:
        UNZIP_ALIVE.discard(job_id)


DIR_COUNT_LIMIT = 999   # 子目录条目数只数到这个值就停（显示 999+），避免大目录卡死列表


def _fs_name(name):
    """文件名兜底。NAS 上从 Windows 拷来的 GBK 文件名在 Linux 里是非法 UTF-8 字节，
    Python 用 surrogateescape 解码成孤立代理字符：json 能序列化（ensure_ascii 会转成
    \\udcxx），但前端 encodeURIComponent 遇到孤立代理会抛 URIError —— 整个播放/下载
    按钮点下去直接异常。这里替换成 U+FFFD，并回报 badname 让前端提示改名。"""
    s = str(name)
    try:
        s.encode("utf-8")
        return s, False
    except UnicodeEncodeError:
        return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace"), True


def _count_children(path):
    """数一层子项，最多数到 DIR_COUNT_LIMIT；目录不可读时返回 0。"""
    n = 0
    try:
        with os.scandir(path) as it:
            for _ in it:
                n += 1
                if n >= DIR_COUNT_LIMIT:
                    break
    except OSError:
        return 0
    return n


def localfiles_listing(rel):
    """列一个目录。path/parent 供前端面包屑；dirs/files 已按名称排序。
    v3.57：空路径返回存储空间顶层——每个卷作为一个目录条目（带 volumeRoot，
    前端点进后 path 用该卷的绝对路径）。"""
    if not str(rel or "").strip("/"):
        dirs = []
        for st in storages():
            try:
                count = 0
                with os.scandir(st["console"]) as it:
                    for e in it:
                        try:
                            if e.is_dir():
                                count += 1
                        except OSError:
                            continue
                        if count >= DIR_COUNT_LIMIT:
                            break
            except OSError:
                count = 0
            dirs.append({"name": st["name"], "count": count, "mtime": 0,
                         "volumeRoot": st["console"],
                         "freeGB": st["freeGB"], "totalGB": st["totalGB"]})
        return {"path": "", "parent": None, "volumes": storages(),
                "dirs": dirs, "files": [], "truncated": False}
    try:
        p = safe_local_path(rel)
    except ValueError as e:
        return {"error": mask_secret(str(e))}
    if not os.path.isdir(p):
        return {"error": "目录不存在或已被移动"}
    segs = [x for x in str(rel or "").split("/") if x not in ("", ".", "..")]
    dirs, files, truncated = [], [], False
    try:
        names = sorted(os.listdir(p), key=lambda s: s.lower())
    except OSError as e:
        return {"error": "读取目录失败：%s" % str(e)[:120]}
    for raw_name in names:
        if raw_name.startswith("."):
            continue                      # 隐藏 .ytdlp 等内部目录
        name, bad = _fs_name(raw_name)
        fp = os.path.join(p, raw_name)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if len(dirs) + len(files) >= MAX_LISTING:
            truncated = True
            break
        if os.path.isdir(fp):
            dirs.append({"name": name, "count": _count_children(fp), "mtime": int(st.st_mtime),
                         "badname": bad})
        elif os.path.isfile(fp):
            ext = os.path.splitext(name)[1].lower()
            item = {"name": name, "size": st.st_size, "mtime": int(st.st_mtime),
                    "ext": ext, "kind": local_kind(ext), "playable": ext in BROWSER_OK,
                    "archive": bool(local_archive_kind(name))}
            if bad:
                # 名字编码坏了 → 拼出来的 URL 一定打不开，前端据此禁用播放/下载
                item["badname"] = True
            files.append(item)
    # v3.57 面包屑：绝对路径保持前导斜杠；卷根的上一级是顶层存储空间列表
    is_abs = str(rel or "").startswith("/")
    path = ("/" if is_abs else "") + "/".join(segs)
    rp = os.path.realpath(p)
    parent = ""
    if not any(rp == os.path.realpath(r) for r in local_roots()):
        up = segs[:-1]
        parent = ("/" + "/".join(up)) if (is_abs and up) else "/".join(up)
    return {"path": path, "parent": parent,
            "dirs": dirs, "files": files, "truncated": truncated}

# ---------------------------------------------------------------------------
# v3.50 本地（NAS 侧）批量重命名：与云盘「批量改名」共用前端规则
#   （模板 / 正则替换 / 序号，先预览后执行），这里直接改 /vol2/download 里的真实条目。
#   · 只接受单层名称；safe_local_path 挡掉 ../ 与软链接越界
#   · 整批预检：非法名 / 重名 / 目标已存在 / 源不存在，一律不动手
#   · 两阶段改名：先把全部源挪到隐藏临时名，再统一改成目标名，
#     交换式改名（A→B 且 B→C）也不会撞车；异常时逐个回滚
# ---------------------------------------------------------------------------
LOCAL_RENAME_TMP = ".__wb_rename_tmp__"


def _local_rename_ok_name(n):
    if _bad_entry_name(n):
        return False
    s = str(n)
    # 隐藏项与 rclone 传输中的临时文件不在本地文件页出现，也不允许被改名/占用
    if s.startswith(".") or s.lower().endswith((".part", ".partial", ".rclone-partial")):
        return False
    return True


def local_rename_batch(rel, items):
    """批量重命名 /vol2/download 下某目录的条目，返回 {"ok": n, "failed": [...]}。"""
    try:
        folder = safe_local_path(rel)
    except ValueError as e:
        return {"error": mask_secret(str(e))}
    if not os.path.isdir(folder):
        return {"error": "目录不存在或已被移动"}
    if not isinstance(items, list) or not items:
        return {"error": "缺少参数：items（要重命名的文件列表）"}
    if len(items) > RENAME_MAX_ITEMS:
        return {"error": "一次最多重命名 %d 项，请分批处理" % RENAME_MAX_ITEMS}

    clean, failed = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        a = str(it.get("from") or "").strip()
        b = str(it.get("to") or "").strip()
        if not _local_rename_ok_name(a) or not _local_rename_ok_name(b):
            failed.append({"name": a or "?", "error": "名称不合法（不能含 /、隐藏名或临时名）"})
            continue
        if a == b:
            continue                     # 没变化，跳过不计失败
        clean.append((a, b))
    if not clean:
        return {"ok": 0, "failed": failed}

    targets = [b for _, b in clean]
    dup = sorted({t for t in targets if targets.count(t) > 1})
    if dup:
        return {"error": "新名字重复：%s" % "、".join(dup[:3]), "conflicts": dup}
    try:
        existing = set(os.listdir(folder))
    except OSError as e:
        return {"error": "读取当前目录失败：%s" % str(e)[:120]}
    srcs = {a for a, _ in clean}
    blocked = [t for t in targets if t in existing and t not in srcs]
    if blocked:
        return {"error": "目标名称已存在：%s" % "、".join(blocked[:3]), "conflicts": blocked}
    missing = [a for a, _ in clean if a not in existing]
    if missing:
        return {"error": "源文件已不存在：%s" % "、".join(missing[:3]), "conflicts": missing}

    root = os.path.realpath(folder)
    for a, b in clean:
        src, dst = os.path.join(folder, a), os.path.join(folder, b)
        # 软链接本身与指向目录外的条目一律拒绝，避免把改名变成越界写入
        if os.path.islink(src) or os.path.islink(dst):
            return {"error": "出于安全考虑不支持改名软链接：%s" % a}
        if os.path.dirname(os.path.realpath(src)) != root or \
           os.path.dirname(os.path.realpath(dst)) != root:
            return {"error": "路径越界：%s" % a}

    stamp = "%d_%d" % (int(time.time() * 1000), threading.get_ident())
    tmps, used = [], set(existing)
    for i in range(len(clean)):
        t = "%s%d_%s" % (LOCAL_RENAME_TMP, i, stamp)
        while t in used:
            t += "_"
        used.add(t)
        tmps.append(t)

    staged = {}
    try:
        for (a, _), t in zip(clean, tmps):
            os.rename(os.path.join(folder, a), os.path.join(folder, t))
            staged[a] = t
    except OSError as e:
        for a, t in staged.items():
            try:
                os.rename(os.path.join(folder, t), os.path.join(folder, a))
            except OSError:
                pass
        return {"error": "准备改名失败，已回滚：%s" % mask_secret(str(e))[:150]}

    ok, renamed = 0, []
    for (a, b), t in zip(clean, tmps):
        try:
            os.rename(os.path.join(folder, t), os.path.join(folder, b))
            ok += 1
            renamed.append((os.path.join(folder, a), os.path.join(folder, b)))
        except OSError as e:
            try:
                os.rename(os.path.join(folder, t), os.path.join(folder, a))
            except OSError:
                pass
            failed.append({"name": a, "error": mask_secret(str(e))[:120]})
    # v3.81 修订：本地改名要同步文件夹基线里的路径映射，否则「下载完毕」会掉标
    if renamed:
        folder_remap_paths(renamed)
    return {"ok": ok, "failed": failed}


# ---------------------------------------------------------------------------
# 一致性校验（v3.16）：云盘 ↔ /vol2/download 落盘内容比对。全程只读，
#   不写云盘、不写本地磁盘、不改动任何任务/账号数据。
#   · quick  : 递归清单 + 文件大小（秒级，不读文件内容）
#   · sample : quick + 对「大小一致」的文件抽查头/中/尾 3 段字节
#              （rclone cat --offset/--count，走 HTTP Range，只拉几十 KB）
#   · full   : quick + 对「大小一致」的文件做完整 SHA256 比对
#              （会把云端内容重新读一遍用于计算，严格限额 + 明显警示）
#   PikPak 后端不提供对象哈希，所以内容校验必须读字节；抽样是默认推荐档。
# ---------------------------------------------------------------------------
VERIFY_MAX_FILES   = 5000
VERIFY_CHUNK       = 32 * 1024
VERIFY_SAMPLE_MAX  = 20
VERIFY_FULL_FILES  = 5
VERIFY_FULL_TOTAL  = 1024 ** 3             # 单次完整校验最多 1 GiB
VERIFY_FULL_SINGLE = 300 * 1024 ** 2       # 单文件最多 300 MiB
VERIFY_TIMEOUT     = 300                   # 整次校验软上限（秒）
RCLONE_BASE = ["docker", "exec", RCLONE_CONTAINER, "rclone"]


def _rclone_run(args, timeout=90):
    return subprocess.run(RCLONE_BASE + list(args), capture_output=True, timeout=timeout)


def _norm_key(s):
    """路径归一化：Unicode NFC（云盘/macOS 常用 NFD）、统一分隔符、去掉 ./ 片段。"""
    s = unicodedata.normalize("NFC", str(s or "").replace("\\", "/"))
    return "/".join(p for p in s.split("/") if p not in ("", "."))


def _junk_name(n):
    """跳过隐藏项与 rclone 传输中的临时文件；__speedtest.tmp 等真实文件保留。"""
    return n.startswith(".") or n.lower().endswith((".part", ".partial", ".rclone-partial"))


def _decode(b):
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b or "")


SCAN_MAX_FILES = 6000          # 单次递归扫描的文件上限（超出即标记 truncated）
SCAN_TIMEOUT = 180             # 秒；PikPak 逐目录列，太深会慢，宁可让用户缩小范围


def cloud_scan(remote, path="", depth=6):
    """递归列云端（相对路径 + 字节 + 修改时间），供目录树 / CSV 导出 / 查重使用。

    v3.43：前端「当前目录」的导出与查重是纯前端计算（零请求、看得到什么导什么），
    勾了「含子目录」才走这里 —— 一次 lsjson -R 拿全，最多 SCAN_MAX_FILES 条。
    全程只读，不写云盘、不产生下载流量。
    """
    sub = str(path or "").strip("/")
    target = "%s:%s" % (remote, sub)
    try:
        d = max(1, min(12, int(depth or 6)))
    except Exception:
        d = 6
    t0 = time.time()
    try:
        p = _rclone_run(["lsjson", "--recursive", "--log-level", "ERROR",
                         "--max-depth", str(d), target], timeout=SCAN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "扫描超时（%d 秒）：目录太大，请缩小范围或减少层数" % SCAN_TIMEOUT}
    except Exception as e:
        return {"error": "扫描失败：%s" % str(e)[:150]}
    if p.returncode != 0:
        return {"error": "扫描失败：%s" % (_decode(p.stderr).strip()[:180] or target)}
    try:
        lst = json.loads(_decode(p.stdout) or "[]")
    except Exception:
        return {"error": "云端返回无法解析"}
    if isinstance(lst, dict):
        lst = [lst]
    files, dirs, truncated = [], [], False
    for e in lst:
        if not isinstance(e, dict):
            continue
        rel = _norm_key(e.get("Path") or e.get("Name"))
        if not rel or rel == ".." or rel.startswith("../"):
            continue
        if e.get("IsDir"):
            dirs.append(rel)
            continue
        files.append({"p": rel, "s": int(e.get("Size") or 0),
                      "m": str(e.get("ModTime") or "")[:19]})
        if len(files) >= SCAN_MAX_FILES:
            truncated = True
            break
    return {"remote": remote, "path": sub, "depth": d,
            "files": files, "dirs": dirs, "truncated": truncated,
            "elapsed": round(time.time() - t0, 2)}


def verify_cloud(remote, path):
    """云端递归列文件。返回结构化 dict：
       files{rel:size}、targets{rel:完整 remote:path}（内容校验直接用，不用再拼路径）、
       scope(file/dir)、target（本次根目录）、truncated、error。"""
    target = "%s:%s" % (remote, path)
    ret = {"files": {}, "targets": {}, "scope": "", "target": target,
           "truncated": False, "error": ""}
    try:
        p = _rclone_run(["lsjson", "--stat", "--log-level", "ERROR", target], timeout=60)
    except Exception as e:
        ret["error"] = "云端连接失败：%s" % str(e)[:150]
        return ret
    if p.returncode != 0:
        ret["error"] = "云端路径不存在或无法访问：%s" % (
            _decode(p.stderr).strip()[:180] or target)
        return ret
    try:
        st = json.loads(_decode(p.stdout) or "{}")
    except Exception:
        ret["error"] = "云端返回无法解析"
        return ret
    # 单文件：lsjson --stat 直接给出该对象
    if isinstance(st, dict) and st.get("IsDir") is False:
        name = _norm_key(st.get("Name") or os.path.basename(path))
        ret["files"][name] = int(st.get("Size") or 0)
        ret["targets"][name] = target
        ret["scope"] = "file"
        return ret
    try:
        p = _rclone_run(["lsjson", "--recursive", "--files-only", "--log-level", "ERROR", target],
                        timeout=150)
    except Exception as e:
        ret["error"] = "云端列目录超时：%s" % str(e)[:120]
        return ret
    if p.returncode != 0:
        ret["error"] = "云端列目录失败：%s" % _decode(p.stderr).strip()[:180]
        return ret
    try:
        lst = json.loads(_decode(p.stdout) or "[]")
    except Exception:
        ret["error"] = "云端列表无法解析"
        return ret
    if isinstance(lst, dict):
        lst = [lst]
    for e in lst:
        if not isinstance(e, dict) or e.get("IsDir"):
            continue
        k = _norm_key(e.get("Path") or e.get("Name"))
        if not k or k == ".." or k.startswith("../"):
            continue
        # 与本地保持同一套过滤规则，否则云端的 .DS_Store 之类会被误报成「本地缺失」
        if any(_junk_name(seg) for seg in k.split("/")):
            continue
        ret["files"][k] = int(e.get("Size") or 0)
        ret["targets"][k] = _cloud_file_target(target, "dir", k)
        if len(ret["files"]) >= VERIFY_MAX_FILES:
            ret["truncated"] = True
            break
    ret["scope"] = "dir"
    return ret


def verify_local(rel):
    """本地递归列文件。返回 (dict{rel: {size, abs}}, scope, absPath, truncated, err)"""
    try:
        p = safe_local_path(rel)
    except ValueError as e:
        return {}, "", "", False, str(e)
    # v3.57：软链接越界基准改为 p 所属的存储空间根（rel 可指向任一卷）
    root = os.path.realpath(primary_root())
    for r in local_roots():
        rp = os.path.realpath(r)
        if p == rp or p.startswith(rp + os.sep):
            root = rp
            break
    if os.path.isfile(p):
        return {_norm_key(os.path.basename(p)): {"size": os.path.getsize(p), "abs": p}}, \
               "file", p, False, ""
    if not os.path.isdir(p):
        return {}, "", p, False, "本地路径不存在或已被移动"
    out, truncated = {}, False
    for cur, dnames, fnames in os.walk(p, followlinks=False):
        dnames[:] = [d for d in dnames if not _junk_name(d)]
        for fn in fnames:
            if _junk_name(fn):
                continue
            fp = os.path.join(cur, fn)
            try:
                rp = os.path.realpath(fp)
                if rp != root and not rp.startswith(root + os.sep):
                    continue                       # 软链接跳出下载根目录
                st = os.stat(fp)
                if not statmod.S_ISREG(st.st_mode):
                    continue
            except OSError:
                continue
            out[_norm_key(os.path.relpath(fp, p))] = {"size": st.st_size, "abs": fp}
            if len(out) >= VERIFY_MAX_FILES:
                truncated = True
                break
        if truncated:
            break
    return out, "dir", p, truncated, ""


def local_download_index(limit=VERIFY_MAX_FILES, with_paths=False):
    """云盘预览「已下载」判定索引：递归扫全部存储空间，返回 {(归一化文件名, 字节数)}。

    落盘位置不固定 —— 单文件可能是 /data/<文件名> 或 /data/<保存目录>/<文件名>，
    整个云端目录下载则是 /data/<目录名>/...，所以不能按云端的相对路径去查，
    也不能只扫根层。用「同名 + 同大小」判定可以覆盖以上所有情况，同时自动
    排除 .partial / .part 未下完的临时文件以及大小不同的旧版本。

    v3.57 起逐个扫描自动识别的存储空间；文件量小（几十~几百），每次列目录
    实时扫一遍即可：不做缓存，下载一完成标签就出现。
    """
    # with_paths=True 时值改成「第一个命中的绝对路径」，供文件夹基线记住
    # 「云端文件落在 NAS 的哪个位置」；同名同大小有多份时保留扫描到的第一份。
    idx = {} if with_paths else set()
    try:
        for r in local_roots():
            root = os.path.realpath(r)
            for cur, dnames, fnames in os.walk(root, followlinks=False):
                dnames[:] = [d for d in dnames if not _junk_name(d)]
                for fn in fnames:
                    if _junk_name(fn):
                        continue
                    fp = os.path.join(cur, fn)
                    try:
                        st = os.stat(fp)
                        if not statmod.S_ISREG(st.st_mode):
                            continue
                    except OSError:
                        continue
                    k = (_norm_key(fn), int(st.st_size))
                    if with_paths:
                        idx.setdefault(k, fp)
                    else:
                        idx.add(k)
                    if len(idx) >= limit:
                        return idx
    except Exception:
        pass
    return idx


# ── v3.81：云端文件夹「下载完毕 / 有更新」三态 ───────────────────────────────
# 需求：只有「整个文件夹都下完」才打标；打标之后云端再加入新文件，改显示「有更新」；
#       其余情况一律不显示任何标识；「有更新」必须用户手动确认才消失。
#
# 判定口径（用户在 v3.81 明确选定）：本地文件逐项比对。递归列出该云端文件夹下的
# 所有文件，逐个查 NAS 上是否有「同名 + 同大小」的文件，全部命中才算下载完毕。
# 沿用 local_download_index() 的既有口径，手动拷进 NAS 的文件也算数。
#
# 关键设计：
#   · 基线 seen = 最后一次「判定完毕」或「用户点确认」时的云端文件签名集合。
#     added = 当前云端集合 - seen，added>0 即「有更新」，与本地是否已补齐无关。
#     这样「已完毕 → 云端加新文件（本地还没下）」时标识不会闪一下就消失。
#   · 点确认只推进 seen，不伪造「已下载」：补齐了显示「下载完毕」，没补齐就无标识。
#   · 云端递归列目录较重，独立成 folderstatus 接口由前端异步请求，不拖慢 dirs；
#     带 5 分钟内存缓存 + 单次最多 24 个文件夹 + 12 秒总预算 + 失败隔离。
#   · 签名用「文件夹内相对路径|字节数」：删文件不误报，改名/新增会报（合理）。
# ────────────────────────────────────────────────────────────────────────────
FOLDER_STATE_FILE = os.path.join(DATA_DIR, "folder_state.json")
FOLDER_STATE_LOCK = threading.Lock()
FOLDER_SCAN_TIMEOUT = 20          # 单个文件夹递归列目录的硬超时（秒）
FOLDER_SCAN_MAX = 6000            # 单文件夹文件数上限（与 SCAN_MAX_FILES 对齐）；
                                  # 真机实测 2141 文件的目录 6.6s 可列完，1500 太保守
FOLDER_SCAN_DEPTH = 6
FOLDER_STATUS_TTL = 300           # 云端扫描结果缓存（秒）
FOLDER_STATUS_MAX_DIRS = 24       # 单次请求最多判定多少个文件夹
FOLDER_STATUS_BUDGET = 12.0       # 单次请求总时间预算（秒）
_FOLDER_SCANS = {}                # (remote, path) -> (ts, sigs, count, bytes, err)
_FOLDER_LOCAL_IDX = (0.0, set())  # (ts, 本地索引)，15 秒内复用，避免重复扫全卷


def _folder_key(remote, path):
    """remote + 路径做 key 时用 JSON 数组编码，避免名字里含分隔符造成碰撞。"""
    return json.dumps([str(remote or ""), _norm_key(path)],
                      ensure_ascii=False, separators=(",", ":"))


def load_folder_state():
    d = _read_json(FOLDER_STATE_FILE, {})
    return d.get("folders") if isinstance(d, dict) and isinstance(d.get("folders"), dict) else {}


def save_folder_state(folders):
    # 长期在几百个目录间浏览会不断累积基线，给状态文件一个上限，避免无限膨胀
    if len(folders) > 400:
        keep = sorted(folders.items(),
                      key=lambda kv: -(kv[1].get("ts") or 0) if isinstance(kv[1], dict) else 0)
        folders = dict(keep[:300])
    _write_json(FOLDER_STATE_FILE, {"v": 1, "folders": folders}, quiet=True)


def folder_cloud_files(remote, path, refresh=False):
    """递归列云端文件夹，返回 (签名集合, 数量, 总字节, 错误)。"""
    ck = (str(remote or ""), _norm_key(path))
    hit = _FOLDER_SCANS.get(ck)
    if not refresh and hit and time.time() - hit[0] < FOLDER_STATUS_TTL:
        return hit[1], hit[2], hit[3], hit[4]
    sigs, total, err = set(), 0, ""
    try:
        target = "%s:%s" % (remote, str(path or "").strip("/"))
        p = _rclone_run(["lsjson", "--recursive", "--max-depth", str(FOLDER_SCAN_DEPTH),
                         "--log-level", "ERROR", target], timeout=FOLDER_SCAN_TIMEOUT)
        if p.returncode != 0:
            err = (_decode(p.stderr).strip()[:120] or "列目录失败")
        else:
            rows = json.loads(_decode(p.stdout) or "[]")
            if isinstance(rows, dict):
                rows = [rows]
            for e in rows:
                if not isinstance(e, dict) or e.get("IsDir"):
                    continue
                rel = _norm_key(e.get("Path") or e.get("Name"))
                if not rel:
                    continue
                size = int(e.get("Size") or 0)
                sigs.add(rel + "|" + str(size))
                total += size
                if len(sigs) >= FOLDER_SCAN_MAX:
                    err = "文件过多（≥%d），无法判定" % FOLDER_SCAN_MAX
                    break
    except subprocess.TimeoutExpired:
        err = "列目录超时"
    except Exception as e:
        err = mask_secret(str(e))[:120]
    _FOLDER_SCANS[ck] = (time.time(), sigs, len(sigs), total, err)
    # 长期浏览大量目录会让缓存无上限增长，超过 500 条时丢掉最老的一半
    if len(_FOLDER_SCANS) > 500:
        for k in sorted(_FOLDER_SCANS, key=lambda k: _FOLDER_SCANS[k][0])[:250]:
            _FOLDER_SCANS.pop(k, None)
    return sigs, len(sigs), total, err


def folder_local_index():
    """带 15 秒 TTL 的本地索引，返回 {(归一化文件名, 字节数): 绝对路径}。

    v3.81 修订：值从「集合」改成「路径」，基线才能记住每个云端文件落在 NAS 哪里，
    本地改名之后不再因为名字对不上而掉标。上限 30000 条，15 秒缓存抵消全卷扫描成本。
    """
    global _FOLDER_LOCAL_IDX
    now = time.time()
    if _FOLDER_LOCAL_IDX[1] and now - _FOLDER_LOCAL_IDX[0] < 15:
        return _FOLDER_LOCAL_IDX[1]
    paths = local_download_index(limit=30000, with_paths=True)
    _FOLDER_LOCAL_IDX = (now, paths)
    return paths


def folder_match(sigs, paths, mapping=None):
    """返回 (命中数, {云端签名: 本地绝对路径})。

    优先用基线里记住的路径 + 大小判定（本地改名 / 移动时由 folder_remap_paths 同步
    更新这条路径）；路径不存在或大小变了才回退到「同名 + 同大小」全局索引。
    """
    mapping = mapping if isinstance(mapping, dict) else {}
    hit, out = 0, {}
    for sig in sigs:
        rel, _, sz = sig.rpartition("|")
        try:
            size = int(sz)
        except ValueError:
            continue
        name = _norm_key(rel.rsplit("/", 1)[-1])
        real = ""
        p = mapping.get(sig)
        if p:
            try:
                if os.path.isfile(p) and os.path.getsize(p) == size:
                    real = p
            except OSError:
                pass
        if not real:
            real = paths.get((name, size)) or ""
        if real:
            hit += 1
            out[sig] = real
    return hit, out


def folder_transfer_map(sigs, seen, mapping):
    """云端改文件名时，把旧签名记住的本地路径转交给同大小的新签名。

    不做这件事的话：云端一改名，新签名在 map 里查不到、按名字回退又对不上，
    文件夹会凭空掉标。签名里的「大小」足够把一进一出的两项配对起来。
    """
    new_sigs, gone_sigs = sigs - seen, seen - sigs
    if not new_sigs or not gone_sigs:
        return mapping
    pool = {}
    for g in gone_sigs:
        p = mapping.get(g)
        if p:
            pool.setdefault(g.rpartition("|")[2], []).append(p)
    out = {k: v for k, v in mapping.items() if k not in gone_sigs}
    for n in sorted(new_sigs):
        cands = pool.get(n.rpartition("|")[2])
        if cands:
            out[n] = cands.pop(0)
    return out


def folder_remap_paths(pairs):
    """本地文件改名 / 移动 / 删除后，同步 folder_state.json 里记住的路径。

    pairs: [(旧绝对路径, 新绝对路径或 None)]。None = 删掉该条映射（文件确实不在了，
    判定应正确掉标；之后从回收站恢复时靠全局索引自动重新命中并写回映射）。
    用 realpath 做键，绕开 Windows / 软链接的路径写法差异。
    """
    lut = {}
    for old, new in (pairs or []):
        if not old:
            continue
        lut[os.path.realpath(old)] = os.path.realpath(new) if new else None
    if not lut:
        return
    with FOLDER_STATE_LOCK:
        folders = load_folder_state()
        changed = False
        for rec in folders.values():
            if not isinstance(rec, dict) or not isinstance(rec.get("map"), dict):
                continue
            nm, touched = {}, False
            for sig, p in rec["map"].items():
                rp = os.path.realpath(p)
                if rp in lut:
                    touched = True
                    if lut[rp]:
                        nm[sig] = lut[rp]
                else:
                    nm[sig] = p
            if touched:
                rec["map"] = nm
                changed = True
        if changed:
            save_folder_state(folders)


def folder_one_status(remote, parent, d, refresh=False):
    """单个文件夹的三态判定。返回给前端的 dict 里 state 只有 ""/done/updated。"""
    name = str(d.get("name") or "")
    path = _cloud_join(parent, name)
    key = _folder_key(remote, path)
    sigs, cnt, byts, err = folder_cloud_files(remote, path, refresh=refresh)
    if err or not sigs:
        # 扫描失败或空文件夹：不显示任何标识，也不建立基线（避免误判）
        return {"state": "", "total": cnt, "added": 0, "hit": 0, "error": err}
    paths = folder_local_index()
    with FOLDER_STATE_LOCK:
        folders = load_folder_state()
        rec = folders.get(key)
        mapping = rec.get("map") if isinstance(rec, dict) and isinstance(rec.get("map"), dict) else {}
        seen = set(rec.get("seen") or []) if isinstance(rec, dict) else set()
        new_sigs, gone_sigs = sigs - seen, seen - sigs
        # 「净增」才算有新文件加入：云端改文件名（一进一出）净增 0，不误报「有更新」。
        # 顺带把旧签名的本地路径转交给新签名，否则云端改名会让文件夹凭空掉标。
        added = max(0, len(new_sigs) - len(gone_sigs)) if rec else 0
        if rec and added == 0 and new_sigs and gone_sigs:
            mapping = folder_transfer_map(sigs, seen, mapping)
        hit, curmap = folder_match(sigs, paths, mapping)
        complete = hit >= len(sigs)
        if not rec:
            if complete:
                rec = {"seen": sorted(sigs), "ts": int(time.time()), "map": curmap}
                folders[key] = rec
                save_folder_state(folders)
        else:
            nr = dict(rec)
            touched = False
            if added == 0 and (new_sigs or gone_sigs):
                nr["seen"] = sorted(sigs)     # 吸收改名 / 删除，避免下次重复计算
                touched = True
            if complete and (curmap != mapping or nr.get("map") != curmap):
                nr["map"] = curmap
                touched = True
            if touched:
                folders[key] = nr
                save_folder_state(folders)
            rec = nr
        state = "updated" if (rec and added) else ("done" if (rec and complete) else "")
    return {"state": state, "total": len(sigs), "added": added,
            "hit": hit, "complete": complete}


def folder_status_batch(remote, parent, items, refresh=False):
    """并发判定当前目录下若干文件夹；总预算到点即返回已算完的部分。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if not isinstance(items, list):
        return {"status": {}, "partial": False}
    items = [x for x in items if isinstance(x, dict) and x.get("name")][:FOLDER_STATUS_MAX_DIRS]
    if not items:
        return {"status": {}, "partial": False}
    status, done, t0 = {}, 0, time.time()
    folder_local_index()      # 先预热一次，避免 3 个线程同时各扫一遍全卷
    ex = ThreadPoolExecutor(max_workers=3)
    futs = {ex.submit(folder_one_status, remote, parent, d, refresh): str(d["name"])
            for d in items}
    try:
        for fut in as_completed(futs, timeout=FOLDER_STATUS_BUDGET):
            nm = futs[fut]
            try:
                status[nm] = fut.result()
            except Exception as e:
                status[nm] = {"state": "", "total": 0, "added": 0,
                              "hit": 0, "error": mask_secret(str(e))[:120]}
            done += 1
    except Exception:
        # as_completed 超时：没算完的文件夹保持无标识，不影响其余结果
        pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    partial = done < len(items)
    return {"status": status, "partial": partial,
            "elapsed": round(time.time() - t0, 2)}


def folder_ack(remote, parent, names):
    """手动确认「有更新」：把基线推进到当前云端集合，不伪造下载状态。"""
    if not isinstance(names, list) or not names:
        return {"error": "没有要确认的文件夹"}
    names = [str(n) for n in names if str(n or "").strip()][:FOLDER_STATUS_MAX_DIRS]
    with FOLDER_STATE_LOCK:
        folders = load_folder_state()
        out = {}
        for name in names:
            path = _cloud_join(parent, name)
            key = _folder_key(remote, path)
            sigs, cnt, byts, err = folder_cloud_files(remote, path, refresh=True)
            if err or not sigs:
                out[name] = {"state": "", "error": err or "文件夹是空的或不可读"}
                continue
            prev = folders.get(key) if isinstance(folders.get(key), dict) else {}
            prev_map = prev.get("map") if isinstance(prev.get("map"), dict) else {}
            # 沿用已有的路径映射：本地改过名也能正确判定「确认后仍是下载完毕」
            hit, curmap = folder_match(sigs, folder_local_index(), prev_map)
            folders[key] = {"seen": sorted(sigs), "ts": int(time.time()),
                            "ack": int(time.time()), "map": curmap}
            out[name] = {"state": "done" if hit >= len(sigs) else "",
                         "total": len(sigs), "added": 0, "complete": hit >= len(sigs)}
        save_folder_state(folders)
    return {"ok": True, "status": out}


# ── v3.81：云端 / 本地文件完整 CRUD ──────────────────────────────────────────
# 已有的能力：cloudrm（单条删）、cloudmv / renamebatch（重命名）、localrenamebatch。
# 本轮补齐：新建文件夹、批量移动（同账号 / 同卷）、批量删除、本地可恢复回收站。
# 安全边界：
#   · 所有本地路径都过 safe_local_path()（挡 ../ 与越界软链接）；名称过 _bad_entry_name
#   · 移动前逐项预检源存在 / 目标无同名 / 不能把文件夹移进自己
#   · 回收站固定在每个存储卷根的隐藏目录下；localfiles_listing 与 local_download_index
#     都会跳过以 "." 开头的条目，所以回收站里的文件不会被误判成「已下载」
#   · 彻底清空前校验条目路径确实落在回收站目录内，避免被篡改的 manifest 指向任意路径
# ────────────────────────────────────────────────────────────────────────────
LOCAL_TRASH_DIRNAME = ".cloud-shuttle-trash"
LOCAL_TRASH_FILE = os.path.join(DATA_DIR, "local_trash.json")
LOCAL_TRASH_LOCK = threading.Lock()
MOVE_MAX_ITEMS = 200


def _crud_name(n):
    """单层名称校验；返回干净名字或空串。"""
    s = str(n or "").strip()
    if _bad_entry_name(s) or s == LOCAL_TRASH_DIRNAME:
        return ""
    return s


def _within_roots(p):
    """realpath 后的路径是否落在任一存储卷内。"""
    rp = os.path.realpath(p)
    for r in local_roots():
        rr = os.path.realpath(r)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def _local_parent(path):
    """目录路径 → 绝对路径。顶层（""）是虚拟的存储空间列表，不允许作为操作目标。"""
    if not str(path or "").strip("/"):
        raise ValueError("请先进入一个目录再操作")
    p = safe_local_path(path)
    if not os.path.isdir(p):
        raise ValueError("目录不存在或已被移动")
    return p


def _local_volume_root(p):
    """找出 p 所属的存储卷根（realpath）。"""
    rp = os.path.realpath(p)
    for r in local_roots():
        rr = os.path.realpath(r)
        if rp == rr or rp.startswith(rr + os.sep):
            return rr
    return os.path.realpath(primary_root())


def _resolve_local_name(parent, name):
    """前端传来的名字可能是替换后的 U+FFFD（非法 UTF-8 文件名），反查磁盘真实条目名。
    这样编码异常的文件也能被移动 / 删除 / 进回收站，不必先改名。"""
    if os.path.lexists(os.path.join(parent, name)):
        return name
    try:
        for raw in os.listdir(parent):
            conv, _bad = _fs_name(raw)
            if conv == name:
                return raw
    except OSError:
        pass
    return None


def local_mkdir(path, name):
    name = _crud_name(name)
    if not name:
        return {"error": "名称不合法（不能为空、含路径分隔符、超过 180 字或使用保留名）"}
    try:
        parent = _local_parent(path)
    except ValueError as e:
        return {"error": str(e)}
    if os.path.lexists(os.path.join(parent, name)) or _resolve_local_name(parent, name):
        return {"error": "已存在同名文件或文件夹"}
    try:
        os.mkdir(os.path.join(parent, name))
    except OSError as e:
        return {"error": "创建失败：" + mask_secret(str(e))[:120]}
    return {"ok": True, "name": name}


def cloud_mkdir(remote, parent, name):
    name = _crud_name(name)
    if not name:
        return {"error": "名称不合法（不能为空、含路径分隔符、超过 180 字或使用保留名）"}
    if not remote:
        return {"error": "缺少参数：remote"}
    lst, err = cloud_list(remote, parent)
    if err:
        return {"error": "读取云端目录失败：" + mask_secret(err)[:150]}
    if any(str(e.get("Name") or "") == name for e in lst):
        return {"error": "云端已存在同名文件或文件夹"}
    path = _cloud_join(parent, name)
    # 用 CLI `rclone mkdir`（与 cloudmove 的 moveto 同一条通道），不依赖 rc
    # operations/mkdir 的参数细节；rclone mkdir 对已存在目录是幂等的，上面已先查重名。
    try:
        code, out, e2 = rclone_cli(["mkdir", remote + ":" + path, "--log-level", "ERROR"],
                                   timeout=60)
        if code != 0:
            raise RuntimeError((e2 or out or "创建失败")[:150])
    except Exception as e:
        return {"error": mask_secret(str(e))[:150]}
    return {"ok": True, "name": name, "path": path}


def _move_result(results, name, err=None):
    results.append({"name": name, "ok": not bool(err), **({"error": err} if err else {})})


def local_move_batch(path, items, dest):
    if not isinstance(items, list) or not items:
        return {"error": "没有要移动的条目"}
    try:
        src_parent = _local_parent(path)
        dst_parent = _local_parent(dest)
    except ValueError as e:
        return {"error": str(e)}
    if os.path.realpath(src_parent) == os.path.realpath(dst_parent):
        return {"error": "目标目录和当前目录相同"}
    results, ok, moved = [], 0, []
    batch = items[:MOVE_MAX_ITEMS]
    for n in batch:
        name = _crud_name(n)
        if not name:
            _move_result(results, str(n), "名称不合法")
            continue
        real = _resolve_local_name(src_parent, name)
        if real is None:
            _move_result(results, name, "源不存在")
            continue
        src = os.path.join(src_parent, real)
        dst = os.path.join(dst_parent, name)
        if os.path.lexists(dst):
            _move_result(results, name, "目标目录已有同名条目")
            continue
        if os.path.islink(src) and not _within_roots(src):
            _move_result(results, name, "软链接指向卷外，禁止移动")
            continue
        if os.path.isdir(src):
            rsrc, rdst = os.path.realpath(src), os.path.realpath(dst_parent)
            if rsrc == rdst or rdst.startswith(rsrc + os.sep):
                _move_result(results, name, "不能把文件夹移进自己")
                continue
        try:
            shutil.move(src, dst)
            ok += 1
            moved.append((src, dst))
            _move_result(results, name)
        except Exception as e:
            _move_result(results, name, mask_secret(str(e))[:120])
    if moved:
        folder_remap_paths(moved)
    return {"ok": ok, "total": len(batch), "results": results}


def cloud_move_batch(remote, parent, items, dest):
    if not remote:
        return {"error": "缺少参数：remote"}
    if not isinstance(items, list) or not items:
        return {"error": "没有要移动的条目"}
    d = _norm_key(dest)
    if d == _norm_key(parent):
        return {"error": "目标目录和当前目录相同"}
    lst, err = cloud_list(remote, d)
    if err:
        return {"error": "目标目录不可读：" + mask_secret(err)[:150]}
    exists = {str(e.get("Name") or "") for e in lst}
    results, ok = [], 0
    batch = items[:MOVE_MAX_ITEMS]
    for n in batch:
        name = _crud_name(n)
        if not name:
            _move_result(results, str(n), "名称不合法")
            continue
        src = _cloud_join(parent, name)
        nsrc = _norm_key(src)
        if d and (d == nsrc or d.startswith(nsrc + "/")):
            _move_result(results, name, "不能把文件夹移进自己")
            continue
        if name in exists:
            _move_result(results, name, "目标目录已有同名条目")
            continue
        dstp = _cloud_join(d, name)
        try:
            code, out, e2 = rclone_cli(
                ["moveto", remote + ":" + src, remote + ":" + dstp, "--log-level", "ERROR"],
                timeout=90)
            if code != 0:
                raise RuntimeError(mask_secret((e2 or out or "移动失败")[:150]))
            exists.add(name)
            ok += 1
            _move_result(results, name)
        except Exception as e:
            _move_result(results, name, mask_secret(str(e))[:150])
    return {"ok": ok, "total": len(batch), "results": results}


def cloud_rm_batch(remote, parent, items, hard=False):
    if not remote:
        return {"error": "缺少参数：remote"}
    if not isinstance(items, list) or not items:
        return {"error": "没有要删除的条目"}
    lst, err = cloud_list(remote, parent)
    if err:
        return {"error": "读取云端目录失败：" + mask_secret(err)[:150]}
    kind = {str(e.get("Name") or ""): bool(e.get("IsDir")) for e in lst}
    results, ok = [], 0
    batch = items[:MOVE_MAX_ITEMS]
    for n in batch:
        name = _crud_name(n)
        if not name or name not in kind:
            _move_result(results, str(n), "名称不合法或云端已无此条目")
            continue
        path = _cloud_join(parent, name)
        try:
            if hard:
                r = cloud_hard_delete(remote, path, not kind[name])
                if isinstance(r, dict) and r.get("error"):
                    raise RuntimeError(str(r["error"]))
            elif kind[name]:
                rc("operations/purge", _strict=True, fs=remote + ":", remote=path)
            else:
                rc("operations/deletefile", _strict=True, fs=remote + ":", remote=path)
            ok += 1
            _move_result(results, name)
        except Exception as e:
            _move_result(results, name, mask_secret(str(e))[:150])
    return {"ok": ok, "total": len(batch), "results": results}


def _trash_slot_ok(rec):
    """校验 manifest 条目确实落在某个卷根的回收站目录里（防止被篡改后误删任意路径）。"""
    try:
        root = os.path.realpath(str(rec.get("root") or ""))
        if not root or not _within_roots(root):
            return False
        tr = os.path.realpath(os.path.join(root, LOCAL_TRASH_DIRNAME))
        slot = os.path.dirname(os.path.realpath(str(rec.get("stored") or "")))
        return slot.startswith(tr + os.sep)
    except Exception:
        return False


def local_trash_batch(path, items):
    if not isinstance(items, list) or not items:
        return {"error": "没有要删除的条目"}
    try:
        parent = _local_parent(path)
    except ValueError as e:
        return {"error": str(e)}
    root = _local_volume_root(parent)
    trash_root = os.path.join(root, LOCAL_TRASH_DIRNAME)
    results, ok, trashed = [], 0, []
    batch = items[:MOVE_MAX_ITEMS]
    with LOCAL_TRASH_LOCK:
        manifest = _read_json(LOCAL_TRASH_FILE, {})
        if not isinstance(manifest, dict):
            manifest = {}
        changed = False
        for n in batch:
            name = _crud_name(n)
            if not name:
                _move_result(results, str(n), "名称不合法")
                continue
            real = _resolve_local_name(parent, name)
            if real is None:
                _move_result(results, name, "源不存在")
                continue
            # 回收站索引是 JSON：真实名若含孤立代理字符（Windows 拷入的 GBK 名）
            # 会让 _write_json 抛 UnicodeEncodeError —— 文件已移走、索引却写不进去，
            # 等于条目「消失」。这类文件先用铅笔重命名再删除。
            if _fs_name(real)[1]:
                _move_result(results, name, "文件名编码异常，请先用铅笔重命名后再删除")
                continue
            src = os.path.join(parent, real)
            if os.path.realpath(src) == root:
                _move_result(results, name, "不能删除存储空间根目录")
                continue
            try:
                os.makedirs(trash_root, exist_ok=True)
                bid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
                slot = os.path.join(trash_root, bid)
                os.makedirs(slot, exist_ok=True)
                dst = os.path.join(slot, real)
                shutil.move(src, dst)
                manifest[bid] = {"id": bid, "name": name, "from": parent, "root": root,
                                 "stored": dst, "ts": int(time.time()),
                                 "isdir": os.path.isdir(dst)}
                ok += 1
                changed = True
                trashed.append((src, None))
                _move_result(results, name)
            except Exception as e:
                _move_result(results, name, mask_secret(str(e))[:120])
        if changed:
            _write_json(LOCAL_TRASH_FILE, manifest, quiet=True)
    # 文件确实不在原位了 → 删掉基线映射，让「下载完毕」正确掉标；
    # 从回收站恢复后靠全局索引自动重新命中，映射会再次写回。
    if trashed:
        folder_remap_paths(trashed)
    return {"ok": ok, "total": len(batch), "results": results}


def local_trash_list():
    m = _read_json(LOCAL_TRASH_FILE, {})
    items = []
    for bid, rec in (m.items() if isinstance(m, dict) else []):
        if not isinstance(rec, dict) or not _trash_slot_ok(rec):
            continue
        items.append({"id": str(bid), "name": str(rec.get("name") or ""),
                      "from": str(rec.get("from") or ""),
                      "ts": int(rec.get("ts") or 0),
                      "isdir": bool(rec.get("isdir")),
                      "exists": os.path.lexists(str(rec.get("stored") or ""))})
    items.sort(key=lambda x: -x["ts"])
    return {"items": items, "total": len(items)}


def _trash_targets(ids, all_items):
    m = _read_json(LOCAL_TRASH_FILE, {})
    if not isinstance(m, dict):
        return {}, []
    if all_items:
        keys = [k for k, v in m.items() if isinstance(v, dict) and _trash_slot_ok(v)]
    else:
        keys = [str(i) for i in (ids or []) if str(i) in m
                and isinstance(m[str(i)], dict) and _trash_slot_ok(m[str(i)])]
    return m, keys


def local_trash_restore(ids, all_items=False):
    with LOCAL_TRASH_LOCK:
        m, keys = _trash_targets(ids, all_items)
        results, ok = [], 0
        for bid in keys:
            rec = m[bid]
            stored = str(rec.get("stored") or "")
            origin = str(rec.get("from") or "")
            if not os.path.lexists(stored):
                _move_result(results, rec.get("name"), "回收站里已无此条目")
                continue
            if not _within_roots(origin):
                _move_result(results, rec.get("name"), "原位置不在存储空间内，已跳过")
                continue
            dst = os.path.join(origin, os.path.basename(stored))
            if os.path.lexists(dst):
                _move_result(results, rec.get("name"), "原位置已有同名条目，请先移走")
                continue
            try:
                os.makedirs(origin, exist_ok=True)
                shutil.move(stored, dst)
                try:
                    os.rmdir(os.path.dirname(stored))
                except OSError:
                    pass
                del m[bid]
                ok += 1
                _move_result(results, rec.get("name"))
            except Exception as e:
                _move_result(results, rec.get("name"), mask_secret(str(e))[:120])
        _write_json(LOCAL_TRASH_FILE, m, quiet=True)
    return {"ok": ok, "total": len(keys), "results": results}


def local_trash_purge(ids, all_items=False):
    with LOCAL_TRASH_LOCK:
        m, keys = _trash_targets(ids, all_items)
        results, ok = [], 0
        for bid in keys:
            rec = m[bid]
            stored = str(rec.get("stored") or "")
            try:
                if os.path.isdir(stored) and not os.path.islink(stored):
                    shutil.rmtree(stored)
                elif os.path.lexists(stored):
                    os.remove(stored)
                try:
                    os.rmdir(os.path.dirname(stored))
                except OSError:
                    pass
                del m[bid]
                ok += 1
                _move_result(results, rec.get("name"))
            except Exception as e:
                _move_result(results, rec.get("name"), mask_secret(str(e))[:120])
        _write_json(LOCAL_TRASH_FILE, m, quiet=True)
    return {"ok": ok, "total": len(keys), "results": results}


def _cloud_file_target(cloud_target, cloud_scope, rel):
    if cloud_scope == "file":
        return cloud_target
    return (cloud_target + rel) if cloud_target.endswith(":") \
        else (cloud_target.rstrip("/") + "/" + rel)


def _cloud_bytes(target, off, cnt, timeout=30):
    p = _rclone_run(["cat", "--log-level", "ERROR", "--offset", str(off),
                     "--count", str(cnt), target], timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(_decode(p.stderr).strip()[:120] or "rclone cat 失败")
    return p.stdout


def _local_bytes(fp, off, cnt):
    with open(fp, "rb") as f:
        f.seek(off)
        return f.read(cnt)


def _cloud_sha256(target, deadline):
    """流式读云端文件内容算 SHA256。用 select 做超时保护，stderr 直接丢弃避免管道堵塞。"""
    if os.name == "nt":
        # Windows（仅本地开发/单测）：没有可用的管道 select，退回一次性读取
        p = _rclone_run(["cat", "--log-level", "ERROR", target], timeout=180)
        if p.returncode != 0:
            raise RuntimeError(_decode(p.stderr).strip()[:120] or "rclone cat 失败")
        return hashlib.sha256(p.stdout).hexdigest()
    proc = subprocess.Popen(RCLONE_BASE + ["cat", "--log-level", "ERROR", target],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    h, total = hashlib.sha256(), 0
    try:
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError("校验总耗时超限")
            ready, _, _ = select.select([proc.stdout], [], [], min(3.0, left))
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(proc.stdout.fileno(), 1 << 20)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
            if total > VERIFY_FULL_TOTAL:
                raise RuntimeError("完整校验读取量超限")
        proc.wait(timeout=5)
        if proc.returncode != 0:
            raise RuntimeError("云端读取中断（rclone cat 退出码 %s）" % proc.returncode)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        try:
            proc.stdout.close()
        except Exception:
            pass
    return h.hexdigest()


def _local_sha256(fp):
    h = hashlib.sha256()
    with open(fp, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify_compare(remote, cloud_path, local_path, mode, limit):
    """比对并返回结构化结果。所有失败都以 {"error": ...} 返回，不抛到 Handler。"""
    t0 = time.time()
    deadline = t0 + VERIFY_TIMEOUT
    remote = str(remote or "").strip()
    cloud_path = str(cloud_path or "").strip().strip("/")
    # v3.57：localPath 支持绝对路径（跨卷校验）；strip 只影响展示用的相对形式
    local_abs_input = str(local_path or "").startswith("/")
    raw_local = str(local_path or "")
    local_path = str(local_path or "").strip().strip("/")
    mode = str(mode or "quick").strip().lower()
    if not re.match(r'^[^:/\\\x00-\x1f]{1,80}$', remote):
        return {"error": "云端账号名不合法（请选择列表里的账号）"}
    if any(seg == ".." for seg in cloud_path.split("/")) or "\x00" in cloud_path \
            or len(cloud_path) > 500:
        return {"error": "云端路径不合法"}
    if mode not in ("quick", "sample", "full"):
        return {"error": "校验模式不合法"}
    try:
        limit = max(1, min(int(limit or 6), VERIFY_SAMPLE_MAX))
    except Exception:
        limit = 6

    ci = verify_cloud(remote, cloud_path)
    if ci["error"]:
        return {"error": ci["error"]}
    cloud, ctargets = ci["files"], ci["targets"]
    cscope, ctarget, ctrunc = ci["scope"], ci["target"], ci["truncated"]
    local, lscope, ltarget, ltrunc, err = verify_local(raw_local if local_abs_input else local_path)
    if err:
        return {"error": err}
    warnings = []
    # 常见场景：本地只选了一个文件，云端填的是它所在的目录。
    # 按文件名在云端目录里定位同名对象，避免整目录 vs 单文件被报成「全部缺失 + 全部多余」。
    if lscope == "file" and cscope == "dir" and len(local) == 1:
        base = next(iter(local))
        if base in ctargets:
            cloud = {base: cloud[base]}
            ctargets = {base: ctargets[base]}
        else:
            cand = [k for k in cloud if _norm_key(k.rsplit("/", 1)[-1]) == base]
            if len(cand) == 1:
                cloud = {base: cloud[cand[0]]}
                ctargets = {base: ctargets[cand[0]]}
            else:
                warnings.append("云端目录里没有唯一的同名文件（%s），请直接填云端文件完整路径" % base[:60])
        cscope = "file"
    if cscope != lscope:
        warnings.append("云端是%s、本地是%s，两侧层级不一致；建议校验同一个目录或同一个文件"
                        % ("单个文件" if cscope == "file" else "目录",
                           "单个文件" if lscope == "file" else "目录"))
    if not cloud and not local:
        warnings.append("两侧都没有文件，无法比较内容")
    if ctrunc or ltrunc:
        warnings.append("单侧文件超过 %d 个，本次只比较前 %d 个" % (VERIFY_MAX_FILES, VERIFY_MAX_FILES))

    items = []
    for k in sorted(set(cloud) | set(local)):
        c, l = cloud.get(k), local.get(k)
        if c is not None and l is not None:
            status = "match" if c == l["size"] else "size"
            note = "" if status == "match" else "大小不同（可能未下完 / 文件被改动）"
        elif c is not None:
            status, note = "missing", "云端有、本地没有（尚未下载或已被清理）"
        else:
            status, note = "extra", "本地有、云端没有（云端已删除 / 其他来源文件）"
        items.append({"path": k, "status": status,
                      "cloudSize": c if c is not None else None,
                      "localSize": l["size"] if l is not None else None,
                      "note": note})

    deep_checked, deep_bytes = 0, 0
    candidates = [it for it in items if it["status"] == "match" and lscope == cscope]
    if mode in ("sample", "full") and candidates:
        cap = VERIFY_SAMPLE_MAX if mode == "sample" else VERIFY_FULL_FILES
        take = min(limit, cap)
        chosen = candidates[:take]
        if mode == "full":
            total_bytes = sum(it["localSize"] or 0 for it in chosen)
            if any((it["localSize"] or 0) > VERIFY_FULL_SINGLE for it in chosen):
                return {"error": "完整校验的单文件上限是 %d MiB，所选文件更大；请改用「抽样校验」"
                                 % (VERIFY_FULL_SINGLE // 1048576)}
            if total_bytes > VERIFY_FULL_TOTAL:
                return {"error": "完整校验需重新读取 %.1f GB 云端内容（上限 1 GiB）；请减少文件数或改用「抽样校验」"
                                 % (total_bytes / 1073741824)}
        if len(candidates) > take:
            warnings.append("有 %d 个大小一致的文件，本次只核对前 %d 个（可在上方调「抽查文件数」）"
                            % (len(candidates), take))
        for it in chosen:
            if time.time() > deadline:
                warnings.append("达到 %d 秒总时限，剩余文件未完成内容核对" % VERIFY_TIMEOUT)
                break
            size = it["localSize"] or 0
            fp = local[it["path"]]["abs"]
            target = ctargets.get(it["path"]) or _cloud_file_target(ctarget, cscope, it["path"])
            try:
                if mode == "sample":
                    pts = [(0, size)] if size <= 2 * VERIFY_CHUNK else [
                        (0, VERIFY_CHUNK), (size // 2, VERIFY_CHUNK),
                        (size - VERIFY_CHUNK, VERIFY_CHUNK)]
                    bad, unknown = False, ""
                    for i, (off, cnt) in enumerate(pts):
                        cb = _cloud_bytes(target, off, cnt, timeout=30)
                        lb = _local_bytes(fp, off, cnt)
                        deep_bytes += len(cb)
                        n = min(len(cb), len(lb))
                        if not cb or n == 0:
                            unknown = "云端第 %d 段未读到数据" % (i + 1)
                            break
                        if cb[:n] != lb[:n]:
                            bad = True
                            break
                        if len(cb) != len(lb):
                            unknown = "第 %d 段读取长度不一致（云端 %d / 本地 %d）" % (
                                i + 1, len(cb), len(lb))
                            break
                    if bad:
                        it["status"], it["note"] = "content", "内容字节不一致（文件可能损坏或不是同一版本）"
                    elif unknown:
                        it["status"], it["note"] = "unknown", "内容无法判定：" + unknown
                    else:
                        it["note"] = "抽样 %d 段字节一致" % len(pts)
                else:
                    ch = _cloud_sha256(target, deadline)
                    lh = _local_sha256(fp)
                    deep_bytes += size
                    if ch != lh:
                        it["status"], it["note"] = "content", "SHA256 不一致（文件内容不同）"
                    else:
                        it["note"] = "SHA256 完全一致"
                deep_checked += 1
            except Exception as e:
                it["status"], it["note"] = "unknown", "内容核对失败：%s" % str(e)[:110]
                warnings.append("%s：%s" % (it["path"][:60], str(e)[:80]))
                deep_checked += 1

    counts = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    summary = {k: counts.get(k, 0) for k in
               ("match", "missing", "extra", "size", "content", "unknown")}
    summary["total"] = len(items)
    summary["deepChecked"] = deep_checked
    summary["deepBytes"] = deep_bytes
    clean = not any(counts.get(k) for k in ("missing", "extra", "size", "content", "unknown"))
    return {
        "ok": clean and bool(items),
        "error": "",
        "mode": mode, "remote": remote,
        "cloudRoot": ctarget,
        "localRoot": ltarget or (primary_root() + ("/" + local_path if local_path else "")),
        "scope": cscope,
        "cloud": {"count": len(cloud), "bytes": sum(cloud.values()), "truncated": ctrunc},
        "local": {"count": len(local),
                  "bytes": sum(v["size"] for v in local.values()), "truncated": ltrunc},
        "summary": summary, "items": items[:VERIFY_MAX_FILES],
        "truncated": ctrunc or ltrunc, "warnings": warnings,
        "elapsed": round(time.time() - t0, 2),
        "hashSupported": False,   # PikPak 不提供对象哈希，内容校验靠读字节
    }


# ── v3.54 在线更新：检查 GitHub Release → 校验 → 备份 → 替换 → 重启 ──────────
# 边界（改动前务必先读）：
#   * 只替换 UPDATE_FILES 白名单里的程序文件，绝不碰 settings.json / jobs.json /
#     accounts_meta.json / rclone.conf / tls/ 和下载目录 —— 更新不能丢配置与任务；
#   * 压缩包必须带 release-manifest.json，逐文件校验 sha256，成员名只能落在白名单里
#     （防 zip-slip / 任意文件写入）；整包校验通过之前不做任何磁盘写入；
#   * 落盘前把旧文件按时间戳备份到 update-backups/，支持一键回滚；
#   * Python 改动必须重启才生效，容器里用 docker CLI 延迟 3 秒重启，留时间回 HTTP 响应。
UPDATE_REPO = os.environ.get("UPDATE_REPO", "Dlany-Cohhh/cloud-shuttle")
UPDATE_API = os.environ.get("UPDATE_API", "https://api.github.com").rstrip("/")
UPDATE_ASSET = os.environ.get("UPDATE_ASSET", "cloud-shuttle.zip")
UPDATE_TOKEN = os.environ.get("GITHUB_TOKEN", "")
UPDATE_TIMEOUT = int(os.environ.get("UPDATE_TIMEOUT", "15") or 15)
UPDATE_MAX_BYTES = 24 * 1024 * 1024
UPDATE_CONTAINER = os.environ.get("UPDATE_CONTAINER", "download-console")
UPDATE_APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPDATE_STATE_FILE = os.path.join(DATA_DIR, "update-state.json")
UPDATE_BACKUP_DIR = os.path.join(DATA_DIR, "update-backups")
UPDATE_FILES = frozenset((
    "download-console.py", "static/index.html", "static/app.js", "static/app.css",
    "static/bm.html", "static/bm-code.js", "collector.ps1", "login-collector.ps1",
))
UPDATE_REQUIRED = ("download-console.py", "static/index.html", "static/app.js")
UPDATE_CACHE = {"at": 0.0, "data": None}
UPDATE_LOCK = threading.Lock()   # 独立锁：更新期间不要阻塞任务轮询


def _update_stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _ver_tuple(v):
    """v3.54 / 3.54.1 / v3.54-beta2 → 可比较的数字元组。"""
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in nums[:4]) or (0,)


def _update_http(url, timeout=None, accept=None):
    if not str(url).lower().startswith(("https://", "http://")):
        raise ValueError("不支持的下载地址协议")
    hdr = {"User-Agent": "CloudShuttle/" + APP_VERSION.lstrip("v"),
           "Accept": accept or "application/vnd.github+json"}
    if UPDATE_TOKEN and str(url).startswith(UPDATE_API):
        hdr["Authorization"] = "Bearer " + UPDATE_TOKEN
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout or UPDATE_TIMEOUT) as r:
        return r.read(UPDATE_MAX_BYTES + 1)


def _update_restart():
    """容器里延迟重启；裸机部署由用户手动重启（返回 False）。

    v3.80：以前直接 `docker restart $UPDATE_CONTAINER`，容器名改过又没设这个环境
    变量时 restart 会静默失败（Popen 不检查退出码）—— 文件已换新、进程还是旧的，
    于是出现「页面版本号没变」「新 JS 配旧 Python」这类诡异状态。现在先按候选名
    逐个 inspect 确认真的存在，再重启；一个都找不到就明确提示手动重启。"""
    if os.environ.get("UPDATE_AUTO_RESTART", "1") != "1" or not os.path.exists("/.dockerenv"):
        return False
    target = ""
    for cand in _self_container_candidates():
        try:
            p = subprocess.run(["docker", "inspect", "-f", "{{.Name}}", cand],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=10)
            if p.returncode == 0 and (p.stdout or "").strip():
                target = cand
                break
        except Exception:
            continue
    if not target:
        print("%s 自动重启跳过：认不出自身容器名（可在 .env 设 UPDATE_CONTAINER）"
              % time.strftime("%F %T"))
        return False
    try:
        subprocess.Popen(["sh", "-c", "sleep 3; docker restart %s" % target],
                         start_new_session=True, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print("%s 自动重启失败（请手动重启）：%s" % (time.strftime("%F %T"), e))
        return False


def _update_safe_path(root, rel):
    """把白名单里的相对路径解析到 root 下，拒绝任何越界。"""
    if rel not in UPDATE_FILES:
        raise ValueError("未授权文件：%s" % rel)
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    dst = os.path.abspath(os.path.join(root, *parts))
    if os.path.commonpath([dst, os.path.abspath(root)]) != os.path.abspath(root):
        raise ValueError("路径越界：%s" % rel)
    return dst


def update_check(force=False):
    """查询 GitHub 最新 Release，结果缓存 10 分钟（API 匿名限额 60 次/小时）。"""
    now = time.time()
    if not force and UPDATE_CACHE["data"] and now - UPDATE_CACHE["at"] < 600:
        return dict(UPDATE_CACHE["data"], cached=True)
    url = "%s/repos/%s/releases/latest" % (UPDATE_API, UPDATE_REPO)
    try:
        rel = json.loads(_update_http(url).decode("utf-8"))
        tag = str(rel.get("tag_name") or "")
        if not tag:
            raise ValueError("发行版没有 tag_name")
        assets = rel.get("assets") or []

        def asset(name):
            return next((a for a in assets if str(a.get("name") or "") == name), None)

        z = asset(UPDATE_ASSET)
        if not z:
            raise ValueError("发行版缺少 %s，无法自动更新（可到 Release 页手动下载）" % UPDATE_ASSET)
        s = asset(UPDATE_ASSET + ".sha256")
        info = {
            "ok": True, "current": APP_VERSION, "latest": tag,
            "hasUpdate": _ver_tuple(tag) > _ver_tuple(APP_VERSION),
            "name": str(rel.get("name") or tag),
            "notes": str(rel.get("body") or "")[:6000],
            "publishedAt": str(rel.get("published_at") or ""),
            "htmlUrl": str(rel.get("html_url") or ""),
            "repo": UPDATE_REPO, "assetName": UPDATE_ASSET,
            "assetSize": int(z.get("size") or 0),
            "assetUrl": str(z.get("browser_download_url") or ""),
            "apiAssetUrl": str(z.get("url") or ""),
            "shaUrl": str((s or {}).get("browser_download_url") or ""),
            "apiShaUrl": str((s or {}).get("url") or ""),
            "checkedAt": _update_stamp(),
        }
        if not info["assetUrl"] and not info["apiAssetUrl"]:
            raise ValueError("发行版资源缺少下载地址")
        UPDATE_CACHE.update(at=now, data=info)
        out = dict(info)
        for k in ("assetUrl", "apiAssetUrl", "shaUrl", "apiShaUrl"):
            out.pop(k, None)        # 下载地址只留在服务端缓存里
        return out
    except Exception as e:
        return {"ok": False, "error": mask_secret(str(e))[:200], "current": APP_VERSION}


def _update_load_state():
    try:
        with open(UPDATE_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _update_save_state(st):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(UPDATE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("%s 更新状态写入失败：%s" % (time.strftime("%F %T"), e))


def update_state():
    return {"ok": True, "current": APP_VERSION, "state": _update_load_state()}


def update_apply():
    """下载 + 校验 + 备份 + 替换 + 重启。任何一步失败都尽量保持原状。"""
    with UPDATE_LOCK:
        info = UPDATE_CACHE["data"]
        if not info or not info.get("ok"):
            return {"error": "请先成功执行一次「检查更新」"}
        if not info.get("hasUpdate"):
            return {"error": "当前已是最新版 " + APP_VERSION}
        def fetch(candidates, timeout, accept=None):
            """API asset 端点优先（与 API 同主机，某些网络只放行 api.github.com），
            失败再回退 Release 直链；两个都失败才报错。"""
            err = None
            for u in candidates:
                if not u:
                    continue
                try:
                    return _update_http(u, timeout=timeout, accept=accept)
                except Exception as e:
                    err = e
            raise err or ValueError("没有可用的下载地址")

        try:
            zbytes = fetch([info.get("apiAssetUrl"), info.get("assetUrl")],
                           max(UPDATE_TIMEOUT, 90), "application/octet-stream")
        except Exception as e:
            return {"error": "下载更新包失败：" + mask_secret(str(e))[:160]}
        if not zbytes or len(zbytes) > UPDATE_MAX_BYTES:
            return {"error": "更新包为空或超过 24MB 上限"}
        if info.get("shaUrl") or info.get("apiShaUrl"):
            try:
                raw_sha = fetch([info.get("apiShaUrl"), info.get("shaUrl")],
                                UPDATE_TIMEOUT, "application/octet-stream")
                first = raw_sha.decode("utf-8", "ignore").strip().split()
                want = (first[0] if first else "").lower()
                if not re.fullmatch(r"[0-9a-f]{64}", want):
                    raise ValueError("sha256 文件格式不正确")
                if hashlib.sha256(zbytes).hexdigest() != want:
                    raise ValueError("包体 sha256 不匹配，已中止")
            except Exception as e:
                return {"error": "更新包校验失败：" + mask_secret(str(e))[:160]}

        # 整包校验：清单版本、成员白名单、逐文件哈希、Python 语法
        try:
            zf = zipfile.ZipFile(io.BytesIO(zbytes))
            names = zf.namelist()
            if "release-manifest.json" not in names:
                raise ValueError("缺少 release-manifest.json")
            mani = json.loads(zf.read("release-manifest.json").decode("utf-8"))
            if str(mani.get("version") or "") != info["latest"]:
                raise ValueError("包内版本与发行版 tag 不一致")
            sums = mani.get("files")
            if not isinstance(sums, dict):
                raise ValueError("清单缺少 files 校验表")
            total, payload = 0, {}
            for name in names:
                if name == "release-manifest.json":
                    continue
                if name not in UPDATE_FILES:
                    raise ValueError("包含未授权文件：" + name)
                blob = zf.read(name)
                total += len(blob)
                if total > UPDATE_MAX_BYTES:
                    raise ValueError("解压后内容超过 24MB 上限")
                if hashlib.sha256(blob).hexdigest() != str(sums.get(name, "")).lower():
                    raise ValueError("文件 sha256 不匹配：" + name)
                if name.endswith(".py"):
                    compile(blob.decode("utf-8"), name, "exec")
                payload[name] = blob
            for req in UPDATE_REQUIRED:
                if req not in payload:
                    raise ValueError("更新包缺少必需文件：" + req)
        except Exception as e:
            return {"error": "更新包无效：" + mask_secret(str(e))[:180]}

        # 备份（只备份确实存在的旧文件；新文件没有旧版本可存）
        bak = os.path.join(UPDATE_BACKUP_DIR, "update-" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.makedirs(bak, exist_ok=True)
            for name in payload:
                src = _update_safe_path(UPDATE_APP_DIR, name)
                if os.path.isfile(src):
                    dst = _update_safe_path(bak, name)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
            with open(os.path.join(bak, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump({"from": APP_VERSION, "to": info["latest"],
                           "at": _update_stamp(), "files": sorted(payload)}, f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            return {"error": "备份失败，已中止更新：" + mask_secret(str(e))[:160]}

        applied = []
        try:
            for name, blob in payload.items():
                dst = _update_safe_path(UPDATE_APP_DIR, name)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                mode = (os.stat(dst).st_mode & 0o777) if os.path.isfile(dst) else 0o644
                tmp = dst + ".__upd_tmp__"
                with open(tmp, "wb") as f:
                    f.write(blob)
                os.chmod(tmp, mode)
                os.replace(tmp, dst)   # 原子替换，保留 inode 语义与挂载兼容性
                applied.append(name)
        except Exception as e:
            for name in applied:      # 尽力恢复已替换的文件
                try:
                    shutil.copy2(_update_safe_path(bak, name),
                                 _update_safe_path(UPDATE_APP_DIR, name))
                except Exception:
                    pass
            return {"error": "写入失败，已回滚：" + mask_secret(str(e))[:160]}

        st = {"ok": True, "from": APP_VERSION, "to": info["latest"],
              "at": _update_stamp(), "files": applied, "backup": bak,
              "restart": _update_restart()}
        _update_save_state(st)
        UPDATE_CACHE["data"] = None
        return {"ok": True, "from": st["from"], "to": st["to"],
                "files": applied, "backup": os.path.basename(bak),
                "restart": st["restart"],
                "message": "已更新到 %s，%s" % (
                    st["to"], "正在重启容器…" if st["restart"] else "请手动重启控制台后生效")}


def update_rollback():
    """从最近一次更新的备份恢复（只恢复当时确实替换过的文件）。"""
    with UPDATE_LOCK:
        st = _update_load_state()
        if not st or not st.get("backup") or not st.get("files"):
            return {"error": "没有可回滚的更新记录"}
        bak = os.path.abspath(str(st["backup"]))
        root = os.path.abspath(UPDATE_BACKUP_DIR)
        if os.path.commonpath([bak, root]) != root or not os.path.isdir(bak):
            return {"error": "备份目录不存在或路径非法"}
        names = list(st["files"])
        try:
            for name in names:
                if not os.path.isfile(_update_safe_path(bak, name)):
                    raise ValueError("备份缺少文件：" + name)
            for name in names:
                src = _update_safe_path(bak, name)
                dst = _update_safe_path(UPDATE_APP_DIR, name)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp = dst + ".__upd_tmp__"
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
        except Exception as e:
            return {"error": "回滚失败：" + mask_secret(str(e))[:160]}
        st["rolledBack"] = True
        st["rolledBackAt"] = _update_stamp()
        st["restart"] = _update_restart()
        _update_save_state(st)
        return {"ok": True, "message": "已回滚到 %s，%s" % (
            st.get("from", "上一版"), "正在重启容器…" if st["restart"] else "请手动重启控制台后生效")}


def api(action, body):
    if action == "updatecheck":
        return update_check(bool(body.get("force")))
    if action == "updateapply":
        return update_apply()
    if action == "updatestate":
        return update_state()
    if action == "updaterollback":
        return update_rollback()

    if action == "localsize":
        # v3.64 本地目录总大小：os.walk 实时求和（NAS 本地盘，不涉及 PikPak 请求）。
        # 结果缓存 60s，避免连续刷新重复扫描大目录；隐藏项与临时文件不计入。
        try:
            p = safe_local_path(str(body.get("path") or ""))
        except ValueError as e:
            return {"error": mask_secret(str(e))}
        rp = os.path.realpath(p)
        if not os.path.isdir(rp):
            return {"error": "目录不存在或已被移动"}
        key = "localsize|" + rp
        now = time.time()
        with QUOTA_LOCK:
            hit = QUOTA_CACHE.get(key)
            if hit and now - hit[0] < 60:
                return hit[1]
        try:
            r = _local_dir_size(rp, time.time() + 55)
        except RuntimeError as e:
            return {"error": str(e)}
        with QUOTA_LOCK:
            QUOTA_CACHE[key] = (now, r)
        return r

    if action == "dirsize":
        # v3.60 目录总大小：rclone size 递归统计。前端勾选文件夹时按需调用，
        # 结果缓存（QUOTA_CACHE 同池，key 前缀隔离）——浏览列表绝不触发，
        # 同一目录反复勾选 / 下载预检不重复递归。
        remote = str(body.get("remote") or "").strip()
        sub = str(body.get("path") or "").strip("/")
        if not remote:
            return {"error": "缺少账号"}
        if any(seg == ".." for seg in sub.split("/")) or "\x00" in sub:
            return {"error": "路径不合法"}
        key = "dirsize|%s|%s" % (remote, sub)
        now = time.time()
        with QUOTA_LOCK:
            hit = QUOTA_CACHE.get(key)
            if hit and now - hit[0] < QUOTA_TTL:
                return hit[1]
        fid = str(body.get("id") or "").strip()
        r = None
        # v3.62：优先走 PikPak 原生 API 递归（直读文件对象 size，不依赖 rclone 递归，
        # 参考 PikPak_Enhancement_Master）；失败再回退 rclone size。
        try:
            if not fid:
                fid = pk_walk(remote, sub)
            r = {"bytes": int(_pk_folder_size(remote, fid, time.time() + PK_SIZE_DEADLINE)), "count": -1}
        except Exception as e:
            p = _rclone_run(["size", "--json", "%s:%s" % (remote, sub)], timeout=120)
            out, err = _decode(p.stdout), _decode(p.stderr)
            if p.returncode != 0:
                return {"error": "目录大小统计失败：%s / %s" % (str(e)[:100], (err or out)[:100])}
            try:
                d = json.loads(out)
            except Exception:
                return {"error": "解析 rclone size 输出失败"}
            r = {"bytes": int(d.get("bytes") or 0), "count": int(d.get("count") or 0)}
        with QUOTA_LOCK:
            QUOTA_CACHE[key] = (now, r)
        return r

    if action == "remotes":
        p = subprocess.run(["docker", "exec", RCLONE_CONTAINER, "rclone", "listremotes"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return {"remotes": [x.strip().rstrip(":") for x in p.stdout.splitlines() if x.strip()]}

    if action == "proxycheck":
        return api_proxy_check()

    if action == "foldercovers":
        # v3.74：PikPak 顶层文件夹常没有 thumbnail_link，异步取内部第一张封面补齐。
        # 单独接口、不阻塞 dirs；前端在缩略图视图加载完后请求并渐进填充。
        name = str(body.get("remote") or "").strip()
        raw = body.get("items") if isinstance(body.get("items"), list) else []
        todo = [(str(x.get("name") or ""), str(x.get("id") or ""))
                for x in raw[:12] if isinstance(x, dict) and x.get("name") and x.get("id")]
        covers = {}
        if name and todo:
            from concurrent.futures import ThreadPoolExecutor

            def one(pair):
                nm, fid = pair
                # v3.80：同一文件夹的封面 1 小时内复用。以前每次切目录/切视图都重新
                # 打 12 个 PikPak 请求，来回点几次就可能触发风控（表现为封面全空）。
                ck = (name, fid)
                hit = FOLDERCOVERS_CACHE.get(ck)
                # 有封面缓存 1 小时；「确实没有封面」只缓存 5 分钟 —— 否则用户传了新
                # 缩略图、云端才开始生成，要干等一小时才可能看到。
                if hit and time.time() - hit[0] < (3600 if hit[1] else 300):
                    return nm, hit[1]
                try:
                    q = ("/drive/v1/files?parent_id=%s&limit=30&phase=PHASE_TYPE_COMPLETE"
                         "&thumbnail_size=SIZE_SMALL" % urllib.parse.quote(fid))
                    code, d = pk_api(name, "GET", q, _timeout=6)
                    if code >= 400:
                        return nm, ""
                    for f in (d.get("files") or []):
                        u = str(f.get("thumbnail_link") or "")
                        host = urllib.parse.urlparse(u).netloc.lower() if u.startswith("https://") else ""
                        if host == "mypikpak.com" or host.endswith(".mypikpak.com"):
                            FOLDERCOVERS_CACHE[ck] = (time.time(), u)
                            return nm, u
                    FOLDERCOVERS_CACHE[ck] = (time.time(), "")   # 没有封面也记一笔，别反复问
                except Exception:
                    pass
                return nm, ""

            with ThreadPoolExecutor(max_workers=3) as ex:
                for nm, u in ex.map(one, todo):
                    if u:
                        covers[nm] = u
        return {"covers": covers}

    if action == "dirs":
        name = body["remote"]
        sub = body.get("path", "").strip("/")
        lst, err = cloud_list(name, sub)
        # v3.40：只有缩略图视图才拉封面元数据；列表视图仍是零额外开销
        tmap = _pk_thumb_map(name, sub) if body.get("thumbs") else {}
        local_idx = local_download_index()
        items = []
        for e in lst:
            nm = str(e.get("Name") or "")
            size = int(e.get("Size") or 0)
            is_dir = bool(e.get("IsDir", False))
            it = {"name": nm, "isdir": is_dir, "size": size, "id": str(e.get("ID") or ""),
                  # v3.43：带修改时间，导出 CSV / 目录树时用得上，列表页也可排序
                  "mtime": str(e.get("ModTime") or "")[:19],
                  # 目录不打标（要判「整个目录都下过」得递归列云端，太重）；
                  # 文件按「同名 + 同大小」判定，与一致性校验同一套归一化规则。
                  "downloaded": bool(not is_dir and size > 0
                                     and (_norm_key(nm), size) in local_idx),
                  # v3.44：压缩包标记，前端据此显示「解压」按钮
                  "archive": bool(not is_dir and is_archive_name(nm)),
                  # v3.45：音视频标记。container = 浏览器能解的容器，
                  # 编码（h264/hevc）要等 playinfo 查详情才知道，这里不下结论。
                  "mediaKind": "" if is_dir else cloud_media_kind(nm),
                  "playable": bool(not is_dir and cloud_playable(nm))}
            meta = tmap.get(nm)
            if meta:
                it.update(meta)
            items.append(it)
        items.sort(key=lambda x: (not x["isdir"], x["name"].lower()))
        # 明确区分「加载失败」与「目录确实为空」，前端据此显示不同提示
        return {"path": sub, "dirs": items,
                "downloadedCount": sum(1 for x in items if x["downloaded"]),
                "listError": ("列表加载失败：" + err) if err else ""}

    if action == "start":
        with LOCK:
            s = load_settings()
            jobs = load_jobs()
            running = sum(1 for j in jobs if j["state"] == "running")
            sub = body.get("path", "").strip("/")
            name = basename_of(sub) if sub else body["remote"]
            folder = str(body.get("folder") or "").strip("/")
            if folder and not re.match(r'^[\w\u4e00-\u9fff][\w\u4e00-\u9fff -]{0,60}$', folder):
                return {"error": "保存目录名不合法（单层：中英文/数字/空格/连字符）"}
            # v3.57 可选存储空间：body["volume"] 传 rclone 侧根（localdirs 返回的 volumes），
            # 缺省 = 主卷；保护门槛按目标卷的有效剩余判断，不再一刀切拦全部任务
            vol_req = str(body.get("volume") or "").strip().rstrip("/")
            vols = STATE.get("volumePairs") or [LEGACY_VOLUME]
            if vol_req:
                vol = next((p for p in vols if p["rclone"] == vol_req), None)
                if not vol:
                    return {"error": "未知存储空间：%s" % vol_req}
            else:
                vol = vols[0]
            vroot = vol["rclone"]
            vf = volume_effective_free(vol, s)
            disk_min = float(s["diskMinGB"] or 0)
            if vf is not None and vf < disk_min:
                return {"error": "「%s」空间保护中（剩余 %.1f GB），请清理该卷或到设置里调低阈值"
                                 % (vol.get("name") or vroot, vf)}
            dest = dedupe_dest(vroot + "/" + (folder if folder else name), fs=vroot)
            src = "%s:%s" % (body["remote"], sub) if sub else "%s:" % body["remote"]
            # 必须先定下 proxy：预检与后续 launch_job 都依赖它。
            # v3.31 曾把赋值放在预检之后，导致批量下载全程抛
            # UnboundLocalError: cannot access local variable 'proxy'。
            proxy = bool(body.get("proxy", s["downloadProxy"]))
            # v3.43 下载过滤：规范化后随任务一起落盘，续传 / 重启都能沿用同一套规则
            flt = norm_download_filter(body.get("filter"))
            # 预检源：sub 可能是目录也可能是单个文件（cloud_precheck 内部区分）
            pre_list, pre_err, is_file = cloud_precheck(body["remote"], sub, proxy)
            if pre_err:
                return {"error": "预检源目录失败: %s" % pre_err}
            if not pre_list:
                return {"error": "源目录为空（%s:%s），没有可下载的文件" % (body["remote"], sub or "/")}
            job = {"id": int(time.time() * 1000), "jobid": None, "remote": body["remote"],
                   "subpath": sub, "src": src,
                   "mode": body.get("mode", "copy"), "dest": dest, "state": "queued",
                   "started": time.strftime("%m-%d %H:%M"), "bytes": 0, "total": 0,
                   "speed": 0, "eta": None, "error": "", "proxy": proxy}
            if flt:
                job["filter"] = flt
                job["filterText"] = describe_filter(flt)
            if is_file:
                parent, _, leaf = sub.rpartition("/")
                job["single_file"] = True
                job["leaf"] = leaf
                job["src_dir"] = "%s:%s" % (body["remote"], parent)
                # 指定保存目录时 dest 本身就是目标目录；否则目标目录是该卷的根
                job["dst_dir"] = dest if folder else vroot
            launched_ok = True
            if running < s["maxConcurrent"]:
                launched_ok = launch_job(job, s)
            jobs.append(job)
            save_jobs(jobs)
            if not launched_ok:
                return {"error": "启动失败: %s" % (job.get("error") or "未知原因"), "job": job}
            return {"job": job}

    if action == "scanpath":
        return cloud_scan(body["remote"], body.get("path", ""), body.get("depth", 6))

    if action == "myshares":
        only = str(body.get("remote") or "").strip()
        names = [only] if only else pikpak_remotes()
        if not names:
            return {"error": "没有可用的 PikPak 账号"}
        shares, errors = [], []
        for n in names:
            try:
                rows = pk_share_list(n, force=bool(body.get("force")))
                shares.extend(map_my_share(n, s) for s in rows if isinstance(s, dict))
            except Exception as e:
                errors.append("%s: %s" % (n, str(e)[:100]))
        lim = load_share_limits()
        return {"shares": shares, "limits": lim["limits"], "log": lim["log"][:20],
                "autoCancel": bool(load_settings().get("shareAutoCancel")),
                "errors": errors}

    if action == "sharelimit":
        remote = str(body.get("remote") or "").strip()
        sid = str(body.get("shareId") or "").strip()
        limit = max(0, min(100000, _as_int(body.get("limit"))))
        if not remote or not sid:
            return {"error": "缺少账号或分享 ID"}
        key = _share_limit_key(remote, sid)
        with SHARE_LIMITS_LOCK:
            d = load_share_limits()
            if limit <= 0:
                d["limits"].pop(key, None)      # 0 = 清除上限
            else:
                d["limits"][key] = {"remote": remote, "shareId": sid,
                                    "limit": limit, "setAt": time.time()}
            save_share_limits(d)
            out = dict(d["limits"])
        return {"ok": True, "limits": out}

    if action == "sharedel":
        remote = str(body.get("remote") or "").strip()
        sid = str(body.get("shareId") or "").strip()
        if not remote or not sid:
            return {"error": "缺少账号或分享 ID"}
        if not pk_share_delete(remote, sid):
            return {"error": "取消分享失败（链接可能已失效或账号凭证异常）"}
        with SHARE_LIMITS_LOCK:
            MYSHARES_CACHE.pop(remote, None)
            d = load_share_limits()
            d["limits"].pop(_share_limit_key(remote, sid), None)
            d["log"].insert(0, {"at": time.strftime("%m-%d %H:%M"), "remote": remote,
                                "shareId": sid, "title": "", "restores": 0,
                                "limit": 0, "ok": True, "manual": True})
            d["log"] = d["log"][:50]
            save_share_limits(d)
        return {"ok": True}

    if action == "shareparse":
        # 只读：解析链接 / 取 pass_code_token / 列分享内某一层，绝不写入网盘
        name = str(body.get("remote") or "").strip()
        sid, pcode, err = parse_share_input(body.get("text"), body.get("passCode"))
        if err:
            return {"error": err}
        if not name:
            return {"error": "请选择用于查看分享的账号"}
        tok = str(body.get("passCodeToken") or "").strip()
        # v3.81：双路尝试。第一路跟随全局网络模式（通常直连）；若 PikPak 因出口地区
        # 返回 PROHIBITED / NOT_FOUND，且设置里「预置」了代理地址，就只让分享解析
        # 改走这个地址重试 —— 下载、云盘数据、封面都保持直连，不被慢代理拖累。
        # proxyUrl 本来就是「可预置、可不启用」的字段，这里复用它，无需新增设置项。
        current = proxy_url()
        preset = (str(load_settings().get("proxyUrl") or "").strip()
                  or str(os.environ.get("PROXY_URL") or "").strip())
        if preset.lower() in PROXY_OFF_WORDS or not PROXY_URL_RE.match(preset):
            preset = ""
        routes = [(True, "跟随全局（%s）" % ("代理" if current else "直连"))]
        if preset and preset != current:
            routes.append((preset, "分享专用代理 " + preset))
        last, tried = "", []
        for px, via in routes:
            tried.append(via)
            try:
                info = pk_share_info(name, sid, pcode, proxy=px, via=via) if not tok else {
                    "shareId": sid, "passCode": pcode, "passCodeToken": tok,
                    "title": str(body.get("title") or "")[:120], "shareStatus": "OK",
                    "shareStatusText": "", "shareUser": "", "via": via}
                items, nxt = pk_share_detail(name, sid, info["passCodeToken"],
                                             str(body.get("parentId") or ""),
                                             str(body.get("pageToken") or ""),
                                             proxy=px)
                return {"ok": True, "info": info, "items": items,
                        "nextPageToken": nxt, "parentId": str(body.get("parentId") or ""),
                        "tried": tried}
            except Exception as e:
                last = str(e)[:300]
                # 只有「换出口可能有救」的状态才重试；提取码 / 凭证错误重试无意义
                if not any(k in last for k in ("PROHIBITED", "NOT_FOUND")):
                    break
        if len(tried) > 1:
            last += "；已尝试 %d 条路径：%s" % (len(tried), " → ".join(tried))
        return {"error": last, "tried": tried}

    if action == "sharesave":
        name = str(body.get("remote") or "").strip()
        sid = str(body.get("shareId") or "").strip()
        tok = str(body.get("passCodeToken") or "").strip()
        ids = [str(x) for x in (body.get("fileIds") or []) if x]
        dst_path = str(body.get("dstPath") or "").strip("/")
        if not name or not sid or not tok:
            return {"error": "缺少分享信息或目标账号，请先重新解析链接"}
        if not ids:
            return {"error": "请至少勾选一个要转存的文件或文件夹"}
        if len(ids) > 200:
            return {"error": "一次最多转存 200 项，请分批选择"}
        if ".." in dst_path.split("/"):
            return {"error": "目标目录不能包含 .."}
        with LOCK:
            dup = next((j for j in load_jobs() if j.get("kind") == "xfer"
                        and j.get("method") == "externalshare"
                        and j.get("shareId") == sid and j.get("remote") == name
                        and j.get("state") in ("running", "queued")), None)
        if dup:
            return {"error": "这个分享正在转存中（任务 #%s）" % dup["id"]}
        job = {"id": int(time.time() * 1000), "jobid": None, "kind": "xfer",
               "method": "externalshare", "mode": "copy", "remote": name,
               "subpath": str(body.get("title") or sid)[:120], "src": "share:" + sid,
               "dstRemote": name, "dstPath": dst_path,
               "dest": "%s:%s" % (name, dst_path or "/"),
               "shareId": sid, "passCodeToken": tok, "fileIds": ids,
               "ancestorIds": [str(x) for x in (body.get("ancestorIds") or []) if x][:20],
               "state": "queued", "phase": "restore",
               "started": time.strftime("%m-%d %H:%M"), "bytes": 0, "total": 0,
               "single_file": False, "speed": 0, "eta": None, "error": "",
               "proxy": False, "srcCount": len(ids)}
        with LOCK:
            jobs = load_jobs()
            jobs.append(job)
            save_jobs(jobs)
        threading.Thread(target=_sharesave_worker, args=(job["id"],), daemon=True).start()
        return {"job": job}

    if action == "xferstart":
        src_remote = str(body.get("srcRemote") or "").strip()
        dst_remote = str(body.get("dstRemote") or "").strip()
        src_path = str(body.get("path") or "").strip("/")
        dst_path = str(body.get("dstPath") or "").strip("/")
        mode = "move" if str(body.get("mode")) == "move" else "copy"
        method = "relay" if str(body.get("method")) == "relay" else "share"
        if not src_remote or not src_path:
            return {"error": "请选择要转移的源文件或文件夹"}
        if not dst_remote:
            return {"error": "请选择目标账号"}
        if dst_remote == src_remote:
            if dst_path == src_path:
                return {"error": "目标与源是同一个位置"}
            if (dst_path + "/").startswith(src_path + "/"):
                return {"error": "不能转移到源目录自身或其子目录"}
        if ".." in dst_path.split("/"):
            return {"error": "目标目录不能包含 .."}
        src_ref = "%s:%s" % (src_remote, src_path)
        with LOCK:
            dup = next((j for j in load_jobs() if j.get("kind") == "xfer"
                        and j.get("state") in ("running", "queued")
                        and j.get("src") == src_ref), None)
        if dup:
            return {"error": "这个路径已经在转移中（任务 #%s）" % dup["id"]}
        proxy = bool(body.get("proxy", load_settings()["downloadProxy"]))
        pre, perr, is_file = cloud_precheck(src_remote, src_path, proxy)
        if perr:
            return {"error": "预检源路径失败：%s" % perr}
        if not pre:
            return {"error": "源路径不存在或为空，没有可转移的内容"}
        fsize = int((pre[0] or {}).get("Size") or 0) if is_file else 0
        if is_file and dst_remote == src_remote:
            parent = src_path.rpartition("/")[0]
            if dst_path == parent:
                return {"error": "目标与源是同一个位置，换个目录再试"}
        job = {"id": int(time.time() * 1000), "jobid": None, "kind": "xfer",
               "method": method, "remote": src_remote, "subpath": src_path,
               "src": src_ref, "dstRemote": dst_remote, "dstPath": dst_path,
               "dest": "%s:%s" % (dst_remote, dst_path or "/"),
               "mode": mode, "state": "queued", "phase": "share",
               "started": time.strftime("%m-%d %H:%M"), "bytes": 0,
               "total": fsize, "single_file": is_file,
               "speed": 0, "eta": None, "error": "", "proxy": proxy,
               "srcCount": 1 if is_file else len(pre)}
        with LOCK:
            jobs = load_jobs()
            jobs.append(job)
            save_jobs(jobs)
        threading.Thread(target=_share_worker if method == "share" else _xfer_worker,
                         args=(job["id"],), daemon=True).start()
        return {"job": job}

    if action == "jobs":
        # v3.80：前端每 2s 轮询一次，而 job_live 单个任务最坏 8s+8s（job/status +
        # core/stats）。同时跑 3 个任务时这里会串行 40s+，接口超过前端 8s 超时，
        # 界面直接报「请求超时」，且长时间持 LOCK 把「停止/暂停」也堵住。
        # 现在给整轮查询一个总预算，超预算的 running 任务本轮沿用上次状态。
        with LOCK:
            jobs = load_jobs()
            try:
                before = json.dumps(jobs, ensure_ascii=False, sort_keys=True)
            except Exception:
                before = None
            deadline = time.time() + 6.0
            for j in jobs:
                if time.time() > deadline and j.get("state") == "running":
                    continue          # 本轮跳过，保留上次落盘的状态
                job_live(j)
            try:
                after = json.dumps(jobs, ensure_ascii=False, sort_keys=True)
            except Exception:
                after = None
            # 前端每 2s 轮询一次；状态没变就不必反复写盘（序列化失败时退回保守保存）
            if before is None or after is None or before != after:
                save_jobs(jobs)
        return {"jobs": jobs, "settings": load_settings(),
                "state": {"diskLow": STATE["diskLow"], "diskFree": STATE["diskFree"],
                        "diskTotal": STATE.get("diskTotal"), "storages": storages(),
                        "proxyRcPort": PROXY_RC_PORT, "version": APP_VERSION},
                "effectiveProxyUrl": proxy_url() or "直连（未启用代理）"}

    if action == "stop":
        with LOCK:
            jobs = load_jobs()
            for j in jobs:
                if j["id"] == body.get("id") and j["state"] in ("running", "queued"):
                    stop_job_entry(j)
            save_jobs(jobs)
        return {"ok": True}

    if action == "resume":
        with LOCK:
            s = load_settings()
            jobs = load_jobs()
            # v3.57：按任务目标卷判断保护（转存到云盘的任务不占下载卷，放行）
            tgt = next((j for j in jobs if j.get("id") == body.get("id")), None)
            if tgt and tgt.get("kind") != "xfer":
                vf = volume_effective_free(job_volume(tgt), s)
                if vf is not None and vf < float(s["diskMinGB"] or 0):
                    return {"error": "目标存储空间保护中（剩余 %.1f GB），无法继续" % vf}
            running = sum(1 for j in jobs if j["state"] == "running")
            for j in jobs:
                if j["id"] == body.get("id") and j["state"] in ("stopped", "error"):
                    if j.get("kind") == "xfer":
                        # 转存类任务：重置阶段后重跑，已存在的同名目录会在定位阶段跳过
                        j.update(state="queued", error="", phase="", speed=0, curfile="")
                        save_jobs(jobs)
                        _resume_worker = (
                            _xfer_worker if j.get("method") == "relay" else
                            _sharesave_worker if j.get("method") == "externalshare" else
                            _share_worker)
                        threading.Thread(target=_resume_worker,
                                         args=(j["id"],), daemon=True).start()
                        break
                    if j.get("kind") == "unzip":
                        # 解压没有「断点续传」这回事，重新提交才是唯一干净的做法
                        j["error"] = "解压任务不支持续传，请重新点「解压」（已解出的文件会保留）"
                        save_jobs(jobs)
                        break
                    # 预检：云端源已空（如 move 已删源）时拒绝续传（URL 直链任务无云端源，跳过）
                    if j.get("remote") != "URL":
                        pre, pre_err, _ = cloud_precheck(j["remote"], j.get("subpath", ""),
                                                        bool(j.get("proxy")))
                        if pre_err:
                            j["error"] = "预检源目录失败: %s" % pre_err[:120]
                            continue
                        if not pre:
                            j["error"] = "源目录已空（%s:%s），无需继续" % (j["remote"], j.get("subpath", "/"))
                            continue
                    if running < s["maxConcurrent"]:
                        if launch_job(j, s):
                            running += 1
                    else:
                        j["state"] = "queued"
                    break
            save_jobs(jobs)
        return {"ok": True}

    if action == "jobreproxy":
        # 切换单个任务的下载路径并立即重启：停掉旧 rclone job → 换守护进程重新 launch
        with LOCK:
            s = load_settings()
            jobs = load_jobs()
            job = next((j for j in jobs if j.get("id") == body.get("id")), None)
            if not job:
                return {"error": "任务不存在"}
            if job.get("remote") == "URL":
                return {"error": "URL 直链任务不支持切换下载路径"}
            new_proxy = bool(body.get("proxy", not job.get("proxy")))
            if job["state"] in ("running", "queued"):
                stop_job_entry(job)
            job["proxy"] = new_proxy
            job["error"] = ""
            ok = launch_job(job, s)
            save_jobs(jobs)
            return {"ok": ok, "proxy": new_proxy,
                    "error": "" if ok else (job.get("error") or "启动失败")}

    if action == "remove":
        with LOCK:
            jobs = load_jobs()
            for j in jobs:
                # 转移任务可能挂着 rclone 子进程，删记录前先收掉，避免孤儿进程
                if j["id"] == body.get("id") and j.get("kind") == "xfer":
                    _xfer_kill(j)
            jobs = [j for j in jobs if j["id"] != body.get("id")]
            save_jobs(jobs)
        return {"ok": True}

    if action == "stats":
        try:
            d = merged_stats()
        except Exception as e:
            return {"speed": 0, "bytes": 0, "transferring": [], "error": str(e)[:150]}
        spd, _ = instant_speed(int(d.get("bytes", 0) or 0))
        return {"speed": spd, "bytes": d.get("bytes", 0),
                "transferring": [{"name": t.get("name", ""), "bytes": t.get("bytes", 0),
                                  "total": t.get("totalBytes", 0), "speed": t.get("speed", 0)}
                                 for t in d.get("transferring", [])]}

    if action == "settings":
        return {"settings": load_settings(),
                "state": {"diskLow": STATE["diskLow"], "diskFree": STATE["diskFree"],
                        "diskTotal": STATE.get("diskTotal"), "storages": storages(),
                        "proxyRcPort": PROXY_RC_PORT, "version": APP_VERSION},
                "effectiveProxyUrl": proxy_url() or "直连（未启用代理）",
                "netMode": net_mode(), "proxyScope": proxy_scope()}

    if action == "savesettings":
        with LOCK:
            old = load_settings()
            s = dict(old)
            for k in ("maxConcurrent", "diskMinGB", "diskQuotaGB", "transfers", "checkers",
                      "multiThreadStreams", "multiThreadCutoffMB", "retries",
                      "autoStart", "autoPause", "orderBy", "bwLimitOn", "bwLimit",
                      "timeout", "idleTimeout", "createEmptySrcDirs", "logLevel", "logFile",
                      "transfersClearedAt", "nasHomeUrl", "proxyUrl", "netMode"):
                if k in body:
                    s[k] = body[k]
            # v3.77：两个旧开关不再由前端单独控制，统一跟随全局模式
            s["proxy"] = s["downloadProxy"] = (str(s.get("netMode") or "direct") == "proxy")
            s["advancedMode"] = _truthy(body.get("advancedMode", s.get("advancedMode", False)))
            s["shareAutoCancel"] = _truthy(body.get("shareAutoCancel", s.get("shareAutoCancel", False)))
            s = _norm_settings(s)      # 统一钳制 / 白名单，非法值退回默认
            save_settings(s)
            STATE["firedStart"] = None
            STATE["firedPause"] = None
            changed = ((old.get("proxyUrl") or "") != (s.get("proxyUrl") or "")
                       or old.get("netMode") != s.get("netMode"))
            # v3.77：选代理却没有合法地址 → 自动回落直连，明确告知，不静默套默认地址
            proxy_note = ""
            if s.get("netMode") == "proxy" and not proxy_url():
                s["netMode"] = "direct"
                s["pkProxyMode"] = "direct"
                s["proxy"] = s["downloadProxy"] = False
                save_settings(s)
                changed = True
                proxy_note = "代理模式需要填写有效代理地址，已自动回落到直连"
        # 只在模式/地址真的变了时重启 5573，且避开正在传输的任务
        note2 = ""
        if changed:
            _, note2 = refresh_proxy_rc()
        return {"ok": True, "settings": s,
                "effectiveProxyUrl": proxy_url() or "直连（未启用代理）",
                "netMode": net_mode(), "proxyScope": proxy_scope(),
                "proxyNote": proxy_note or note2}

    if action == "resetsettings":
        with LOCK:
            s = dict(DEFAULTS)
            # 清空「已完成传输」是显示层面的水位，不应该因为恢复默认设置而撤销
            s["transfersClearedAt"] = load_settings().get("transfersClearedAt", 0)
            save_settings(s)
            STATE["firedStart"] = None
            STATE["firedPause"] = None
        return {"ok": True, "settings": s,
                "effectiveProxyUrl": proxy_url() or "直连（未启用代理）"}

    if action == "cleartransfers":
        # 「本次运行已完成传输」来自 rclone 容器日志；清空 = 把日志读取起点推到当前时刻，
        # 之后新完成的文件仍会正常出现，旧记录不再回来（刷新页面也一样）。
        with LOCK:
            s = load_settings()
            s["transfersClearedAt"] = time.time()
            save_settings(s)
        return {"ok": True}

    if action == "clearfinished":
        # v3.37 下载任务页「一键清理」：清掉所有已结束（完成 / 出错 / 暂停）的任务记录，
        # 保留运行中与排队中的任务；顺带把已完成传输的日志水位推到当前时刻，
        # 让统计页那张卡片一起变干净。绝不动已下载到本地的文件。
        with LOCK:
            jobs = load_jobs()
            keep, removed = [], 0
            for j in jobs:
                if j.get("state") in ("running", "queued"):
                    keep.append(j)
                    continue
                removed += 1
                if j.get("kind") == "xfer":
                    try:
                        _xfer_kill(j)
                    except Exception:
                        pass
            save_jobs(keep)
            s = load_settings()
            s["transfersClearedAt"] = time.time()
            save_settings(s)
        return {"ok": True, "removed": removed}

    if action == "quota":
        remotes = [str(r) for r in (body.get("remotes") or [])]
        force = bool(body.get("force"))
        now = time.time()

        def _quota_one(r):
            try:
                code, d = pk_api(r, "GET", "/vip/v1/quantity/list?type=transfer&limit=200")
                if code >= 400 or (d or {}).get("error"):
                    return {"remote": r, "error": _pk_err(code, d)}
                b = (d or {}).get("base") or {}
                def item(k):
                    o = b.get(k) or {}
                    return {"used": int(o.get("assets") or o.get("size") or 0),
                            "total": int(o.get("total_assets") or 0)}
                # 官方网页的四档：云下载 / 下行 / 上传 / 非会员每日下行
                return {"remote": r, "offline": item("offline"), "download": item("download"),
                        "upload": item("upload"), "daily": item("download_daily"),
                        "vip": pk_vip_active(b),
                        "sub": bool(b.get("sub_status"))}
            except Exception as e:
                msg = str(e)
                # openlist / 本地盘等不是 PikPak 账号，没有 access_token 也就没有传输配额；
                # 这是「不适用」，不是「读取失败」，前端应灰显而不是报红。
                unsupported = ("access_token" in msg.lower() or
                               "不在 rclone 配置里" in msg or "token" in msg.lower())
                return {"remote": r, "error": ("非 PikPak 账号，无传输配额" if unsupported
                                               else msg[:150]), "unsupported": unsupported}

        with QUOTA_LOCK:
            todo = [r for r in remotes
                    if force or not (QUOTA_CACHE.get(r) and now - QUOTA_CACHE[r][0] < QUOTA_TTL)]
        if todo:
            try:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=min(9, len(todo))) as ex:
                    results = list(ex.map(_quota_one, todo))
                with QUOTA_LOCK:
                    for r, out in zip(todo, results):
                        QUOTA_CACHE[r] = (now, out)
            except Exception:
                for r in todo:
                    out = _quota_one(r)
                    with QUOTA_LOCK:
                        QUOTA_CACHE[r] = (now, out)
        out = []
        with QUOTA_LOCK:
            for r in remotes:
                ent = QUOTA_CACHE.get(r)
                out.append(ent[1] if ent else {"remote": r, "error": "未查询"})
        return {"accounts": out}

    if action == "history":
        # 24h 速度曲线（5 分钟均值）+ 最近 14 天下载量
        h = load_history()
        now = int(time.time())
        buckets = {}
        for ts, sp in h.get("samples", []):
            if now - 24 * 3600 <= ts <= now:
                k = ts // 300
                buckets.setdefault(k, []).append(sp)
        series = [[k * 300, round(sum(v) / len(v), 1)] for k, v in sorted(buckets.items())]
        daily = [{"date": d, "bytes": b} for d, b in sorted(h.get("daily", {}).items())[-14:]]
        return {"series": series, "daily": daily}

    if action == "about":
        remotes = [str(r) for r in (body.get("remotes") or [])]

        def _about_one(r):
            try:
                # 短超时：PikPak 等网盘常不支持 About，会挂到超时才失败；8s 拿不到就标记。
                a = rc("operations/about", _timeout=8, fs=r + ":")
                # rclone RC 出错是 HTTP 200 + {"error":...}，rc() 默认不抛异常；
                # 不检查就会把失败当成 0 GB / 0 GB 配额显示，掩盖账号配置问题。
                if isinstance(a, dict) and a.get("error"):
                    raise RuntimeError(str(a["error"])[:150])
                return {"remote": r,
                        "used": int(a.get("used") or 0),
                        "total": int(a.get("total") or 0),
                        "free": int(a.get("free") or 0)}
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                if isinstance(e, subprocess.TimeoutExpired) or ("timed out" in low) or ("timeout" in low):
                    err, unsupported = "读取超时，稍后重试", False
                elif ("about" in low) or ("not supported" in low) or ("not implement" in low) or ("doesn't support" in low):
                    err, unsupported = "该网盘不提供容量配额接口", True
                elif ("username" in low) or ("password" in low) or ("credential" in low) or ("no auth" in low):
                    # v3.47：容量接口报这个同样是 token 失效后回退直登失败，
                    # 不是「缺个字段去补一下」，指向重新登录采集才修得好。
                    err, unsupported = "账号凭证已失效，请点该账号「重新登录」重新采集", False
                else:
                    err, unsupported = re.sub(r"Command '.*?'", "rclone", msg)[:100], False
                return {"remote": r, "used": None, "total": None, "free": None,
                        "unsupported": unsupported, "error": err}

        # 并发查询：9 个账号串行 8s×9≈72s，前端会长时间停在骨架屏（几乎看不见的假空白）。
        # 并发后总耗时≈最慢的一个（≤8s）；线程各自独立 subprocess，互不影响。
        out = []
        try:
            from concurrent.futures import ThreadPoolExecutor
            n = min(12, max(1, len(remotes)))
            with ThreadPoolExecutor(max_workers=n) as ex:
                out = list(ex.map(_about_one, remotes))
        except Exception:
            out = [_about_one(r) for r in remotes]
        return {"accounts": out}

    if action == "transfers":
        # 本次运行已完成传输：从 rclone 日志解析 Copied 行（rclone 保留源 mtime，
        # 文件系统时间无法区分，故 core/stats 的 transferring 差异也不可靠）。
        try:
            jobs = load_jobs()
            run = [j for j in jobs if j.get("state") == "running"]
            if run:
                since = min((j.get("started_ts") or time.time()) for j in run)
            elif jobs:
                since = max((j.get("started_ts") or 0) for j in jobs) or (time.time() - 21600)
            else:
                since = time.time() - 21600
            # 「清空记录」水位：旧日志不再解析，之后新完成的文件照常出现
            since = max(since, float(load_settings().get("transfersClearedAt") or 0))
            return {"list": completed_transfers(since)[:80]}
        except Exception as e:
            return {"list": [], "error": str(e)[:120]}

    if action == "bwlimit":
        try:
            d = rc("core/bwlimit")
            return {"bytesPerSecond": d.get("bytesPerSecond", -1),
                    "transferred": d.get("bytesTransferred", 0)}
        except Exception as e:
            return {"error": str(e)[:150]}

    if action == "setbwlimit":
        rate = (str(body.get("rate") or "off")).strip()
        try:
            rc_ok("core/bwlimit", rate=rate)
        except Exception as e:
            return {"error": str(e)[:150]}
        try:
            d = rc("core/bwlimit")
            return {"ok": True, "bytesPerSecond": d.get("bytesPerSecond", -1)}
        except Exception:
            return {"ok": True}

    if action == "version":
        # consoleVersion 同源返回，前端页脚不再硬编码，避免版本显示落后
        try:
            d = rc("core/version")
            return {"version": str(d.get("version", "")), "os": str(d.get("os", "")),
                    "arch": str(d.get("arch", "")), "consoleVersion": APP_VERSION,
                    "https": tls_cert_ready()}
        except Exception as e:
            return {"error": str(e)[:150], "consoleVersion": APP_VERSION,
                    "https": tls_cert_ready()}

    if action == "jobdetail":
        # 任务详情：实时传输文件 + 落盘文件清单（含总大小）
        jid = body.get("id")
        with LOCK:
            jobs = load_jobs()
            job = next((j for j in jobs if j.get("id") == jid), None)
            if job:
                job_live(job)
                save_jobs(jobs)
        if not job:
            return {"error": "任务不存在"}
        d = {"job": job, "transferring": [], "files": [], "diskTotal": 0,
             "diskCount": 0, "exists": False, "truncated": False}
        if job["state"] == "running" and job.get("jobid"):
            try:
                s = rc("core/stats", group="job/%d" % job["jobid"],
                       _proxy=bool(job.get("proxy")))
                d["transferring"] = [{"name": t.get("name", ""), "bytes": t.get("bytes", 0),
                                      "total": t.get("totalBytes", 0), "speed": t.get("speed", 0)}
                                     for t in (s.get("transferring") or [])]
            except Exception:
                pass
        dest = str(job.get("dest") or "")
        lp = local_dest(dest)
        p = os.path.realpath(lp) if lp else ""
        # v3.57：落盘内容须落在任一存储空间内，且不能是卷根本身
        in_root = bool(lp) and any(
            p != os.path.realpath(r) and p.startswith(os.path.realpath(r) + os.sep)
            for r in local_roots())
        if in_root and os.path.isdir(p):
            d["exists"] = True
            total, rows = 0, []
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for fn in sorted(files):
                    fp = os.path.join(root, fn)
                    try:
                        if not os.path.isfile(fp):
                            continue
                        st = os.stat(fp)
                    except OSError:
                        continue
                    total += st.st_size
                    rows.append({"path": os.path.relpath(fp, p), "size": st.st_size,
                                 "mtime": int(st.st_mtime)})
                    if len(rows) >= 1000:
                        d["truncated"] = True
                        break
                if d["truncated"]:
                    break
            rows.sort(key=lambda x: -x["size"])
            d["files"], d["diskTotal"], d["diskCount"] = rows, total, len(rows)
        return d

    if action == "cleanup":
        # 清理落盘文件（危险操作：仅允许存储空间内的任务目录，防止路径逃逸或整卷删除）
        dest = str(body.get("dest") or "")
        # v3.61：该目录还有任务在排队/下载时拒绝清理，避免边下边删
        with LOCK:
            busy = sum(1 for j in load_jobs()
                       if j.get("dest") == dest and j.get("state") in ("running", "queued"))
        if busy:
            return {"error": "该目录还有 %d 个任务在排队或下载中，请先暂停/等任务结束再清理" % busy}
        lp = local_dest(dest)
        p = os.path.realpath(lp) if lp else ""
        if not lp or not any(
                p != os.path.realpath(r) and p.startswith(os.path.realpath(r) + os.sep)
                for r in local_roots()):
            return {"error": "非法路径，仅允许清理存储空间内的任务目录"}
        if not os.path.isdir(p):
            return {"error": "目录不存在（可能已清理过）"}
        freed = 0
        for root, dirs, files in os.walk(p):
            for fn in files:
                try:
                    freed += os.stat(os.path.join(root, fn)).st_size
                except OSError:
                    pass
        shutil.rmtree(p)
        with LOCK:
            jobs = load_jobs()
            for j in jobs:
                if j.get("dest") == dest:
                    j["cleaned"] = True
            save_jobs(jobs)
        return {"ok": True, "freed": freed}

    if action == "accounts":
        # 账号列表：只回传名称、类型与身份记录里的登录邮箱，token/密码等敏感字段绝不离开后端
        # v3.51：config/dump 会列出「只有 captcha_token、没有 type」的空壳段，
        # 那是 rclone 给已改名/删除的旧账号名回写验证码留下的残留，rclone 无法实例化，
        # 放进列表就会显示 type=? 并在「一键测试全部」里刷一排 didn't find section。
        # 这里统一过滤，并把名字单独回传给前端供「清理残留」使用。
        try:
            d = rc("config/dump")
        except Exception as e:
            return {"error": str(e)[:150], "accounts": [], "ghosts": []}
        meta = load_meta()
        out, ghosts = [], []
        for k, v in sorted(d.items()):
            atype = str((v or {}).get("type") or "").strip()
            if not atype:
                ghosts.append(k)
                continue
            ident = meta.get(k)
            email = str((ident or {}).get("email") or "") if isinstance(ident, dict) else ""
            cred = cred_health(k, v, meta) if atype == "pikpak" else None
            out.append({"name": k, "type": atype, "email": email, "cred": cred})
        return {"accounts": out, "ghosts": ghosts}

    if action == "providers":
        return {"providers": PROVIDERS}

    if action == "credcheck":
        # 手动触发一轮凭证保活 / 体检（后台保活每 12 小时自动跑一次，无需人工点）
        names = body.get("names") if isinstance(body.get("names"), list) else None
        rs = cred_keepalive_once(names)
        return {"results": rs, "okCount": sum(1 for r in rs if r["ok"]), "total": len(rs)}

    if action == "acctadd":
        name = str(body.get("name") or "").strip()
        atype = str(body.get("type") or "").strip()
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        if not re.match(r'^[\w\u4e00-\u9fff-]{1,64}$', name):
            return {"error": "账号名只能包含中英文/数字/下划线/连字符（1-64 位）"}
        if atype not in [p["type"] for p in PROVIDERS]:
            return {"error": "不支持的类型: " + atype[:40]}
        clean = {}
        for k, v in params.items():
            k, v = str(k).strip(), str(v).strip()
            if k and v:
                clean[k] = v
        if atype == "webdav":
            # v3.67：OpenList/Alist 场景 vendor 几乎总是 other，不填自动补默认，
            # 保证 rclone 非交互 config/create 不会因缺 vendor 失败
            clean.setdefault("vendor", "other")
        if atype == "pikpak" and clean.get("refresh_token") and len(clean["refresh_token"]) < 20:
            return {"error": "refresh_token 看起来无效（长度不足）"}
        if atype == "pikpak":
            # pikpak 后端只认 token 字段：refresh_token 换发，或账号密码直登
            if clean.get("refresh_token"):
                try:
                    tok_json, newrt = pikpak_token_json(clean["refresh_token"])
                except RuntimeError as e:
                    return {"error": mask_secret(str(e))}
                clean["token"] = tok_json
                clean["refresh_token"] = newrt
            elif clean.get("username") and clean.get("password"):
                try:
                    tok_json, newrt, did = pikpak_signin(clean["username"], clean["password"],
                                                        clean.get("device_id"))
                except RuntimeError as e:
                    return {"error": mask_secret(str(e))}
                clean.pop("username", None)
                clean.pop("password", None)
                clean["token"] = tok_json
                clean["refresh_token"] = newrt
                clean["device_id"] = did
            else:
                return {"error": "PikPak 需要填 refresh_token，或填账号 + 密码"}
        try:
            conf = rc("config/dump")
            if name in conf:
                return {"error": "账号名已存在"}
        except Exception:
            pass
        try:
            rc("config/create", name=name, type=atype,
               parameters=json.dumps(clean, ensure_ascii=False), obscure="true")
        except Exception:
            pass  # 后端实例化校验可能报错，但配置段已写入 —— 以 list 实测为准
        ok, err = (pikpak_verify(name) if atype == "pikpak" else verify_list(name))
        if not ok:
            try:
                rc("config/delete", name=name)
            except Exception:
                pass
            return {"error": "连接验证失败，已回滚删除：%s" % err}
        return {"ok": True, "note": err} if err else {"ok": True}

    if action == "acctdel":
        name = str(body.get("name") or "").strip()
        try:
            rc("config/delete", name=name)
        except Exception as e:
            return {"error": str(e)[:150]}
        # v3.68：代理 rcd（5573）内存里也删一份 —— 只删主进程时，代理进程的下一次
        # SaveConfig 会把已删段落写回 rclone.conf，账号就在云盘浏览列表里"复活"
        try:
            rc("config/delete", name=name, _proxy=True)
        except Exception:
            pass
        # v3.51：两个 rcd 各清一次，否则另一个进程的下一次 SaveConfig 会把它写回来
        try:
            purge_ghost_sections([name])
        except Exception:
            pass
        return {"ok": True}

    if action == "acctpurge":
        # v3.51 手动清理无 type 的残留空壳段（改名 / 删除后 rclone 的 captcha 回写）
        removed = purge_ghost_sections()
        # v3.68 双进程对账：代理 rcd（5573）内存里比主进程多出来的段（含有效段）同步删除。
        # 根治「账号删除后云盘浏览列表又冒出来」——旧版只删主进程，代理侧写回即复活。
        ghost = []
        try:
            main = rc("config/dump", _timeout=20)
        except Exception:
            main = {}
        try:
            pconf = rc("config/dump", _proxy=True, _timeout=20)
        except Exception:
            pconf = {}
        if isinstance(main, dict) and isinstance(pconf, dict):
            for k in pconf:
                if k not in main:
                    try:
                        rc("config/delete", name=k, _proxy=True, _timeout=20)
                        ghost.append(k)
                    except Exception:
                        pass
        return {"ok": True, "removed": removed, "resynced": ghost}

    if action == "acctrename":
        # rclone RC 没有 config/rename，只能「读整段 → 建新段 → 删旧段」。
        # config/dump 的值已是配置文件里的形态，必须用 obscure=false 原样写回
        # （实测 token / refresh_token / device_id / captcha_token 逐字段一致）。
        old = str(body.get("name") or "").strip()
        new = str(body.get("newName") or "").strip()
        if not old or not new:
            return {"error": "账号名不能为空"}
        if old == new:
            return {"ok": True, "name": new}
        if not re.fullmatch(r"[\w\u4e00-\u9fff.\-]{1,64}", new):
            return {"error": "账号名只能用中英文、数字、_ - .，最长 64 字符"}
        try:
            conf = rc_ok("config/dump")
        except Exception as e:
            return {"error": "读取配置失败：%s" % str(e)[:120]}
        if old not in conf:
            return {"error": "原账号不存在：" + old}
        if new in conf:
            return {"error": "账号名已存在：" + new}
        seg = dict(conf[old] or {})
        atype = str(seg.pop("type") or "")
        if not atype:
            return {"error": "账号类型缺失，无法重命名"}
        try:
            rc_ok("config/create", name=new, type=atype,
                  parameters=json.dumps(seg, ensure_ascii=False), obscure="false")
        except Exception as e:
            return {"error": "创建新账号失败：%s" % str(e)[:150]}
        try:
            chk = rc_ok("config/get", name=new)
            if str(chk.get("type") or "") != atype:
                raise RuntimeError("类型不一致")
        except Exception as e:
            try:
                rc("config/delete", name=new)
            except Exception:
                pass
            return {"error": "新账号回读失败，已回滚：%s" % str(e)[:120]}
        # 身份表与任务记录的改写必须持 LOCK：jobs 轮询 / worker 线程随时可能并发写盘，
        # 裸奔的整表覆盖写会把别人刚写的进度/新任务回滚掉（v3.57 修复的丢数据竞态）
        try:
            with LOCK:
                m = load_meta()
                if old in m:
                    m[new] = m.pop(old)
                    save_meta(m)
        except Exception:
            pass
        # 任务记录：remote 和 src 的「账号:路径」前缀同步替换，避免历史任务指向空配置
        try:
            with LOCK:
                jobs = load_jobs()
                changed = False
                for j in jobs if isinstance(jobs, list) else []:
                    if not isinstance(j, dict):
                        continue
                    if str(j.get("remote") or "") == old:
                        j["remote"] = new
                        changed = True
                    src = str(j.get("src") or "")
                    if src.startswith(old + ":"):
                        j["src"] = new + src[len(old):]
                        changed = True
                if changed:
                    save_jobs(jobs)
        except Exception:
            pass
        try:
            rc_ok("config/delete", name=old)
        except Exception as e:
            return {"error": "新名已生效，但旧名删除失败（请手动清理）：%s" % str(e)[:120]}
        # v3.51：pikpak 后端可能给刚删掉的旧名回写 captcha_token 造出空壳段，
        # 两个 rcd 都清一次，避免下次「一键测试全部」又列出旧账号。
        try:
            purge_ghost_sections([old])
        except Exception:
            pass
        return {"ok": True, "name": new}

    if action == "acctrepair":
        # 一键修复 PikPak 账号：token 空则用 refresh_token 重换；device_id 被风控则换新
        name = str(body.get("name") or "").strip()
        try:
            conf = rc("config/dump")
        except Exception as e:
            return {"error": str(e)[:150]}
        targets = [name] if name else [k for k, v in conf.items()
                                       if str((v or {}).get("type")) == "pikpak"]
        results = []
        for n in targets:
            cfg = conf.get(n) or {}
            msg = ""
            if not str(cfg.get("token") or ""):
                rt = cfg_rt(cfg)
                if not rt:
                    results.append({"name": n, "ok": False, "msg": "没有 refresh_token，请在「编辑」里填新的"})
                    continue
                try:
                    tok_json, newrt = pikpak_token_json(rt)
                except RuntimeError as e:
                    results.append({"name": n, "ok": False, "msg": str(e)[:180]})
                    continue
                try:
                    rc("config/update", name=n, type="pikpak",
                       parameters=json.dumps({"token": tok_json, "refresh_token": newrt}))
                    msg = "已用 refresh_token 换发 token；"
                except Exception as e:
                    results.append({"name": n, "ok": False, "msg": "写入 token 失败：%s" % str(e)[:120]})
                    continue
            ok, note = pikpak_verify(n)
            if ok:
                meta_patch(n, {"lastOk": int(time.time()), "lastErr": "", "cred": "ok"})
            else:
                meta_patch(n, {"lastErr": str(note)[:200], "lastCheck": int(time.time())})
            sync_rt_mirror(n)
            results.append({"name": n, "ok": ok, "msg": (msg + (note or "连接正常"))[:200]})
        return {"results": results, "okCount": sum(1 for r in results if r["ok"]),
                "total": len(results)}

    if action == "accttest":
        name = str(body.get("name") or "").strip()
        # 真连接测试：list 根目录会真正实例化该 remote（about 很多后端不支持，会假成功）
        atype = ""
        try:
            atype = str((rc("config/get", name=name) or {}).get("type") or "")
        except Exception:
            pass
        if atype == "pikpak":
            ok, err = pikpak_verify(name)   # 被风控时自动换 device_id 自愈
        else:
            ok, err = verify_list(name)
        if atype == "pikpak":
            if ok:
                meta_patch(name, {"lastOk": int(time.time()), "lastErr": "", "cred": "ok"})
                sync_rt_mirror(name)        # rclone 若刚轮换过 rt，立刻同步顶层镜像
            else:
                meta_patch(name, {"lastErr": str(err)[:200], "lastCheck": int(time.time())})
        if not ok:
            # v3.38：告诉前端这是「只能重新登录」的凭证错误，行内直接给按钮
            return {"ok": False, "error": mask_secret(err[:250]), "relink": need_relink(err)}
        used = total = free = None
        try:
            a = rc_ok("operations/about", fs=name + ":")
            used, total, free = a.get("used"), a.get("total"), a.get("free")
        except Exception:
            pass
        try:
            items = len((rc_ok("operations/list", fs=name + ":", remote="") or {}).get("list") or [])
        except Exception:
            items = None
        return {"ok": True, "used": used, "total": total, "free": free,
                "items": items, "note": err}

    if action == "acctget":
        # 编辑账号：只回传脱敏后的当前值，refresh_token / 密码等绝不离开后端
        name = str(body.get("name") or "").strip()
        try:
            cfg = rc("config/get", name=name)
        except Exception as e:
            return {"error": "读取账号配置失败: %s" % str(e)[:150]}
        atype = str(cfg.get("type") or "")
        if not atype:
            return {"error": "账号不存在: " + name}
        prov = next((p for p in PROVIDERS if p["type"] == atype), None)
        if prov:
            defs = [{"name": p["name"], "label": p["label"],
                     "secret": bool(p.get("secret")) or bool(re.search(r"token|pass|secret|key", p["name"], re.I)),
                     "ph": p.get("ph", "")} for p in prov["params"]]
        else:
            # 未知类型：按现有配置键生成字段，跳过 rclone 内部缓存字段
            defs = [{"name": k, "label": k,
                     "secret": bool(re.search(r"pass|token|secret|key", k, re.I)), "ph": ""}
                    for k in sorted(cfg) if k not in ("type", "token", "captcha_token")]
        fields = []
        for d in defs:
            s = str(cfg.get(d["name"]) or "")
            if not s:
                masked = ""
            elif d["secret"]:
                masked = "已设置 · %d 位" % len(s)
            else:
                masked = (s[:6] + "…" + s[-4:]) if len(s) > 14 else s
            fields.append(dict(d, has=bool(s), masked=masked))
        ident = load_meta().get(name)
        ident = ident if isinstance(ident, dict) else {}
        return {"name": name, "type": atype, "fields": fields,
                "email": str(ident.get("email") or ""),
                "sub": str(ident.get("sub") or "")}

    if action == "acctedit":
        # 局部更新账号配置。留空的字段保持原值；pikpak 可用现有 refresh_token 重新换发 token
        name = str(body.get("name") or "").strip()
        renewed = bool(body.get("refresh"))
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        clean = {}
        for k, v in params.items():
            k, v = str(k).strip(), str(v).strip()
            if k and v:
                clean[k] = v
        if not clean and not renewed:
            return {"error": "没有填写任何要修改的内容"}
        try:
            cfg = rc("config/get", name=name)
        except Exception as e:
            return {"error": "账号不存在或读取失败: %s" % str(e)[:150]}
        atype = str(cfg.get("type") or "")
        if not atype:
            return {"error": "账号不存在: " + name}
        if atype == "pikpak" and "refresh_token" in clean and len(clean["refresh_token"]) < 20:
            return {"error": "refresh_token 看起来无效（长度不足）"}
        if atype == "pikpak" and (renewed or ("refresh_token" in clean and body.get("clearToken", True))):
            # pikpak 后端不读 refresh_token，必须换出 access_token 后整段写回 token
            rt = clean.get("refresh_token") or cfg_rt(cfg)
            if not rt:
                return {"error": "该账号没有可用的 refresh_token，请在下面填写一个新的"}
            try:
                tok_json, newrt = pikpak_token_json(rt)
            except RuntimeError as e:
                return {"error": mask_secret(str(e))}
            clean["token"] = tok_json
            clean["refresh_token"] = newrt
        # config/update 在后端实例化校验失败时也会返回 error，但配置已落盘 —— 以回读为准
        try:
            rc("config/update", name=name, type=atype,
               parameters=json.dumps(clean, ensure_ascii=False), obscure="true")
        except Exception:
            pass
        try:
            now = rc("config/get", name=name)
            bad = []
            for k, v in clean.items():
                cur = str(now.get(k) or "")
                if v == "" and cur:
                    bad.append(k + "(未清空)")
                elif v != "" and not cur:
                    bad.append(k + "(未写入)")
            if bad:
                return {"error": "写入未生效: " + ", ".join(bad)}
        except Exception as e:
            return {"error": "回读配置失败: %s" % str(e)[:150]}
        # 写完立刻实测：配置落盘 ≠ 能连上（token 过期/被轮换/device_id 被风控都会失败）
        ok, err = (pikpak_verify(name) if atype == "pikpak" else verify_list(name))
        if not ok:
            return {"error": "配置已写入，但 rclone 仍连不上：%s" % err}
        return {"ok": True, "verified": True, "note": err} if err else {"ok": True, "verified": True}

    if action == "acctimport":
        # 批量导入：面板/采集器/粘贴都走这里，服务端负责识别账号、换发、实测
        items = body.get("items") if isinstance(body.get("items"), list) else []
        if not items:
            return {"error": "没有可导入的条目"}
        return import_items(items, create=bool(body.get("create")), dry=bool(body.get("dryRun")))

    if action == "acctimporttext":
        # 粘贴导入：每行「账号名 token」或只贴 token；也可直接贴面板导出的 JSON
        items = parse_import_text(body.get("text"))
        if not items:
            return {"error": "没解析出 refresh_token —— 每行格式：账号名 + 空格 + token，或只贴一长串 token"}
        # v3.38 定向重登：从某个失效账号的「重新登录」按钮进来时，
        # 只贴 token 的条目一律归到该账号，且禁止自动新建（防止登错号时多出一个新账号）。
        # 身份防错配仍由 import_items 用换发返回的真实 sub 兜底，不会把 A 的凭证写进 B。
        target = str(body.get("target") or "").strip()
        if target:
            for it in items:
                if isinstance(it, dict) and not str(it.get("env") or "").strip():
                    it["env"] = target
            return import_items(items, create=False)
        return import_items(items, create=bool(body.get("create", True)))

    if action == "acctident":
        # 识别账号身份（sub / 邮箱）写进身份表：一次识别，之后任何电脑导入都能自动对号
        names = body.get("names") if isinstance(body.get("names"), list) else []
        try:
            conf = rc("config/dump")
        except Exception as e:
            return {"error": str(e)[:150]}
        targets = [n for n in names if n in conf] or [k for k, v in conf.items()
                                                     if str((v or {}).get("type")) == "pikpak"]
        results = []
        for n in targets:
            cfg = conf.get(n) or {}
            at = ""
            try:
                at = str(json.loads(str(cfg.get("token") or "{}")).get("access_token") or "")
            except Exception:
                at = ""
            sub, mail = pikpak_me(at)
            note = ""
            if not sub:
                rt = cfg_rt(cfg)
                if not rt:
                    results.append({"name": n, "ok": False, "msg": "没有 refresh_token，无法识别"})
                    continue
                try:
                    tok_json, newrt, sub, at = pikpak_token_full(rt)
                except RuntimeError as e:
                    results.append({"name": n, "ok": False, "msg": str(e)[:160]})
                    continue
                try:
                    rc("config/update", name=n, type="pikpak",
                       parameters=json.dumps({"token": tok_json, "refresh_token": newrt}))
                    note = "token 已顺带换发"
                except Exception:
                    pass
                _s, mail = pikpak_me(at)
                if mail:
                    note = (note + "；").lstrip("；") if note else note
            meta_set(n, sub, mail)
            results.append({"name": n, "ok": bool(sub), "sub": sub, "email": mail,
                            "msg": (note or "") + ((" " + mail) if mail else "") or (sub or "已识别")})
        return {"results": [dict(r, msg=(r.get("msg") or "")[:160]) for r in results],
                "okCount": sum(1 for r in results if r["ok"]), "total": len(results)}

    if action == "urldl":
        # URL 直链下载：rclone 直接抓取到 NAS（/<卷根>/<folder>/），走任务队列展示
        url = str(body.get("url") or "").strip()
        sub = str(body.get("folder") or "URL下载").strip("/")
        if not re.match(r'^https?://\S+$', url):
            return {"error": "仅支持 http(s) 直链"}
        if "/" in sub or sub in (".", "..") or not sub:
            return {"error": "保存目录需为单层文件夹名"}
        # v3.57：可选存储空间（volume 传 localdirs 返回的 rclone 根，缺省主卷）
        vol_req = str(body.get("volume") or "").strip().rstrip("/")
        vols = STATE.get("volumePairs") or [LEGACY_VOLUME]
        if vol_req:
            uvol = next((p for p in vols if p["rclone"] == vol_req), None)
            if not uvol:
                return {"error": "未知存储空间：%s" % vol_req}
        else:
            uvol = vols[0]
        uroot = uvol["rclone"]
        try:
            r = rc("operations/copyurl", fs=uroot + "/" + sub, remote="",
                   url=url, autoFilename="true", _async="true")
        except Exception as e:
            return {"error": "提交失败: %s" % str(e)[:150]}
        jid = r.get("jobid")
        if not jid:
            return {"error": "未获取到任务 id: %s" % json.dumps(r)[:120]}
        guess = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "url-file"
        job = {"id": int(time.time() * 1000), "jobid": jid, "remote": "URL",
               "subpath": guess, "src": url, "mode": "copy",
               "dest": uroot + "/" + sub, "state": "running",
               "started": time.strftime("%m-%d %H:%M"),
               "bytes": 0, "total": 0, "speed": 0, "eta": None, "error": ""}
        with LOCK:
            jobs = load_jobs()
            jobs.append(job)
            save_jobs(jobs)
        return {"job": job}

    if action == "cloudrm":
        remote = str(body.get("remote") or "").strip()
        path = str(body.get("path") or "").strip("/")
        if not remote or not path:
            return {"error": "缺少参数"}
        # v3.44：hard=true 走 PikPak 的彻底删除（files:batchDelete），不进回收站
        if _truthy(body.get("hard")):
            return cloud_hard_delete(remote, path, bool(body.get("isfile")))
        try:
            if body.get("isfile"):
                rc("operations/deletefile", _strict=True, fs=remote + ":", remote=path)
            else:
                rc("operations/purge", _strict=True, fs=remote + ":", remote=path)
        except Exception as e:
            return {"error": str(e)[:200]}
        return {"ok": True}

    if action == "renamebatch":
        remote = str(body.get("remote") or "").strip()
        items = body.get("items")
        if not isinstance(items, list) or not items:
            return {"error": "缺少参数：items（要重命名的文件列表）"}
        if len(items) > RENAME_MAX_ITEMS:
            return {"error": "一次最多重命名 %d 项，请分批处理" % RENAME_MAX_ITEMS}
        return rename_batch(remote, str(body.get("path") or "").strip("/"), items)

    if action == "localrenamebatch":
        # v3.50 本地文件页批量改名：真实改动 NAS 磁盘，整批预检后执行
        return local_rename_batch(str(body.get("path") or ""), body.get("items"))

    if action == "trashlist":
        remote = str(body.get("remote") or "").strip()
        if not remote:
            return {"error": "缺少参数：remote（请选择账号）"}
        if remote not in pikpak_remotes():
            return {"error": "只有 PikPak 账号有回收站；该账号不是 PikPak 类型",
                    "unsupported": True}
        try:
            rows = pk_trash_list(remote)
        except Exception as e:
            return {"error": "读取回收站失败：%s" % mask_secret(str(e))[:150]}
        items = [map_trash(f) for f in rows]
        return {"items": items, "total": len(items),
                "bytes": sum(i["size"] for i in items)}

    if action == "trashop":
        # op=delete(彻底删除) / restore(恢复到原位置)
        remote = str(body.get("remote") or "").strip()
        op = str(body.get("op") or "").strip()
        if not remote:
            return {"error": "缺少参数：remote（请选择账号）"}
        if remote not in pikpak_remotes():
            return {"error": "只有 PikPak 账号有回收站", "unsupported": True}
        if op not in ("delete", "restore"):
            return {"error": "op 只能是 delete / restore"}
        ids = body.get("ids")
        if _truthy(body.get("all")):
            try:
                ids = [f.get("id") for f in pk_trash_list(remote, max_pages=8)]
            except Exception as e:
                return {"error": "读取回收站失败：%s" % mask_secret(str(e))[:150]}
        if not isinstance(ids, list) or not ids:
            return {"error": "没有要处理的条目"}
        try:
            r = pk_batch_ids(remote, "batchDelete" if op == "delete" else "batchUntrash", ids)
        except Exception as e:
            return {"error": "操作失败：%s" % mask_secret(str(e))[:150]}
        return {"ok": r.get("ok"), "op": op,
                "text": ("已彻底删除 %d 项" % r.get("ok")) if op == "delete"
                        else ("已恢复 %d 项" % r.get("ok"))}

    if action == "playinfo":
        # 只签发地址，不代理流量：浏览器直连 CDN 才能跑满带宽
        return cloud_play_info(str(body.get("remote") or "").strip(),
                               str(body.get("path") or "").strip("/"))

    if action == "unzipinfo":
        return unzip_preview(str(body.get("remote") or "").strip(),
                             str(body.get("path") or "").strip("/"),
                             str(body.get("password") or ""))

    if action == "unzip":
        return unzip_start_cloud(str(body.get("remote") or "").strip(),
                                 str(body.get("path") or "").strip("/"),
                                 str(body.get("password") or ""))

    if action == "localunzip":
        return local_unzip_start(str(body.get("path") or ""),
                                 str(body.get("password") or ""),
                                 _truthy(body.get("deleteAfter")))

    if action == "cloudmv":
        # 仅支持重命名云端文件（rclone rc 没有目录改名方法）
        remote = str(body.get("remote") or "").strip()
        path = str(body.get("path") or "").strip("/")
        newname = str(body.get("newname") or "").strip()
        if not remote or not path or not newname:
            return {"error": "缺少参数"}
        if "/" in newname or newname in (".", ".."):
            return {"error": "新名称不合法"}
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        dst = (parent + "/" + newname) if parent else newname
        try:
            rc("operations/movefile", _strict=True,
               srcFs=remote + ":", srcRemote=path,
               dstFs=remote + ":", dstRemote=dst)
        except Exception as e:
            return {"error": str(e)[:200]}
        return {"ok": True, "newPath": dst}

    if action == "cloudlink":
        # 分享链接：按开关决定 优先代理/优先直连，另一条路兜底（PikPak 分享受地区限制）
        remote = str(body.get("remote") or "").strip()
        path = str(body.get("path") or "").strip("/")
        if not remote or not path:
            return {"error": "缺少参数"}
        s = load_settings()
        order = [("proxy", True), ("direct", False)] if s["proxy"] else \
                [("direct", False), ("proxy", True)]
        errs = []
        for via, pv in order:
            code, out, err = rclone_cli(["link", remote + ":" + path], proxy=pv)
            if code == 0 and out.startswith("http"):
                return {"ok": True, "url": out, "via": via}
            errs.append("%s: %s" % (via, (err or out)[:90]))
        return {"error": " ｜ ".join(errs)}

    if action == "speedtest":
        # 同一云端文件：直连 / 代理各下载最多 SPEEDTEST_SECONDS 秒，对比 MiB/s。
        # 前端可带 phase 分两次调用，用来显示「正在测直连 / 正在测代理」；
        # 不带 phase 时两条都跑，兼容未强刷的旧页面。
        remote = str(body.get("remote") or "").strip()
        path = str(body.get("path") or "").strip("/")
        if not remote or not path:
            return {"error": "缺少参数"}
        phase = str(body.get("phase") or "both").strip()
        if phase in ("direct", "proxy"):
            routes = [(phase, phase == "proxy")]
        elif phase == "both":
            routes = [("direct", False), ("proxy", True)]
        else:
            return {"error": "phase 只能是 direct / proxy / both"}
        src = remote + ":" + path
        # 测速临时文件放主卷（对比测速本身与存储空间无关，只是需要一个落盘点）
        tmp_local = primary_root().rstrip("/") + "/__speedtest.tmp"
        tmp_cont = (STATE.get("volumePairs") or [LEGACY_VOLUME])[0]["rclone"].rstrip("/") + "/__speedtest.tmp"
        # 硬超时必须放在容器内：PikPak captcha / TLS 连接被重置时 rclone 会一直等远端，
        # --max-duration 只是软限制（等 API 响应期间不检查），只靠 Python 超时杀掉
        # docker exec 客户端又会让容器内 rclone 残留，下一次测速两个进程互踩同一临时文件。
        cont_cmd = ["/usr/bin/timeout", "-s", "KILL",
                    "%ds" % (SPEEDTEST_SECONDS + 3), "rclone", "copyto", src, tmp_cont,
                    "--max-duration", "%ds" % SPEEDTEST_SECONDS, "--ignore-checksum",
                    "--buffer-size", "0", "--log-level", "ERROR"]
        res, errs = {}, {}
        try:
            for label, pv in routes:
                try:
                    os.remove(tmp_local)
                except OSError:
                    pass
                cmd = ["docker", "exec"] + (
                    proxy_env_args() if pv else []
                ) + [RCLONE_CONTAINER] + cont_cmd
                t0 = time.time()
                p = None
                try:
                    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                       timeout=SPEEDTEST_SECONDS + 6)
                except subprocess.TimeoutExpired:
                    pass
                except Exception as e:
                    errs[label] = str(e)[:160]
                elapsed = time.time() - t0
                # 小文件提前下完时用真实耗时做分母，避免固定窗口把速度算低
                window = min(max(elapsed, 0.2), float(SPEEDTEST_SECONDS))
                try:
                    sz = os.path.getsize(tmp_local)
                except OSError:
                    sz = 0
                res[label] = round(sz / 1048576.0 / window, 2)
                tail = ((p.stderr or "") + (p.stdout or ""))[-180:] if p else "timeout"
                if not tail.strip() or tail == "timeout":
                    # 被 timeout -s KILL 掐掉时 rclone 来不及写日志，别把空诊断丢给用户
                    tail = ("远端在 %ds 内无响应，已强制终止（常见于 PikPak captcha / 风控 / 连接被重置）"
                            % (SPEEDTEST_SECONDS + 3))
                errs[label] = (errs.get(label, "") + " " + tail).strip()[:200]
            out = {"seconds": SPEEDTEST_SECONDS, "phase": phase}
            out.update(res)
            if all(res.get(k, 0) == 0 for k, _ in routes):
                out["error"] = ("没拉到数据（文件不存在 / 日配额 / 风控 captcha / 连接被重置都可能）。"
                                "诊断: %s") % (errs.get(routes[0][0], "")[:150])
            return out
        finally:
            try:
                os.remove(tmp_local)
            except OSError:
                pass

    # 用户已删除 aria2-pro / yt-dlp 容器，BT 磁力与视频下载入口从控制台移除。
    # 这里只给尚未强刷的旧页面一个明确提示，不再调用任何已不存在的服务。
    if action in ("btadd", "btlist", "btctrl", "ytadd", "ytlist", "ytdel"):
        return {"error": "BT / 磁力与视频下载已停用（aria2-pro、yt-dlp 容器已删除），请 Ctrl+F5 刷新页面",
                "tasks": []}

    if action == "verify":
        # 一致性校验：云盘 ↔ 落盘目录/单文件。只读，不改任何数据。
        return verify_compare(body.get("remote"), body.get("cloudPath"),
                              body.get("localPath"), body.get("mode"), body.get("limit"))

    # ── v3.81：云端文件夹「下载完毕 / 有更新」 ─────────────────────────────
    if action == "folderstatus":
        remote = str(body.get("remote") or "").strip()
        if not remote:
            return {"error": "缺少参数：remote"}
        return folder_status_batch(remote, str(body.get("path") or "").strip("/"),
                                   body.get("folders"), bool(body.get("refresh")))

    if action == "folderack":
        remote = str(body.get("remote") or "").strip()
        if not remote:
            return {"error": "缺少参数：remote"}
        return folder_ack(remote, str(body.get("path") or "").strip("/"),
                          body.get("names"))

    # ── v3.81：云端 / 本地文件完整 CRUD ───────────────────────────────────
    if action == "cloudmkdir":
        return cloud_mkdir(str(body.get("remote") or "").strip(),
                           str(body.get("path") or "").strip("/"),
                           body.get("name"))

    if action == "cloudmove":
        return cloud_move_batch(str(body.get("remote") or "").strip(),
                                str(body.get("path") or "").strip("/"),
                                body.get("items"),
                                str(body.get("dest") or "").strip("/"))

    if action == "cloudrmbatch":
        return cloud_rm_batch(str(body.get("remote") or "").strip(),
                              str(body.get("path") or "").strip("/"),
                              body.get("items"), bool(body.get("hard")))

    if action == "localmkdir":
        return local_mkdir(str(body.get("path") or ""), body.get("name"))

    if action == "localmove":
        return local_move_batch(str(body.get("path") or ""), body.get("items"),
                                str(body.get("dest") or ""))

    if action == "localtrash":
        return local_trash_batch(str(body.get("path") or ""), body.get("items"))

    if action == "localtrashlist":
        return local_trash_list()

    if action == "localtrashrestore":
        return local_trash_restore(body.get("ids"), bool(body.get("all")))

    if action == "localtrashpurge":
        return local_trash_purge(body.get("ids"), bool(body.get("all")))

    if action == "localfiles":
        # 本地文件页：只读列目录（文件本体走 /media/ 的 Range 流）
        return localfiles_listing(str(body.get("path") or ""))

    if action == "localdirs":
        # 各存储空间下的现有目录（供保存目录下拉点选）。
        # v3.57 返回分组 volumes（root=rclone 侧根，下载时随 start/urldl 传回 volume）；
        # dirs 保留主卷平铺列表，兼容旧前端。
        def _vol_dirs(root):
            try:
                return sorted(
                    n for n in os.listdir(root)
                    if os.path.isdir(os.path.join(root, n))
                    and not n.startswith(".") and n != "__speedtest.tmp"
                )
            except Exception as e:
                return []
        vols = storages()
        volumes = [{"root": v["rclone"], "console": v["console"], "name": v["name"],
                    "dirs": _vol_dirs(v["console"])} for v in vols]
        return {"dirs": volumes[0]["dirs"] if volumes else [], "volumes": volumes}

    if action == "health":
        try:
            m = rc("core/memstats")
        except Exception as e:
            return {"error": str(e)[:150]}
        fin = 0
        try:
            jl = rc("job/list")
            fin = len(jl.get("finishedIds") or [])
        except Exception:
            pass
        return {"allocMB": round((m.get("Alloc") or 0) / 1048576, 1),
                "sysMB": round((m.get("Sys") or 0) / 1048576, 1),
                "finishedJobs": fin}

    raise RuntimeError("未知操作")

# ---------------------------------------------------------------------------
# 前端静态资源（./static/，容器内即 /app/static）
# ---------------------------------------------------------------------------
# v3.80：候选里必须带上「脚本所在目录」—— 之前只有 /app 与 /vol1 两个写死的路径，
# 用户把仓库放到 /opt/cloud-shuttle 或任意 bind 目录时，两处都不存在，STATIC_DIR 就
# 落回不存在的 /app/static，整站静态资源 404（表现为打开页面一片白）。
_APP_HOME = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR_CANDIDATES = ("/app/static", "/vol1/@appshare/zcode/console/static",
                         os.path.join(_APP_HOME, "static"))
STATIC_DIR = next((p for p in STATIC_DIR_CANDIDATES if os.path.isdir(p)), STATIC_DIR_CANDIDATES[-1])
COLLECTOR_PS_CANDIDATES = ("/app/collector.ps1", "/vol1/@appshare/zcode/console/collector.ps1",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "collector.ps1"))
LOGIN_PS_CANDIDATES = ("/app/login-collector.ps1", "/vol1/@appshare/zcode/console/login-collector.ps1",
                       os.path.join(os.path.dirname(os.path.abspath(__file__)), "login-collector.ps1"))


def _ps_to_bat(ps_candidates, console_url, title, stem, missing):
    """现场生成一次性 .bat：把 PowerShell 脚本 base64 内嵌，用 certutil 解码后执行。
    双击即可运行，目标电脑不需要装 Python / 开端口 / 手工复制。"""
    ps_path = next((p for p in ps_candidates if os.path.isfile(p)), "")
    if not ps_path:
        raise RuntimeError("服务器缺少 " + missing)
    with open(ps_path, encoding="utf-8") as f:
        ps = f.read().replace("__CONSOLE_URL__", console_url)
    raw = b"\xef\xbb\xbf" + ps.encode("utf-8")            # BOM：PS 5.1 才会按 UTF-8 读中文
    b64 = base64.b64encode(raw).decode("ascii")
    lines = [b64[i:i + 700] for i in range(0, len(b64), 700)]  # cmd 单行上限 8191，必须切块
    bat = ["@echo off", "chcp 65001 >nul", "setlocal", "title " + title,
           'set "B64=%TEMP%\\' + stem + '.b64"', 'set "PS1=%TEMP%\\' + stem + '.ps1"',
           '> "%B64%" echo -----BEGIN CERTIFICATE-----']
    bat += ['>> "%B64%" echo ' + ln for ln in lines]
    bat += ['>> "%B64%" echo -----END CERTIFICATE-----',
            'certutil -decode -f "%B64%" "%PS1%" >nul',
            'if errorlevel 1 (echo   生成脚本失败 & pause & exit /b 1)',
            'powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"',
            'del "%B64%" >nul 2>nul', 'del "%PS1%" >nul 2>nul',
            "echo.", "pause"]
    return ("\r\n".join(bat) + "\r\n").encode("utf-8")


def collector_bat(console_url):
    """ProfileX 扫描采集器：读取本机各浏览器环境里已登录的 PikPak token。"""
    return _ps_to_bat(COLLECTOR_PS_CANDIDATES, console_url,
                      "PikPak Token Collector", "pk_collect", "collector.ps1")


def login_collector_ps(console_url):
    """v3.65：直接输出 login-collector.ps1（替换 CONSOLE_URL），供一键协议安装脚本下载。"""
    ps_path = next((p2 for p2 in LOGIN_PS_CANDIDATES if os.path.isfile(p2)), "")
    if not ps_path:
        raise RuntimeError("服务器缺少 login-collector.ps1")
    with open(ps_path, encoding="utf-8") as f:
        ps = f.read().replace("__CONSOLE_URL__", console_url)
    return b"\xef\xbb\xbf" + ps.replace("\n", "\r\n").encode("utf-8")  # BOM：PS 5.1 按 UTF-8 读中文


def login_collector_setup_bat(console_host):
    """v3.65：一键协议安装脚本。运行一次后，Windows 里 pkcollect: 链接会直接
    弹出干净浏览器采集窗口（不再需要每次下载 .bat）。只写 HKCU（用户级），可随时卸载。"""
    bat = [
        "@echo off",
        "chcp 65001 >nul",
        "title PikPak 一键采集协议安装",
        'set "DIR=%LOCALAPPDATA%\\PikPakCollector"',
        'set "HOST=' + console_host + '"',
        "echo [1/3] 下载采集脚本（来自控制台 http://%HOST%/login-collector.ps1）...",
        'if not exist "%DIR%" mkdir "%DIR%"',
        'set "PKHOST=%HOST%"',
        'set "PKDIR=%DIR%"',
        'powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = '
        '[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri (\'http://\' + $env:PKHOST + '
        '\'/login-collector.ps1\') -OutFile (Join-Path $env:PKDIR \'login-collector.ps1\') -UseBasicParsing } '
        'catch { Write-Host $_.Exception.Message; exit 1 }"',
        "if errorlevel 1 (echo   下载失败：请确认能访问控制台地址后重试 & pause & exit /b 1)",
        "echo [2/3] 注册 pkcollect:// 一键协议（仅当前用户）...",
        'reg add HKCU\\Software\\Classes\\pkcollect /ve /d "URL:PikPak Collector" /f >nul',
        'reg add HKCU\\Software\\Classes\\pkcollect /v "URL Protocol" /f >nul',
        'reg add HKCU\\Software\\Classes\\pkcollect\\shell\\open\\command /ve /d '
        '"powershell -NoProfile -ExecutionPolicy Bypass -File \\"%DIR%\\login-collector.ps1\\" \\"%1\\"" /f >nul',
        "echo [3/3] 完成！以后在控制台的「重新登录」弹窗里点「打开干净浏览器」即可直接弹出采集窗口。",
        "echo.",
        "echo   卸载：删除目录 %DIR%，并运行 reg delete HKCU\\Software\\Classes\\pkcollect /f",
        "pause",
    ]
    return ("\r\n".join(bat) + "\r\n").encode("utf-8")


def login_collector_bat(console_url):

    """干净浏览器登录采集器：开一个全新临时 profile，用户登录后自动抓 token，
    结束即删除临时目录。不读取用户现有浏览器的任何数据。"""
    return _ps_to_bat(LOGIN_PS_CANDIDATES, console_url,
                      "PikPak Clean Login", "pk_login", "login-collector.ps1")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                ".woff2": "font/woff2", ".woff": "font/woff", ".png": "image/png",
                ".ico": "image/x-icon", ".map": "application/json"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    # ── 安全（v3.18）：同源校验 + 拒绝简单请求 CSRF ──────────────────────────
    def _origin(self):
        return (self.headers.get("Origin") or "").strip().lower()

    def _same_origin(self):
        """Origin 缺失（curl/rclone 等非浏览器客户端）视为可信；浏览器跨站一律拒绝。
        这个控制台能"清理任务 / 删账号 / 删云端目录"，不能让任意网页一个 POST 就打过来。"""
        o = self._origin()
        if not o:
            return True
        hosts = {(self.headers.get("Host") or "").strip().lower()}
        # v3.80：反向代理（nginx / 群晖反代 / Cloudflare Tunnel）若改写或丢了 Host，
        # 浏览器带来的 Origin 就对不上，所有写操作会被 403 拦死。把部署时声明的
        # 对外地址也算作同源，用户设了 CONSOLE_PUBLIC_HOST 就能正常用域名访问。
        pub = str(os.environ.get("CONSOLE_PUBLIC_HOST") or "").strip().lower()
        if pub:
            hosts.add(pub)
        return any(h and o in ("http://" + h, "https://" + h) for h in hosts)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        # media-src 必须放行 PikPak CDN：Chromium 在 CSP 缺 media-src 时回落到
        # default-src 'self'，<video> 加载跨源直链会直接报 MEDIA_ELEMENT_ERROR
        # 「Media load rejected by URL safety check」，与 http/https 无关。
        # 只放 *.mypikpak.com，不放行任意 https 源，避免把播放器变成任意媒体代理。
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data: blob: https://*.mypikpak.com; "
                         "media-src 'self' blob: https://*.mypikpak.com; "
                         "style-src 'self' 'unsafe-inline'; "
                         "font-src 'self' data:; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                         "connect-src 'self' http: https:; frame-ancestors 'self'")

    def _cors(self):
        """只给同源回显 CORS；不再放行 Private-Network —— 那等于允许任意公网页面
        穿透到局域网设备上读写接口。"""
        origin = self._origin()
        if origin and self._same_origin():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_response(self, code, message=None):
        super().send_response(code, message)
        self._security_headers()
        self._cors()

    def _send(self, code, ctype, data):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass   # 客户端提前关闭（刷新页面/取消下载）不算服务端错误

    def _serve_static(self, rel):
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        full = os.path.join(STATIC_DIR, *parts) if parts else ""
        if not parts or not os.path.isfile(full):
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        ext = os.path.splitext(full)[1].lower()
        try:
            stt = os.stat(full)
            mtime, size = int(stt.st_mtime), stt.st_size
            etag = '"%x-%x"' % (mtime, size)
            # 协商缓存：no-cache 仍然每次询问，但命中就 304，省掉几百 KB 的 app.js 重传
            if (self.headers.get("If-None-Match") or "").strip() == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", self.date_time_string(mtime))
            self.send_header("Cache-Control",
                             "no-cache" if ext in (".html", ".js", ".css") else "max-age=86400")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _serve_media(self, rel, force_download=False):
        """只读文件流：支持 HTTP Range/206，浏览器才能在线拖动播放大视频。
        ?dl=1 时强制 attachment，配合前端 <a download> 下载到本机。"""
        from urllib.parse import unquote, quote
        head_only = self.command == "HEAD"
        try:
            full = media_local_path(unquote(rel or ""))
        except ValueError as e:
            self._send(403, "text/plain; charset=utf-8", str(e).encode("utf-8"))
            return
        if not os.path.isfile(full):
            self._send(404, "text/plain; charset=utf-8", b"file not found")
            return
        try:
            size = os.path.getsize(full)
            ctype = MEDIA_TYPES.get(os.path.splitext(full)[1].lower(),
                                    "application/octet-stream")
            start, end, partial = 0, max(size - 1, 0), False
            rng = (self.headers.get("Range") or "").strip()
            if rng.lower().startswith("bytes="):
                m = re.match(r"^(\d*)-(\d*)$", rng[6:])
                if not m or (not m.group(1) and not m.group(2)):
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else max(size - 1, 0)
                else:
                    start = max(0, size - int(m.group(2)))
                    end = max(size - 1, 0)
                end = min(end, max(size - 1, 0))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                partial = True
            length = end - start + 1 if size else 0
            safe_name = re.sub(r'[\r\n"]', "_", os.path.basename(full))
            ascii_name = safe_name.encode("ascii", "ignore").decode() or "file"
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Disposition",
                             "%s; filename=\"%s\"; filename*=UTF-8''%s" %
                             ("attachment" if force_download else "inline",
                              ascii_name, quote(safe_name, safe="")))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if head_only or not length:
                return
            with open(full, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        except Exception as e:
            # 响应头可能已发出，不能再写错误页，仅记录
            print("%s media 异常: %s" % (time.strftime("%F %T"), e))

    def do_GET(self):
        raw = self.path
        q = raw.split("?", 1)[1] if "?" in raw else ""
        path = raw.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
        elif path == "/thumb":
            # v3.40 封面同源代理：只放行 PikPak 自家 CDN 的 https 链接，避免开放代理
            u = (urllib.parse.parse_qs(q).get("u") or [""])[0]
            host = urllib.parse.urlparse(u).netloc.lower()
            if not (u.startswith("https://") and
                    (host == "mypikpak.com" or host.endswith(".mypikpak.com"))):
                self._send(400, "text/plain; charset=utf-8", b"bad thumb url")
                return
            data, ct = _thumb_fetch(u)
            if not data:
                self._send(404, "text/plain; charset=utf-8", b"thumb unavailable")
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
        elif path in ("/favicon.ico", "/favicon.svg"):
            # 有就用文件，没有就静默 204：避免每次开页面都留一条 404 噪声
            name = "favicon" + os.path.splitext(path)[1]
            if os.path.isfile(os.path.join(STATIC_DIR, name)):
                self._serve_static(name)
            else:
                self._send(204, "image/x-icon", b"")
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        elif path == "/media" or path.startswith("/media/"):
            rel = path[7:] if path.startswith("/media/") else ""
            self._serve_media(rel, force_download=("dl=1" in q))
        elif path in ("/collector.bat", "/collector"):
            host = self.headers.get("Host") or CONSOLE_PUBLIC_HOST
            try:
                data = collector_bat("http://%s/" % host)
            except Exception as e:
                self._send(500, "text/plain; charset=utf-8", str(e).encode())
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 'attachment; filename="PikPak-Token-Collector.bat"')
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
        elif path == "/login-collector.ps1":
            host = self.headers.get("Host") or CONSOLE_PUBLIC_HOST
            try:
                data = login_collector_ps("http://%s/" % host)
            except Exception as e:
                self._send(500, "text/plain; charset=utf-8", str(e).encode())
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 'attachment; filename="login-collector.ps1"')
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
        elif path == "/login-collector-setup.bat":
            host = self.headers.get("Host") or CONSOLE_PUBLIC_HOST
            data = login_collector_setup_bat(host)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 'attachment; filename="PikPak-Collector-Setup.bat"')
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
        elif path in ("/login-collector.bat", "/logincollector"):

            host = self.headers.get("Host") or CONSOLE_PUBLIC_HOST
            try:
                data = login_collector_bat("http://%s/" % host)
            except Exception as e:
                self._send(500, "text/plain; charset=utf-8", str(e).encode())
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 'attachment; filename="PikPak-Clean-Login.bat"')
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        self._send(204, "text/plain; charset=utf-8", b"")

    def do_POST(self):
        try:
            # 跨站写操作直接拒绝（CSRF）：清理任务 / 删账号 / 删云端目录都是一次 POST 的事
            if not self._same_origin():
                self._send(403, "application/json; charset=utf-8",
                           json.dumps({"error": "已拒绝跨站请求"}).encode())
                return
            # 只接受 application/json。表单 / text-plain 属于"简单请求"，浏览器不预检就能发；
            # 限定这个类型后，跨域请求必须先过 CORS 预检，而预检已被同源策略挡掉。
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._send(415, "application/json; charset=utf-8",
                           json.dumps({"error": "Content-Type 必须是 application/json"}).encode())
                return
            ln = int(self.headers.get("Content-Length", 0) or 0)
            if ln > 2_000_000:
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"error": "请求体过大"}).encode())
                return
            body = json.loads(self.rfile.read(ln) or b"{}")
            if not isinstance(body, dict):
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"error": "请求体必须是 JSON 对象"}).encode())
                return
            action = body.pop("action", "")
            if action in ("dirs", "start", "cloudrm", "cloudmv", "cloudlink", "speedtest",
                          "verify", "scanpath", "renamebatch", "trashlist", "trashop",
                          "unzipinfo", "unzip", "playinfo") \
                    and not str(body.get("remote") or "").strip():
                self._send(200, "application/json; charset=utf-8",
                           json.dumps({"error": "缺少参数：remote（请选择账号）"}).encode())
                return
            out = json.dumps(api(action, body)).encode()
            self._send(200, "application/json; charset=utf-8", out)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return   # 客户端已断开，别再回包（否则 stderr 打一串 traceback）
        except Exception as e:
            # v3.80：兜底错误也要脱敏 —— PikPak 的 error_description 会把整个
            # RefreshToken 拼进报错，直接回显等于把可用凭证贴到屏幕上。
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"error": mask_secret(str(e))[:300]}).encode())


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    _moved = migrate_legacy_data()
    if _moved:
        print("%s 已从旧路径 %s 迁移数据: %s" % (time.strftime("%F %T"), LEGACY_DATA_DIR, ", ".join(_moved)))
    print("%s 数据目录: %s" % (time.strftime("%F %T"), DATA_DIR))
    # v3.57：启动即识别存储空间（docker inspect 配对失败会在 scheduler 里重试），
    # 保证第一个请求就能看到正确的卷列表与容量
    try:
        _stors = refresh_storages()
        print("%s 存储空间: %s" % (time.strftime("%F %T"),
              ", ".join("%s(%s→%s, 剩余 %s GB)" % (st["name"], st["rclone"], st["console"], st["freeGB"])
                        for st in _stors) or "未识别到，回退内置单卷"))
    except Exception as e:
        print("%s 存储空间识别失败: %s" % (time.strftime("%F %T"), e))
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=cred_keepalive_loop, daemon=True).start()
    print("PikPak 多账号下载器（云梭 CloudShuttle）%s (按任务选择 mihomo 代理 · 凭证保活 · 测速每路 %ss): "
          "http://0.0.0.0:%d  ·  static: %s"
          % (APP_VERSION, SPEEDTEST_SECONDS, CONSOLE_PORT, STATIC_DIR))
    class ConsoleHTTPServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 128   # 默认 5，8 并发时偶发连接被拒；放宽队列
        allow_reuse_address = True
        ssl_ctx = None

        def get_request(self):
            """同端口双协议：首字节 0x16 是 TLS 握手，其余按明文 HTTP 处理。"""
            sock, addr = self.socket.accept()
            ctx = self.ssl_ctx
            if ctx is None:
                return sock, addr
            try:
                # v3.61：静默连接（端口扫描/健康探测）最多等 10s 首字节，
                # 超时走下面 OSError 分支返回，不再挂死整个 accept 循环
                sock.settimeout(10)
                head = sock.recv(1, socket.MSG_PEEK)
            except OSError:
                return sock, addr
            if not head or head[0] != 0x16:
                sock.settimeout(None)      # 明文连接恢复阻塞，避免拖慢慢速客户端
                return sock, addr
            try:
                tls = ctx.wrap_socket(sock, server_side=True)
                tls.settimeout(None)
                return tls, addr
            except (ssl.SSLError, OSError):
                try:
                    sock.close()
                except OSError:
                    pass
                raise        # socketserver 只捕 OSError/SSLError，不会打 traceback

    srv = ConsoleHTTPServer(("0.0.0.0", CONSOLE_PORT), Handler)
    srv.ssl_ctx = tls_make_context()
    if srv.ssl_ctx:
        print("%s HTTPS 已启用（同端口 %d · 自签证书）：在线播放需要它" % (time.strftime("%F %T"), CONSOLE_PORT))
    else:
        print("%s 没有证书（%s），只提供 HTTP；在线播放会被浏览器安全策略拦" %
              (time.strftime("%F %T"), TLS_DIR))
    try:
        import signal
        def _graceful(signum, frame):
            # docker restart 先发 SIGTERM。shutdown() 必须在别的线程调用，
            # 让在飞的 HTTP 请求自然收尾；此处绝不触碰 rclone 任务。
            threading.Thread(target=srv.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)
    except Exception:
        pass
    srv.serve_forever()
