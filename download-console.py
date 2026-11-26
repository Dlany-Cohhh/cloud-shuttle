#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PikPak 多账号下载器（云梭 CloudShuttle）· 批量下载控制台 v3.0 —— metacubexd 设计系统重构（sunset 暗色主题 · Vue 3 · 弹性动效）
   前端在 ./static/（Vue3 + Tailwind v4 + DaisyUI v5 全部自托管，无外网 CDN 依赖）；
   API 与 v2.2 完全兼容，任务/设置数据路径不变"""
import base64, hashlib, html, io, json, os, queue, re, select, shutil, socket, ssl, stat as statmod, subprocess, sys, tarfile, threading, time
from datetime import datetime as _dt
from concurrent.futures import ThreadPoolExecutor
import fnmatch, unicodedata, urllib.error, urllib.parse, urllib.request, uuid, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── 数据目录（v3.18 修复数据分叉）────────────────────────────────────────────
# 历史坑：早期把数据路径写死成 /vol1/@appshare/zcode/console/*.json。容器里这个路径是
# 镜像自带的独立目录（落进容器可写层），宿主机同名目录只通过 /app 绑定挂载进来，
# 两者互不相通 —— 于是任务/历史全写进了容器内，docker rm 重建即全部丢失。
# 现在统一跟随脚本所在目录（容器内 /app == 宿主机 /vol1/@appshare/zcode/console），
# 需要时可再用环境变量 CONSOLE_DATA_DIR 覆盖。
# v4.21 结构重构（STRUCTURE-OPTIMIZATION-GUIDE 4.2）：脚本所在目录唯一出处。
# 全文 6 处 dirname(abspath(__file__)) 收敛引用本常量，行为完全不变。
APP_HOME = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CONSOLE_DATA_DIR") or APP_HOME
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
                   os.path.join(APP_HOME, "accounts_meta.json"))
# 控制台版本：启动横幅与前端页脚共用这一处，避免前端硬编码再次落后
APP_VERSION = "v4.21"


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


# v3.84：网络层故障指纹。这类错误重试有可能好，与账号凭证无关；判定顺序必须
# 在「凭证失效 / 风控」之前，否则会被 captcha / unauthorized 等关键字误伤。
NET_ERROR_KEYS = (
    "connection reset by peer", "connection reset", "reset by peer",
    "remotedisconnected", "connection aborted",
    "connection refused", "broken pipe", "network is unreachable",
    "no route to host", "read timed out", "i/o timeout", "dial tcp",
    "tls handshake timeout", "unexpected eof", "eof occurred",
    "temporarily unavailable", "temporary failure in name resolution",
    "502 bad gateway", "503 service unavailable", "504 gateway timeout",
    # v3.85：后端自己翻译过的中文文案也要能被认出来，否则「网络连接被重置」这段
    # 提示本身不含英文指纹，二次分类时又会掉回 captcha 分支。
    "连接被重置", "连接超时", "网络连接被重置", "网络抖动", "网络不可达",
    "连接被拒绝", "远程主机强迫关闭", "目标主机不可达",
)


def is_net_error(text):
    """是不是网络层故障（连接被重置 / 超时 / DNS / 网关错误），而不是凭证或业务错误。"""
    s = str(text or "").lower()
    return any(k in s for k in NET_ERROR_KEYS)


def trim_err(err, keep=300):
    """错误文案统一压尾：rclone 的报错前缀（error in ListJSON: couldn't list files:
    couldn't get captcha token: ... Post "https://…）能占掉 150 字符以上，真正的原因
    （read tcp …: connection reset by peer）永远在末尾。保留尾部既看得清原因，
    也能让 is_net_error() 认出这是网络抖动。"""
    s = mask_secret(err)
    return s if len(s) <= keep else "…" + s[-keep:]


RELINK_ERROR_KEYS = (
    "refreshed by other", "已被别处刷新", "invalid refresh token", "refresh_token 无效",
    "refresh_token 已失效", "invalid_grant", "invalid_token", "unauthorized",
    # v3.86：PikPak 的 access_token 被服务端作废时回的是 unauthenticated / please re-login，
    # 以前不在表里，界面上就只剩一串英文天书，用户不知道要去重新登录。
    "unauthenticated", "please re-login", "re-login",
    "no username", "账号凭据不完整", "captcha", "需重新登录", "请重新登录",
    "重新采集", "鉴权失效", "凭证已失效", "401",
)


def need_relink(text):
    """这类错误重试多少次都救不回来，只能重新在浏览器登录拿新的 refresh_token。

    v3.84：先排除网络层故障。PikPak 的 captcha 接口（/v1/shield/captcha/init）被
    线路重置时，rclone 的报错原文是「couldn't get captcha token: ... connection
    reset by peer」—— 含 "captcha" 字样但和凭证毫无关系。以前这里会命中 captcha
    关键字，把一次网络抖动翻译成「请重新登录」，用户登录多少次都救不回来。
    """
    low = str(text or "").lower()
    if is_net_error(low):
        return False
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
# v3.93：两个容器都是 host 网络，控制台容器里 127.0.0.1:RC_PORT 直接可达，
# 所以 RC 默认走 HTTP 直连——不再为每次调用 `docker exec` 起一个 rclone 进程。
# HTTP 不可用（非 host 网络部署、rcd 没起来）时自动回退 exec，行为与旧版一致。
RC_TRANSPORT = os.environ.get("RC_TRANSPORT", "http").strip().lower()   # http | exec
RC_HTTP_FALLBACK = os.environ.get("RC_HTTP_FALLBACK", "1") != "0"
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
            "pkProxyMode": "direct",
            # ── v3.83 长文件名安全命名（docs/LONG-FILENAME-DOWNLOAD-DESIGN.md）──
            # 只作用于「新创建」的任务：老任务没有 naming=v2 标记，永远走旧逻辑。
            # 源端永远用云端原始文件名，只有落盘名会被清洗 / 截断 / 加哈希后缀。
            "safeNaming": True,          # 总开关：关掉后新任务恢复 v3.82 及以前的原样命名
            "safeNameMaxBytes": 180,     # 单个落盘文件名的 UTF-8 字节上限（Linux 单组件通常 255）
            "safeNameHashLen": 12,       # 哈希后缀长度，输入 = 账号 + 完整源相对路径
            "safePathMaxBytes": 240,     # 相对路径总字节上限，给 Windows 根路径前缀留余量
            "stuckWatchdogMin": 10,      # v2 任务无进度自动终止阈值（分钟），0 = 关闭
            # ── v4.03 低速自动重连 ──────────────────────────────────────
            # PikPak 的连接会「慢速蠕动」：不报错、不超时、字节还在涨，但只有几十 KB/s，
            # 于是 stuckWatchdog（要求速度为 0）永远不触发，任务挂着跑几十小时。
            # 2026-11-23 实测：同账号同文件，僵死连接 58 KB/s，手动「停→继续」后
            # 立刻 8.9~23.4 MB/s。判定用「窗口内平均速度」，触发后等价于点一次继续。
            "autoReconnect": True,
            "reconnectSlowKBps": 384,    # 窗口均速低于该值视为僵死（实测满速 8~23 MB/s）
            "reconnectWindowSec": 180,   # 判定窗口（秒）；每次重连后窗口翻倍，最多 1 小时
            "reconnectMax": 5,           # 单个任务最多自动重连几次，0 = 关闭
            "fileRetryMax": 3,           # 单文件瞬时/未知错误重试次数（退避 2s / 8s / 30s）
            # ── v3.94 自动测速调优（docs/AUTOTUNE-DESIGN.md）──
            # 开启后：任务启动时自动套用「实测最优档」；测速把「并发×流数」逐档试一遍，
            # 用目标目录真实落盘增量（MB/s）判优劣，不信任 rclone core/stats 自报速度。
            "autoTune": True,            # 总开关
            "autoTuneSeconds": 45,       # 每档测速窗口（秒）
            "autoTuneSettle": 30,        # 每档启动后「等落盘」的耐心（秒，真正的计分从首次增长开始）
            # ── v3.95 龟速让位（docs/AUTOTUNE-DESIGN.md 第 5 节）──
            # 单个文件连续低于 50 KB/s 满 stallSeconds 秒就让位，把并发槽腾给还在动的文件；
            # 抬到第二轮后只占 1 个槽，不再出现「2 个龟速文件吃掉一半并发」。
            "stallSeconds": 90,          # 30~600 秒
            # ── v4.04 下载后自动转封装（MPEG-TS → MP4）──────────────────
            # PikPak 上大量资源是 HLS/m3u8 转出来的 MPEG-TS 流，源端连扩展名都没有，
            # 落盘后播放器认不出、用户以为「下载坏了」或「不能转 mp4」。
            # 这类 TS 的视频是 H.264、音频是 AAC（MP4 原生组合），`-c copy` 无损转封装
            # 即可，实测 1.90 GB / 34 秒，体积还小 8%（去掉了 TS 的 188 字节包开销）。
            "autoRemuxMp4": True,        # 下载完成后自动探测容器并转封装
            "remuxKeepSource": True,     # 保留原 TS 源文件（关掉 = 转成功后删源省一半空间）
            "remuxTimeoutSec": 1800,     # 单文件转封装超时（秒），超时按失败处理并删半成品
            # ── v4.10 PushPlus 微信通知 ──────────────────────────────────
            # 任务「下载成功 / 失败 / 故障停止（带错误信息的 stopped）」时推一条微信。
            # 巡检式实现（见 notify_scan），不依赖某条具体代码路径触发，重启不补发历史。
            "pushplusOn": False,         # 总开关（默认关，避免没填 token 时每 5 秒报一次错）
            "pushplusToken": "",         # PushPlus 的 32 位 token
            # ── v4.13 失败任务自动重试 ──────────────────────────────────
            # 任务进入 error 后等 autoRetryMin 分钟自动点一次「继续」，最多 autoRetryMax 次。
            # 只救「能靠重试救回来」的失败（网络瞬断 / 服务端 5xx / 低速僵死），
            # 账号失效、人机验证、额度用尽、磁盘保护这类重试只会白等，直接跳过（见 AUTO_RETRY_SKIP_RE）。
            "autoRetryOn": False,        # 总开关（默认关：失败原因不明时自动重跑，可能把风控刷得更狠）
            "autoRetryMin": 10,          # 失败后等多少分钟再重试（1~1440）
            "autoRetryMax": 3}           # 同一任务最多自动重试几次（0~10，0 = 只等不重试）

# v4.03：savesettings 允许写入的字段白名单。
# 之前这个元组直接内联在接口里，新增设置项忘了加进来就会「前端能改、保存无效」——
# 低速自动重连第一版就漏了，所以提出来单独放，并由 tests/test_reconnect.py 守住。
SETTINGS_WRITABLE = (
    "maxConcurrent", "diskMinGB", "diskQuotaGB", "transfers", "checkers",
    "multiThreadStreams", "multiThreadCutoffMB", "retries",
    "autoTune", "autoTuneSeconds", "autoTuneSettle", "stallSeconds",
    "autoStart", "autoPause", "orderBy", "bwLimitOn", "bwLimit",
    "timeout", "idleTimeout", "createEmptySrcDirs", "logLevel", "logFile",
    "transfersClearedAt", "nasHomeUrl", "proxyUrl", "netMode",
    "safeNaming", "safeNameMaxBytes", "safeNameHashLen",
    "safePathMaxBytes", "stuckWatchdogMin", "fileRetryMax",
    "autoReconnect", "reconnectSlowKBps", "reconnectWindowSec", "reconnectMax",
    "autoRemuxMp4", "remuxKeepSource", "remuxTimeoutSec",
    "pushplusOn", "pushplusToken",
    "autoRetryOn", "autoRetryMin", "autoRetryMax",
)

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

def _rc_auth_header():
    tok = base64.b64encode(("%s:%s" % (RCLONE_RC_USER, RCLONE_RC_PASS)).encode()).decode()
    return "Basic " + tok


def _rc_http_call(port, method, kv, timeout):
    """v3.93：HTTP 直连 rcd。rclone 的业务错误是「非 200 + JSON 体」，这里照样解析，
    与 exec 路径返回的 stdout 等价，_strict 才能继续生效。"""
    body = json.dumps(kv or {}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/%s" % (port, method), data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": _rc_auth_header()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:          # job not found 等：500 + JSON
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
    try:
        return json.loads(raw or "{}")
    except Exception:
        raise RuntimeError("rc %s 返回无法解析：%s" % (method, (raw or "")[:200]))


def _rc_exec_call(prefix, method, kv, timeout):
    args = prefix + [method] + ["%s=%s" % (k, v) for k, v in (kv or {}).items()]
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def rc(method, _timeout=120, _strict=False, _proxy=False, **kv):
    """_timeout 供持锁路径（如 jobs 轮询里的状态查询）设短超时，避免 rclone 卡住拖死接口。
    _strict=True 时额外检查返回体里的 error 字段并抛异常：rclone RC 出错是
    「HTTP 200 + {"error": ...}」，不抛异常的话调用方会把错误 dict 当正常结果继续处理
    （历史 bug：job/status 返回 job not found 时任务永远卡在 running）。默认 False，
    以保持其余 action 的既有行为。"""
    base = RC_PROXY if _proxy else RC
    port = PROXY_RC_PORT if _proxy else RC_PORT
    d = None
    http_err = None
    if RC_TRANSPORT != "exec":
        try:
            d = _rc_http_call(port, method, kv, _timeout)
        except Exception as he:
            http_err = he
            d = None
    if d is None:
        if RC_TRANSPORT != "exec" and not RC_HTTP_FALLBACK:
            raise RuntimeError("rc %s 本地控制面 %d 无响应（%.0fs 超时）：%s"
                               % (method, port, _timeout, http_err))
        try:
            p = _rc_exec_call(base, method, kv, _timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("rc %s 本地 rclone 控制面 %.0fs 无响应"
                               "（不是源端慢；rclone 可能繁忙或容器异常）" % (method, _timeout))
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
        v = rc("core/version", _timeout=6, _proxy=True)
        return bool(isinstance(v, dict) and v.get("version"))
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
        # v3.85：原来的 300 字符上限会把 rclone 的 captcha 报错尾巴（真正的
        # "connection reset by peer"）切掉，网络抖动又变成凭证问题。保留完整原因。
        raise RuntimeError(str(r.get("error"))[:600])
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
            # v3.85：这里必须保留足够长的原文。rclone 的报错前缀
            # 「error in ListJSON: couldn't list files: couldn't get captcha token:
            #  failed to retrieve captcha token from api: Post "https://…」本身就有
            # 150 字符左右，截到 150 会把末尾的 connection reset by peer 整段切掉，
            # is_net_error() 于是看不到网络指纹，网络抖动又被判成「需重新登录」。
            err = str(e)[:600]
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
    # v3.84：PikPak 的 shield/captcha 接口偶发被线路 RST（实测 NAS 直连 43.160.170.231
    # 时不定期 reset），一次抖动就把任务判「预检失败」，还会被翻译成「请重新登录」。
    # 这里对网络层错误再做有限重试（直连/代理交替），重试耗尽才把带「网络问题」标记
    # 的错误返回，让用户知道该重试而不是去重新登录。
    if result[1] and is_net_error(result[1]):
        for i in range(2):
            time.sleep(0.8 * (i + 1))
            use_proxy = (not proxy) if i == 0 else bool(proxy)   # 第一次换条路，第二次回到原路
            alt = _once(use_proxy)
            if not alt[1]:
                return alt
            result = alt
        if result[1] and is_net_error(result[1]):
            # v3.85：只把原文尾部留给界面（rclone 前缀太长，全量展示会撑爆任务卡），
            # 但尾部正好是 "read tcp …: connection reset by peer" 这段关键原因。
            return result[0], ("网络连接被重置/超时（已自动重试 2 次，这不是凭证问题；"
                              "稍等再点「继续」，或给该任务打开代理）：%s"
                              % trim_err(result[1], 220)), result[2]
    err = trim_err(result[1], 300)
    if need_relink(err):
        # v3.86：凭证类错误在预检阶段就翻译成可操作的话。以前直接把 rclone 原文
        # 「error in ListJSON: couldn't list files: Error "unauthenticated" (16):
        #  Unauthenticated, please re-login」丢给界面，用户不知道该点哪里。
        return result[0], ("账号凭证已失效（%s）。这不是网络问题，重试任务没用："
                          "请到账号页点该账号的「重新登录」，在官方登录页过完安全验证后"
                          "重新采集 Token，再点任务的「继续」" % err), result[2]
    return result[0], err, result[2]


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
    # v3.83 安全命名：字节上限 / 哈希长度 / 看门狗 / 单文件重试都钳到安全区间
    num("safeNameMaxBytes", 64, 240)
    num("safeNameHashLen", 6, 24)
    num("safePathMaxBytes", 120, 1024)
    num("stuckWatchdogMin", 0, 1440)
    # v4.03 低速自动重连：阈值/窗口/次数都钳住，避免 0 秒窗口把每次启动都当成僵死
    num("reconnectSlowKBps", 32, 8192)
    num("reconnectWindowSec", 60, 3600)
    num("reconnectMax", 0, 20)
    d["autoReconnect"] = _truthy(d.get("autoReconnect", True))
    num("fileRetryMax", 0, 10)
    # v4.04 转封装：超时钳到 60 秒~6 小时，避免误填 0 让每个文件秒失败
    num("remuxTimeoutSec", 60, 21600)
    d["autoRemuxMp4"] = _truthy(d.get("autoRemuxMp4", True))
    d["remuxKeepSource"] = _truthy(d.get("remuxKeepSource", True))
    # v4.10 PushPlus 通知：token 只允许字母数字，避免把整段网址贴进来后每 5 秒报一次错
    d["pushplusOn"] = _truthy(d.get("pushplusOn", False))
    _pt = str(d.get("pushplusToken") or "").strip()
    d["pushplusToken"] = _pt if NOTIFY_TOKEN_RE.match(_pt) else ""
    # v4.13 自动重试：等待时间 1~1440 分钟（1 天），次数 0~10（0 = 只记录不重试）
    d["autoRetryOn"] = _truthy(d.get("autoRetryOn", False))
    num("autoRetryMin", 1, 1440)
    num("autoRetryMax", 0, 10)
    # v3.94 自动测速调优：窗口与稳定期钳到合理区间，避免误填 0 秒或 1 小时
    num("autoTuneSeconds", 15, 300)
    num("autoTuneSettle", 5, 120)
    num("stallSeconds", 30, 600)
    d["autoTune"] = _truthy(d.get("autoTune", True))
    d["safeNaming"] = _truthy(d.get("safeNaming", True))
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


# ── v4.10 PushPlus 微信通知（下载成功 / 失败 / 故障停止）────────────────────
# 为什么不在这几个状态切换点各插一句调用：任务进入终态的地方有六七处
# （rclone job、v2 目录 worker、转封装、跨账号转移、解压、卡住看门狗、低速重连用尽），
# 挨个插一遍，哪天新增一条路径忘了插就静默失效，而且测试也没法覆盖。
# 这里改成「巡检 + 状态跃迁」：每 5 秒读一次 jobs.json，看有没有任务刚进入终态，
# 跃迁一次就发一次，去重键落在 notify-state.json（重启不补发历史）。
PUSHPLUS_API = os.environ.get("PUSHPLUS_API") or "https://www.pushplus.plus/send"
NOTIFY_STATE_FILE = os.path.join(DATA_DIR, "notify-state.json")
NOTIFY_LOCK = threading.Lock()
NOTIFY_SCAN_SEC = 5           # 巡检间隔（秒）
NOTIFY_SEND_GAP = 1.0         # 一批里多条消息之间的间隔，别被 PushPlus 限流
NOTIFY_SEEN_MAX = 400         # 去重表保留条数
# v4.15：同一任务、同一终态、同一错因的最短重发间隔。
# v4.13 的去重键里带 bytes，本意是「点继续后再失败、字节变了就该再通知」；
# 但任务进入 error 后，残留的在飞传输仍在推进字节，键每 5 秒变一次，
# 于是一次故障被当成几十次新失败连发（实测 22:30 起每 6 秒一条，PushPlus 直接限流报
# 「服务端验证错误」）。改为键里去掉 bytes，改用「同键 30 分钟内只发一次」。
NOTIFY_REPEAT_SEC = 1800      # v4.19：只用于「同一个失败错因反复失败」，成功通知不受它约束
NOTIFY_MAX_PER_SCAN = 3       # 一轮巡检最多发几条，防雪崩
NOTIFY_STATE_VER = 2          # v4.19 去重表结构：按任务记「上次见到的状态 + 错因」
NOTIFY_BACKOFF_BASE = 60      # 发送失败后的退避起点（秒）
NOTIFY_BACKOFF_MAX = 300      # 退避上限
NOTIFY_BACKOFF = {"until": 0.0, "fails": 0}   # 退避状态（内存即可，重启清零不算错）
NOTIFY_TRY_MAX = 6            # v4.20：同一条事件最多尝试发送几次（60→300 秒退避下约 20 分钟）
NOTIFY_TRY = {}               # 事件 → 已尝试次数（内存即可，重启清零）
NOTIFY_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
# v4.11：容器/守护进程重启会把在传的任务标成「控制台重启过，下载已中断…」「守护进程重启过…」的 stopped，
# 那不是任务故障，是运维动作 —— 照 fail 推的话，每次升级/重启都给你发一串「任务失败」。
NOTIFY_RESTART_MARK = ("控制台重启过", "守护进程重启过", "已暂停")


def pushplus_token_ok(t):
    """PushPlus 的 token 是 32 位十六进制/字母数字；这里放 16~64 位，挡住误填网址。"""
    return bool(NOTIFY_TOKEN_RE.match(str(t or "").strip()))


def notify_event(job):
    """任务该不该发通知。success = 下完；fail = 失败，或「带错误信息的停止」（额度用尽 / 看门狗 / 重连用尽）。
    用户自己点「停止」的 stopped 没有 error，不发；重启中断 / 暂停收尾的 stopped 也不发
    （「已暂停：还有 N 个文件未完成」是暂停的正常收尾，不是故障）。"""
    j = job or {}
    st = str(j.get("state") or "")
    err = str(j.get("error") or "").strip()
    if st == "done":
        return "success"
    if st == "error":
        return "fail"
    if st == "stopped" and err:
        if err.startswith(NOTIFY_RESTART_MARK):   # str.startswith 支持元组前缀
            return None
        return "fail"
    return None


def notify_sig(job):
    """v4.19：只取错因摘要，配合「状态」一起判断有没有变化。
    不再把 id|state|error 拼成一次性键 —— 那种键配「30 分钟冷却」会变成心跳：
    任务一直停在 done，冷却一过就当成新事件再推一遍（实测一天推了几十轮）；
    错因被清空（比如点详情后重试/清错）也会换键再推一次。"""
    return str((job or {}).get("error") or "").strip()[:60]


def _notify_state_load():
    """读去重表，返回 (jobs_map, need_seed)。
    v2 结构：{"v": 2, "jobs": {"<id>": {"st": 状态, "sig": 错因, "ts": 通知时间}}}
    老格式（v4.15 的 "id|state|err" → ts 平表）迁移时 need_seed=True：
    只把当前状态登记一遍、不补发，避免升级瞬间又炸一串。"""
    raw = _read_json(NOTIFY_STATE_FILE, None)
    if isinstance(raw, dict) and raw.get("v") == NOTIFY_STATE_VER and isinstance(raw.get("jobs"), dict):
        return {str(k): dict(v) for k, v in raw["jobs"].items() if isinstance(v, dict)}, False
    jobs = {}
    if isinstance(raw, dict):                       # 老平表 → 迁移
        for k, v in raw.items():
            jid = str(k).split("|", 1)[0]
            if jid and jid not in jobs:
                jobs[jid] = {"st": None, "sig": None, "ts": int(v or 0)}
        return jobs, True
    return {}, True                                  # 首次启用


def _notify_state_save(jobs, live_ids=None):
    """落盘。只清理「任务已经不在了」的条目 —— 仍在列表里的任务绝不能丢，
    一丢下次巡检就从「没有记忆」变成「又是新事件」，等于把通知重新发一遍。"""
    if live_ids is not None:
        jobs = {k: v for k, v in jobs.items() if k in live_ids}
    if len(jobs) > NOTIFY_SEEN_MAX:
        keep = sorted(jobs.items(), key=lambda kv: int(kv[1].get("ts") or 0), reverse=True)
        jobs = dict(keep[:NOTIFY_SEEN_MAX])
    _write_json(NOTIFY_STATE_FILE, {"v": NOTIFY_STATE_VER, "jobs": jobs}, quiet=True)



def _h(s, n=200):
    """转义后截断，PushPlus 的 content 是 HTML，任务名里带 < & 会破版。"""
    return html.escape(str(s or ""))[:n]


def _human_bytes(n):
    try:
        n = float(n or 0)
    except Exception:
        n = 0
    if n >= 1024 ** 3:
        return "%.2f GB" % (n / 1073741824.0)
    if n >= 1024 ** 2:
        return "%.1f MB" % (n / 1048576.0)
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % int(n)


def notify_title(job, kind):
    j = job or {}
    leaf = j.get("safe_leaf") or j.get("leaf") or j.get("name") or ""
    if not leaf:
        leaf = (str(j.get("subpath") or "").strip("/").rsplit("/", 1)[-1]
                or str(j.get("dest") or "").rsplit(":", 1)[-1].strip("/").rsplit("/", 1)[-1])
    tag = "下载完成" if kind == "success" else "任务失败"
    return ("云梭｜%s：%s" % (tag, leaf))[:95]


def notify_content(job, kind, when):
    j = job or {}
    name = j.get("safe_leaf") or j.get("leaf") or j.get("name") or "(未命名)"
    src = "%s:%s" % (j.get("remote") or "", j.get("subpath") or "")
    if j.get("src"):
        src = str(j.get("src"))
    done, total = int(j.get("filesDone") or 0), int(j.get("filesTotal") or 0)
    failed = int(j.get("filesFailed") or 0)
    # v4.11：只有目录任务（安全命名 v2）才有文件级计数，rclone 老任务这些字段缺省，
    # 以前会显示「文件：0/0 个」—— 明显是错的，干脆不提这一行。
    files = ""
    if total:
        files = "%d/%d 个" % (done, total)
        if failed:
            files += "（失败 %d）" % failed
    elif done:
        files = "%d 个" % done
        if failed:
            files += "（失败 %d）" % failed
    elif j.get("single_file") and kind == "success":
        files = "1 个"
    rows = [("任务", name),
            ("来源", src),
            ("落盘", j.get("dest") or "-")]
    if files:
        rows.append(("文件", files))
    rows += [("大小", "%s / %s" % (_human_bytes(j.get("bytes")), _human_bytes(j.get("total")))),
             ("结束", when)]
    if kind == "fail" and str(j.get("error") or "").strip():
        rows.append(("错误", str(j.get("error"))[:300]))
    body = "".join("<p><b>%s</b>：%s</p>" % (_h(k, 20), _h(v, 400)) for k, v in rows)
    head = "<p style='font-size:15px'><b>%s</b></p>" % _h(notify_title(j, kind), 100)
    tip = ("<p style='color:#888;font-size:12px'>云梭 CloudShuttle · 任务 %s</p>" % _h(j.get("id")))
    return head + body + tip


def pushplus_send(token, title, content, timeout=10):
    """调 PushPlus 发一条微信消息。返回它自己的 JSON（code=200 才算成功）。"""
    import urllib.request
    payload = json.dumps({"token": str(token).strip(), "title": str(title)[:100],
                          "content": str(content), "template": "html"}, ensure_ascii=False)
    req = urllib.request.Request(PUSHPLUS_API, data=payload.encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 常量地址，非用户输入
            body = r.read().decode("utf-8", "replace")
    except Exception as e:
        return {"code": -1, "msg": "请求失败：%s" % str(e)[:150]}
    try:
        d = json.loads(body)
    except Exception:
        return {"code": -1, "msg": "无法解析返回：%s" % body[:150]}
    return d if isinstance(d, dict) else {"code": -1, "msg": "返回格式异常"}


def notify_scan(send=None, now=None):
    """巡检一次：把「刚进入终态」的任务发到微信。返回 {enabled, sent, skipped, errors, seeded}。
    send 可注入（测试用）；now 可注入（测试用）。

    v4.19 改为边沿触发：按任务记住「上次见到的状态 + 错因」，只有真变了才发。
    v4.15 的「同键 30 分钟冷却」是平事件触发，任务停在 done 时等于每半小时重播一次，
    错因被清空也会换键再发 —— 这就是用户看到的「一直推送」。"""
    s = load_settings()
    enabled = _truthy(s.get("pushplusOn"))
    token = str(s.get("pushplusToken") or "").strip()
    out = {"enabled": enabled, "sent": 0, "skipped": 0, "errors": [], "seeded": False,
           "backoff": 0}
    if not enabled:
        return out
    if not pushplus_token_ok(token):
        out["errors"].append("PushPlus Token 未填写或格式不对（16~64 位字母数字）")
        return out
    ts_now = time.time() if now is None else float(now)
    if ts_now < float(NOTIFY_BACKOFF.get("until") or 0):
        out["backoff"] = int(float(NOTIFY_BACKOFF["until"]) - ts_now)
        return out
    with LOCK:
        jobs = load_jobs()
    with NOTIFY_LOCK:
        state, seed = _notify_state_load()
        out["seeded"] = seed
        ts = int(ts_now)
        if seed:
            # 首次启用 / 从老格式迁移：只登记现状，不把历史任务补发一遍
            for j in jobs:
                jid = str((j or {}).get("id") or "")
                if jid:
                    state[jid] = {"st": str(j.get("state") or ""), "sig": notify_sig(j), "ts": ts}
            _notify_state_save(state, {str((j or {}).get("id") or "") for j in jobs})
            return out
        live = {str((j or {}).get("id") or "") for j in jobs}
        changed = False
        for j in jobs:
            jid = str((j or {}).get("id") or "")
            if not jid:
                continue
            st = str(j.get("state") or "")
            sig = notify_sig(j)
            prev = state.get(jid) or {}
            if prev.get("st") == st and (prev.get("sig") or "") == sig:
                continue                     # 状态没动：不发、不写，天然安静
            kind = notify_event(j)
            nts = int(prev.get("nts") or 0)          # 上次真正发出去的时间（不是观察时间）
            if not kind:
                state[jid] = {"st": st, "sig": sig, "ts": ts, "nts": nts}
                changed = True
                continue
            if kind == "success" and prev.get("st") == "done":
                # 完成通知只认「非 done → done」这一次跃迁；done 之后错因被清空/改写
                # 只是文案变化，不许再当成新的完成事件重推一遍
                state[jid] = {"st": st, "sig": sig, "ts": ts, "nts": nts}
                changed = True
                continue
            # 失败类兜底：同一任务 30 分钟内最多一条「失败」，自动重试/看门狗来回跑时不刷屏
            if kind == "fail" and nts and ts - nts < NOTIFY_REPEAT_SEC:
                state[jid] = {"st": st, "sig": sig, "ts": ts, "nts": nts}
                changed = True
                out["skipped"] += 1
                continue
            if out["sent"] >= NOTIFY_MAX_PER_SCAN:
                out["skipped"] += 1
                continue                     # 不登记，下一轮接着发（这一条不能丢）
            try:
                res = (send or pushplus_send)(token, notify_title(j, kind),
                                              notify_content(j, kind, time.strftime("%Y-%m-%d %H:%M")))
                code = int((res or {}).get("code") or 0) if isinstance(res, dict) else -1
                if code == 200:
                    out["sent"] += 1
                    state[jid] = {"st": st, "sig": sig, "ts": ts, "nts": ts}
                    changed = True
                    NOTIFY_TRY.pop("%s|%s|%s" % (jid, st, sig), None)
                    NOTIFY_BACKOFF["fails"] = 0
                    NOTIFY_BACKOFF["until"] = 0.0
                else:
                    msg = (res or {}).get("msg") if isinstance(res, dict) else str(res)
                    out["errors"].append("%s：%s" % (notify_title(j, kind)[:40], str(msg)[:80]))
                    # 失败不登记状态，退避后再试；但同一条事件最多试 NOTIFY_TRY_MAX 次
                    # （v4.20：实测 PushPlus 侧 TLS/限流挂掉时，一条通知会被重试二十多分钟，
                    #  最后突然补发一条「下载完成」，用户看到的还是莫名其妙的推送）
                    tkey = "%s|%s|%s" % (jid, st, sig)
                    tries = int(NOTIFY_TRY.get(tkey) or 0) + 1
                    NOTIFY_TRY[tkey] = tries
                    if tries >= NOTIFY_TRY_MAX:
                        state[jid] = {"st": st, "sig": sig, "ts": ts, "nts": ts}
                        changed = True
                        out["errors"].append("已放弃重试（%d 次仍失败，不再补发）" % tries)
                        NOTIFY_TRY.pop(tkey, None)
                    fails = int(NOTIFY_BACKOFF.get("fails") or 0) + 1
                    NOTIFY_BACKOFF["fails"] = fails
                    wait = min(NOTIFY_BACKOFF_MAX, NOTIFY_BACKOFF_BASE * (2 ** (fails - 1)))
                    NOTIFY_BACKOFF["until"] = ts_now + wait
                    out["backoff"] = wait
                    break
            except Exception as e:
                out["errors"].append("发送异常：%s" % str(e)[:100])
                tkey = "%s|%s|%s" % (jid, st, sig)
                tries = int(NOTIFY_TRY.get(tkey) or 0) + 1
                NOTIFY_TRY[tkey] = tries
                if tries >= NOTIFY_TRY_MAX:
                    state[jid] = {"st": st, "sig": sig, "ts": ts, "nts": ts}
                    changed = True
                    out["errors"].append("已放弃重试（%d 次异常，不再补发）" % tries)
                    NOTIFY_TRY.pop(tkey, None)
                fails = int(NOTIFY_BACKOFF.get("fails") or 0) + 1
                NOTIFY_BACKOFF["fails"] = fails
                wait = min(NOTIFY_BACKOFF_MAX, NOTIFY_BACKOFF_BASE * (2 ** (fails - 1)))
                NOTIFY_BACKOFF["until"] = ts_now + wait
                out["backoff"] = wait
                break
            time.sleep(NOTIFY_SEND_GAP)
        if changed:
            _notify_state_save(state, live)
    return out


def notify_loop():
    """常驻巡检线程。任何异常都吞掉并记日志 —— 通知失败不能影响下载。"""
    while True:
        try:
            r = notify_scan()
            if r.get("errors"):
                print("%s 通知失败: %s" % (time.strftime("%F %T"), "；".join(r["errors"])[:200]), flush=True)
            elif r.get("sent"):
                print("%s 已发送 %d 条任务通知" % (time.strftime("%F %T"), r["sent"]), flush=True)
        except Exception as e:
            print("%s 通知巡检异常: %s" % (time.strftime("%F %T"), str(e)[:200]), flush=True)
        time.sleep(NOTIFY_SCAN_SEC)


# ── v4.13 失败任务自动重试（等 N 分钟再点一次「继续」）─────────────────────
# 为什么走本地 HTTP API 而不是直接调 launch_job：
# 「继续」那套流程里有一堆必须复用的前置判断 —— 旧工作线程是否已退出、目标卷磁盘保护、
# 云端源目录预检（预检要联网列目录，几秒到几十秒）、并发槽位、queued 还是 running。
# 在这里复制一份等于把两套逻辑慢慢跑偏；直接 POST 给本机 /api 就一次都不漏。
# 代价是必须放开锁再发请求（api 处理时自己拿 LOCK，持锁调用会死锁）。
AUTO_RETRY_SCAN_SEC = 30          # 巡检间隔（秒）；与等待分钟数无关，30 秒粒度足够
AUTO_RETRY_SKIP_RE = re.compile(r"额度|流量已达|存储空间保护|空间不足|磁盘")
# v4.14：账号失效 / 需重新登录 / 人机验证 不再一律跳过 —— 先修账号再继续（见 AUTO_RETRY_CRED_RE）。
# 还留在跳过名单里的只剩「等多久都没用」的两类：额度用尽（要等第二天）、磁盘保护（要你先腾空间）。
AUTO_RETRY_CRED_RE = re.compile(
    r"重新登录|登录失效|captcha|人机验证|unauthorized|401|refresh_token|token 失效|凭证")
AUTO_RETRY_WORKER_BUSY = "旧工作线程还没退出"   # 这不是失败，是还没退干净：不计次数，下轮再看


def _api_post(payload, timeout=150):
    """给本机控制台 API 发一个 POST（不带 Origin → 走「非浏览器客户端」通道）。"""
    import urllib.request
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d/api" % CONSOLE_PORT, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 本机回环
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception as e:
        return {"error": "内部请求失败：%s" % str(e)[:120]}


def auto_retry_scan(now=None, resume=None, repair=None):
    """巡检一次：把「失败已满 autoRetryMin 分钟、且还没试满 autoRetryMax 次」的任务重新拉起来。
    resume(job_id) 可注入（测试用），默认实现回环调本机 /api；返回 (ok, msg)。
    repair(账号名) 可注入（测试用），默认 pikpak_verify —— 账号类失败先修账号再继续。"""
    s = load_settings()
    on = _truthy(s.get("autoRetryOn"))
    wait = int(s.get("autoRetryMin") or 10) * 60
    most = int(s.get("autoRetryMax") or 0)
    out = {"on": on, "retried": 0, "skipped": 0, "cleared": 0, "errors": [], "waiting": []}
    if not on or most <= 0:
        return out
    ts = int(now if now is not None else time.time())

    # 第一段（持锁）：找出候选 + 给第一次看到的失败打时间戳。不发网络请求。
    cands, dirty = [], False
    with LOCK:
        jobs = load_jobs()
        for j in jobs:
            st = j.get("state")
            if st == "done" and (j.get("_arTries") or j.get("_arErrAt")):
                # 成功一次就把次数清零，以后这批任务再失败又是完整的 N 次额度
                j["_arTries"], j["_arErrAt"] = 0, None
                out["cleared"] += 1
                dirty = True
                continue
            if st != "error":
                continue
            err = str(j.get("error") or "").strip()
            if err and AUTO_RETRY_SKIP_RE.search(err):
                out["skipped"] += 1
                continue
            tries = int(j.get("_arTries") or 0)
            if tries >= most:
                out["skipped"] += 1
                continue
            at = int(j.get("_arErrAt") or 0)
            if not at:
                # 第一次看到这个失败：从现在开始计时（重启后也一样，不会立刻重跑）
                j["_arErrAt"] = ts
                dirty = True
                out["waiting"].append(j.get("id"))
                continue
            left = at + wait - ts
            if left > 0:
                out["waiting"].append(j.get("id"))
                continue
            cands.append((j.get("id"), err[:120], str(j.get("remote") or "")))
        if dirty:
            save_jobs(jobs)

    # 第二段（不持锁）：账号类失败先修账号，再点「继续」。api 自己拿锁、自己做预检。
    for jid, err, remote in cands:
        cred = bool(err and AUTO_RETRY_CRED_RE.search(err))
        if cred and remote and remote != "URL":     # URL 直链任务没有账号可修，直接重试
            # v4.14：账号失效 / 需要重新登录 / 人机验证 —— pikpak_verify 会自动换 device_id 重试
            # （PikPak 的 captcha 风控常常换一个 device_id 就过），凭证被轮换的情况也会顺手同步镜像。
            # 修不好就记一次尝试、继续等下一轮，别每 30 秒去撞一次验证码。
            fixed, why = True, ""
            try:
                fixed, why = (repair or pikpak_verify)(remote)
            except Exception as e:
                fixed, why = False, str(e)[:100]
            if not fixed:
                out["credFail"] = int(out.get("credFail") or 0) + 1
                out["errors"].append("任务 %s 所属账号「%s」仍未恢复：%s" % (jid, remote, str(why)[:80]))
                with LOCK:
                    jobs = load_jobs()
                    for j in jobs:
                        if j.get("id") == jid:
                            j["_arTries"] = int(j.get("_arTries") or 0) + 1
                            j["_arErrAt"] = None
                    save_jobs(jobs)
                continue
            print("%s 任务 %s 的账号「%s」已修复（%s），接着重试" % (
                time.strftime("%F %T"), jid, remote, str(why or "连通")[:60]), flush=True)
        ok, msg = True, ""
        try:
            r = (resume or (lambda i: _api_post({"action": "resume", "id": i, "_auto": True})))(jid)
            if isinstance(r, dict):                     # 默认实现返回 API 的 JSON
                ok = not r.get("error")
                msg = str(r.get("error") or "")
            elif isinstance(r, tuple):                  # 注入实现返回 (ok, msg)
                ok, msg = r
        except Exception as e:
            ok, msg = False, str(e)[:120]
        if ok:
            out["retried"] += 1
            print("%s 自动重试任务 %s（上次失败：%s）" % (time.strftime("%F %T"), jid, err[:60]), flush=True)
            with LOCK:
                jobs = load_jobs()
                for j in jobs:
                    if j.get("id") == jid and j.get("state") != "done":
                        j["_arTries"] = int(j.get("_arTries") or 0) + 1
                        j["_arErrAt"] = None    # 下次失败会重新计时
                save_jobs(jobs)
        elif AUTO_RETRY_WORKER_BUSY in msg:
            # 旧线程还没退干净：不计次数，30 秒后再来一次
            with LOCK:
                jobs = load_jobs()
                for j in jobs:
                    if j.get("id") == jid:
                        j["_arErrAt"] = ts
                save_jobs(jobs)
            out["errors"].append("任务 %s 仍在收尾，稍后再试" % jid)
        else:
            # 点不动（磁盘保护 / 预检源空了）：算一次尝试，避免每 30 秒撞一次
            out["errors"].append("任务 %s 重试失败：%s" % (jid, msg[:100]))
            with LOCK:
                jobs = load_jobs()
                for j in jobs:
                    if j.get("id") == jid:
                        j["_arTries"] = int(j.get("_arTries") or 0) + 1
                        j["_arErrAt"] = None
                save_jobs(jobs)
    return out


def auto_retry_loop():
    """常驻巡检线程。异常一律吞掉 —— 自动重试出问题也不能把下载拖死。"""
    while True:
        try:
            r = auto_retry_scan()
            if r.get("retried"):
                print("%s 自动重试：已重新拉起 %d 个任务" % (time.strftime("%F %T"), r["retried"]), flush=True)
            for e in (r.get("errors") or [])[:3]:
                print("%s 自动重试告警: %s" % (time.strftime("%F %T"), e[:160]), flush=True)
        except Exception as e:
            print("%s 自动重试巡检异常: %s" % (time.strftime("%F %T"), str(e)[:200]), flush=True)
        time.sleep(AUTO_RETRY_SCAN_SEC)


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


# ────────────────────────────────────────────────────────────────────────────
# v3.83 长文件名安全命名（docs/LONG-FILENAME-DOWNLOAD-DESIGN.md）
#
# 核心原则：源端永远用云端原始文件名，目标端永远用安全落盘名。
#   · 只有「新任务」会带 naming=v2 标记，老任务完全走旧逻辑，行为不变；
#   · 正常文件名原样保留，只有被清洗过 / 超长的名字才追加 ~哈希 后缀；
#   · 哈希输入 = 账号 + 完整源相对路径，同一源文件无论何时重算都得到同一个落盘名；
#   · 原名 ↔ 落盘名的对应关系写进每卷的 .cloudshuttle-name-map.json，可追溯。
# ────────────────────────────────────────────────────────────────────────────
WIN_RESERVED = set('<>:"/\\|?*')
WIN_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def truncate_utf8(s, limit_bytes):
    """按 UTF-8 字节截断，绝不切出半个汉字（按字符截断对中文基本无效）。"""
    b = str(s or "").encode("utf-8")
    if len(b) <= limit_bytes:
        return str(s or "")
    return b[:max(0, int(limit_bytes))].decode("utf-8", "ignore")


def clean_segment(s):
    """单个路径段清洗：NFC 归一 → Windows 非法字符/控制字符换 _ → 去首尾空格句点 → 设备名加前缀。"""
    s = unicodedata.normalize("NFC", str(s or ""))
    s = "".join("_" if ch in WIN_RESERVED or ord(ch) < 32 else ch for ch in s)
    s = s.strip(" .")
    if not s:
        s = "unnamed"
    if s.split(".")[0].upper() in WIN_DEVICE_NAMES:
        s = "_" + s
    return s


def _name_digest(remote, src_rel, hash_len=12):
    return hashlib.sha256(
        (str(remote) + "\x00" + str(src_rel)).encode("utf-8")
    ).hexdigest()[:max(6, int(hash_len or 12))]


def safe_basename(original, remote, src_rel, max_bytes=180, hash_len=12):
    """原始文件名 → 安全落盘名。没清洗也没超长就原样返回，避免满屏哈希名。"""
    raw = unicodedata.normalize("NFC", str(original or ""))
    cleaned = clean_segment(raw)
    stem, ext = os.path.splitext(cleaned)
    if ext:
        body = clean_segment(ext[1:])
        ext = "" if body == "unnamed" else "." + body
        if len(ext.encode("utf-8")) > 32:          # 超长扩展名同样按字节截断
            ext = truncate_utf8(ext, 32).rstrip(" .")
    # 仅「设备名加 _ 前缀」不算改动，不需要再挂哈希（_CON.mp4 已经可读且唯一）
    only_device_prefix = (cleaned == "_" + raw)
    need_hash = (len(cleaned.encode("utf-8")) > max_bytes) or (cleaned != raw and not only_device_prefix)
    if not need_hash:
        return cleaned
    suffix = "~" + _name_digest(remote, src_rel, hash_len)
    budget = max_bytes - len(suffix.encode("utf-8")) - len(ext.encode("utf-8")) - 1
    if budget < 16:
        budget = 16
    stem = truncate_utf8(stem, budget).rstrip(" .") or "file"
    return stem + suffix + ext


def safe_relpath(src_rel, remote, max_name_bytes=180, max_total=240, hash_len=12):
    """目录段逐段清洗 + 相对路径总字节封顶。

    总长度超限时先压缩目录段（保留哈希保证唯一），再压缩文件主名；
    目录层级实在太深时折叠中间层为一个哈希目录，绝不把文件名截没。"""
    parts = [p for p in str(src_rel or "").split("/") if p not in ("", ".", "..")]
    if not parts:
        return ""
    full = "/".join(parts)
    dirs, leaf = parts[:-1], parts[-1]
    safe_dirs = [safe_basename(p, remote, full, max_bytes=min(120, max_name_bytes), hash_len=hash_len)
                 for p in dirs]
    safe_leaf = safe_basename(leaf, remote, full, max_bytes=max_name_bytes, hash_len=hash_len)
    rel = "/".join(safe_dirs + [safe_leaf])
    if len(rel.encode("utf-8")) <= max_total:
        return rel
    # 第二档：目录段压成「8 字节前缀 + ~哈希」，文件主名按剩余预算再截
    short_dirs = [truncate_utf8(sp, 8).rstrip(" .") + "~" + _name_digest(remote, full + "\x00" + p, hash_len)
                  for p, sp in zip(dirs, safe_dirs)]
    stem, ext = os.path.splitext(safe_leaf)

    def _fit(dir_list):
        used = len("/".join(dir_list).encode("utf-8")) + (1 if dir_list else 0)
        keep = max(16, max_total - used - len(ext.encode("utf-8")))
        return truncate_utf8(stem, keep).rstrip(" .") + ext

    leaf2 = _fit(short_dirs)
    if len(("/".join(short_dirs + [leaf2])).encode("utf-8")) > max_total + 32 and len(short_dirs) > 1:
        short_dirs = [short_dirs[0], "~" + _name_digest(remote, full, hash_len)]
        leaf2 = _fit(short_dirs)
    return "/".join(short_dirs + [leaf2])


def build_name_map(files, remote, src_base="", s=None):
    """扫描阶段一次性生成全部「源相对路径 → 安全相对路径」映射并消解冲突。

    files: rclone operations/list 的条目（Path / Size）。返回 list[dict]，按源路径排序，
    顺序固定 → 冲突时追加的 -2 / -3 序号在重启、续传后不会漂移。"""
    s = s or DEFAULTS
    mb = int(s.get("safeNameMaxBytes") or 180)
    hl = int(s.get("safeNameHashLen") or 12)
    mt = int(s.get("safePathMaxBytes") or 240)
    entries, used = [], {}
    rows = sorted((f for f in files if isinstance(f, dict) and not f.get("IsDir")),
                  key=lambda f: str(f.get("Path") or ""))
    for f in rows:
        rel = str(f.get("Path") or "").strip("/")
        if not rel:
            continue
        src_full = (str(src_base).strip("/") + "/" + rel).strip("/")
        safe = safe_relpath(rel, remote, max_name_bytes=mb, max_total=mt, hash_len=hl)
        parent, _, leaf = safe.rpartition("/")
        stem, ext = os.path.splitext(leaf)
        key_dir = parent.casefold()
        bucket = used.setdefault(key_dir, set())
        candidate, n = leaf, 2
        while candidate.casefold() in bucket:
            candidate = "%s-%d%s" % (stem, n, ext)     # 截断撞名 / 大小写撞名：追加序号
            n += 1
        bucket.add(candidate.casefold())
        safe = (parent + "/" if parent else "") + candidate
        entries.append({"src": rel, "srcFull": src_full, "dst": safe,
                        "size": int(f.get("Size") or 0), "status": "pending",
                        "attempts": 0, "error": ""})
    return entries


# ── 映射清单（每卷一份，隐藏文件；原子写 + 锁内串行）────────────────────────
NAME_MAP_VERSION = 2
NAME_MAP_FILENAME = ".cloudshuttle-name-map.json"
NAME_MAPS = {}                 # path -> data（常驻内存，任务启动时加载一次）
NAME_MAP_DIRTY = set()         # 有待落盘变更的 path，由 flush_name_maps 批量写
NAME_MAP_LOCK = threading.RLock()


def _name_map_path(volume):
    root = str((volume or {}).get("console") or "").strip()
    if root and os.path.isdir(root):
        return os.path.join(root, NAME_MAP_FILENAME)
    return os.path.join(DATA_DIR, NAME_MAP_FILENAME)   # 卷不可写时退回数据目录，不丢追溯能力


def load_name_map(volume):
    path = _name_map_path(volume)
    with NAME_MAP_LOCK:
        if path in NAME_MAPS:
            return NAME_MAPS[path]
        d = _read_json(path, None)
        if not isinstance(d, dict) or d.get("version") != NAME_MAP_VERSION \
                or not isinstance(d.get("entries"), dict):
            if isinstance(d, dict) and d:
                try:                    # 旧版本不自动迁移：备份成 .old 后重建 v2
                    if not os.path.exists(path + ".old"):
                        shutil.copy2(path, path + ".old")
                except Exception:
                    pass
            d = {"version": NAME_MAP_VERSION, "updated": "", "entries": {}}
        NAME_MAPS[path] = d
        return d


def save_name_map(volume, data=None):
    path = _name_map_path(volume)
    with NAME_MAP_LOCK:
        d = data or NAME_MAPS.get(path) or load_name_map(volume)
        d["updated"] = time.strftime("%F %T")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _write_json(path, d)
        except Exception:
            pass
        NAME_MAP_DIRTY.discard(path)


def flush_name_maps():
    """把标记为脏的清单批量落盘（worker 每轮进度写盘时顺带调用）。"""
    with NAME_MAP_LOCK:
        paths = [p for p in NAME_MAP_DIRTY if NAME_MAPS.get(p)]
    for p in paths:
        with NAME_MAP_LOCK:
            d = NAME_MAPS.get(p)
            d["updated"] = time.strftime("%F %T")
            try:
                _write_json(p, d)
            except Exception:
                pass
            NAME_MAP_DIRTY.discard(p)


def get_safe_path(volume, remote, src_rel):
    ent = load_name_map(volume)["entries"].get(str(remote) + ":" + str(src_rel))
    return str((ent or {}).get("safe") or "")


def record_file_state(volume, remote, src_rel, _flush=False, **fields):
    """更新单个文件的映射条目（原名 / 落盘名 / 大小 / 状态 / 错误 / 时间）。"""
    try:
        with NAME_MAP_LOCK:
            d = load_name_map(volume)
            key = str(remote) + ":" + str(src_rel)
            ent = d["entries"].setdefault(key, {"original": str(src_rel), "remote": str(remote)})
            ent.update(fields)
            ent["updated"] = time.strftime("%F %T")
            NAME_MAP_DIRTY.add(_name_map_path(volume))
        if _flush:
            save_name_map(volume)
    except Exception:
        pass    # 清单只用于追溯，写失败绝不能拖垮下载


# ── 每个目录任务一份「文件级执行计划」，避免把几千条明细塞回 jobs.json ──────
JOB_FILES_DIR = os.path.join(DATA_DIR, "jobfiles")


def _plan_path(job_id):
    return os.path.join(JOB_FILES_DIR, "%s.json" % int(job_id))


def load_file_plan(job_id):
    d = _read_json(_plan_path(job_id), None)
    return d if isinstance(d, dict) and isinstance(d.get("entries"), list) else None


def save_file_plan(job_id, plan):
    try:
        os.makedirs(JOB_FILES_DIR, exist_ok=True)
        _write_json(_plan_path(job_id), plan)
    except Exception:
        pass


# ── 错误分类：文件级 / 账号级 / 磁盘权限 / 瞬时 / 未知 ──────────────────────
FILE_FATAL_KEYS = (
    "file name too long", "filename too long", "name too long", "path too long",
    "file name or extension is too long", "文件名或扩展名太长", "路径太长",
)
# v4.06：源端「只有壳、没有内容」的文件。
# 2026-11-24 实测：PikPak 里某个 1.71 GB 的 mp4，列表里 size 正常、audit 也是 Normal，
# 但 hash 结尾是 `TEMP`、medias 与 web_content_link 全空 —— 是转存时没落到实体的占位文件。
# 表现：rclone 报 `failed to open source object: can't download: no link`，一个字节都传不出来。
# 这种错误重试无意义（放再久也不会自己好），必须立刻判死，否则会跟看门狗互相拉扯。
SOURCE_DEAD_KEYS = (
    "no link", "can't download", "cannot download", "failed to open source object",
    "no available link", "no download link",
)
DISK_FATAL_KEYS = (
    "no space left", "permission denied", "access denied", "read-only file system", "disk full",
    # v4.07：EPERM。假卷 / 卷被卸载 / 文件系统只读保护都长这样 —— 能建 0 字节文件，
    # 写数据时才回 `open ...: operation not permitted`，而且重试一万次都一样。
    # 不归类到这里就会一直判 UNKNOWN → 无限重试，界面上永远是 0.0%。
    "operation not permitted", "not permitted",
)
TRANSIENT_KEYS = (
    "timeout", "timed out", "tls handshake", "eof", "i/o timeout",
    "connection reset", "connection refused", "temporarily unavailable",
)
# v3.93：两类超时在 rclone 报错里长得很像，但方向完全相反，必须分开提示。
RC_TIMEOUT_HINTS = (
    ("i/o timeout", "下载源读取超时（源端慢或线路抖动，可重试）"),
    ("timeout awaiting response headers", "与下载源握手超时（多为代理/线路问题，可重试）"),
    ("context deadline exceeded", "本地等待超过截止时间（控制面或线路慢，可重试）"),
)


def classify_error(msg, context=""):
    """返回 (类别, 友好提示)。类别 ∈ FILE_FATAL / ACCOUNT_FATAL / DISK_FATAL / TRANSIENT / UNKNOWN。"""
    raw = str(msg or "")
    low = raw.lower()
    # v4.06：先判「源端没直链」。这类报错里常带 download / timeout 之类的词，
    # 放到后面会被 is_net_error 抢走，变成「网络抖动，可重试」—— 实际重试一万次也没用。
    for k in SOURCE_DEAD_KEYS:
        if k in low:
            return "FILE_FATAL", ("源端没有可用直链（PikPak 侧文件未就绪或已损坏，"
                                  "需重新转存该文件；同任务的其它文件不受影响）：%s"
                                  % mask_secret(raw[:160]))
    for k in FILE_FATAL_KEYS:
        if k in low:
            return "FILE_FATAL", "文件名或路径超出文件系统限制（原始：%s）" % mask_secret(raw[:160])
    ctx = str(context or "").lower()
    if "invalid argument" in low and (any(t in (low + ctx) for t in ("name", "path", "file", "文件名"))
                                      or "/" in ctx or "\\" in ctx):
        return "FILE_FATAL", "文件名不被目标文件系统接受（原始：%s）" % mask_secret(raw[:160])
    if is_net_error(raw):
        # v3.84：必须在 _fatal_job_error 之前判定。下载接口报 captcha / unauthorized
        # 之前先撞上的往往是 connection reset / i/o timeout，这类抖动重试就好，
        # 不能归到账号类终态，否则任务会被判死、账号被标红「需重新登录」。
        # v3.93：把「本地控制面超时」与「源端超时」分开，排障不再指向错的方向。
        hint = "网络抖动（连接被重置或超时）"
        if "本地控制面" in raw or "本地 rclone 控制面" in raw:
            hint = "控制台连不到本地 rclone（控制面超时，不是源端慢；先看 rclone 容器）"
        else:
            for k, h in RC_TIMEOUT_HINTS:
                if k in low:
                    hint = h
                    break
        return "TRANSIENT", "%s，可重试（原始：%s）" % (hint, mask_secret(raw[:160]))
    fatal = _fatal_job_error(raw)          # 既有业务错误（额度 / 凭证 / captcha…）
    if fatal:
        return "ACCOUNT_FATAL", fatal[1]
    for k in DISK_FATAL_KEYS:
        if k in low:
            return "DISK_FATAL", "磁盘空间不足或没有写入权限（原始：%s）" % mask_secret(raw[:160])
    for k in TRANSIENT_KEYS:
        if k in low:
            return "TRANSIENT", raw
    return "UNKNOWN", raw


def launch_job(job, s):
    proxy = bool(job.get("proxy"))
    s = autotune_profile_for(s, job.get("id"))    # v3.94：单文件 / 旧式目录任务也走最优档
    if proxy and not ensure_proxy_rc():
        job["state"] = "error"
        job["error"] = "代理守护进程（%d）启动失败，请检查代理客户端与 %s 容器" % (PROXY_RC_PORT, RCLONE_CONTAINER)
        return False
    move = job["mode"] == "move"
    if not job.get("single_file") and job.get("naming") == "v2":
        # v3.83：目录任务不再用 sync/copy（它会原样保留文件名），
        # 改为「列目录 → 生成映射 → 线程池逐文件 copyfile」，错误按文件隔离。
        return _start_safe_dir_job(job, s)
    if job.get("single_file"):
        # sync/copy 的 srcFs/dstFs 都必须是目录，单文件必须用专用接口；
        # src_dir / dst_dir / leaf 由 start 在预检判定为文件后写进 job。
        # v3.83：源端永远用原始文件名，目标端用安全落盘名（无 safe_leaf 即旧任务，原样）。
        method = "operations/movefile" if move else "operations/copyfile"
        # v4.08：单文件也可能落在 Downloads/<子目录>/ 下（v4.08 起不再写卷根），
        # copyfile 的 dstFs 必须已存在，这里先幂等地建一次；失败不拦，让 copyfile
        # 自己报出真正的原因（与逐文件下载里的做法一致）。
        try:
            rc("operations/mkdir", fs=job.get("dst_dir") or job["dest"],
               _timeout=60, _proxy=proxy)
        except Exception:
            pass
        kv = dict(srcFs=job["src_dir"], srcRemote=job["leaf"],
                  dstFs=job["dst_dir"], dstRemote=job.get("safe_leaf") or job["leaf"], _async="true")
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
    kv["_group"] = job_group_of(job["id"])     # v4.15：整任务一个组，停止/清理按组取消
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
        if job.get("single_file") and job.get("naming") == "v2":
            job["curfile"] = job.get("leaf") or ""
            record_file_state(job_volume(job), job.get("remote"), job.get("subpath"),
                              safe=job.get("safe_leaf") or job.get("leaf"),
                              original=job.get("subpath"), status="running", _flush=True)
        return True
    except Exception as e:
        job["state"] = "error"
        job["error"] = "启动失败: %s" % str(e)[:150]
        return False

# ────────────────────────────────────────────────────────────────────────────
# v3.83 目录任务：逐文件安全命名下载（阶段 2）
#   旧的 sync/copy 会原样保留云端文件名，遇到超长名只会整批反复重试。
#   这里改成：列目录 → 一次性生成映射并消解冲突 → 线程池逐文件 copyfile。
#   同步调用（不开 _async）是为了让错误归属到具体文件：坏文件标 failed 继续下一个，
#   账号级 / 磁盘级错误才中止整个任务。
# ────────────────────────────────────────────────────────────────────────────
SAFE_JOBS = {}                 # job_id -> {"stop": Event}；暂停信号 + 重启兜底判定
SAFE_JOBS_LOCK = threading.Lock()
SAFE_LIVE = {}                 # job_id -> worker 的内存 state；详情页读它的 live 采样
                               # v3.92：异步 copyfile 之后 job 不再有单一 jobid，
                               # 详情页原先读 core/stats 的路径失效，文件级进度恒为 0
SAFE_THREADS = {}              # job_id -> worker Thread；stop/remove 用它等在飞任务退出
SAFE_RESTART_WAIT = 8          # v4.17：重复启动时等旧世代退出的上限（秒），等不到就不起新的
SAFE_BACKOFF = (2, 8, 30)      # 瞬时错误指数退避（秒）
FILE_RC_TIMEOUT = 3600         # 单文件 copyfile 的硬超时（含异步轮询总时长）
COPY_POLL_INTERVAL = 1.0       # 异步 copyfile 状态轮询间隔（秒）
LIVE_SAMPLE_INTERVAL = 2.0     # 目录任务「在传字节 / 速度」采样间隔（秒）
STALL_MIN_SPEED = 50 * 1024    # 低于该速度视为「龟速」（字节/秒）
STALL_SECONDS = 90             # 连续龟速超过该秒数就取消，让位给后面的文件（v3.95：300→90）
SLOW_TAIL_WORKERS = 1          # v3.95：第二轮「已让位过的文件」最多占几个槽（实际按连接预算算）
SLOW_TAIL_MAX_SECONDS = 1800   # 尾池里单文件最多再试 30 分钟
SLOW_TAIL_MAX_BYTES = 1048576  # 30 分钟连 1MB 都推不动，判定源端不可达（别无限期卡住任务）
# v4.06：一个字节都没进来，说明连源对象都没打开成功（典型是 PikPak 没有直链）。
# 这种情况不该等满 30 分钟 —— 10 分钟零字节就够说明问题了。
ZERO_TAIL_SECONDS = 600
# v4.06：文件久传不动时，直接问一次 PikPak「这个文件到底有没有直链」。
# 起因：rclone 自己会对取链请求做低层重试，错误在 180 秒内根本浮不上来；
# 看门狗一停，rclone 又把它记成 `context canceled`，两边打架，任务在 97% 无限重启。
ZERO_PROBE_SECONDS = 60
PARTIAL_ORPHAN_SECONDS = 24 * 3600   # 目标目录里超过 24h 没动的 *.partial 视为孤儿分片


class _JobAbort(Exception):
    """账号级 / 磁盘级错误：中止整个目录任务，state 为要落入的终态。"""

    def __init__(self, state, msg):
        super().__init__(msg)
        self.state, self.msg = state, str(msg)


def unfinished_entries(entries):
    """还没落盘完成的条目：pending / deferred / running。

    v4.15：running 必须算在内。收尾时若还有在飞文件（status=running）就写 done，
    会出现「已完成 209/212、错误行说 3 个文件失败，详情 failedFiles 却是空」的自相矛盾，
    「仅重试失败项」也无从下手。"""
    return [e for e in (entries or [])
            if str(e.get("status") or "") in ("pending", "deferred", "running")]


def build_live_transferring(entries, lives):
    """v3.92：把 worker 采样到的「在传」字节/速度还原成详情页的 transferring 列表。

    entries 是计划文件里的文件清单（status=running 才算在传），lives 是
    state["live"]：{原相对路径: (已传字节, 实时速度, rclone 显示的 name)}。
    采样缺失时退化为 0，不影响详情页其余字段。
    """
    out = []
    for e in entries:
        if e.get("status") != "running":
            continue
        v = lives.get(e.get("src")) or (0, 0, "")
        out.append({"name": e.get("src"),
                    "bytes": int(v[0] or 0),
                    "total": int(e.get("size") or 0),
                    "speed": float(v[1] or 0)})
        if len(out) >= 20:
            break
    return out


def _safe_stop_event(job_id):
    with SAFE_JOBS_LOCK:
        ent = SAFE_JOBS.get(job_id)
        return ent["stop"] if ent else None


def _safe_requested_stop(job_id):
    ev = _safe_stop_event(job_id)
    return bool(ev and ev.is_set())


# ── v4.15 任务级 job 分组：停止/出错/重启后把残留传输一起取消 ─────────────
# 逐文件下载是「一个文件一个异步 job」，worker 只 job/stop 自己手上那一个。
# 一旦 job/stop 失败被吞掉，工作线程会重试同一个文件 → 旧 job 还在传、新 job 又起，
# 同一个文件被并行重下几份，连接数翻倍、每条都慢（实测同一个视频在 3 个 job 组里同时传），
# 面板就会出现「任务已停止/出错，却还有流量」。
# 现在给每个任务的所有异步 job 打上统一的 _group，停止时按组一次全停。
JOB_GROUP_PREFIX = "cloudshuttle/"


def job_group_of(task_id):
    return "%s%s" % (JOB_GROUP_PREFIX, task_id)


def stop_job_group(task_id, proxy=False):
    """取消某任务名下所有 rclone 异步 job。优先 job/stopgroup，老版 rclone 退化为逐个 job/stop。"""
    g = job_group_of(task_id)
    ok = False
    try:
        rc("job/stopgroup", group=g, _timeout=10, _proxy=proxy)
        return True
    except Exception:
        pass
    try:
        lst = (rc("job/list", _timeout=10, _proxy=proxy) or {}).get("jobs") or []
        for jb in lst:
            if str(jb.get("group") or "") == g:
                try:
                    rc("job/stop", jobid=jb.get("id"), _timeout=10, _proxy=proxy)
                    ok = True
                except Exception:
                    pass
    except Exception:
        pass
    return ok


def sweep_orphan_groups(quiet=True):
    """把「没有任何在跑任务认领」的 cloudshuttle/* job 组停掉。

    控制台重启、worker 异常退出、老版本没打组标签的残留，都会留下仍在传的 async job；
    面板读的是 rclone 的累计字节，于是出现「进行中 0，却一直有速度」。返回被停掉的组数。
    """
    with LOCK:
        jobs = load_jobs()
    with SAFE_JOBS_LOCK:
        alive = set(str(x) for x in SAFE_JOBS.keys())
    keep = set(alive)
    for j in jobs:
        if str(j.get("state") or "") in ("running", "queued"):
            keep.add(str(j.get("id")))
    killed = 0
    for use_proxy in (False, True):
        groups = _active_job_groups(use_proxy)
        for g in groups:
            if g.startswith(JOB_GROUP_PREFIX):
                if g[len(JOB_GROUP_PREFIX):] in keep:
                    continue
            elif keep:
                # 还有任务在跑时，无标签组可能是该任务升级前的老 job，不能乱停
                continue
            try:
                rc("job/stopgroup", group=g, _timeout=10, _proxy=use_proxy)
                killed += 1
                if not quiet:
                    print("%s 清理无主传输组 %s" % (time.strftime("%F %T"), g), flush=True)
            except Exception:
                pass
    return killed


def _active_job_groups(proxy=False):
    """当前真在传 / 未结束的 job 组。只看活的 —— core/group-list 会把历史组一直列着，
    按它清理会每分钟「杀掉」同一个早已结束的组，日志全是噪音。"""
    groups = set()
    try:
        cs = rc("core/stats", _timeout=15, _proxy=proxy) or {}
        for t in (cs.get("transferring") or []):
            if t.get("group"):
                groups.add(str(t["group"]))
    except Exception:
        pass
    try:
        for jb in ((rc("job/list", _timeout=15, _proxy=proxy) or {}).get("jobs") or []):
            if jb.get("group"):
                groups.add(str(jb["group"]))
    except Exception:
        pass
    return groups


def orphan_guard_loop(interval=60):
    """常驻：启动时先扫一遍，之后每分钟扫一遍孤儿 job 组。"""
    time.sleep(3)
    while True:
        try:
            n = sweep_orphan_groups()
            if n:
                log_event("system", "orphan-sweep", groups=n)
        except Exception as e:
            print("%s 孤儿传输巡检失败: %s" % (time.strftime("%F %T"), str(e)[:100]), flush=True)
        time.sleep(max(10, int(interval)))


def wait_worker_exit(job_id, timeout=10.0):
    """v3.93：等某个任务的 worker 线程真正退出，返回 (等待秒数, 是否仍在跑)。

    只对由本进程拉起的线程有意义（控制台重启过的老任务没有线程，直接视为已退出）。
    join 放在 LOCK 之外调用，避免把「停止」以外的接口一起堵住。
    """
    with SAFE_JOBS_LOCK:
        th = SAFE_THREADS.get(job_id)
    if not th or not th.is_alive():
        with SAFE_JOBS_LOCK:
            SAFE_THREADS.pop(job_id, None)
        return 0.0, False
    t0 = time.time()
    try:
        th.join(timeout=max(0.1, float(timeout)))
    except Exception:
        return round(time.time() - t0, 1), th.is_alive()
    elapsed = round(time.time() - t0, 1)
    alive = th.is_alive()
    if not alive:
        with SAFE_JOBS_LOCK:
            SAFE_THREADS.pop(job_id, None)
    return elapsed, alive


def fix_permissions(local_dir, dir_mode=0o755, file_mode=0o644):
    """v3.93：把 rclone 建出来的 0000 权限修回来（目录 0755 / 文件 0644）。

    只处理 mode 恰为 0 的条目，绝不改动用户自己设过的权限；只 stat + chmod，
    不读内容，所以哪怕目录很大也只是一次元数据遍历。返回 (改目录数, 改文件数)。
    """
    if not local_dir or not os.path.isdir(local_dir):
        return 0, 0
    nd = nf = 0
    for root, dirs, files in os.walk(local_dir):
        for d in dirs:
            p = os.path.join(root, d)
            try:
                if statmod.S_IMODE(os.stat(p).st_mode) == 0:
                    os.chmod(p, dir_mode)
                    nd += 1
            except OSError:
                continue
        for f in files:
            p = os.path.join(root, f)
            try:
                if statmod.S_IMODE(os.stat(p).st_mode) == 0:
                    os.chmod(p, file_mode)
                    nf += 1
            except OSError:
                continue
    return nd, nf


def cleanup_orphan_partials(local_dir, now=None, max_age=PARTIAL_ORPHAN_SECONDS):
    """v3.93：清理 rclone 中断后留下的孤儿分片（`<名字>.<hash>.partial`）。

    只删「超过 max_age 没被改动」的：正在传的分片 mtime 是新鲜的，不会被误删。
    返回 (删除个数, 释放字节)。单个文件失败就跳过，不影响任务本身。
    """
    if not local_dir or not os.path.isdir(local_dir):
        return 0, 0
    now = time.time() if now is None else now
    n = freed = 0
    for root, _dirs, files in os.walk(local_dir):
        for f in files:
            if not f.lower().endswith(".partial"):
                continue
            p = os.path.join(root, f)
            try:
                st_ = os.stat(p)
                if now - st_.st_mtime < max_age:
                    continue
                os.remove(p)
                n += 1
                freed += st_.st_size
            except OSError:
                continue
    return n, freed


def log_event(job_id, event, **kw):
    """v3.93：每个文件一条结构化日志，docker logs 里可以直接 grep event=skip。

    字段按 key=value 拼接，值里的空白压成下划线，保证一条决策占一行、便于对账。
    """
    try:
        parts = ["%s=%s" % (k, re.sub(r"\s+", "_", str(v))[:120]) for k, v in kw.items()]
        print("%s [job %s] event=%s %s"
              % (time.strftime("%F %T"), job_id, event, " ".join(parts)), flush=True)
    except Exception:
        pass


def _start_safe_dir_job(job, s):
    """启动目录任务的工作线程。

    v4.17：同一个任务绝不允许两代 worker 并存。以前这里发现旧线程只做 `old["stop"].set()`
    就马上把 SAFE_JOBS[id] 换成新的 Event —— 而旧线程判停是按 job_id 去查 SAFE_JOBS 的，
    换掉之后它永远看不到那个已置位的信号，会把自己那一套 entries 一路跑到底。两代交替写
    同一个计划文件、同一个 bytes，界面就出现「进度条一会长一会短、完成数从 5 掉回 2」。
    现在的做法：置旧信号 → 按组取消在飞 job → 等旧线程真正退出 → 才换新 Event；
    等不到（例如卡在收尾阶段）就拒绝启动，交给下次「继续」。
    """
    jid = job["id"]
    with SAFE_JOBS_LOCK:
        old = SAFE_JOBS.get(jid)
        old_th = SAFE_THREADS.get(jid)
    if old:
        old["stop"].set()
    if old or (old_th and old_th.is_alive()):
        try:
            stop_job_group(jid, bool(job.get("proxy")))   # 先打断在飞的 copyfile
        except Exception:
            pass
        if old_th and old_th.is_alive() and old_th is not threading.current_thread():
            old_th.join(SAFE_RESTART_WAIT)
        if old_th and old_th.is_alive():
            # v4.18：等不到就排回队列，交给 scheduler 每 20 秒重试一次 —— 它一定会等到
            # 旧线程退出，既不会两代并存，也不用用户手动再点「继续」。
            job["state"] = "queued"
            job["error"] = ("上一轮的工作线程还在收尾（在飞传输已取消），已排队，"
                            "退出后自动开始")
            return False
    with SAFE_JOBS_LOCK:
        SAFE_JOBS[jid] = {"stop": threading.Event()}
    job["jobid"] = None                # 没有 rclone 异步 job，进度由工作线程自己写
    job["started_ts"] = time.time()
    job["state"] = "running"
    job["phase"] = "scan"
    job["error"] = ""
    job["curfile"] = ""
    job["speed"] = 0
    job["eta"] = None
    # start 是「先 launch 再 append + save_jobs」，工作线程若立刻按 id 读 jobs.json
    # 会拿不到任务而直接退出（任务永远 running）。把快照一并传过去，读盘只用于进度回写。
    th = threading.Thread(target=_safe_dir_worker, args=(job["id"], dict(job)), daemon=True)
    th.start()
    with SAFE_JOBS_LOCK:              # v3.93：让 stop/remove 能等这条线程真正退出
        SAFE_THREADS[job["id"]] = th
    return True


# ────────────────────────────────────────────────────────────────────────────
# v3.94 自动测速调优：把「手测不同并发档 → 取最快 → 写回设置」自动化
#
# "口径" 很重要：rclone 的 core/stats.speed 会把「被取消后重试的字节」重复计入，
# 实测出现过 12 MB/s 自报速度而磁盘 108 秒零增长。所以这里一律用**目标目录真实
# 落盘增量**作为成绩，与手工验证的口径完全一致。
#   ── 为什么不能无脑加并发 ──
#   实测（PikPak 会员号）：4 文件 × 4 流（16 连接）= 4.22 MB/s；8 × 2（16 连接）= 2.48；
#   2 文件 × 8 流（16 连接）= 2.2；3 文件 × 8 流（24 连接）= 0.11；2 × 16（32 连接）= 0.07。
#   两个结论：① 服务端按「账号连接数」静默限流，超过阈值不是变快而是塌一个数量级；
#   ② 连接总数相同的前提下，摊到更多文件更抗「单个慢文件」——慢文件会独占预算，
#   2 个文件时一个慢一个快，总额度只能吃一半；4 个文件时其它三条能把额度吃满。
#   因此候选档一律限制在 AUTOTUNE_MAX_CONNS 以内，并把「多文件少流」放在首位。
# ────────────────────────────────────────────────────────────────────────────
AUTOTUNE_FILE = os.path.join(DATA_DIR, "autotune.json")
AUTOTUNE_LOCK = threading.Lock()
AUTOTUNE_RUN = {}                  # 当前 / 最近一次测速的内存态，供 autotuneinfo 轮询
AUTOTUNE_CANDIDATES = ((4, 4), (2, 8), (1, 16), (8, 2))
AUTOTUNE_MAX_CONNS = 16            # 实测超过 16 连接必被服务端猛限速


def _at_read():
    try:
        d = _read_json(AUTOTUNE_FILE, {})
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _at_write(d):
    try:
        _write_json(AUTOTUNE_FILE, d)
    except Exception:
        pass


def dir_bytes(dest):
    """目标目录当前真实占用（字节）。基准是本地挂载路径，不含 .partial 之外的临时项。"""
    p = local_dest(str(dest or ""))
    if not p or not os.path.isdir(p):
        return 0
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def measure_rate(dest, seconds, settle=0):
    """返回真实吞吐（MB/s）：先让连接稳定 settle 秒，再测 seconds 秒的落盘增量。"""
    if settle:
        time.sleep(max(0, int(settle)))
    b0, t0 = dir_bytes(dest), time.time()
    time.sleep(max(1, int(seconds)))
    dt = max(0.001, time.time() - t0)
    return (dir_bytes(dest) - b0) / dt / 1048576.0


def wait_growth(dest, timeout=120, step=3, need=2 * 1048576):
    """v3.94：等「真的开始落盘」再计分。

    换档后 worker 要先列源目录（159 个文件走一次网络）、建映射、索引目标目录，
    这段时间磁盘几乎不动。实测这时候直接开 25 秒窗口，会把第一名打成 0.11 MB/s，
    最后一名的偶然突发又被记成 7.66 —— 结论完全反了。所以先等 3 秒内涨 2MB，
    等不到就返回 None（这一档按 0 计，并标注「无增长」）。"""
    t0 = time.time()
    b = dir_bytes(dest)
    while time.time() - t0 < timeout:
        time.sleep(step)
        nb = dir_bytes(dest)
        if nb - b >= need:
            return time.time() - t0
        b = nb
    return None


def at_label(t, mts):
    return "%dx%d" % (int(t), int(mts))


def at_best():
    b = _at_read().get("best") or {}
    try:
        t, m = int(b.get("transfers") or 0), int(b.get("mts") or 0)
    except Exception:
        return None
    if t < 1 or m < 1:
        return None
    return {"transfers": t, "mts": m, "rate": float(b.get("rate") or 0),
            "label": b.get("label") or at_label(t, m), "at": b.get("at")}


def autotune_profile_for(s, job_id=None):
    """v3.94：开启自动调优且已学到最优档时，任务按该档启动（不改用户设置以外的语义）。

    注意：测速期间必须原样返回——否则每次试跑都会被「已存最优档」覆盖回去，
    四档实际都在跑同一档，成绩全是假的（v3.94 首轮实测踩到过）。"""
    if AUTOTUNE_RUN.get("running"):
        return s
    if not _truthy((s or {}).get("autoTune", DEFAULTS.get("autoTune", False))):
        return s
    b = at_best()
    if not b:
        return s
    if (int(s.get("transfers") or 0) == b["transfers"]
            and int(s.get("multiThreadStreams") or 0) == b["mts"]):
        return s
    s = dict(s)
    s["transfers"], s["multiThreadStreams"] = b["transfers"], b["mts"]
    if job_id:
        try:
            log_event(job_id, "autotune-apply", profile=b["label"],
                      rate=b["rate"], why="沿用实测最优档")
        except Exception:
            pass
    return s


def _at_stop_job(job_id, wait=15):
    with LOCK:
        jobs = load_jobs()
        for j in jobs:
            if j.get("id") == job_id and j.get("state") in ("running", "queued"):
                stop_job_entry(j)
        save_jobs(jobs)
    return wait_worker_exit(job_id, timeout=wait)


def _at_start_job(job_id, s):
    """等价于点「继续」的下载部分：把指定任务按给定设置拉起来（不走云端预检，省时间）。"""
    with LOCK:
        jobs = load_jobs()
        j = next((x for x in jobs if x.get("id") == job_id), None)
        if not j:
            return False, "任务不存在"
        if j.get("state") in ("running", "queued"):
            return True, ""
        j.update(state="queued", error="", phase="", speed=0, curfile="")
        ok = launch_job(j, s)
        save_jobs(jobs)
        return bool(ok), "" if ok else str(j.get("error") or "启动失败")


def _autotune_worker(job_id, seconds, settle, candidates):
    """逐档试跑：停任务 → 套档 → 启动 → 稳定 settle → 测 seconds 秒落盘增量 → 停。

    代价说明：每换一档都会取消在飞文件，被打断的字节要重下（这是人工测速也有
    的代价）。所以只测 4 档、窗口 45 秒，并把成绩写到 autotune.json 供下次直接复用。"""
    prev = load_settings()
    res = {"running": True, "job": job_id, "step": "准备", "results": [],
           "best": None, "started": time.time(), "error": "", "note": ""}
    with AUTOTUNE_LOCK:
        AUTOTUNE_RUN.clear()
        AUTOTUNE_RUN.update(res)
    try:
        job = next((j for j in load_jobs() if j.get("id") == job_id), None)
        if not job:
            raise RuntimeError("任务不存在")
        if job.get("kind") == "xfer" or job.get("single_file") or job.get("naming") != "v2":
            raise RuntimeError("只有「安全命名」的目录任务支持测速（逐文件下载才能按档切换）")
        emsg = str(job.get("error") or "").lower()
        if "captcha" in emsg or "风控" in emsg or "重新登录" in emsg:
            raise RuntimeError("账号正在人工验证（captcha / 风控），先去账号页「重新登录」过完安全验证，"
                               "否则每档都会被风控拦下、任务也跑不起来")
        others = [j for j in load_jobs()
                  if j.get("state") in ("running", "queued") and j.get("id") != job_id]
        if others:
            raise RuntimeError("还有 %d 个任务在跑，先暂停它们再测速，否则测到的是叠加带宽"
                               % len(others))
        plan = load_file_plan(job_id) or {}
        pending = [e for e in (plan.get("entries") or [])
                   if e.get("status") not in ("done", "skipped")]
        if not pending:
            raise RuntimeError("这个任务没有待下载的文件，无需测速")
        dest = job.get("dest")
        _at_stop_job(job_id)
        results = []
        for t, m in candidates:
            t, m = int(t), int(m)
            if t * m > AUTOTUNE_MAX_CONNS:
                continue
            AUTOTUNE_RUN.update(step="%s 测速中" % at_label(t, m), results=list(results))
            s = dict(prev)
            s["transfers"], s["multiThreadStreams"] = t, m
            save_settings(s)
            ok, err = _at_start_job(job_id, s)
            if not ok:
                raise RuntimeError("启动任务失败：%s" % err)
            # 先等真正落盘（列目录 / 建索引期间磁盘不动），避免把启动开销算成速度
            warm = wait_growth(dest, timeout=int(settle) + 90)
            if warm is None:
                rate, wnote = 0.0, "无增长"
            else:
                rate, wnote = measure_rate(dest, seconds, settle=0), ""
            _at_stop_job(job_id)
            results.append({"label": at_label(t, m), "transfers": t, "mts": m,
                            "rate": round(rate, 2), "warm": round(warm or 0, 1),
                            "note": wnote})
            try:
                log_event(job_id, "autotune", profile=at_label(t, m), rate=round(rate, 2))
            except Exception:
                pass
            AUTOTUNE_RUN.update(results=list(results))
        if not results:
            raise RuntimeError("没有可用的候选档位")
        ranked = sorted(results, key=lambda r: r["rate"], reverse=True)
        best = ranked[0]
        second = ranked[1]["rate"] if len(ranked) > 1 else 0.0
        # v3.94：某一档高出第二名 2 倍以上时先复核一遍，防「偶然突发」被当成最优
        if best["rate"] >= 1.0 and best["rate"] > 2.0 * second:
            AUTOTUNE_RUN.update(step="复核 %s" % best["label"])
            s = dict(prev)
            s["transfers"], s["multiThreadStreams"] = best["transfers"], best["mts"]
            save_settings(s)
            ok, err = _at_start_job(job_id, s)
            warm2 = wait_growth(dest, timeout=int(settle) + 90) if ok else None
            rate2 = measure_rate(dest, seconds) if warm2 is not None else 0.0
            _at_stop_job(job_id)
            best = dict(best)
            best["confirm"] = round(rate2, 2)
            best["rate"] = round(min(best["rate"], rate2), 2)   # 取两次的保守值
            ranked[0] = best
        st = _at_read()
        hist = [h for h in (st.get("history") or []) if isinstance(h, dict)][-19:]
        hist.append({"at": time.time(), "job": job_id, "seconds": seconds,
                     "settle": settle, "results": results, "best": best["label"]})
        st["history"] = hist
        st["best"] = {"transfers": best["transfers"], "mts": best["mts"],
                      "rate": best["rate"], "label": best["label"], "at": time.time()}
        _at_write(st)
        s = dict(load_settings())
        s["transfers"], s["multiThreadStreams"] = best["transfers"], best["mts"]
        save_settings(s)
        _at_start_job(job_id, s)
        res.update(step="完成", best=best, results=results, running=False,
                   note="实测最快 %.2f MB/s（%s），已写回设置并继续下载"
                        % (best["rate"], best["label"]))
        try:
            log_event(job_id, "autotune-done", best=best["label"], rate=best["rate"])
        except Exception:
            pass
    except Exception as e:
        # 失败也要先把任务拉起来，并把设置退回测速前的样子（已保存的历史成绩不动）
        msg = mask_secret(str(e)[:200])
        try:
            b = at_best()
            s = dict(prev)
            if b:
                s["transfers"], s["multiThreadStreams"] = b["transfers"], b["mts"]
            save_settings(s)
            _at_start_job(job_id, s)
        except Exception:
            pass
        res.update(running=False, error=msg, step="中止")
    finally:
        with AUTOTUNE_LOCK:
            AUTOTUNE_RUN.update(res)


def expand_source_files(job):
    """递归列出源目录全部文件（只读），目录任务的下载过滤规则在这里继续生效。

    v4.01：分账号任务带 fileList（本账号分到的相对路径），这里按清单精确裁一次 ——
    分片必须互斥，多下来一个文件就是两个账号抢写同一个目标文件。"""
    kv = dict(fs=job["src"], remote="", recurse=True,
              _timeout=300, _proxy=bool(job.get("proxy")), _strict=True)
    fopt = build_filter_opt(job.get("filter") or {})
    if fopt:
        kv["_filter"] = json.dumps(fopt, ensure_ascii=False)
    r = rc("operations/list", **kv)
    rows = [e for e in (r.get("list") or []) if isinstance(e, dict) and not e.get("IsDir")]
    wanted = job.get("fileList")
    if not wanted:
        return rows
    keep = {str(p).strip("/") for p in wanted if str(p).strip("/")}
    picked = [e for e in rows if str(e.get("Path") or "").strip("/") in keep]
    have = {str(e.get("Path") or "").strip("/") for e in picked}
    lost = keep - have
    if lost:
        # 云端被改名/删除时不能让文件「悄悄消失」：留一条可 grep 的日志，数量也进任务错误
        log_event(job.get("id"), "split-missing", count=len(lost),
                  sample="|".join(sorted(lost)[:3]))
    return picked



MISSING_DIR_HINTS = ("directory not found", "object not found", "doesn't exist",
                     "no such file or directory", "not found")
INDEX_RETRY_DELAYS = (2, 5)     # 目标目录索引重试间隔（秒）


def _is_missing_dir_error(msg):
    low = str(msg or "").lower()
    return any(h in low for h in MISSING_DIR_HINTS)


def _dest_index(job):
    """一次性列出目标目录已有文件：避免每个文件都单独 stat（每次 rc 都是一次 docker exec）。

    v3.91：失败不再静默当空目录。旧版任何异常都 `return {}`，等于告诉上层「目录里
    什么都没有」，于是已下好的文件全被判成缺失、整批重下（v3.88 那批重复下载的温床）。
    现在：目录不存在仍按空处理（新任务首次下载是正常的），其余错误重试三次后显式抛错，
    由工作线程把任务标成 error 并说明原因，绝不拿「空目录」糊弄过去。"""
    last = ""
    for i in range(3):
        try:
            r = rc("operations/list", fs=job["dest"], remote="", recurse=True,
                   _timeout=300, _proxy=bool(job.get("proxy")))
            if isinstance(r, dict) and r.get("error"):
                raise RuntimeError(str(r["error"])[:200])
            return {str(e.get("Path") or ""): int(e.get("Size") or 0)
                    for e in (r.get("list") or []) if isinstance(e, dict) and not e.get("IsDir")}
        except Exception as e:
            last = str(e)
            if _is_missing_dir_error(last):
                return {}           # 目标目录还没建：合法空集合，不是故障
            if i < 2:
                time.sleep(INDEX_RETRY_DELAYS[i])
    raise RuntimeError("目标目录索引失败，无法确认哪些文件已下载（为避免重复拷贝已中止任务）：%s"
                       % mask_secret(last[:200]))


def stall_limits(s):
    """v3.95：从设置里取「龟速让位」阈值，返回 (最低速度 B/s, 连续秒数)。

    stallSeconds 由 _norm_settings 钳到 30~600；这里再兜一次，避免绕过接口直接改文件。
    """
    try:
        secs = int(float((s or {}).get("stallSeconds") or STALL_SECONDS))
    except Exception:
        secs = STALL_SECONDS
    return STALL_MIN_SPEED, max(30, min(600, secs))


def split_round2(todo):
    """v3.95：第二轮拆成「没让位过的」和「让位过的」两批。

    实测（2026-11-23）：4 个并发槽里 2 个被 0.01~0.02 MB/s 的文件长期占住，
    合计吞吐被腰斩。让位过的文件单独走尾池，先让没让位过的吃满并发。
    """
    return ([e for e in todo if not e.get("slow")],
            [e for e in todo if e.get("slow")])


def tail_workers(s, workers):
    """v3.96：尾池该开几个槽——按「账号连接预算」算，不是固定 1 个。

    尾池开跑时快文件已经全下完了，只剩慢文件，此时固定 1 个槽等于把 16 连接预算
    只用了 4 条。实测（2026-11-23 收尾段）：1 个槽只剩 0.28~0.37 MB/s，
    而 5 个慢文件并发能到 1.5 MB/s 上下。
    上限仍受 AUTOTUNE_MAX_CONNS 约束：槽数 × 每文件流数 ≤ 16，超了服务端会猛限速。
    """
    try:
        mts = max(1, int((s or {}).get("multiThreadStreams") or 1))
    except Exception:
        mts = 4
    budget = max(1, AUTOTUNE_MAX_CONNS // mts)
    return max(1, min(int(workers or 1), budget))


def _probe_source_dead(job, rel):
    """v4.06：零字节久候后，直接问一次源端有没有直链，给个确定结论。

    正常下载时这个函数不会被调用（只有累计字节为 0 才探）。返回空串表示没结论，
    调用方继续按原逻辑跑；返回非空就是「这个文件确实拿不到」，直接判失败。

    为什么不靠 rclone 的报错：它对 `GET /drive/v1/files/{id}` 有低层重试，
    几分钟内不会把 `can't download: no link` 抛给上层；等看门狗把它停掉，
    留下的反而是我们自己造成的 `context canceled`，排障时非常误导。
    """
    try:
        if job.get("single_file"):
            path = str(rel).strip("/")
        else:
            sub = str(job.get("subpath") or "").strip("/")
            path = (sub + "/" + str(rel)).strip("/") if sub else str(rel).strip("/")
        r = pk_play_info(job.get("remote"), path)
        err = str((r or {}).get("error") or "")
        if err and ("直链" in err or "找不到这个文件" in err):
            return err[:200]
        return ""
    except Exception:
        return ""


def slow_tail_hopeless(elapsed, grown, max_seconds=SLOW_TAIL_MAX_SECONDS,
                       max_bytes=SLOW_TAIL_MAX_BYTES, zero_seconds=ZERO_TAIL_SECONDS):
    """v3.95：尾池里的文件是否该认输（继续跑下去只会让任务永远停在 running）。

    v4.06 追加「零字节早退」：grown 还是 0 说明源对象根本没打开成功，
    再怎么等也不会突然开始传，等到 10 分钟就认输，比原来干等 30 分钟有用得多。
    """
    if grown <= 0 and elapsed >= zero_seconds:
        return True
    return elapsed >= max_seconds and grown < max_bytes


def _copy_one_async(job, entry, s, proxy, speed_of=None, allow_defer=True, peer_speed_of=None):
    """单文件复制走 rclone 异步 job：暂停能真正中断，龟速能让位。

    旧的同步 copyfile 会把工作线程死死堵在一次 HTTP 调用里，stop 只置事件、
    remove 只删记录，谁都打不断它；反复暂停/重建后残留 copyfile 越积越多，
    实测几十条连接互抢带宽，把每条都压到几 KB/s（面板设 2 并发、实际 8 条以上）。
    现在拿到 jobid 后轮询 job/status：
      - 每秒检查暂停事件，一旦暂停立刻 job/stop，传输真正被取消，返回 pending；
      - 注入 _job_config（含 multi-thread-streams），与旧式任务参数保持一致；
      - v3.91：speed_of 给出该文件当前速度，连续 STALL_SECONDS 低于 STALL_MIN_SPEED
        就取消并返回 stalled，让工作线程把它挪到第二轮，先下完跑得快的文件。
    返回 ("done","") / ("pending","") / ("stalled","原因") / ("hopeless","原因")；
    可重试错误继续抛异常。"""
    rel, safe = entry["src"], entry["dst"]
    min_speed, stall_secs = stall_limits(s)
    cfg = _job_config(s)
    # 单文件 copyfile 用不上 Transfers/Checkers，真正影响速度的是分块并发。
    # 旧版这里完全不注入，设置页的 multi-thread-streams 对目录任务等于摆设；
    # 即使高级模式关着也补上（cutoff 以下的小文件 rclone 自己不会分块）。
    cfg.setdefault("MultiThreadStreams", max(1, int(s.get("multiThreadStreams") or 1)))
    cfg.setdefault("MultiThreadCutoff", max(1, int(s.get("multiThreadCutoffMB") or 1)) * 1048576)
    r = rc("operations/copyfile", srcFs=job["src"], srcRemote=rel,
           dstFs=job["dest"], dstRemote=safe, _async="true",
           _group=job_group_of(job["id"]),      # v4.15：按任务分组，停止时能一次全停
           _config=json.dumps(cfg),
           _timeout=120, _proxy=proxy, _strict=True)
    jid = r.get("jobid")
    if not jid:
        return "done", ""          # 对端忽略 _async、已同步完成（兼容旧行为）
    deadline = time.time() + FILE_RC_TIMEOUT
    low_since = None
    tail_start = time.time()
    tail_grown = 0.0
    seen_grown = 0.0
    probed = False
    while True:
        if _safe_requested_stop(job["id"]):
            try:
                rc("job/stop", jobid=jid, _timeout=10, _proxy=proxy)
            except Exception:
                pass                # 取消失败也要退出：任务已暂停，不能继续占着 worker
            return "pending", ""
        st = rc("job/status", jobid=jid, _timeout=20, _proxy=proxy, _strict=True)
        if st.get("finished"):
            if st.get("success"):
                return "done", ""
            raise RuntimeError("rc operations/copyfile: %s" % str(st.get("error") or "未知错误")[:300])
        if time.time() > deadline:
            try:
                rc("job/stop", jobid=jid, _timeout=10, _proxy=proxy)
            except Exception:
                pass
            raise RuntimeError("rc operations/copyfile: 超过 %d 秒未完成" % FILE_RC_TIMEOUT)
        if speed_of:
            sp = speed_of(rel)
            if sp is not None:
                seen_grown += max(0.0, sp) * COPY_POLL_INTERVAL
                if allow_defer and sp < min_speed:
                    peer = peer_speed_of() if peer_speed_of else None
                    if peer is not None and peer < min_speed * 2:
                        low_since = None    # 本任务所有连接都慢：账号/网络整体限速，让位没意义
                    elif low_since is None:
                        low_since = time.time()
                    elif time.time() - low_since >= stall_secs:
                        try:
                            rc("job/stop", jobid=jid, _timeout=10, _proxy=proxy)
                        except Exception:
                            pass
                        return ("stalled", "连续 %d 秒低于 %d KB/s，已让位给其它文件，稍后重试"
                                % (stall_secs, min_speed // 1024))
                elif allow_defer:
                    low_since = None
                else:
                    # v3.95：第二轮不再让位，但也不能无限期挂着——30 分钟推不动 1MB 就认输，
                    # 否则 2 个龟速文件会让任务永远停在 running、进度永远差最后几 GB。
                    tail_grown += max(0.0, sp) * COPY_POLL_INTERVAL
                    if slow_tail_hopeless(time.time() - tail_start, tail_grown):
                        try:
                            rc("job/stop", jobid=jid, _timeout=10, _proxy=proxy)
                        except Exception:
                            pass
                        return ("hopeless", "尾池 %d 分钟内只传入 %.1f MB，判定该文件源端不可达"
                                % (SLOW_TAIL_MAX_SECONDS // 60, tail_grown / 1048576.0))

        # v4.06：一分钟一个字节都没进来 → 主动问一次源端，别让它一直悬着。
        # 命中「无直链」就立刻判失败：这比等 rclone 的低层重试快得多，
        # 也不会再触发「停了又起、起了又停」的循环。
        if (not probed) and seen_grown <= 0 and (time.time() - tail_start) >= ZERO_PROBE_SECONDS:
            probed = True
            dead = _probe_source_dead(job, rel)
            if dead:
                try:
                    rc("job/stop", jobid=jid, _timeout=10, _proxy=proxy)
                except Exception:
                    pass
                return "hopeless", dead

        time.sleep(COPY_POLL_INTERVAL)


def transfer_one(job, entry, s, existing, made_dirs, xlock=None, speed_of=None,
                 allow_defer=True, peer_speed_of=None):
    """同步复制单个文件：源端原名 → 目标端安全名。返回 (status, error)。

    existing 是目标目录快照（会被本函数就地更新），made_dirs 缓存已建目录。
    两者被线程池里的多个 worker 共享，读写都必须走 xlock —— 否则一边遍历
    casefold 一边有别的线程 pop，会抛 dictionary changed size during iteration。
    speed_of(name) 由工作线程提供，返回该文件的实时速度（未知即 None）；
    allow_defer=False 时不再因龟速让位（第二轮兜底，必须下完）。"""
    xlock = xlock or threading.Lock()
    proxy = bool(job.get("proxy"))
    src_fs, dst_fs = job["src"], job["dest"]
    rel, safe, size = entry["src"], entry["dst"], int(entry.get("size") or 0)
    # 1) 已存在文件：同大小跳过；不同大小先改名保留再重下，绝不静默覆盖
    with xlock:
        exists = safe in existing
        if exists and int(existing[safe]) == size:
            log_event(job["id"], "skip", file=safe, size=size, why="同名同大小已存在")
            return "skipped", ""
        # v3.88：兼容历史任务按「源原名」落盘的文件。v3.83 之前的老任务不做安全命名，
        # 文件名原样落盘；新任务按 safe 名（截断 + ~hash）落盘，两者名字不同，
        # 只看 safe 名会把已下好的文件判成「缺失」而整批重下（实测 5 对、约 4 GB）。
        # 这里额外认一次「源相对路径同名 + 同大小」：命中即视为已下载，不重复传。
        if not exists and rel != safe and rel in existing and int(existing[rel]) == size:
            log_event(job["id"], "skip", file=rel, size=size, why="源原名同大小已存在")
            return "skipped", ""
        if not exists and safe.casefold() in {p.casefold() for p in existing}:
            return "failed", "目标目录已存在大小写相近的同名文件，为避免覆盖已跳过，请手动处理后重试"
        if exists:
            conflict = "%s.conflict-%s" % (safe, time.strftime("%Y%m%d%H%M%S"))
            try:
                rc("operations/movefile", srcFs=dst_fs, srcRemote=safe,
                   dstFs=dst_fs, dstRemote=conflict, _timeout=120, _proxy=proxy, _strict=True)
                existing[conflict] = existing.pop(safe)
            except Exception as e:
                return "failed", "目标有同名不同大小的文件，保留旧文件时改名失败：%s" % mask_secret(str(e)[:140])
        parent = safe.rpartition("/")[0]
        need_mkdir = bool(parent) and parent not in made_dirs
        if need_mkdir:
            made_dirs.add(parent)
    if need_mkdir:
        try:
            rc("operations/mkdir", fs=dst_fs, remote=parent,
               _timeout=60, _proxy=proxy, _strict=True)
        except Exception:
            pass            # 目录多半已存在；真建不出来会在 copyfile 报出具体原因
    # 2) 复制：瞬时 / 未知错误按 2s / 8s / 30s 退避重试，确定性错误立刻归类
    log_event(job["id"], "copy", file=safe, size=size, src=rel)
    attempts = max(0, int(s.get("fileRetryMax") or 0)) + 1
    last = ""
    for i in range(attempts):
        if _safe_requested_stop(job["id"]):
            return "pending", ""
        try:
            st_, _ = _copy_one_async(job, entry, s, proxy, speed_of=speed_of,
                                     allow_defer=allow_defer, peer_speed_of=peer_speed_of)
            if st_ == "pending":
                return "pending", ""       # 暂停时已 job/stop 中断，保留待续传状态
            if st_ == "stalled":
                return "stalled", _
            if st_ == "hopeless":
                return "failed", _         # 不重试：重试只会再挂 30 分钟
            with xlock:
                existing[safe] = size
            if job.get("mode") == "move":
                try:        # 只有复制成功才删云端源；删失败不抹掉下载本身的成功
                    rc("operations/deletefile", fs=src_fs, remote=rel,
                       _timeout=120, _proxy=proxy, _strict=True)
                except Exception as e:
                    return "done", "已下载，但删除云端源文件失败：%s" % mask_secret(str(e)[:120])
            return "done", ""
        except Exception as e:
            last = str(e)
            kind, friendly = classify_error(last, context=rel)
            if kind == "FILE_FATAL":
                return "failed", friendly
            if kind == "ACCOUNT_FATAL":
                st_ = _fatal_job_error(last)
                raise _JobAbort(st_[0] if st_ else "error", friendly)
            if kind == "DISK_FATAL":
                raise _JobAbort("stopped", friendly + "；已停止任务，处理后点「继续」")
            if i < attempts - 1:
                time.sleep(SAFE_BACKOFF[min(i, len(SAFE_BACKOFF) - 1)])
    return "failed", "重试 %d 次仍失败：%s" % (attempts - 1, mask_secret(last[:200]))


# ────────────────────────────────────────────────────────────────────────────
# v4.04 下载后自动转封装（MPEG-TS → MP4）
#
# 起因：用户报「下载的文件不能转化为 mp4」。实测 PikPak 上有一批资源本身就是
# HLS/m3u8 转出来的 MPEG-TS 流，源端连扩展名都不带 —— 名字映射清单里的 original
# 就是证据：「…#绳缚日记1…极光白丝」（无扩展名）与「…#闪闪工作室…女俘虏.mp4」。
# 落盘后播放器认不出类型，看着就像「下载坏了 / 转不了 mp4」。
#
# 这批 TS 的视频是 H.264、音频是 AAC，正好是 MP4 的原生组合 → `-c copy` 无损
# 转封装即可，不重编码、画质零损失。2026-11-23 实测：1.90 GB 用时 34 秒，
# 输出 1.75 GB（小 8%，去掉了 TS 每包 188 字节的封装开销），时长 3471.700000 秒
# 与源完全一致。
#
# 三条硬规矩：
#   1. 只处理「文件头确实是 TS」的，别的类型一律不碰；
#   2. 先写 .part 再原子改名，中途断电不会留下「看起来已完成」的半截 mp4；
#   3. 任何失败都保留源文件，绝不因为「转不动」把已经下好的东西弄丢。
# ────────────────────────────────────────────────────────────────────────────
REMUX_Q = queue.Queue()        # (job_id, 绝对路径)
REMUX_DONE = set()             # 已入队/已处理的路径，防重复任务重复转
REMUX_LOCK = threading.Lock()
REMUX_STAT = {"ok": 0, "fail": 0, "skip": 0, "running": ""}
REMUX_THREAD = None
_FFMPEG = {"probed": False, "bin": None, "why": "", "at": 0.0}
REMUX_SEEN_MAX = 5000          # 去重集合上限，超过就清空（任务记录本身也会老化）


def remux_public():
    """给前端的转封装状态（jobs 轮询里带，不额外接口）。"""
    with REMUX_LOCK:
        st = dict(REMUX_STAT)
    st["queued"] = REMUX_Q.qsize()
    st["ffmpeg"] = ffmpeg_bin() or ""
    st["why"] = _FFMPEG.get("why") or ""
    st["root"] = DOWNLOAD_ROOT
    return st


def _quiet_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def ffmpeg_bin():
    """返回可用的 ffmpeg 路径；没有就返回 None（调用方降级，不抛异常）。

    容器是 python:3.12-alpine，ffmpeg 需要 apk 安装。探测结果要缓存（避免每个文件
    都走一次 PATH 查找），但**失败不能永久缓存**——否则先起服务、后装 ffmpeg 时，
    进程会一直记着「没有」，只能重启才生效。所以负结果只记 60 秒。
    """
    now = time.time()
    if _FFMPEG["probed"] and (_FFMPEG["bin"] or now - _FFMPEG["at"] < 60):
        return _FFMPEG["bin"]
    _FFMPEG["probed"] = True
    _FFMPEG["at"] = now
    _FFMPEG["bin"] = None
    for cand in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg",
                 "/opt/ffmpeg/ffmpeg", "/app/bin/ffmpeg"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            _FFMPEG["bin"] = cand
            break
    if not _FFMPEG["bin"]:
        _FFMPEG["bin"] = shutil.which("ffmpeg")
    if not _FFMPEG["bin"]:
        _FFMPEG["why"] = "容器内没有 ffmpeg（apk add ffmpeg 或重建镜像后可用）"
    else:
        _FFMPEG["why"] = ""
    return _FFMPEG["bin"]


def ffprobe_bin():
    """ffprobe 一般和 ffmpeg 同目录（alpine 的 ffmpeg 包里都带）。"""
    exe = ffmpeg_bin()
    if exe:
        cand = os.path.join(os.path.dirname(exe), "ffprobe")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which("ffprobe")


def media_duration(path, is_ts=None):
    """读时长（秒）；拿不到就返回 0.0。"""
    exe = ffprobe_bin()
    if not exe or not path:
        return 0.0
    cmd = [exe, "-v", "error"]
    if is_ts:
        cmd += ["-f", "mpegts", "-probesize", "8M", "-analyzeduration", "8M"]
    cmd += ["-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
        return float((p.stdout or b"").decode("utf-8", "replace").strip() or 0)
    except Exception:
        return 0.0


def zero_holes(path, samples=9, pack=188):
    """抽样查找「全零区段」，返回命中的位置比例列表。

    为什么要这一步：2026-11-23 那个「转不出 mp4」的文件，`ls -l` 尺寸完整、
    `du` 也不是稀疏文件，但 0.75 GB → 3.2 GB 全是写进去的零字节。
    ffmpeg 会在那里反复找同步字节（日志 `max resync size reached`），
    转出一个小体积 mp4 就卡死 —— 用户看到的「转不了 mp4」，本质是文件本身是坏的。
    所以转封装前先抽查，坏文件直接点名报错，不要产出「看起来能播、其实只有前 10 分钟」的东西。
    """
    try:
        size = os.path.getsize(path)
    except Exception:
        return []
    if size < pack * 8:
        return []
    out = []
    win = pack * 200
    with open(path, "rb") as f:
        for i in range(1, samples + 1):
            frac = i / (samples + 1.0)
            off = max(0, int(size * frac) // pack * pack)
            try:
                f.seek(off)
                b = f.read(win)
            except Exception:
                continue
            if not b:
                continue
            if not any(b):                       # 整窗全零 = 空洞
                out.append(round(frac, 3))
    return out


def sniff_media_container(path):
    """只读文件头判断容器类型，返回 'ts' / 'mp4' / None。

    TS 用两个 sync 字节确认：第 0 字节是 0x47，且第 188 字节（TS 包长）也是 0x47。
    只读 400 字节，不用 ffprobe 扫全文件 —— 几 GB 的文件逐个 probe 会把控制台拖死。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(400)
    except Exception:
        return None
    if not head:
        return None
    if len(head) >= 200 and head[0] == 0x47 and head[188] == 0x47:
        return "ts"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "mp4"
    return None


def remux_to_mp4(path, s=None, timeout=None):
    """TS → MP4 无损转封装。返回 (ok, out_path, msg)。

    只取 v:0 / a:0（TS 里常见一个 bin_data 私有流，MP4 装不下，带上会失败）。
    """
    s = s or DEFAULTS
    exe = ffmpeg_bin()
    if not exe:
        return False, "", _FFMPEG.get("why") or "容器内没有 ffmpeg"
    src = str(path)
    if not os.path.isfile(src):
        return False, "", "源文件不存在"
    low = src.lower()
    base = src[:-3] if low.endswith(".ts") else src
    out = base + ".mp4"
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return True, out, "已存在"
    part = out + ".part"
    _quiet_remove(part)
    kind = sniff_media_container(src)
    if kind == "ts":
        holes = zero_holes(src)
        if holes:
            # 坏文件不转：否则会得到一个「只有前半段」的 mp4，比没有更坑
            return False, "", ("源文件中间有空洞（约 %d%% 处整段是 0 字节），转了也只有前半段能播；"
                              "这个文件需要重新下载" % int(holes[0] * 100))
    try:
        tmo = int(timeout or s.get("remuxTimeoutSec") or 1800)
    except Exception:
        tmo = 1800
    cmd = [exe, "-hide_banner", "-v", "error", "-nostdin", "-y",
           "-i", src, "-map", "0:v:0", "-map", "0:a:0",
           "-c", "copy", "-f", "mp4", part]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=tmo)
    except subprocess.TimeoutExpired:
        _quiet_remove(part)
        return False, "", "转封装超时（%d 秒），源文件保留" % tmo
    except Exception as e:
        _quiet_remove(part)
        return False, "", "无法启动 ffmpeg：%s" % mask_secret(str(e)[:160])
    if p.returncode != 0:
        tail = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        _quiet_remove(part)
        return False, "", "ffmpeg 退出码 %d：%s" % (p.returncode, (" / ".join(tail) or "无输出")[:200])
    if not os.path.isfile(part) or os.path.getsize(part) <= 0:
        _quiet_remove(part)
        return False, "", "ffmpeg 没有产出文件"
    # 第二道闸：时长对不上就是转漏了（源被截断 / 中途坏段），别把半个文件当成品
    ds = media_duration(src, is_ts=(kind == "ts"))
    do = media_duration(part)
    if ds >= 30 and do >= 1 and do < ds * 0.98:
        _quiet_remove(part)
        return False, "", "转出不完整（%.0f 秒 / 源 %.0f 秒），源文件可能被截断" % (do, ds)
    try:
        os.replace(part, out)          # 原子改名：读到的要么是完整 mp4，要么不存在
    except Exception as e:
        _quiet_remove(part)
        return False, "", "改名失败：%s" % mask_secret(str(e)[:160])
    return True, out, ""


def _remux_worker():
    """串行消费转封装队列。单线程是有意的：转封装是磁盘密集操作，
    和下载抢 IO 只会把两边都拖慢，串行反而最快也最稳。"""
    while True:
        item = REMUX_Q.get()
        jid, src = (item if isinstance(item, tuple) else (None, item))
        try:
            with REMUX_LOCK:
                REMUX_STAT["running"] = os.path.basename(src)[:80]
            s = load_settings()
            if not _truthy(s.get("autoRemuxMp4", True)):
                continue
            kind = sniff_media_container(src)
            if kind != "ts":
                with REMUX_LOCK:
                    REMUX_STAT["skip"] += 1
                continue
            ok, out, msg = remux_to_mp4(src, s)
            if ok:
                with REMUX_LOCK:
                    REMUX_STAT["ok"] += 1
                log_event(jid, "remux", file=os.path.basename(src)[:80],
                          out=os.path.basename(out)[:80], skip=(msg == "已存在"))
                # 默认保留源文件：用户确认能播之前，绝不替人做删除决定
                if not _truthy(s.get("remuxKeepSource", True)):
                    _quiet_remove(src)
            else:
                with REMUX_LOCK:
                    REMUX_STAT["fail"] += 1
                log_event(jid, "remux_fail", file=os.path.basename(src)[:80], why=msg[:160])
        except Exception:
            pass
        finally:
            with REMUX_LOCK:
                REMUX_STAT["running"] = ""
            try:
                REMUX_Q.task_done()
            except Exception:
                pass


def _ensure_remux_worker():
    global REMUX_THREAD
    with REMUX_LOCK:
        if REMUX_THREAD is None or not REMUX_THREAD.is_alive():
            REMUX_THREAD = threading.Thread(target=_remux_worker, name="remux", daemon=True)
            REMUX_THREAD.start()
    return REMUX_THREAD


def enqueue_remux(job_id, path):
    """把落盘文件丢进转封装队列（幂等：同一路径只排一次）。"""
    if not path:
        return False
    try:
        key = os.path.abspath(str(path))
    except Exception:
        return False
    with REMUX_LOCK:
        if key in REMUX_DONE:
            return False
        if len(REMUX_DONE) > REMUX_SEEN_MAX:
            REMUX_DONE.clear()
        REMUX_DONE.add(key)
    _ensure_remux_worker()
    REMUX_Q.put((job_id, key))
    return True


def _schedule_remux_for_job(job, s=None):
    """任务进入 done 时补排单文件任务的产物。

    v2 目录任务在「每个文件下完」时就已经单独入队了，这里只补单文件任务；
    旧式 sync/copy 任务拿不到准确落盘名（v3.82 及以前建的老任务），直接跳过。
    """
    s = s or load_settings()
    if not _truthy(s.get("autoRemuxMp4", True)):
        return False
    if not job.get("single_file"):
        return False
    try:
        root = local_dest(str(job.get("dst_dir") or ""))
        name = job.get("safe_leaf") or job.get("leaf") or ""
        if not root or not name:
            return False
        return enqueue_remux(job.get("id"), os.path.join(root, name))
    except Exception:
        return False


def remux_candidates(root, maxdepth=3, limit=2000):
    """列出 root 下「像 TS 但还不是 mp4」的文件。

    判据分两级，先便宜后昂贵：
      1. 名字层：没有扩展名，或扩展名是 .ts/.m2ts/.mts（PikPak 的 HLS 资源就这样）；
      2. 内容层：读前 400 字节确认 0x47 同步头 —— 名字是 .mp4 但其实是 TS 的也有。
    已经有同名 .mp4 的直接跳过，避免重复转。
    """
    out = []
    if not root or not os.path.isdir(root):
        return out
    for cur, dirs, files in os.walk(root):
        # 用 relpath 算层级：直接数 "/" 在 Windows 上永远是 0（测试跑在本机）
        rel = os.path.relpath(cur, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= maxdepth:
            dirs[:] = []
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for name in files:
            if name.startswith(".") or name.endswith((".part", ".partial", ".mp4")):
                continue
            low = name.lower()
            ext = os.path.splitext(low)[1]
            if ext and ext not in (".ts", ".m2ts", ".mts"):
                continue                      # .mkv/.avi 之类不归这里管
            full = os.path.join(cur, name)
            mp4 = full + ".mp4" if not low.endswith(".ts") else full[:-3] + ".mp4"
            if os.path.exists(mp4):
                continue
            try:
                if os.path.getsize(full) < 188 * 4:
                    continue
            except Exception:
                continue
            if sniff_media_container(full) != "ts":
                continue
            out.append(full)
            if len(out) >= limit:
                return out
    return out


def remux_scan(root, maxdepth=3):
    """手动把某个目录下已下载的 TS 补转成 mp4（给历史文件用）。"""
    files = remux_candidates(root, maxdepth=maxdepth)
    n = 0
    for f in files:
        if enqueue_remux(None, f):
            n += 1
    return {"found": len(files), "queued": n,
            "ffmpeg": bool(ffmpeg_bin()), "why": _FFMPEG.get("why") or ""}


def _safe_dir_worker(job_id, snapshot=None):
    job = snapshot or _xfer_fetch(job_id)
    if not job:
        return
    s = autotune_profile_for(load_settings(), job_id)   # v3.94：套用实测最优档
    vol = job_volume(job)

    def bail(state, msg):
        _xfer_patch(job_id, state=state, error=str(msg)[:400], phase="",
                    speed=0, eta=None, curfile="")
        # v4.15：进入终态必须把本任务名下的异步 job 全停掉，否则残留 job 会继续吃带宽，
        # 面板就出现「任务出错/停止，但还在跑流量」，同时 bytes 一直涨还会引爆重复通知。
        try:
            stop_job_group(job_id, bool(job.get("proxy")))
        except Exception:
            pass
        flush_name_maps()

    try:
        try:
            files = expand_source_files(job)
        except Exception as e:
            st_ = _fatal_job_error(str(e))
            return bail(st_[0] if st_ else "error",
                        st_[1] if st_ else ("扫描源目录失败：%s" % mask_secret(str(e)[:200])))
        entries = build_name_map(files, job.get("remote"), job.get("subpath") or "", s)
        total = sum(e["size"] for e in entries)
        plan = {"version": 2, "remote": job.get("remote"), "src": job.get("src"),
                "dest": job.get("dest"), "entries": entries}
        # 续传：同一次任务的计划文件还在，就把已完成的文件状态接回来（映射不漂移）
        prev = load_file_plan(job_id)
        if prev and prev.get("src") == plan["src"] and prev.get("dest") == plan["dest"]:
            old = {e.get("src"): e for e in prev["entries"]}
            for e in entries:
                o = old.get(e["src"])
                if o and o.get("dst") == e["dst"] and o.get("status") in ("done", "skipped"):
                    e["status"] = o["status"]
        save_file_plan(job_id, plan)
        if not entries:
            return bail("done", "源目录为空，或文件全被过滤规则跳过，没有需要下载的内容")

        state = {"bytes": sum(e["size"] for e in entries if e["status"] in ("done", "skipped")),
                 "lastPatch": 0.0, "lastPlan": 0.0, "lastBytes": 0, "lastTs": time.time(),
                 "active": {}, "abort": None, "live": {}, "allowDefer": True}
        state["lastBytes"] = state["bytes"]
        try:
            with SAFE_JOBS_LOCK:
                SAFE_LIVE[job_id] = state          # v3.92：详情页从这里读在传字节/速度
        except Exception:
            pass
        plock = threading.Lock()
        try:
            existing = _dest_index(job)
        except Exception as ie:
            # v3.91：索引失败绝不能当空目录继续——那会把已下好的文件整批重下。
            return bail("error", str(ie))
        try:      # v3.93：顺手清掉 24h 以上的孤儿分片（正在传的 mtime 是新的，不会误删）
            n_orphan, freed = cleanup_orphan_partials(local_dest(str(job.get("dest") or "")))
            if n_orphan:
                log_event(job_id, "cleanup", partials=n_orphan, freed_mb=round(freed / 1048576.0, 2))
        except Exception:
            pass
        made_dirs = set()
        xlock = threading.Lock()

        def patch(force=False):
            with plock:
                now = time.time()
                if not force and now - state["lastPatch"] < 1.0:
                    return
                dt = max(0.001, now - state["lastTs"])
                speed = max(0.0, (state["bytes"] - state["lastBytes"]) / dt)
                state["lastTs"], state["lastBytes"], state["lastPatch"] = now, state["bytes"], now
                done = sum(1 for e in entries if e["status"] in ("done", "skipped"))
                failed = sum(1 for e in entries if e["status"] == "failed")
                # v3.90：面板字节数与速度要算「正在传」的部分。旧版只统计 done/skipped，
                # 大文件传到 99% 面板也一动不动；速度改成 rclone 实测值，避免
                # dt 被压到 0.001s 时算出几十 GB/s 的假速度。
                live = dict(state.get("live") or {})
                inflight = sum(v[0] for v in live.values())
                live_speed = sum(v[1] for v in live.values())
                shown = state["bytes"] + inflight
                if live_speed > 0:
                    speed = live_speed
                cur = next((v[2] for v in live.values()), "") or next(iter(state["active"].values()), "")
                fields = dict(bytes=shown, total=total, speed=round(speed, 1),
                              eta=(max(0, total - shown) / speed if speed > 0 else None),
                              filesTotal=len(entries), filesDone=done, filesFailed=failed,
                              files=len(live) or len(state["active"]),
                              curfile=cur)
                if force or now - state["lastPlan"] > 5.0:
                    state["lastPlan"] = now
                    save_file_plan(job_id, plan)
            _xfer_patch(job_id, **fields)
            flush_name_maps()

        # v3.90：定时采样 core/stats，把本任务正在传的文件字节/速度喂给 patch。
        # 没有它 patch 只会在文件结束时触发，计划文件几十分钟不更新、curfile 停在旧名字。
        live_stop = threading.Event()
        name_of = {}
        for e in entries:
            name_of[e["src"]] = e["src"]
            name_of[e["dst"]] = e["src"]

        def sample_live():
            while not live_stop.is_set():
                try:
                    st = rc("core/stats", _timeout=15, _proxy=bool(job.get("proxy")))
                    live = {}
                    for t in (st.get("transferring") or []):
                        nm = str(t.get("name") or "")
                        key = name_of.get(nm)
                        if key:
                            live[key] = (int(t.get("bytes") or 0), float(t.get("speed") or 0), nm)
                    with plock:
                        state["live"] = live
                    patch()
                except Exception:
                    pass            # 采样失败不影响下载本身，下一轮再试
                live_stop.wait(LIVE_SAMPLE_INTERVAL)


        def speed_of(name):
            """给 transfer_one 用的实时速度查询：直接读采样线程的结果，不再单独发 rc。"""
            with plock:
                v = (state.get("live") or {}).get(name)
            return v[1] if v else None

        def peer_speed_of():
            """本任务当前最快的一条连接：它也很慢时说明是账号级限速，降级毫无意义。"""
            with plock:
                vals = [v[1] for v in (state.get("live") or {}).values()]
            return max(vals) if vals else None

        def run_one(e):
            if state["abort"] is not None or _safe_requested_stop(job_id):
                return                      # 保持 pending，下次继续从这里接着下
            e["status"] = "running"
            with plock:
                state["active"][e["src"]] = e["src"]     # curfile 始终显示原始相对路径
            try:
                st, err = transfer_one(job, e, s, existing, made_dirs, xlock,
                                       speed_of=speed_of,
                                       allow_defer=bool(state.get("allowDefer")),
                                       peer_speed_of=peer_speed_of)
                if st == "stalled":
                    e["status"], e["error"] = "deferred", err or ""
                    e["slow"] = True               # v3.95：第二轮只给它 1 个槽
                    e["attempts"] = int(e.get("attempts") or 0) + 1
                    log_event(job_id, "stalled", file=e["dst"], size=e["size"],
                              why=err or "连续低速", attempts=e["attempts"])
                    record_file_state(vol, job.get("remote"), e["srcFull"], safe=e["dst"],
                                      original=e["srcFull"], size=e["size"], status="deferred",
                                      attempts=e["attempts"], error=e["error"])
                    return
                e["status"] = "pending" if st == "pending" else st
                e["error"] = err or ""
                e["attempts"] = int(e.get("attempts") or 0) + 1
                if st in ("done", "skipped"):
                    with plock:
                        state["bytes"] += e["size"]
                    log_event(job_id, st, file=e["dst"], size=e["size"])
                    # v4.04：刚下完的文件立刻排队探测容器。是 MPEG-TS 就无损转成 mp4
                    # （PikPak 有一批 HLS 转出的资源，源端连扩展名都没有）。
                    if st == "done" and _truthy(s.get("autoRemuxMp4", True)):
                        try:
                            _root = local_dest(str(job.get("dest") or ""))
                            if _root:
                                enqueue_remux(job_id, os.path.join(_root, e["dst"]))
                        except Exception:
                            pass
                elif st == "failed":
                    log_event(job_id, "failed", file=e["dst"], size=e["size"], why=e["error"][:120])
                record_file_state(vol, job.get("remote"), e["srcFull"], safe=e["dst"],
                                  original=e["srcFull"], size=e["size"], status=e["status"],
                                  attempts=e["attempts"], error=e["error"])
            except _JobAbort as ex:
                state["abort"] = ex
                e["status"], e["error"] = "failed", ex.msg
                ev = _safe_stop_event(job_id)
                if ev:
                    ev.set()      # 让其余文件尽快退出
                # v4.15：光置事件不够 —— 在飞的异步 job 要按组一起取消，否则它们继续传，
                # 任务已经 error 了字节还在涨、流量还在走。
                try:
                    stop_job_group(job_id, bool(job.get("proxy")))
                except Exception:
                    pass
            except Exception as ex:
                e["status"], e["error"] = "failed", mask_secret(str(ex)[:200])
            finally:
                with plock:
                    state["active"].pop(e["src"], None)
                patch()

        _xfer_patch(job_id, state="running", phase="copy", total=total,
                    bytes=state["bytes"], filesTotal=len(entries), error="")
        workers = max(1, min(8, int(s.get("transfers") or 4)))
        sampler = threading.Thread(target=sample_live, daemon=True)
        sampler.start()
        try:
            for rnd in (0, 1):
                todo = [e for e in entries if e["status"] == "pending"]
                if not todo:
                    break
                if rnd == 1:
                    if _safe_requested_stop(job_id):
                        break
                    # 第一轮因「龟速」让位的文件，第二轮必须下完：不再降级。
                    for e in entries:
                        if e["status"] == "deferred":
                            e["status"], e["error"] = "pending", ""
                    state["allowDefer"] = False
                    _xfer_patch(job_id, phase="copy", error="", speed=0, curfile="")
                    todo = [e for e in entries if e["status"] == "pending"]
                    # v3.95：让位过的文件单独走「慢速尾池」（1 个槽），其余仍用全池。
                    fast, slow = split_round2(todo)
                    if fast:
                        with ThreadPoolExecutor(max_workers=workers) as pool:
                            list(pool.map(run_one, fast))
                    if slow and not _safe_requested_stop(job_id):
                        tail = tail_workers(s, workers)
                        log_event(job_id, "slowtail", files=len(slow), workers=tail)
                        with ThreadPoolExecutor(max_workers=tail) as pool:
                            list(pool.map(run_one, slow))
                    continue
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    list(pool.map(run_one, todo))
        finally:
            live_stop.set()
            sampler.join(timeout=LIVE_SAMPLE_INTERVAL + 2)
            with plock:
                state["live"] = {}          # 收尾后不再有「在传」分量，避免终态字节虚高
            # v4.15：无论 done / stopped / error，收尾都把本任务剩下的 job 组清掉
            try:
                stop_job_group(job_id, bool(job.get("proxy")))
            except Exception:
                pass
        try:      # v3.93：rclone 建出来的 0000 权限修回来（只改 mode 为 0 的条目）
            nd, nf = fix_permissions(local_dest(str(job.get("dest") or "")))
            if nd or nf:
                log_event(job_id, "chmod", dirs=nd, files=nf)
        except Exception:
            pass
        patch(force=True)

        cur = _xfer_fetch(job_id) or {}
        if state["abort"] is not None:
            ex = state["abort"]
            return bail(ex.state, ex.msg)
        if cur.get("state") == "stopped":       # 用户点了暂停：不要覆盖成其它终态
            _xfer_patch(job_id, speed=0, eta=None, phase="", curfile="",
                        error="已暂停（点「继续」会跳过已完成文件接着传）")
            return
        # verifying：文件数与字节数对账（逐文件复制时已按大小校验过目标）
        _xfer_patch(job_id, phase="check", speed=0, eta=None, curfile="")
        done = sum(1 for e in entries if e["status"] in ("done", "skipped"))
        skipped = sum(1 for e in entries if e["status"] == "skipped")
        failed = [e for e in entries if e["status"] == "failed"]
        got = sum(e["size"] for e in entries if e["status"] in ("done", "skipped"))
        if not done and failed:
            return bail("error", "全部 %d 个文件都下载失败：%s" % (len(failed), failed[0]["error"][:200]))
        # v3.89：还有 pending 就不能标 done。暂停/续传时若旧线程的外层 copyfile 还在飞，
        # 工作线程会把整批文件「看到 stop 就原样保持 pending」，然后一路走到这里被标成
        # done —— 界面显示完成、其实还剩几十 GB 没下（实测 45 个 / 58 GB 被吞）。
        # v4.15：running 也要算「没完成」。以前只看 pending/deferred，收尾时若还有在飞的
        # 文件（status=running），任务会被标成 done —— 界面显示「209/212 完成」+ 错误行说
        # 「3 个文件失败」，但详情里 failedFiles 是空的、只重试失败项也没得可重试。
        still = unfinished_entries(entries)
        if still:
            _xfer_patch(job_id, state="stopped", phase="", speed=0, eta=None, curfile="",
                        bytes=got, total=total,
                        error="已暂停：还有 %d 个文件未完成，点「继续」接着下（已完成/已跳过的不会重下）"
                        % len(still))
            flush_name_maps()
            return
        notes = []
        if failed:
            notes.append("%d 个文件失败，其余 %d 个已完成；点「详情」查看原因并可仅重试失败项"
                         % (len(failed), done))
        if job.get("mode") == "move":
            if not failed:
                try:    # 全部成功后清空源目录里的空壳（保留根目录本身）
                    rc("operations/rmdirs", fs=job["src"], remote="", leaveRoot=True,
                       _timeout=300, _proxy=bool(job.get("proxy")))
                except Exception:
                    pass
            if skipped:
                notes.append("%d 个文件目标已存在而跳过，对应云端源文件保留未删" % skipped)
        _xfer_patch(job_id, state="done", phase="", speed=0, eta=None, curfile="",
                    bytes=got, total=total, error="；".join(notes),
                    filesDone=done, filesFailed=len(failed))
        flush_name_maps()
    except Exception as e:
        bail("error", "目录下载异常终止：%s" % mask_secret(str(e)[:250]))
    finally:
        with SAFE_JOBS_LOCK:
            SAFE_JOBS.pop(job_id, None)
            SAFE_LIVE.pop(job_id, None)
            SAFE_THREADS.pop(job_id, None)


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
    ("unauthenticated", "error", "账号凭证已被 PikPak 作废（unauthenticated），请到账号页点该账号的「重新登录」，在官方登录页过完安全验证后重新采集 Token"),
    ("invalid_grant", "error", "账号凭证已失效，请到账号页点该账号的「重新登录」重新采集 Token"),
    ("invalid_token", "error", "账号凭证已失效，请到账号页点该账号的「重新登录」重新采集 Token"),
    # v3.47：旧文案「请在账号页补全用户名」指向了一个并不存在的入口 ——
    # acctadd / import_items 都刻意只写 token+refresh_token，用户名密码会被 pop 掉，
    # 用户照着做也修不好。真实原因是 token 失效后 rclone 回退直登，配置里却没账号密码。
    ("no username", "error", "账号 Token 已失效（rclone 回退直登时缺账号密码），请到账号页点该账号的「重新登录」重新采集"),
    ("account_not_found", "error", "云端账号不存在，请检查账号配置"),
    # v3.83：文件名 / 路径超出文件系统限制是确定性失败，重试 100 次也不会成功。
    # 必须让任务立刻脱离 running，否则就是「永远下载中、0 速度、进度不动」的假死。
    # 对 v2 目录任务，这类错误在 transfer_one 里按 FILE_FATAL 只标记单个文件，不走这里。
    ("file name too long", "error", "文件名超出文件系统长度限制，已终止（v3.83 起新任务会自动安全命名后下载）"),
    ("filename too long", "error", "文件名超出文件系统长度限制，已终止（v3.83 起新任务会自动安全命名后下载）"),
    ("name too long", "error", "文件名超出文件系统长度限制，已终止（v3.83 起新任务会自动安全命名后下载）"),
    ("path too long", "error", "路径超出文件系统长度限制，已终止（可缩短保存目录名后重试）"),
    ("file name or extension is too long", "error", "文件名或扩展名太长，已终止（v3.83 起新任务会自动安全命名后下载）"),
    ("文件名或扩展名太长", "error", "文件名或扩展名太长，已终止（v3.83 起新任务会自动安全命名后下载）"),
    ("路径太长", "error", "路径超出文件系统长度限制，已终止（可缩短保存目录名后重试）"),
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
    if is_net_error(raw):
        # v3.84：连接被重置 / 超时是线路问题，交给 rclone 的低级重试与看门狗兜底。
        # 以前这类报错文本里带 captcha token，会被判成「需重新登录」的终态。
        return None
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


# v3.81：代理节点偶发 TLS 中断（UNEXPECTED_EOF_WHILE_READING / Connection reset），
# 这类瞬时抖动重试一次通常就过。以前直接把异常文本当响应返回，用户只能手动反复点
# 「解析链接」，还会被误报成「需要提取码」。同一路径内做有限次重试，不换出口。
PK_HTTP_RETRIES = 2
PK_HTTP_RETRY_DELAY = 0.35
_TRANSIENT_NET_MARKS = (
    "unexpected_eof_while_reading", "eof occurred", "eof in violation",
    "connection reset", "remotedisconnected", "connection aborted",
    "read timed out", "timed out", "urlopen error [ssl", "urlopen error [errno",
    "broken pipe", "temporarily unavailable", "connection refused",
)
_CERT_NET_MARKS = ("certificate_verify_failed", "certificate verify failed",
                   "unknown ca", "self signed", "ssl: wrong_version_number")


def _is_transient_net(msg):
    """只有网络层瞬时故障才重试；证书配置错误重试一百次也不会好。"""
    s = str(msg or "").lower()
    if any(k in s for k in _CERT_NET_MARKS):
        return False
    return any(k in s for k in _TRANSIENT_NET_MARKS)


def _pk_http(url, body=None, headers=None, method="GET", timeout=45, proxy=True,
             _tries=None):
    """先走 mihomo，失败再直连（两个方向都可能被 PikPak 拦，互为兜底）。

    v3.81：proxy 除 True/False 外还接受一个具体的代理地址字符串，只在本次请求生效，
    用于「分享解析单独走代理、下载仍直连」的双路尝试。显式指定地址时失败**不**
    静默回落直连 —— 否则调用方会误以为代理试过了，其实又走了直连。
    同一路径遇到瞬时网络抖动会自动重试 PK_HTTP_RETRIES 次（短退避），重试耗尽
    才把带「已自动重试」标记的错误交给上层，避免用户误以为系统没试过。
    """
    data = json.dumps(body).encode() if body is not None else None
    p = proxy if isinstance(proxy, str) else (pk_proxy_url() if proxy else "")
    handlers = [urllib.request.ProxyHandler({"http": p, "https": p} if p else {})]
    tries = PK_HTTP_RETRIES if _tries is None else _tries
    last, did_retry = "", False
    for i in range(tries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            # 400/401/404 等是服务端明确答复，重试没有意义
            return e.code, e.read().decode("utf-8", "ignore")
        except Exception as e:
            last = str(e)
            if i < tries and _is_transient_net(last):
                did_retry = True
                time.sleep(PK_HTTP_RETRY_DELAY * (2 ** i))
                continue
            break
    if proxy is True and p:
        # 只有真的走了代理才有必要回落直连；本来就是直连时别把同一条路再跑一遍
        return _pk_http(url, body, headers, method, timeout, proxy=False,
                        _tries=tries)
    if did_retry:
        last += "（同一路径已自动重试 %d 次仍失败）" % tries
    return 0, last


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


def _share_raw_error(d, stage, via=""):
    """非 JSON 响应统一在这里拦住：取 token 与列文件两个阶段都不能把它当空结果。

    v3.81 真机发现：列文件阶段若拿到 TLS 中断的 _raw，旧代码会静默返回空列表，
    界面显示「解析成功」却没有任何可勾选的文件 —— 比直接报错更迷惑人。
    """
    if not (isinstance(d, dict) and d.get("_raw")):
        return ""
    raw_txt = mask_secret(str(d["_raw"])).replace("\n", " ").replace("\r", " ")
    hint = ("；这条路径已自动重试仍失败，稍等几秒再点一次「解析链接」，"
            "若持续出现请切换代理节点" if "自动重试" in raw_txt else "")
    return ("PikPak 返回了非 JSON 响应（代理链路可能被拦截、出口被重置或节点异常）："
            + raw_txt[:160] + ("；本次请求走：" + via if via else "")
            + "；发生在" + stage + hint)


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
    # 响应不是 JSON 时 pk_api 会把原文塞进 _raw。以前会一路走到「需要提取码」，
    # 把代理链路的问题伪装成提取码问题 —— 必须在这里就拦住并说清楚。
    raw_err = _share_raw_error(d, "读取分享信息", via)
    if raw_err:
        raise RuntimeError(raw_err)
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
    raw_err = _share_raw_error(d, "列出分享文件")
    if raw_err:
        raise RuntimeError(raw_err)
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


SHARE_CREATE_MAX_ITEMS = 50
SHARE_CREATE_DAYS = (-1, 1, 7, 30)      # -1 = 永久有效


def _share_region_block(msg):
    """服务端明确说「你所在地区不能用分享」——这类拒绝不会创建任何分享。"""
    s = str(msg or "").lower()
    return any(k in s for k in ("region", "prohibited", "地区",
                                "not available in your country"))


def _share_preset_proxy():
    """取「可预置、可不启用」的代理地址；与当前全局出口相同则没必要再试。"""
    p = (str(load_settings().get("proxyUrl") or "").strip()
         or str(os.environ.get("PROXY_URL") or "").strip())
    if p.lower() in PROXY_OFF_WORDS or not PROXY_URL_RE.match(p):
        return ""
    try:
        if p == proxy_url():
            return ""
    except Exception:
        pass
    return p


def share_create(remote, items, days=7, pcode="", limit=100, _proxy=True):
    """给账号里的文件 / 文件夹创建一条**正式的 PikPak 分享链接**（v3.82）。

    与 `cloudlink`（rclone link 临时下载直链）不同，这里生成的是
    `https://mypikpak.com/s/<id>` 形式的分享，可被别人转存、能在「我的分享」
    里查看提取次数 / 取消。请求体沿用 `_share_worker` 里已验证可用的参数组合，
    但分享会保留下来而不是用完即删；多个 file_ids 会合成**一条**分享，
    正好对应前端「勾选多项 → 一次分享」。

    写操作默认**不重试**，否则网络抖动可能重复创建多条分享。唯一例外是服务端
    明确返回地区限制（`Share Unavailable in Your Region` / `PROHIBITED`）：这类
    请求被业务层拒绝、没有产生任何分享，换一条出口重试一次是安全的。真机实测
    直连必被拒、走代理可成功，所以默认直连部署也能用，无需切全局代理模式。
    """
    remote = str(remote or "").strip()
    if not remote:
        return {"error": "请选择账号"}
    raw = items if isinstance(items, list) else []
    ids, names = [], []
    for it in raw[:SHARE_CREATE_MAX_ITEMS]:
        if isinstance(it, dict):
            fid = str(it.get("id") or "").strip()
            nm = str(it.get("name") or "").strip()
        else:
            fid, nm = str(it or "").strip(), ""
        # PikPak 的 file_id 是 base64url 风格字符串；挡住路径 / 空值 / 明显脏数据
        if fid and re.fullmatch(r"[A-Za-z0-9_-]{4,128}", fid) and fid not in ids:
            ids.append(fid)
            names.append(nm or fid)
    if not ids:
        return {"error": "没有可用的文件 ID：请刷新当前目录后重试（文件与文件夹都要用列表里的 ID）"}
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    if days not in SHARE_CREATE_DAYS:
        days = 7
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 9999))
    pcode = str(pcode or "").strip()
    if pcode and not re.fullmatch(r"[A-Za-z0-9]{4,10}", pcode):
        return {"error": "提取码只能是 4~10 位字母或数字"}
    if not pcode:
        pcode = _pk_pass()
    payload = {"file_ids": ids, "share_to": "encryptedlink",
               "expiration_days": days, "pass_code_option": "REQUIRED",
               "custom_pass_code": pcode, "limit_count": limit}
    try:
        code, d = pk_api(remote, "POST", "/drive/v1/share", payload, _proxy=_proxy)
    except Exception as e:
        return {"error": "创建分享失败：" + mask_secret(str(e))[:160]}
    x = d if isinstance(d, dict) else {}
    if x.get("_raw"):
        snip = mask_secret(str(x["_raw"])[:100]).replace("\n", " ")
        return {"error": "创建分享失败：服务端返回了非 JSON 响应（可能是网络或代理异常）：" + snip}
    data = x.get("data") if isinstance(x.get("data"), dict) else {}
    sid = str(x.get("share_id") or data.get("share_id") or "")
    if code >= 400 or not sid:
        err = _pk_err(code, d)
        if _proxy is True and _share_region_block(err):
            alt = _share_preset_proxy()
            if alt:
                r = share_create(remote, items, days, pcode, limit, _proxy=alt)
                if r.get("ok"):
                    r["via"] = "分享专用代理 " + alt
                    r["retried"] = True
                return r
            return {"error": "创建分享失败：" + err +
                    "。请在「设置 → 代理」填一个可用代理地址（无需切换全局模式）后重试"}
        return {"error": "创建分享失败：" + err}
    url = str(x.get("share_url") or data.get("share_url") or "").strip()
    if not url.startswith("https://"):
        url = "https://mypikpak.com/s/" + sid
    code_out = str(x.get("pass_code") or data.get("pass_code") or pcode)
    return {"ok": True, "shareId": sid, "url": url, "passCode": code_out,
            "title": str(x.get("title") or data.get("title") or "")[:120],
            "days": days, "limit": limit, "count": len(ids), "names": names[:20],
            "text": url + "  提取码：" + code_out}


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


# ────────────────────────────────────────────────────────────────────────────
# v4.01 分账号下载：一个源目录 → 多个账号并行分担
#
# 为什么值得做：PikPak 按「账号」静默限流（实测单账号超过 16 连接反而塌一个数量级），
# 所以「一个账号下整个目录」的天花板就是单账号带宽 —— 这也是 2026-11-23 那次
# 2.28 GB 只有 42 KB/s 时唯一有效的提速方向。同一份资源若已转存到多个账号，
# 每个账号各有独立配额，把文件拆开并行下载才能真正叠加吞吐。
#
# 三条硬约束（改这段之前务必先读）：
#   1) 拆出来的文件必须**互斥且覆盖全集**：同一个文件被两个账号同时写，只会互相截断；
#   2) 所有分片任务必须落**同一个**目录：否则 dedupe_dest 会造出「目录-2」，
#      等于把一份资源拆成好几个文件夹，校验和对账全乱；
#   3) 只按「账号可见文件集合完全一致」才允许分：分给 A 的文件 B 没有，任务必然失败。
# ────────────────────────────────────────────────────────────────────────────
SPLIT_MAX_ACCOUNTS = 8
SPLIT_MIN_FILES = 2


def split_src(name, sub):
    sub = str(sub or "").strip("/")
    return "%s:%s" % (name, sub) if sub else "%s:" % name


def split_list_files(name, sub, proxy=False):
    """列某账号下某路径的全部文件（递归、只读）。失败抛异常，由调用方归属到具体账号。"""
    r = rc("operations/list", fs=split_src(name, sub), remote="", recurse=True,
           _timeout=180, _proxy=bool(proxy), _strict=True)
    out = []
    for e in (r.get("list") or []):
        if not isinstance(e, dict) or e.get("IsDir"):
            continue
        p = str(e.get("Path") or "").strip("/")
        if p:
            out.append({"Path": p, "Size": int(e.get("Size") or 0)})
    return out


def _filter_size_bytes(v):
    m = re.match(r"^(\d+(?:\.\d+)?)([bBkKmMgGtT]?)$", str(v or "").strip())
    if not m:
        return None
    mul = {"": 1, "b": 1, "k": 1024, "m": 1048576, "g": 1073741824, "t": 1099511627776}
    return int(float(m.group(1)) * mul[(m.group(2) or "").lower()])


def split_apply_filter(files, flt):
    """按下载过滤规则在本机先过一遍（分片任务因此不再向 rclone 注入 _filter）。

    分账号是按文件精确分配的，分配之前就必须知道哪些文件真的会被下载 ——
    否则界面报「这个账号分了 40 个」，实际只下 30 个，看起来就像漏了文件。
    规则语义与 build_filter_opt 对齐：不含 `/` 的花样按文件名匹配，大小写不敏感。
    """
    if not flt:
        return files
    mn = _filter_size_bytes(flt.get("minSize"))
    if flt.get("skipEmpty") and mn is None:
        mn = 1                      # 与 build_filter_opt 的「0 字节按 1 字节下限」一致
    mx = _filter_size_bytes(flt.get("maxSize"))
    inc = [str(p).casefold() for p in (flt.get("incExt") or [])]
    exc = [str(p).casefold() for p in (list(flt.get("excExt") or []) + list(flt.get("excKw") or []))]

    def hit(pats, name, path):
        low_n, low_p = name.casefold(), path.casefold()
        return any(fnmatch.fnmatchcase(low_n, p) or fnmatch.fnmatchcase(low_p, p) for p in pats)

    out = []
    for f in files:
        size = int(f.get("Size") or 0)
        path = str(f.get("Path") or "")
        name = path.rsplit("/", 1)[-1]
        if mn is not None and size < mn:
            continue
        if mx is not None and size > mx:
            continue
        if inc and not hit(inc, name, path):
            continue
        if exc and hit(exc, name, path):
            continue
        out.append(f)
    return out


def split_assign(files, remotes):
    """按字节做 LPT 贪心：大文件先分给当前累计最少的账号。

    按字节均衡而不是按文件数均衡 —— 一个 10 GB 和一个 100 MB 算「各一个」时，
    某个账号会早早跑完干等，另一个还差一大半，等于白拆。
    排序里带上路径，保证同一批文件每次分配结果都一样（可复现，便于对账）。
    """
    buckets = {r: [] for r in remotes}
    load = {r: 0 for r in remotes}
    cnt = {r: 0 for r in remotes}
    for f in sorted(files, key=lambda x: (-int(x.get("Size") or 0), str(x.get("Path") or ""))):
        size = int(f.get("Size") or 0)
        r = min(remotes, key=lambda x: (load[x], cnt[x], x))
        buckets[r].append(str(f.get("Path") or ""))
        load[r] += size
        cnt[r] += 1
    return buckets, {r: {"files": cnt[r], "bytes": load[r]} for r in remotes}


def make_split_jobs(remotes, sub, assign, stats, dest, proxy=False, flt=None, s=None):
    """把「账号 → 文件清单」变成 N 条分片任务。

    抽成独立函数是为了能测三条不变式（任何一条破了都会安静地出错）：
      · dest 必须**完全相同** —— 否则后面的分片会被 dedupe_dest 改成「目录-2」；
      · fileList 两两**不相交** —— 否则两个账号同时写同一个目标文件；
      · 并集等于全部文件 —— 漏一个就是永久缺文件，而且看不出是谁漏的。
    """
    s = s or DEFAULTS
    gid = int(time.time() * 1000)
    made = []
    for i, r in enumerate(remotes):
        paths = list(assign.get(r) or [])
        nbytes = int((stats.get(r) or {}).get("bytes") or 0)
        job = {"id": gid + i, "jobid": None, "remote": r, "subpath": sub,
               "src": split_src(r, sub), "mode": "copy", "dest": dest,
               "state": "queued", "started": time.strftime("%m-%d %H:%M"),
               "bytes": 0, "total": nbytes, "speed": 0, "eta": None,
               "error": "", "proxy": bool(proxy),
               "createdVersion": APP_VERSION, "createdAt": int(time.time() * 1000),
               # 分片清单：expand_source_files 只认这些文件，保证互斥
               "fileList": paths,
               "split": {"group": gid, "index": i + 1, "of": len(remotes),
                         "files": len(paths), "bytes": nbytes}}
        if s.get("safeNaming", True):
            job["naming"] = "v2"
        if flt:
            # 过滤已经在本机算进 fileList 了，这里只留文案给界面显示：
            # 再向 rclone 注入一次 _filter 只会引入第二套匹配语义
            # （Python 侧大小写不敏感、rclone 侧默认敏感），
            # 一旦两边判得不一样，被分到的文件会「悄悄不下」。
            job["filterText"] = describe_filter(flt)
        made.append(job)
    return made


def split_peer_running(job, running):
    """同一分片组里已经有成员在跑时返回 True（用于突破 maxConcurrent）。

    分片本来就是「一份资源拆开并行」，只放一片跑等于白拆。NAS 重启后按队列恢复时
    最容易出现「只醒一片」：这里让同组后续分片跟着第一个醒来的成员一起恢复。
    """
    g = (job.get("split") or {}).get("group")
    if not g:
        return False
    return any((x.get("split") or {}).get("group") == g and x.get("state") == "running"
               for x in running)


def split_scan(remotes, sub, flt=None, proxy=False, want_files=False):
    """只读预检：每个账号能否看到这份资源、文件集合是否一致、拆完各分多少。

    返回的 consistent=False 时**不允许**真的开始下载：文件集合不一致意味着
    「分给 A 的文件 B 没有」，建出来的任务必然有一个要失败。
    各账号并行列目录：串行时 4 个账号最坏要等 4 次 90 秒，前端早就超时了。
    """
    def one(r):
        item = {"remote": r, "state": "ok", "files": 0, "bytes": 0,
                "identical": False, "missing": 0, "extra": 0, "error": ""}
        try:
            files = split_apply_filter(split_list_files(r, sub, proxy), flt)
            item["_set"] = {(str(f.get("Path")), int(f.get("Size") or 0)) for f in files}
            item["files"] = len(files)
            item["bytes"] = sum(int(f.get("Size") or 0) for f in files)
            if not files:
                item["state"] = "empty"
                item["error"] = "这个账号下看不到文件（未转存 / 目录为空 / 路径不同）"
        except Exception as e:
            # 「目录不存在」不是故障，是这个账号还没转存：区分开，界面才不会吓人。
            # rclone 对不存在的目录返回 `error in ListJSON: directory not found`（HTTP 200）。
            if _is_missing_dir_error(str(e)):
                item["state"] = "empty"
                item["error"] = "这个账号下没有该目录（很可能还没转存）"
            else:
                item["state"] = "error"
                item["error"] = mask_secret(str(e))[:180]
        return item

    if len(remotes) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(remotes))) as ex:
            rows = list(ex.map(one, remotes))
    else:
        rows = [one(r) for r in remotes]

    sets = {x["remote"]: x.get("_set") for x in rows if "_set" in x}
    for x in rows:
        # v4.02：记下「这次到底列到目录没有」。列不到和「列到但为空」在界面上必须是两句话：
        # 前者是「这个账号没有这份资源」，后者是「目录在但一个文件都没有」。
        x["listed"] = "_set" in x
        x.pop("_set", None)
    # 用「并集」而不是「交集」算缺口：用户要看的是「这个账号少了别人有的东西」，
    # 交集口径下 missing 恒为 0，只剩「多」，报错方向是反的。
    union = set.union(*sets.values()) if sets else set()
    common = set.intersection(*sets.values()) if sets else set()
    ok_all = bool(rows) and len(rows) >= SPLIT_MIN_FILES and len(sets) == len(rows)
    for x in rows:
        s = sets.get(x["remote"])
        if s is None:
            # v4.02：列不到目录的账号，缺口是「并集里所有文件」而不是 0 —— 旧逻辑在这里
            # `continue`，界面显示成「0 个 · 0 KB 缺 0 / 多 0」，看上去像「一个都不缺」。
            x["missing"] = len(union)
            x["extra"] = 0
            x["identical"] = False
            continue
        x["missing"] = len(union - s)
        x["extra"] = len(s - union)
        x["identical"] = (s == union)
    blind = [x for x in rows if not x["listed"]]           # 根本看不到这个目录的账号
    mismatched = [x for x in rows if x["listed"] and not x["identical"]]
    same = ok_all and not blind and not mismatched and bool(union)

    files = [{"Path": p, "Size": s} for p, s in sorted(common)]
    stats = {}
    if same:
        _buckets, stats = split_assign(files, [x["remote"] for x in rows])
    warnings = []
    if not same:
        # v4.02：旧逻辑只给「看到了但不一样」的账号发警告，于是「N 个账号没有该目录」
        # 这种情况 warnings 是空的 —— 界面只有一句「不一致」，用户无从知道该去干什么。
        if blind:
            names = "、".join(x["remote"] for x in blind[:5])
            if len(blind) > 5:
                names += " 等 %d 个账号" % len(blind)
            warnings.append("%s 看不到「%s」这个目录（还没转存过来）：分给它们的文件会直接失败"
                            % (names, sub or "根目录"))
        for x in mismatched:
            warnings.append("%s：缺 %d 个、多 %d 个（与其它账号的文件集合不一致）"
                            % (x["remote"], x["missing"], x["extra"]))
    out = {"path": str(sub or "").strip("/"), "accounts": rows,
           "consistent": same,
           # fileCount/bytes 用并集：不一致时也要能说清「这份资源一共多大」，
           # 交集口径下会少报（别人多出来的文件被算没）。
           "fileCount": len(union), "bytes": sum(s for _p, s in union),
           "commonCount": len(common),
           "missingDir": len(blind), "mismatched": len(mismatched),
           "assign": [{"remote": r, "files": stats[r]["files"], "bytes": stats[r]["bytes"]}
                      for r in stats],
           "warnings": warnings}
    if want_files and same:
        out["files"] = files       # 只给 splitstart 复用，避免再列一次目录（最坏多等 90 秒）
    return out



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


JOBS_SNAP = {"ts": 0.0, "payload": None}   # 1.2 秒内的 jobs 响应复用（多标签页高频轮询）
JOBS_LIVE_TTL = 1.2
JOB_LIVE_TMO = 4.0      # 单次 job/status、core/stats 上限（原来是 8 秒，两个调用就吃掉整轮预算）


# job_live 会改的字段；写回磁盘时只合并这些，别把「停止 / 改名 / 删任务」的结果覆盖掉
JOB_LIVE_KEYS = ("bytes", "total", "speed", "eta", "files", "curfile", "phase",
                 "transferred", "_xf", "_miss", "_mapState", "filesDone", "filesFailed")


def _merge_live_job(tgt, src):
    """把锁外查到的进度合并进最新读出的任务记录。

    state 只在目标仍是 running/queued 时才跟着变 —— 否则用户刚点了「停止」，
    这边一轮迟到的 job_live 会把它改回 running。
    """
    for k in JOB_LIVE_KEYS:
        if k in src:
            tgt[k] = src[k]
    if src.get("state") != tgt.get("state") and tgt.get("state") in ("running", "queued"):
        tgt["state"] = src.get("state")
    if src.get("error"):
        tgt["error"] = src["error"]


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
    if job.get("naming") == "v2" and not job.get("single_file"):
        # v3.83 目录任务：由 _safe_dir_worker 线程推进度，没有 rclone 异步 job 可查。
        with SAFE_JOBS_LOCK:
            alive = job["id"] in SAFE_JOBS
        if job.get("state") == "running" and not alive:
            job["state"] = "stopped"
            job["speed"] = 0
            job["eta"] = None
            job["phase"] = ""
            job["curfile"] = ""
            job["error"] = "控制台重启过，下载已中断（点「继续」会跳过已完成文件接着传）"
        return
    if job.get("naming") == "v2" and job.get("single_file") \
            and job.get("state") in ("done", "stopped", "error"):
        # 单文件安全命名：终态时把结果回写映射清单，原名 ↔ 落盘名可追溯
        if job.get("_mapState") != job["state"]:
            job["_mapState"] = job["state"]
            record_file_state(job_volume(job), job.get("remote"), job.get("subpath"),
                              safe=job.get("safe_leaf") or job.get("leaf"),
                              original=job.get("subpath"), size=int(job.get("bytes") or 0),
                              status=job["state"], error=str(job.get("error") or "")[:200],
                              _flush=True)
        return
    jid = job.get("jobid")
    if not jid or job["state"] not in ("running",):
        return
    pxy = bool(job.get("proxy"))
    try:
        st = rc("job/status", _timeout=JOB_LIVE_TMO, _strict=True, _proxy=pxy, jobid=jid)
        job["_miss"] = 0          # 连续失败计数归零（仅 job not found 时累加）
        if st.get("finished"):
            # 完成时补取一次统计，把最终 bytes/total 落盘（否则秒完成/重启后永远是 0/0）
            try:
                s = rc("core/stats", _timeout=JOB_LIVE_TMO, _proxy=pxy, group="job/%d" % jid)
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
                # v4.04：单文件任务下完后补排转封装（v2 目录任务已在下完每个文件时单独入队）
                _schedule_remux_for_job(job)
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
                # v3.85：先排除网络抖动，否则「captcha token + connection reset」会把
                # 好账号标红成「需重新登录」，用户照着点只会更糊涂。
                if not is_net_error(raw_err) and any(k in raw_err.lower() for k in CRED_FATAL_KEYS):
                    try:
                        src = str(job.get("src") or "")
                        remote = src.split(":", 1)[0] if ":" in src else ""
                        if remote:
                            meta_patch(remote, {"lastErr": "任务失败：" + str(raw_err)[:160],
                                                "lastCheck": int(time.time())})
                    except Exception:
                        pass
            return
        s = rc("core/stats", _timeout=JOB_LIVE_TMO, _proxy=pxy, group="job/%d" % jid)
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
                s = rc("core/stats", _timeout=JOB_LIVE_TMO, _proxy=pxy, group="job/%d" % jid)
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
                    _schedule_remux_for_job(job)     # v4.04：同上面的完成分支
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
    if j.get("naming") == "v2" and not j.get("single_file"):
        # v3.83 目录任务：通知工作线程停止接收新文件
        ev = _safe_stop_event(j["id"])
        if ev:
            ev.set()
        # v4.15：在飞的异步 job 按组一次取消（单个文件一个 job，逐个 stop 会漏）
        try:
            stop_job_group(j["id"], bool(j.get("proxy")))
        except Exception:
            pass
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
    try:      # v4.15：单文件任务也可能有并行的旧 job 残留，按组清一遍
        stop_job_group(j["id"], bool(j.get("proxy")))
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
# v3.87：CONSOLE_DOWNLOAD_SUBDIR 以前只写在文档里、代码没读，导致新建任务直接写进卷根
# （/data/绿帽学院），而历史任务都在 /data/Downloads/绿帽学院 下，同一批文件被判成「没下过」
# 又要整目录重下。这里真正把它接上：folder 缺省时用「下载子目录/任务名」拼 dest。
DOWNLOAD_SUBDIR = ((os.environ.get("CONSOLE_DOWNLOAD_SUBDIR") or "").strip().strip("/")
                   or "Downloads")
LEGACY_VOLUME = {"rclone": RCLONE_DATA_ROOT, "console": DOWNLOAD_ROOT, "host": ""}


def single_file_dst_dir(vroot, folder, parent, dest):
    """v4.08：单文件任务的目标目录（即 rclone copyfile 的 dstFs）。

    指定了 folder 时 dest 本身就是目标目录；没指定时落到
    「Downloads/<源文件所在文件夹名>/」，与目录任务保持同一层级，
    单文件下载自然落在同目录其它文件旁边，不会散在卷根。
    旧版这里直接返回卷根，文件真被写进 /data/xxx.partial，
    而卡片上显示的是 /data/Downloads/xxx —— 显示与落点差一层目录。
    """
    if folder:
        return dest
    pname = basename_of(parent) if parent else ""
    return vroot + "/" + DOWNLOAD_SUBDIR + ("/" + pname if pname else "")


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


# ── v4.07 卷可写性实探 ───────────────────────────────────────────────
# 2026-11-24 现场：NAS 换盘后宿主留下「假卷」目录（/vol2/Data-storage 落在 55 GB 系统盘上，
# 不是任何真实卷），而两个容器的 bind 还指着它。它最坑的地方是「半可写」——
# `touch`、`open(O_CREAT)` 都能成功建出 0 字节文件，`write()` 才回 EPERM，
# 连 `cat` 都读不出来。旧版把它认成主卷（rclone=/data），新任务 100% 落进去、
# 每秒重试一次、界面永远 0.0%，红字只有一句 operation not permitted。
# 所以判定「可写」不能看权限位、也不能看能否建文件，必须真写一次数据再删。
WRITABLE_TTL = 120.0
_WRITABLE_CACHE = {}          # 控制台路径 -> (是否可写, 探测时刻)
_WRITABLE_LOCK = threading.RLock()


def volume_writable(root, ttl=None):
    """真写一次再删：建文件 → 写 4 字节 → fsync → 删除，全过才算可写。
    结果按 ttl（默认 WRITABLE_TTL 秒）缓存，自愈式：卷修好后最多两分钟重新可用。"""
    p = str(root or "").strip()
    if not p:
        return False
    ttl = WRITABLE_TTL if ttl is None else float(ttl)
    now = time.time()
    with _WRITABLE_LOCK:
        hit = _WRITABLE_CACHE.get(p)
        if hit and (now - hit[1]) < ttl:
            return hit[0]
    probe = os.path.join(p, ".cloudshuttle-wtest.%d" % os.getpid())
    ok = False
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, b"wtest")
            os.fsync(fd)
            ok = True
        finally:
            os.close(fd)
    except OSError:
        ok = False
    try:
        os.unlink(probe)
    except OSError:
        pass
    with _WRITABLE_LOCK:
        _WRITABLE_CACHE[p] = (ok, now)
    return ok


def _volume_capacity(p):
    """卷的总容量（字节），只用于排序。
    用「总容量」而不是「剩余空间」：容量是稳定值，不会随着下完一部片子就翻转，
    界面上的「存储空间 N」编号也就不会跳来跳去。识别不到就返回 0（排最后）。"""
    try:
        return shutil.disk_usage(p.get("console") or "/").total
    except Exception:
        return 0


def rank_pairs(pairs):
    """可写的卷排前面；同为可写时**容量大的优先**。
    排序影响「主卷」：新建任务不指定卷时取第一个，所以不可写的卷绝不能排第一，
    小盘也不能顶掉大盘 —— 把 50 GB 的系统副盘当默认下载盘，几十个任务就能写满。

    v4.09：可写性每轮都重探一次（volume_writable 自带 120 秒 TTL，代价只是一次
    4 字节写入）。v4.07 只在首次识别时探一次并缓存进 dict，注释说的「卷修好两分钟内
    自动恢复」其实不成立 —— 必须重启控制台才会重新判定。"""
    for p in pairs:
        p["writable"] = volume_writable(p.get("console"))
        p["capacity"] = _volume_capacity(p)
    pairs.sort(key=lambda p: (not p.get("writable"),
                              -int(p.get("capacity") or 0),
                              str(p.get("rclone") or "")))
    return pairs


def storage_pairs():
    """识别可用下载卷：宿主 Source 相同的 (控制台挂载点, rclone挂载点) 配对。
    rclone=/data 的主卷排最前（历史 dest 约定）；识别失败回退内置单卷。
    v4.07：可写性实探后再排序，不可写的卷沉到最后。"""
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
            return rank_pairs(pairs)
    except Exception:
        pass
    fb = dict(LEGACY_VOLUME)
    fb["writable"] = volume_writable(fb.get("console"))
    return [fb]

def refresh_storages(s=None):
    """重算每个存储空间的容量（真实值 + 可选配额折算），写入 STATE["storages"]。
    挂载关系在容器生命周期内不变，只识别一次；容量与可写性每轮 scheduler 刷新。"""
    if not STATE.get("volumePairs"):
        STATE["volumePairs"] = storage_pairs()
    else:
        # v4.07：每轮重排一次（探针带 120 秒缓存，开销就是一次 4 字节写入）。
        # 卷从坏到好、或从好到坏，两分钟内就能反映到「主卷是谁」上，不用重启控制台。
        rank_pairs(STATE["volumePairs"])
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
                      "writable": bool(p.get("writable", True)),
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


def active_speed(stats):
    """当前在传文件的实测速度合计（字节/秒）。

    v4.15：总速度不再用「累计字节的两次差分」。两个守护进程（主 5572 / 代理 5573）的
    累计字节各自随进程重启归零，一合并就会算出 250 MB/s 这种离谱值，然后 EMA 要好几秒
    才降下来，曲线看着像在飞。rclone 给每个在传文件都报了实时 speed，直接加总最诚实。
    """
    return float(sum(float(t.get("speed") or 0) for t in (stats.get("transferring") or [])))


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

def _stuck_watchdog(j, s):
    """v3.83 卡住看门狗（只作用于 v2 任务，旧任务行为完全不变）。

    判定刻意保守：字节数整个窗口没动 **且** 当前速度为 0 **且** 没有在传文件，
    才认定卡死。大文件单线程复制可能十几分钟不涨字节，但那时 files>0，不会误杀。"""
    if j.get("naming") != "v2" or j.get("state") != "running":
        return
    minutes = float(s.get("stuckWatchdogMin") or 0)
    if minutes <= 0:
        return
    now = time.time()
    cur = int(j.get("bytes") or 0)
    prev = j.get("_progBytes")
    # 注意不能用 `j.get("_progBytes") or -1`：0 字节是合法进度，会被或运算吃掉
    if prev is None or cur != int(prev) or not j.get("_progAt"):
        j["_progBytes"], j["_progAt"] = cur, now
        return
    if j.get("speed") or j.get("files"):
        j["_progAt"] = now          # 有速度 / 有在传文件 = 还活着，重新计窗口
        return
    if now - float(j["_progAt"]) < minutes * 60:
        return
    if int(j.get("filesFailed") or 0) > 0:
        return          # v3.90：已有明确失败原因，别用「疑似卡住」盖掉真正的报错
    try:
        stop_job_entry(j)
    except Exception:
        pass
    j["state"] = "error"
    j["speed"], j["eta"], j["curfile"], j["phase"] = 0, None, "", ""
    j["error"] = ("任务超过 %d 分钟没有任何进度，已自动终止（疑似卡住）；"
                  "点「继续」可跳过已完成文件重试" % int(minutes))


# ── v4.03 低速自动重连 ──────────────────────────────────────────────────
# 和上面的「卡住看门狗」不是一回事：那个要求速度为 0 且字节完全不动。
# PikPak 更常见的是第三种状态 —— 连接还在、字节还在涨，但只有几十 KB/s，
# 既不报错也不触发任何超时，于是任务挂在那儿跑几十小时。
# 2026-11-23 现场实测（会员号：存储1-new）：
#   僵死连接 58~74 KB/s → 手动「停→继续」→ 立刻 8.9 MB/s / 23.4 MB/s。
# 对照实验：同一时刻同账号 8 路并发只多拿到 82 KB/s（6 路 0 字节），
#   pik-3 单流 448 KB/s；磁盘 116 MB/s、国内 17.8 MB/s、TCP 重传 0.00%。
#   所以既不是磁盘也不是带宽，更不是「账号额度」，就是那条连接自己废了。
# 判定用「窗口内平均速度」而不是瞬时速度：瞬时值抖动极大（实测同文件
# 10 秒内从 204 KB/s 掉到 16 KB/s），拿它做判据会误杀。
def _auto_reconnect_worker(job_id):
    """把一次「停 → 重新排队」完整走完，等价于用户手点一次「继续」。

    必须等旧 worker 线程真退出再置 queued，否则新旧线程会同时写同一个文件
    （v3.93 已经在「继续」接口里踩过这个坑）。"""
    try:
        with LOCK:
            jobs = load_jobs()
            j = next((x for x in jobs if x.get("id") == job_id), None)
            if not j or j["state"] != "running":
                return
            stop_job_entry(j)
            save_jobs(jobs)
        wait_worker_exit(job_id, timeout=15)
        with LOCK:
            jobs = load_jobs()
            j = next((x for x in jobs if x.get("id") == job_id), None)
            if j and j["state"] == "stopped":
                j["state"] = "queued"
                j["error"] = ""
                # 重连后进度会从文件头重来（rclone 会删掉半截 .partial），
                # 基准清零，避免下一窗口把「负数增量」当成更慢又连着踢一次。
                j["_kickAt"], j["_kickBytes"] = 0, None
                save_jobs(jobs)
    except Exception as e:
        print("%s 自动重连异常: %s" % (time.strftime("%F %T"), str(e)[:120]))


def _reconnect_watchdog(j, s):
    """v4.03：窗口内平均速度低于阈值 → 判定连接僵死，自动重连一次。

    返回值表示「job 字典被改过」：调度器只在返回 True 时才写盘。这条以前踩过坑 ——
    基线只记在内存里、每轮又从 jobs.json 读回 0，于是 `not base_t` 永远成立、
    看门狗永远停在「记基线」这一步，一次都不会触发（2026-11-23 真机测试抓到）。
    触发后窗口翻倍，在真的只有几百 KB/s 的线路上不会反复丢掉半截文件：
    180s → 360s → 720s …→ 3600s。"""
    if j.get("state") != "running" or j.get("kind") in ("xfer", "unzip"):
        return False
    if not _truthy(s.get("autoReconnect", True)):
        return False
    maxk = int(s.get("reconnectMax") or 0)
    if maxk <= 0:
        return False
    now = time.time()
    cur = int(j.get("bytes") or 0)
    base_t = j.get("_kickAt")
    base_b = j.get("_kickBytes")
    if not base_t or base_b is None:
        j["_kickAt"], j["_kickBytes"] = now, cur
        return True
    win = float(j.get("_kickWin") or s.get("reconnectWindowSec") or 180)
    elapsed = now - float(base_t)
    if elapsed < win:
        return False
    rate = (cur - int(base_b)) / max(elapsed, 1.0)
    if cur < int(base_b):
        # 字节计数器回退 = 任务自己重启过（rclone 删掉半截 .partial 后重算），
        # 不是「变慢」。拿它当判据会误踢：真机第一次实测就被这个踢了一脚。
        j["_kickAt"], j["_kickBytes"] = now, cur
        return True
    thr = float(s.get("reconnectSlowKBps") or 384) * 1024.0
    if rate >= thr:
        j["_kickAt"], j["_kickBytes"] = now, cur     # 还跑得动，重新计窗口
        return True
    # v4.06：同一个文件连续两轮「一个字节都没进来」→ 停止重连。
    # 现场（2026-11-24）：PikPak 里一个 TEMP 占位文件没有可用直链，看门狗每 180 秒
    # 就把任务停掉重排队，而单文件自己的重试（约 2 分钟）永远走不到「失败」，
    # 任务于是卡在 97.4% 无限重启。停止踢之后，文件级尾池会在零字节满 10 分钟时认输、
    # 把它记成失败，任务才能收尾（48/49 完成 + 1 个失败，而不是永远 0 KB/s）。
    cf = str(j.get("curfile") or "")
    prev_cf = str(j.get("_kickFile") or "")
    prev_b = int(j.get("_kickFileBytes") or 0)
    same = (int(j.get("_kickSame") or 0) + 1) if (cf and cf == prev_cf and cur <= prev_b) else 0
    j["_kickFile"], j["_kickFileBytes"], j["_kickSame"] = cf, cur, same
    j["_kickAt"], j["_kickBytes"] = now, cur
    if same >= 2:
        if same == 2:          # 只在第一次放弃时打一行，之后每轮不再刷屏
            try:
                log_event(j.get("id"), "kick_giveup", file=cf[:80], strikes=same,
                          why="同一文件连续两轮零字节，交给文件级重试判失败")
            except Exception:
                pass
        return True
    n = int(j.get("_kickN") or 0)
    if n >= maxk:
        return False
    j["_kickN"] = n + 1
    j["_kickWin"] = min(win * 2, 3600.0)
    j["_kickAt"], j["_kickBytes"] = now, cur
    try:
        log_event(j.get("id"), "reconnect", rate=int(max(rate, 0) / 1024),
                  kick="%d/%d" % (n + 1, maxk), win=int(win))
    except Exception:
        pass
    threading.Thread(target=_auto_reconnect_worker, args=(j["id"],), daemon=True).start()
    return True


def poll_jobs_live():
    """后端兜底轮询任务终态：不再依赖浏览器一直开着。
    与 jobs action 同样在 LOCK 内执行，保证 jobs.json 读写串行。"""
    with LOCK:
        jobs = load_jobs()
        s = load_settings()
        before = json.dumps(jobs, ensure_ascii=False, sort_keys=True)
        for j in jobs:
            if j["state"] == "running":
                job_live(j)
                _stuck_watchdog(j, s)
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
                    elif _reconnect_watchdog(j, s):
                        # v4.03：连接僵死（几十 KB/s 的「慢速蠕动」）自动重连，
                        # 真停真起由后台线程完成，这里只需要标脏
                        changed = True
                for j in jobs:
                    if j["state"] != "queued" or vol_low(j):
                        continue
                    # v4.01：分片组内已经有一片在跑时，剩下的不再受 maxConcurrent 限制。
                    # 分片本意就是「一份资源拆开并行」，只醒一片等于白拆 ——
                    # NAS 重启后按队列恢复时最容易出现这种「只醒一片」。
                    if len(running) >= s["maxConcurrent"] and not split_peer_running(j, running):
                        continue
                    if launch_job(j, s):
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
VERIFY_LIST_TMO    = (60, 150)             # 递归列目录：首次 + 抖动后重试
VERIFY_CHUNK       = 32 * 1024
VERIFY_SAMPLE_MAX  = 20
VERIFY_FULL_FILES  = 5
VERIFY_LIST_TIMEOUTS = VERIFY_LIST_TMO
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
           "truncated": False, "error": "", "attempts": 0}
    # v3.97/v3.98 实测：`lsjson --stat` 指向「目录」时在 PikPak 上永远不会返回
    # （25 秒超时、无输出），所以原来那次「探一下是不是单文件」在目录场景纯属白等 12 秒。
    # 现在直接递归列一次，用返回值自己判断：单文件只列出一条、且 Path 恰好是它自己的文件名。
    lst, err, ret["attempts"] = None, "", 0
    for i, tmo in enumerate(VERIFY_LIST_TIMEOUTS):
        ret["attempts"] = i + 1
        try:
            p = _rclone_run(["lsjson", "--recursive", "--files-only", "--log-level", "ERROR", target],
                            timeout=tmo)
        except Exception:
            err = "云端列目录超时（第 %d 次，%d 秒）" % (i + 1, tmo)
            continue
        if p.returncode != 0:
            err = "云端列目录失败：%s" % (_decode(p.stderr).strip()[:180] or ("exit %s" % p.returncode))
            continue
        try:
            lst = json.loads(_decode(p.stdout) or "[]")
        except Exception:
            err = "云端列表无法解析"
            continue
        err = ""
        break
    if lst is None:
        ret["error"] = err
        return ret
    if isinstance(lst, dict):
        lst = [lst]
    # 单文件：云端把「单个对象」也列成一条，且 Path 就是它自己的文件名
    base = _norm_key(str(path or "").strip("/").rsplit("/", 1)[-1])
    if base and len(lst) == 1 and isinstance(lst[0], dict) \
            and not lst[0].get("IsDir") \
            and _norm_key(lst[0].get("Path") or lst[0].get("Name")) == base:
        ret["files"][base] = int(lst[0].get("Size") or 0)
        ret["targets"][base] = target
        ret["scope"] = "file"
        return ret
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


def rename_map_for(remote, cloud_path, local_root):
    """v3.97：取出「云端原名 → 落盘安全名」映射，供一致性校验用。

    开了安全命名（默认）后，超过字节上限的文件名会被截断并加 `~hash` 后缀，
    云端名和本地名因此不同。v3.96 之前的校验按名字直接配对，会把同一批文件
    同时报成「本地缺失 10 + 本地多余 10」——2026-11-23 实测 159 个文件里就有 10 对。
    """
    out = {}
    try:
        root = _local_volume_root(local_root) or primary_root()
        ent = (load_name_map({"console": root}) or {}).get("entries") or {}
    except Exception:
        return out
    for k, v in ent.items():
        r, _, src = str(k).partition(":")
        if r != str(remote):
            continue
        prefix = (str(cloud_path).strip("/") + "/") if str(cloud_path or "").strip("/") else ""
        if prefix and not src.startswith(prefix):
            continue
        rel = src[len(prefix):] if prefix else src
        safe = str((v or {}).get("safe") or "")
        if rel and safe and safe != rel:
            out[_norm_key(rel)] = _norm_key(safe)
    return out


def rename_alias_map(cloud_path, local_root):
    """v4.01：把**所有账号**在同一前缀下的「原名 → 落盘名」都收进来。

    安全命名的哈希输入包含账号名，所以同一个云端文件由 A 账号落盘、却拿 B 账号去校验时，
    B 的映射表里压根没有那条记录 —— 分账号下载后这种「同目录混着多个账号的落盘结果」
    会让一批文件同时被报成缺失 + 多余。这里按相对路径聚合成别名表，校验时只要
    「某个别名在本地存在」就认定是同一个文件，不会再退化成按字节数猜。"""
    out = {}
    try:
        root = _local_volume_root(local_root) or primary_root()
        ent = (load_name_map({"console": root}) or {}).get("entries") or {}
    except Exception:
        return out
    prefix = (str(cloud_path or "").strip("/") + "/") if str(cloud_path or "").strip("/") else ""
    for k, v in ent.items():
        _r, _, src = str(k).partition(":")
        src = str(src)
        if prefix and not src.startswith(prefix):
            continue
        rel = src[len(prefix):] if prefix else src
        safe = str((v or {}).get("safe") or "")
        if rel and safe and safe != rel:
            lst = out.setdefault(_norm_key(rel), [])
            key = _norm_key(safe)
            if key not in lst:
                lst.append(key)
    return out


def pair_renames_by_size(items):
    """v3.97：没有映射时按「字节数唯一」把 missing/extra 配成改名对。

    只在某个大小上恰好 1 个缺失 + 1 个多余时配对，避免把真实缺失/多余吞掉；
    配对后只保留云端那条（状态 renamed，带 localPath），本地那条标 _merged 由调用方丢弃，
    这样 169 条不会变成「10 对变成 20 条」的噪声。
    """
    miss = [it for it in items if it["status"] == "missing"]
    extra = [it for it in items if it["status"] == "extra"]
    by_size_m, by_size_e = {}, {}
    for it in miss:
        by_size_m.setdefault(it.get("cloudSize"), []).append(it)
    for it in extra:
        by_size_e.setdefault(it.get("localSize"), []).append(it)
    paired = 0
    for size, mlist in by_size_m.items():
        elist = by_size_e.get(size) or []
        if size is None or len(mlist) != 1 or len(elist) != 1:
            continue
        m, e = mlist[0], elist[0]
        m["status"] = "renamed"
        m["renameSource"] = "size"
        m["localPath"] = e["path"]
        m["localSize"] = size
        m["note"] = "本地名为 %s（仅按字节数唯一配对，未列入安全命名清单；建议抽样校验或人工确认）" % e["path"][:70]
        e["_merged"] = True
        paired += 1
    return paired


def verify_rename_counts(items):
    """v3.99：把 renamed 按证据强度拆开，便于界面提示人工确认。

    name-map = 安全命名清单里查到的原名↔落盘名（可信）；
    size     = 清单没覆盖时按「同大小且唯一」推断（仅人工可确认）。
    """
    by_map = by_size = 0
    for it in items:
        if it.get("status") != "renamed":
            continue
        if it.get("renameSource") == "size":
            by_size += 1
        else:
            by_map += 1
    return {"byMap": by_map, "bySize": by_size}


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
    if ci.get("attempts", 1) > 1:
        warnings.append("云端列目录第 %d 次才成功（PikPak 偶发抖动，已自动重试）" % ci["attempts"])
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
    rmap = rename_map_for(remote, cloud_path, ltarget)
    expect = {}
    for k, size in cloud.items():
        expect[rmap.get(k, k)] = (k, size)
    # v4.01：分账号下载会把多个账号的落盘名混进同一个目录。先算出「哪些云端文件
    # 能靠别账号的映射在本地找到」，免得它们同时被报成缺失 + 多余（v3.97 的假差异温床）。
    aliases = rename_alias_map(cloud_path, ltarget)
    alias_hits, alias_taken = {}, set()
    for k in cloud:
        if k in local:
            continue
        for a in aliases.get(k, []):
            if a in local and a not in alias_taken:
                alias_hits[k] = a
                alias_taken.add(a)
                break
    for k in sorted(set(expect) | set(local)):
        c, l = expect.get(k), local.get(k)
        orig = c[0] if c else None
        if c is not None and l is not None:
            note = ""
            if orig and orig != k:
                note = "落盘名已按安全命名截断（云端原名：%s）" % orig[:80]
            status = "renamed" if (orig and orig != k) else (
                "match" if c[1] == l["size"] else "size")
            if status == "size":
                note = "大小不同（可能未下完 / 文件被改动）"
            item = {"path": orig or k, "localPath": k if orig and orig != k else None,
                    "status": status, "cloudSize": c[1], "localSize": l["size"],
                    "note": note}
            if status == "renamed":
                item["renameSource"] = "name-map"
            items.append(item)
        elif c is not None:
            alt = alias_hits.get(k)
            if alt:
                items.append({"path": orig, "localPath": alt, "status": "renamed",
                              "cloudSize": c[1], "localSize": local[alt]["size"],
                              "renameSource": "name-map",
                              "note": "落盘名由其它账号写入（分账号下载），已按安全命名清单认定同一文件"})
                continue
            items.append({"path": orig, "status": "missing", "cloudSize": c[1],
                          "localSize": None, "note": "云端有、本地没有（尚未下载或已被清理）"})
        else:
            if k in alias_taken:      # 已被某个云端文件按别名认领，不再重复算「多余」
                continue
            items.append({"path": k, "status": "extra", "cloudSize": None,
                          "localSize": l["size"], "note": "本地有、云端没有（云端已删除 / 其他来源文件）"})
    # v3.97：映射只覆盖「本程序处理过」的文件，历史任务/更早版本落的盘对不上；
    # 剩下的按字节数唯一配对兜底（实测这一步把最后 15 对也认了出来）。
    renamed = pair_renames_by_size(items)
    if renamed:
        items = [it for it in items if not it.get("_merged")]
        warnings.append("%d 对文件仅按字节数唯一配对认定为改名（未列入安全命名清单，建议抽样校验或人工确认）"
                        % renamed)

    deep_checked, deep_bytes = 0, 0
    candidates = [it for it in items if it["status"] in ("match", "renamed") and lscope == cscope]
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
            fp = local[it.get("localPath") or it["path"]]["abs"]
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
               ("match", "renamed", "missing", "extra", "size", "content", "unknown")}
    summary["total"] = len(items)
    summary["deepChecked"] = deep_checked
    summary["deepBytes"] = deep_bytes
    rcounts = verify_rename_counts(items)
    summary["renamedByMap"] = rcounts["byMap"]
    summary["renamedBySize"] = rcounts["bySize"]
    if counts.get("renamed"):
        warnings.append("%d 个文件落盘名与云端不同：安全命名清单确认 %d 个，按字节数推断 %d 个（推断项需人工确认）"
                        % (counts["renamed"], rcounts["byMap"], rcounts["bySize"]))
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
        "attempts": ci.get("attempts", 0),
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
UPDATE_APP_DIR = APP_HOME
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



# ────────────────────────────────────────────────────────────────────────────
# v4.21 结构重构：api() 铺平为注册表。原 2,114 行 if action == "..." 分支
# 机械搬移为下面的 @action 函数（只搬不改，AST 逐分支等价断言见
# docs/audit-refactor/）。注册顺序 = 原分支顺序。
# ────────────────────────────────────────────────────────────────────────────
HANDLERS = {}


def action(*names):
    """把函数注册为 /api 的一个（或多个）action。"""
    def deco(fn):
        for n in names:
            HANDLERS[n] = fn
        return fn
    return deco

@action("updatecheck")
def h_updatecheck(body):
    return update_check(bool(body.get("force")))

@action("updateapply")
def h_updateapply(body):
    return update_apply()

@action("updatestate")
def h_updatestate(body):
    return update_state()

@action("updaterollback")
def h_updaterollback(body):
    return update_rollback()

@action("localsize")
def h_localsize(body):
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

@action("dirsize")
def h_dirsize(body):
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

@action("remotes")
def h_remotes(body):
    p = subprocess.run(["docker", "exec", RCLONE_CONTAINER, "rclone", "listremotes"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return {"remotes": [x.strip().rstrip(":") for x in p.stdout.splitlines() if x.strip()]}

@action("proxycheck")
def h_proxycheck(body):
    return api_proxy_check()

@action("foldercovers")
def h_foldercovers(body):
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

@action("dirs")
def h_dirs(body):
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

@action("start")
def h_start(body):
    with LOCK:
        s = load_settings()
        jobs = load_jobs()
        running = sum(1 for j in jobs if j["state"] == "running")
        sub = body.get("path", "").strip("/")
        name = basename_of(sub) if sub else body["remote"]
        folder = str(body.get("folder") or "").strip("/")
        # v3.87：folder 允许最多 4 层（每层仍按原来的安全字符集校验），
        # 这样 API / 批量脚本能精确指定「Downloads/子目录」，与历史任务的 dest 对齐。
        if folder:
            parts = folder.split("/")
            if len(parts) > 4 or not all(
                    p and re.match(r'^[\w\u4e00-\u9fff][\w\u4e00-\u9fff -]{0,60}$', p)
                    for p in parts):
                return {"error": "保存目录名不合法（最多 4 层，每层中英文/数字/空格/连字符）"}
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
        # v4.07：不可写的卷一律不给建任务。现场那个假卷「能建 0 字节文件、写数据 EPERM」，
        # 旧版放行后表现就是 0.0% 无限重试；这里提前拦住，错误信息直接指向根本原因。
        if not vol.get("writable", True):
            return {"error": "「%s」（%s）当前不可写：能创建文件、但写数据被系统拒绝"
                             "（operation not permitted）。这通常不是磁盘满或账号问题，"
                             "而是宿主上这个目录已经失效（换盘 / 卷被卸载后留下的空壳），"
                             "容器还 bind 在上面。请改选其它存储空间；"
                             "或在 NAS 上把该容器的挂载路径改到真实卷后重建容器。"
                             % (vol.get("name") or vroot, vroot)}
        vf = volume_effective_free(vol, s)
        disk_min = float(s["diskMinGB"] or 0)
        if vf is not None and vf < disk_min:
            return {"error": "「%s」空间保护中（剩余 %.1f GB），请清理该卷或到设置里调低阈值"
                             % (vol.get("name") or vroot, vf)}
        # v3.87：没指定 folder 时落到「下载子目录/任务名」，与历史任务（/data/Downloads/xxx）
        # 保持一致。v3.83 起这里少了 Downloads 层，新建任务会写进卷根（/data/绿帽学院），
        # 与已有数据不是同一目录，rclone 无从判断「已下过」，只能整目录重下。
        src = "%s:%s" % (body["remote"], sub) if sub else "%s:" % body["remote"]
        base_dest = vroot + "/" + (folder if folder else (DOWNLOAD_SUBDIR + "/" + name))
        force = bool(body.get("force"))
        warn = ""
        # v3.93：同一个源重复建任务只会抢带宽、还可能两个线程同时写同一个文件；
        # 而旧版 dedupe_dest 会在这时造出「目录-2」，等于把已有数据全部重下一遍。
        live = next((j for j in jobs if j.get("src") == src
                     and j.get("state") in ("running", "queued")), None)
        if live and not force:
            return {"error": "同一个源已有任务在跑（id %s，落点 %s）。重复建任务只会抢带宽、"
                             "还可能同时写同一个文件；等它结束，或带 force=true 才并行。"
                             % (live.get("id"), live.get("dest")),
                    "duplicate": True, "existing": live.get("id")}
        prev = next((j for j in jobs if j.get("src") == src
                     and j.get("state") not in ("running", "queued")
                     and not j.get("single_file")), None)
        if force:
            dest = base_dest
        elif prev and str(prev.get("dest") or "").startswith(vroot + "/"):
            # 直接沿用同源旧任务的落点：既不会造出「-2」，也能按名字+大小跳过已下好的文件
            dest = prev["dest"]
            warn = ("沿用同源旧任务 %s 的落点 %s（已完成的文件会按「名字+大小」跳过，不会重下）"
                    % (prev.get("id"), dest))
        else:
            dest = dedupe_dest(base_dest, fs=vroot)
            if dest != base_dest:
                warn = ("目标目录 %s 已存在，落点改为 %s：这一份会从头下载。"
                        "要接着用已有数据请显式指定 folder，或带 force=true"
                        % (base_dest, dest))
        # 必须先定下 proxy：预检与后续 launch_job 都依赖它。
        # v3.31 曾把赋值放在预检之后，导致批量下载全程抛
        # UnboundLocalError: cannot access local variable 'proxy'。
        proxy = bool(body.get("proxy", s["downloadProxy"]))
        # v3.43 下载过滤：规范化后随任务一起落盘，续传 / 重启都能沿用同一套规则
        flt = norm_download_filter(body.get("filter"))
        # v4.05：预检走在 launch_job 之前，而 5573 只在 launch_job 里按需拉起。
        # rclone-backend 重启后 5573 就没了，此时任何「走代理」的新任务都会在预检
        # 阶段报「connection refused」，看起来像网络故障。所以这里也先确保 5573 在。
        if proxy and not ensure_proxy_rc():
            return {"error": "代理守护进程（%d）启动失败，请检查代理客户端与 %s 容器"
                             % (PROXY_RC_PORT, RCLONE_CONTAINER)}
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
               "speed": 0, "eta": None, "error": "", "proxy": proxy,
               # v3.93：记下建任务时的版本，列表里可区分「哪版建的」同名任务
               "createdVersion": APP_VERSION, "createdAt": int(time.time() * 1000)}
        if flt:
            job["filter"] = flt
            job["filterText"] = describe_filter(flt)
        # v3.83：安全命名只作用于「新创建」的任务；老任务没有 naming 字段，
        # 继续走 v3.82 及以前的原样命名逻辑，升级不会影响在跑 / 排队的旧任务。
        if s.get("safeNaming", True):
            job["naming"] = "v2"
        if is_file:
            parent, _, leaf = sub.rpartition("/")
            job["single_file"] = True
            job["leaf"] = leaf
            job["src_dir"] = "%s:%s" % (body["remote"], parent)
            if job.get("naming") == "v2":
                # 源端仍用 leaf（原名），只有落盘名换成安全名
                job["safe_leaf"] = safe_basename(
                    leaf, body["remote"], sub,
                    max_bytes=int(s.get("safeNameMaxBytes") or 180),
                    hash_len=int(s.get("safeNameHashLen") or 12))
            # v4.08：单文件任务的「显示落点」必须等于「真实落点」。
            # 旧版未指定 folder 时 dst_dir 直接取卷根，rclone 实际写的是
            # /data/xxx.mp4.partial，而卡片上显示 /data/Downloads/xxx.mp4 ——
            # 差一层目录。出现 operation not permitted 这类落盘错误时，
            # 报错路径和界面路径对不上，排障会被带偏（现场已踩过一次）。
            if folder:
                dst_dir = dest                      # 指定保存目录：dest 就是该目录
            else:
                dst_dir = single_file_dst_dir(vroot, folder, parent, dest)
            job["dst_dir"] = dst_dir
            dest = dst_dir + "/" + (job.get("safe_leaf") or leaf)
            job["dest"] = dest
        launched_ok = True
        if running < s["maxConcurrent"]:
            launched_ok = launch_job(job, s)
        jobs.append(job)
        save_jobs(jobs)
        if not launched_ok:
            return {"error": "启动失败: %s" % (job.get("error") or "未知原因"), "job": job}
        if warn:
            return {"job": job, "warn": warn}
        return {"job": job}

@action("splitscan")
def h_splitscan(body):
    remotes = [str(x).strip() for x in (body.get("remotes") or []) if str(x).strip()]
    remotes = list(dict.fromkeys(remotes))
    sub = str(body.get("path") or "").strip("/")
    if len(remotes) < 2:
        return {"error": "至少要选 2 个账号才有意义（单账号请用普通下载）"}
    if len(remotes) > SPLIT_MAX_ACCOUNTS:
        return {"error": "一次最多 %d 个账号" % SPLIT_MAX_ACCOUNTS}
    avail = set(pikpak_remotes())
    bad = [r for r in remotes if r not in avail]
    if bad:
        return {"error": "这些账号不是可用的 PikPak 账号：%s" % "、".join(bad)}
    s = load_settings()
    proxy = bool(body.get("proxy", s["downloadProxy"]))
    flt = norm_download_filter(body.get("filter"))
    out = split_scan(remotes, sub, flt, proxy)
    out["filterText"] = describe_filter(flt)
    return out

@action("splitstart")
def h_splitstart(body):
    remotes = [str(x).strip() for x in (body.get("remotes") or []) if str(x).strip()]
    remotes = list(dict.fromkeys(remotes))
    sub = str(body.get("path") or "").strip("/")
    if len(remotes) < 2:
        return {"error": "至少要选 2 个账号才能分账号下载"}
    if len(remotes) > SPLIT_MAX_ACCOUNTS:
        return {"error": "一次最多 %d 个账号" % SPLIT_MAX_ACCOUNTS}
    avail = set(pikpak_remotes())
    bad = [r for r in remotes if r not in avail]
    if bad:
        return {"error": "这些账号不是可用的 PikPak 账号：%s" % "、".join(bad)}
    s = load_settings()
    proxy = bool(body.get("proxy", s["downloadProxy"]))
    flt = norm_download_filter(body.get("filter"))
    scan = split_scan(remotes, sub, flt, proxy, want_files=True)
    if not scan["fileCount"]:
        detail = "；".join("%s：%s" % (x["remote"], x["error"] or "没有文件")
                          for x in scan["accounts"] if x["state"] != "ok")
        return {"error": "源目录在这些账号里都没有可下载的文件" + ("（" + detail + "）" if detail else ""),
                "scan": scan}
    if not scan["consistent"]:
        return {"error": "这些账号看到的文件集合不一致，不能直接分：" +
                         "；".join(scan["warnings"] or ["有账号看不到该目录"]),
                "scan": scan}
    files = scan.get("files") or split_apply_filter(split_list_files(remotes[0], sub, proxy), flt)
    scan.pop("files", None)

    if len(files) < SPLIT_MIN_FILES:
        return {"error": "只有 %d 个文件，分给 %d 个账号没有收益（建议直接普通下载）"
                         % (len(files), len(remotes)), "scan": scan}
    assign, stats = split_assign(files, remotes)
    if not all(assign.get(r) for r in remotes):
        return {"error": "文件数（%d）少于账号数（%d），无法保证每个账号都有活干；"
                         "请减少账号或改用普通下载" % (len(files), len(remotes))}

    folder = str(body.get("folder") or "").strip("/")
    if folder:
        parts = folder.split("/")
        if len(parts) > 4 or not all(
                p and re.match(r'^[\w\u4e00-\u9fff][\w\u4e00-\u9fff -]{0,60}$', p)
                for p in parts):
            return {"error": "保存目录名不合法（最多 4 层，每层中英文/数字/空格/连字符）"}
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

    name = basename_of(sub) if sub else remotes[0]
    # 分片任务一律落同一个目录：dedupe_dest 在这里是**反效果** —— 它会把第二个
    # 任务改到「目录-2」，等于把一份资源拆进两个文件夹，校验和对账全乱。
    dest = vroot + "/" + (folder if folder else (DOWNLOAD_SUBDIR + "/" + name))
    warn = []
    try:
        dest_abs = local_dest(dest)
        if dest_abs and os.path.isdir(dest_abs) and os.listdir(dest_abs):
            warn.append("目标目录 %s 已有文件：同名同大小的会被跳过（等同续传），"
                        "不会重复下载" % dest)
    except Exception:
        pass

    with LOCK:
        jobs = load_jobs()
        for r in remotes:
            src = split_src(r, sub)
            live = next((j for j in jobs if j.get("src") == src
                         and j.get("state") in ("running", "queued")), None)
            if live and not bool(body.get("force")):
                return {"error": "账号 %s 已有同源任务在跑（#%s，落点 %s）。"
                                 "先停掉它再分账号，或带 force=true 强制并行。"
                                 % (r, live.get("id"), live.get("dest")),
                        "duplicate": True, "existing": live.get("id")}
        made = make_split_jobs(remotes, sub, assign, stats, dest,
                               proxy=proxy, flt=flt, s=s)
        failed = []
        for job in made:
            # 分账号是「同一份资源的分片」，共用一个源目录，不受 maxConcurrent 限制：
            # 排队等前一片跑完就完全失去了并行的意义。每片仍是独立账号、独立连接预算。
            if not launch_job(job, s):
                failed.append({"remote": job["remote"],
                               "error": job.get("error") or "启动失败"})
        jobs.extend(made)
        save_jobs(jobs)
    ok = [j for j in made if j.get("state") != "error"]
    if not ok:
        return {"error": "全部分片都启动失败：" +
                         "；".join("%s %s" % (x["remote"], x["error"]) for x in failed),
                "jobs": made}
    return {"jobs": made, "dest": dest, "fileCount": len(files),
            "bytes": sum(int(f.get("Size") or 0) for f in files),
            "assign": [{"remote": r, "files": stats[r]["files"],
                        "bytes": stats[r]["bytes"]} for r in remotes],
            "failed": failed, "warnings": warn}

@action("scanpath")
def h_scanpath(body):
    return cloud_scan(body["remote"], body.get("path", ""), body.get("depth", 6))

@action("myshares")
def h_myshares(body):
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

@action("sharelimit")
def h_sharelimit(body):
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

@action("sharedel")
def h_sharedel(body):
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

@action("shareparse")
def h_shareparse(body):
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
            if not any(k in last for k in ("PROHIBITED", "NOT_FOUND", "非 JSON")):
                break
    if len(tried) > 1:
        last += "；已尝试 %d 条路径：%s" % (len(tried), " → ".join(tried))
    return {"error": last, "tried": tried}

@action("sharesave")
def h_sharesave(body):
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

@action("xferstart")
def h_xferstart(body):
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

@action("jobs")
def h_jobs(body):
    now = time.time()
    snap = JOBS_SNAP.get("payload")
    if snap is not None and now - JOBS_SNAP["ts"] < JOBS_LIVE_TTL:
        return dict(snap, remux=remux_public())
    with LOCK:
        jobs = load_jobs()
        # v3.93：列表里补版本/创建时间，同名任务才分得清「哪版建的」
        for j in jobs:
            if not j.get("version"):
                j["version"] = j.get("createdVersion") or "旧版"
    try:
        before = json.dumps(jobs, ensure_ascii=False, sort_keys=True)
    except Exception:
        before = None
    deadline = time.time() + 4.0
    for j in jobs:
        if time.time() > deadline and j.get("state") == "running":
            continue          # 本轮跳过，保留上次落盘的状态
        try:
            job_live(j)
        except Exception:
            pass              # 单个任务查失败不该让整张列表报错
    try:
        after = json.dumps(jobs, ensure_ascii=False, sort_keys=True)
    except Exception:
        after = None
    # 只写回「本轮真的查到进度」的那几个字段，避免把别人刚改的（停止/改名/删任务）覆盖掉
    if before is None or after is None or before != after:
        with LOCK:
            cur = load_jobs()
            by_id = {}
            for j in cur:
                by_id[j.get("id")] = j
            for j in jobs:
                tgt = by_id.get(j.get("id"))
                if isinstance(tgt, dict):
                    _merge_live_job(tgt, j)
            save_jobs(cur)
    payload = {"jobs": [{k: v for k, v in j.items() if k != "fileList"} for j in jobs],
               "settings": load_settings(),
               "state": {"diskLow": STATE["diskLow"], "diskFree": STATE["diskFree"],
                         "diskTotal": STATE.get("diskTotal"), "storages": storages(),
                         "proxyRcPort": PROXY_RC_PORT, "version": APP_VERSION},
               "effectiveProxyUrl": proxy_url() or "直连（未启用代理）",
               "proxyScope": proxy_scope(),
               "remux": remux_public()}
    JOBS_SNAP.update({"ts": time.time(), "payload": payload})
    return payload

@action("stop")
def h_stop(body):
    tid = body.get("id")
    with LOCK:
        jobs = load_jobs()
        for j in jobs:
            if j["id"] == tid and j["state"] in ("running", "queued"):
                stop_job_entry(j)
        save_jobs(jobs)
    # v3.93：等 worker 线程真正退出后再返回。在飞的 copyfile 会被 job/stop 打断，
    # 以前这里立刻返回，界面显示「已暂停」但 rclone 里还在传，下一次建任务就抢带宽。
    waited, alive = wait_worker_exit(tid, timeout=float(body.get("wait") or 10))
    return {"ok": True, "waited": waited, "alive": alive}

@action("resume")
def h_resume(body):
    if wait_worker_exit(body.get("id"), timeout=0.2)[1]:
        return {"error": "该任务的旧工作线程还没退出（正在中断在飞文件），等几秒再点「继续」，"
                         "否则新旧线程会同时写同一个文件"}
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
                        # v3.85：这里以前 [:120] 从头部硬切，正好把预检文案尾部的
                        # 「connection reset by peer」切掉，任务卡于是又显示成
                        # 「couldn't get captcha token…请重新登录」。pre_err 已在
                        # cloud_precheck 里压过尾，直接用即可。
                        j["error"] = "预检源目录失败: %s" % pre_err
                        continue
                    if not pre:
                        j["error"] = "源目录已空（%s:%s），无需继续" % (j["remote"], j.get("subpath", "/"))
                        continue
                # v4.03：人工点「继续」= 用户明确想再试一次，把自动重连的次数与窗口清零，
                # 否则被自动重连过 5 次的任务再点继续也不会再自动重连了
                j["_kickN"], j["_kickWin"] = 0, None
                j["_kickAt"], j["_kickBytes"] = 0, None
                if not body.get("_auto"):
                    # v4.13：人工点「继续」= 用户明确再试一次，把自动重试次数清零重新给额度
                    j["_arTries"], j["_arErrAt"] = 0, None
                if running < s["maxConcurrent"]:
                    if not launch_job(j, s):
                        # v4.17/v4.18：旧世代还没退出时不会硬起（见 _start_safe_dir_job）。
                        # 已排回队列的任务由 scheduler 自动重试，这里不算失败。
                        save_jobs(jobs)
                        if j.get("state") == "queued":
                            return {"ok": True, "queued": True}
                        return {"error": j.get("error") or "启动失败"}
                    running += 1
                else:
                    j["state"] = "queued"
                break
        save_jobs(jobs)
    return {"ok": True}

@action("jobretryfailed")
def h_jobretryfailed(body):
    with LOCK:
        s = load_settings()
        jobs = load_jobs()
        job = next((j for j in jobs if j.get("id") == body.get("id")), None)
        if not job:
            return {"error": "任务不存在"}
        if job.get("naming") != "v2" or job.get("single_file"):
            return {"error": "只有安全命名的目录任务支持按文件重试"}
        if job.get("state") in ("running", "queued"):
            return {"error": "任务正在运行或排队中，请先暂停再重试失败项"}
        plan = load_file_plan(job["id"])
        if not plan:
            return {"error": "没有找到该任务的文件清单，请直接点「继续」"}
        n = 0
        for e in plan["entries"]:
            if e.get("status") == "failed":
                e["status"], e["error"], e["attempts"] = "pending", "", 0
                n += 1
        if not n:
            return {"error": "没有失败的文件需要重试"}
        save_file_plan(job["id"], plan)
        job["error"] = ""
        job["filesFailed"] = 0
        running = sum(1 for j in jobs if j["state"] == "running")
        if running < s["maxConcurrent"]:
            launch_job(job, s)
        else:
            job["state"] = "queued"
        save_jobs(jobs)
    return {"ok": True, "count": n}

@action("jobreproxy")
def h_jobreproxy(body):
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

@action("remove")
def h_remove(body):
    tid = body.get("id")
    with LOCK:
        jobs = load_jobs()
        for j in jobs:
            # 转移任务可能挂着 rclone 子进程，删记录前先收掉，避免孤儿进程
            if j["id"] == tid and j.get("kind") == "xfer":
                _xfer_kill(j)
            if j["id"] == tid and j.get("naming") == "v2":
                ev = _safe_stop_event(tid)
                if ev:
                    ev.set()
                # v4.15：记录都删了，还挂着异步 job 就永久变成孤儿流量
                try:
                    stop_job_group(tid, bool(j.get("proxy")))
                except Exception:
                    pass
    # v3.93：线程还在收尾就删记录，会让它继续往已删除的任务写进度/计划文件。
    # 先等在飞文件中断，再删；等不到就拒绝并提示用户先「停止」。
    waited, alive = wait_worker_exit(tid, timeout=float(body.get("wait") or 10))
    if alive:
        return {"error": "任务仍在收尾（在飞文件还没中断），等几秒再删记录", "alive": True}
    with LOCK:
        jobs = load_jobs()
        jobs = [j for j in jobs if j["id"] != tid]
        save_jobs(jobs)
    try:                    # v3.83：文件级执行计划随任务记录一起删，不留下孤儿 JSON
        os.remove(_plan_path(body.get("id")))
    except Exception:
        pass
    return {"ok": True}

@action("stats")
def h_stats(body):
    try:
        d = merged_stats()
    except Exception as e:
        return {"speed": 0, "bytes": 0, "transferring": [], "error": str(e)[:150]}
    spd, _ = instant_speed(int(d.get("bytes", 0) or 0))   # 保留采样状态，历史页仍用它
    # v4.15：没有任何在跑/排队的任务时速度必须是 0；有任务时用「在传文件实测速度合计」。
    # 残留 / 重启的异步 job 会让累计字节继续涨，按累计差分算会显示还在下载，其实任务早停了。
    try:
        with LOCK:
            busy = any(str(j.get("state") or "") in ("running", "queued") for j in load_jobs())
    except Exception:
        busy = True
    if not busy:
        spd = 0.0
    else:
        spd = round(active_speed(d), 1)
    return {"speed": spd, "bytes": d.get("bytes", 0),
            "transferring": [{"name": t.get("name", ""), "bytes": t.get("bytes", 0),
                              "total": t.get("totalBytes", 0), "speed": t.get("speed", 0)}
                             for t in d.get("transferring", [])]}

@action("settings")
def h_settings(body):
    return {"settings": load_settings(),
            "state": {"diskLow": STATE["diskLow"], "diskFree": STATE["diskFree"],
                    "diskTotal": STATE.get("diskTotal"), "storages": storages(),
                    "proxyRcPort": PROXY_RC_PORT, "version": APP_VERSION},
            "effectiveProxyUrl": proxy_url() or "直连（未启用代理）",
            "netMode": net_mode(), "proxyScope": proxy_scope(),
            "remux": remux_public()}

@action("remuxscan")
def h_remuxscan(body):
    raw = str(body.get("path") or "").strip()
    root = raw if raw.startswith("/") else os.path.join(DOWNLOAD_ROOT, raw) if raw else DOWNLOAD_ROOT
    root = os.path.normpath(root)
    roots = [DOWNLOAD_ROOT.rstrip("/")] + [p["console"].rstrip("/")
                                           for p in (STATE.get("volumePairs") or [])]
    # 只允许扫下载根/已挂载卷之内的目录：这个接口会批量起 ffmpeg，别给它任意路径
    if not any(root == r or root.startswith(r + "/") for r in roots if r):
        return {"error": "只能扫描下载目录内的路径"}
    if not os.path.isdir(root):
        return {"error": "目录不存在：%s" % root}
    try:
        n = int(body.get("depth") or 3)
    except Exception:
        n = 3
    res = remux_scan(root, maxdepth=max(1, min(n, 6)))
    res["root"] = root
    return res

@action("autotune")
def h_autotune(body):
    if AUTOTUNE_RUN.get("running"):
        return {"error": "已有一次测速在进行（%s），等它跑完" % (AUTOTUNE_RUN.get("step") or "")}
    jid = body.get("id")
    if not jid:
        return {"error": "请指定要测速的任务"}
    s = load_settings()
    secs = int(body.get("seconds") or s.get("autoTuneSeconds") or 45)
    settle = int(body.get("settle") or s.get("autoTuneSettle") or 20)
    cands = [(4, 4), (2, 8), (1, 16), (8, 2)]
    threading.Thread(target=_autotune_worker, args=(jid, secs, settle, cands),
                     daemon=True).start()
    # 先占位，避免并发点两次都通过上面的 running 判定
    AUTOTUNE_RUN.update({"running": True, "job": jid, "step": "准备",
                         "results": [], "best": None, "error": "", "note": ""})
    return {"ok": True, "candidates": [at_label(*c) for c in cands],
            "seconds": secs, "settle": settle,
            "etaSeconds": len(cands) * (secs + settle + 6)}

@action("autotuneinfo")
def h_autotuneinfo(body):
    st = _at_read()
    return {"ok": True, "best": at_best(),
            "history": (st.get("history") or [])[-5:],
            "run": dict(AUTOTUNE_RUN),
            "candidates": [at_label(*c) for c in AUTOTUNE_CANDIDATES],
            "maxConns": AUTOTUNE_MAX_CONNS}

@action("tput")
def h_tput(body):
    job = next((j for j in load_jobs() if j.get("id") == body.get("id")), None)
    if not job:
        return {"error": "任务不存在"}
    secs = max(5, min(120, int(body.get("seconds") or 20)))
    return {"ok": True, "seconds": secs,
            "mbps": round(measure_rate(job.get("dest"), secs), 2),
            "destBytes": dir_bytes(job.get("dest"))}

@action("savesettings")
def h_savesettings(body):
    with LOCK:
        old = load_settings()
        s = dict(old)
        for k in SETTINGS_WRITABLE:
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

@action("resetsettings")
def h_resetsettings(body):
    with LOCK:
        s = dict(DEFAULTS)
        # 清空「已完成传输」是显示层面的水位，不应该因为恢复默认设置而撤销
        s["transfersClearedAt"] = load_settings().get("transfersClearedAt", 0)
        save_settings(s)
        STATE["firedStart"] = None
        STATE["firedPause"] = None
    return {"ok": True, "settings": s,
            "effectiveProxyUrl": proxy_url() or "直连（未启用代理）",
            "proxyScope": proxy_scope()}

@action("cleartransfers")
def h_cleartransfers(body):
    with LOCK:
        s = load_settings()
        s["transfersClearedAt"] = time.time()
        save_settings(s)
    return {"ok": True}

@action("clearfinished")
def h_clearfinished(body):
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
            if j.get("naming") == "v2":
                try:        # v3.83：一并清掉该任务的文件级执行计划
                    os.remove(_plan_path(j["id"]))
                except Exception:
                    pass
        save_jobs(keep)
        s = load_settings()
        s["transfersClearedAt"] = time.time()
        save_settings(s)
    return {"ok": True, "removed": removed}

@action("pushplustest")
def h_pushplustest(body):
    s = load_settings()
    token = str(body.get("token") or s.get("pushplusToken") or "").strip()
    if not pushplus_token_ok(token):
        return {"error": "PushPlus Token 未填写或格式不对（16~64 位字母数字）"}
    res = pushplus_send(token, "云梭｜测试通知",
                        "<p>这是一条测试消息：收到说明 PushPlus 链路正常。</p>"
                        "<p style='color:#888;font-size:12px'>之后任务下载成功 / 失败会各推一条。</p>")
    code = int(res.get("code") or 0) if isinstance(res, dict) else -1
    if code != 200:
        return {"error": "PushPlus 返回：%s" % str((res or {}).get("msg") or res)[:150]}
    return {"ok": True, "msg": (res or {}).get("msg") or "已发送"}

@action("notifystate")
def h_notifystate(body):
    s = load_settings()
    token = str(s.get("pushplusToken") or "")
    seen, _ = _notify_state_load()
    return {"on": bool(_truthy(s.get("pushplusOn"))),
            "tokenSet": pushplus_token_ok(token),
            "tokenTail": token[-4:] if token else "",
            "sentCount": len(seen)}

@action("quota")
def h_quota(body):
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

@action("history")
def h_history(body):
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

@action("about")
def h_about(body):
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

@action("transfers")
def h_transfers(body):
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

@action("bwlimit")
def h_bwlimit(body):
    try:
        d = rc("core/bwlimit")
        return {"bytesPerSecond": d.get("bytesPerSecond", -1),
                "transferred": d.get("bytesTransferred", 0)}
    except Exception as e:
        return {"error": str(e)[:150]}

@action("setbwlimit")
def h_setbwlimit(body):
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

@action("version")
def h_version(body):
    try:
        d = rc("core/version")
        return {"version": str(d.get("version", "")), "os": str(d.get("os", "")),
                "arch": str(d.get("arch", "")), "consoleVersion": APP_VERSION,
                "https": tls_cert_ready()}
    except Exception as e:
        return {"error": str(e)[:150], "consoleVersion": APP_VERSION,
                "https": tls_cert_ready()}

@action("jobdetail")
def h_jobdetail(body):
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
    if job.get("naming") == "v2":
        # v3.83：详情里同时给出原名与落盘名，以及文件级进度 / 失败清单
        d["naming"] = "v2"
        d["originalName"] = job.get("leaf") or basename_of(job.get("subpath") or "")
        d["safeName"] = job.get("safe_leaf") or ""
        plan = None if job.get("single_file") else load_file_plan(job["id"])
        if plan:
            entries = plan.get("entries") or []
            d["fileStats"] = {
                "total": len(entries),
                "done": sum(1 for e in entries if e.get("status") in ("done", "skipped")),
                "failed": sum(1 for e in entries if e.get("status") == "failed"),
                "pending": sum(1 for e in entries if e.get("status") in ("pending", "running")),
            }
            fails = [{"src": e.get("src"), "dst": e.get("dst"),
                      "error": e.get("error") or "", "attempts": e.get("attempts") or 0}
                     for e in entries if e.get("status") == "failed"]
            d["failedFiles"] = fails[:200]
            d["failedTruncated"] = len(fails) > 200
            d["nameMap"] = [{"src": e.get("src"), "dst": e.get("dst"),
                             "status": e.get("status"), "size": e.get("size") or 0}
                            for e in entries if e.get("src") != e.get("dst")][:200]
            with SAFE_JOBS_LOCK:
                live_state = SAFE_LIVE.get(job["id"]) or {}
            lives = dict(live_state.get("live") or {})
            # v3.92：用 worker 的实时采样填字节/速度，不再写死 0
            d["transferring"] = build_live_transferring(entries, lives)
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

@action("cleanup")
def h_cleanup(body):
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

@action("accounts")
def h_accounts(body):
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
    # v3.93：账号页只刷一排超时时，用户看不出「某几个账号凭证已经彻底死了」。
    # 这里聚合一次，前端可以只渲染一条提示 + 直接去重新登录。
    dead = [{"name": a["name"], "note": (a.get("cred") or {}).get("note", "")}
            for a in out
            if isinstance(a.get("cred"), dict) and a["cred"].get("state") == "dead"]
    alerts = []
    if dead:
        alerts.append({"kind": "cred_dead", "level": "error",
                       "title": "%d 个账号凭证已失效" % len(dead),
                       "text": "、".join(d["name"] for d in dead[:5])
                               + "，点对应账号的「重新登录」重新采集 Token",
                       "accounts": dead})
    return {"accounts": out, "ghosts": ghosts, "alerts": alerts}

@action("providers")
def h_providers(body):
    return {"providers": PROVIDERS}

@action("credcheck")
def h_credcheck(body):
    names = body.get("names") if isinstance(body.get("names"), list) else None
    rs = cred_keepalive_once(names)
    return {"results": rs, "okCount": sum(1 for r in rs if r["ok"]), "total": len(rs)}

@action("acctadd")
def h_acctadd(body):
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

@action("acctdel")
def h_acctdel(body):
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

@action("acctpurge")
def h_acctpurge(body):
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

@action("acctrename")
def h_acctrename(body):
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

@action("acctrepair")
def h_acctrepair(body):
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

@action("accttest")
def h_accttest(body):
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

@action("acctget")
def h_acctget(body):
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

@action("acctedit")
def h_acctedit(body):
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

@action("acctimport")
def h_acctimport(body):
    items = body.get("items") if isinstance(body.get("items"), list) else []
    if not items:
        return {"error": "没有可导入的条目"}
    return import_items(items, create=bool(body.get("create")), dry=bool(body.get("dryRun")))

@action("acctimporttext")
def h_acctimporttext(body):
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

@action("acctident")
def h_acctident(body):
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

@action("urldl")
def h_urldl(body):
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
    # v4.07：与 action=start 同一道闸，不可写的卷不接单
    if not uvol.get("writable", True):
        return {"error": "「%s」（%s）当前不可写（写数据被系统拒绝），请改选其它存储空间"
                         % (uvol.get("name") or uroot, uroot)}
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

@action("cloudrm")
def h_cloudrm(body):
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

@action("renamebatch")
def h_renamebatch(body):
    remote = str(body.get("remote") or "").strip()
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return {"error": "缺少参数：items（要重命名的文件列表）"}
    if len(items) > RENAME_MAX_ITEMS:
        return {"error": "一次最多重命名 %d 项，请分批处理" % RENAME_MAX_ITEMS}
    return rename_batch(remote, str(body.get("path") or "").strip("/"), items)

@action("localrenamebatch")
def h_localrenamebatch(body):
    return local_rename_batch(str(body.get("path") or ""), body.get("items"))

@action("trashlist")
def h_trashlist(body):
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

@action("trashop")
def h_trashop(body):
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

@action("playinfo")
def h_playinfo(body):
    return cloud_play_info(str(body.get("remote") or "").strip(),
                           str(body.get("path") or "").strip("/"))

@action("unzipinfo")
def h_unzipinfo(body):
    return unzip_preview(str(body.get("remote") or "").strip(),
                         str(body.get("path") or "").strip("/"),
                         str(body.get("password") or ""))

@action("unzip")
def h_unzip(body):
    return unzip_start_cloud(str(body.get("remote") or "").strip(),
                             str(body.get("path") or "").strip("/"),
                             str(body.get("password") or ""))

@action("localunzip")
def h_localunzip(body):
    return local_unzip_start(str(body.get("path") or ""),
                             str(body.get("password") or ""),
                             _truthy(body.get("deleteAfter")))

@action("cloudmv")
def h_cloudmv(body):
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

@action("sharecreate")
def h_sharecreate(body):
    return share_create(body.get("remote"), body.get("items"),
                        body.get("days", 7), body.get("passCode", ""),
                        body.get("limit", 100))

@action("cloudlink")
def h_cloudlink(body):
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

@action("speedtest")
def h_speedtest(body):
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

@action("btadd", "btlist", "btctrl", "ytadd", "ytlist", "ytdel")
def h_disabled_legacy(body):
    return {"error": "BT / 磁力与视频下载已停用（aria2-pro、yt-dlp 容器已删除），请 Ctrl+F5 刷新页面",
            "tasks": []}

@action("verify")
def h_verify(body):
    return verify_compare(body.get("remote"), body.get("cloudPath"),
                          body.get("localPath"), body.get("mode"), body.get("limit"))

@action("folderstatus")
def h_folderstatus(body):
    remote = str(body.get("remote") or "").strip()
    if not remote:
        return {"error": "缺少参数：remote"}
    return folder_status_batch(remote, str(body.get("path") or "").strip("/"),
                               body.get("folders"), bool(body.get("refresh")))

@action("folderack")
def h_folderack(body):
    remote = str(body.get("remote") or "").strip()
    if not remote:
        return {"error": "缺少参数：remote"}
    return folder_ack(remote, str(body.get("path") or "").strip("/"),
                      body.get("names"))

@action("cloudmkdir")
def h_cloudmkdir(body):
    return cloud_mkdir(str(body.get("remote") or "").strip(),
                       str(body.get("path") or "").strip("/"),
                       body.get("name"))

@action("cloudmove")
def h_cloudmove(body):
    return cloud_move_batch(str(body.get("remote") or "").strip(),
                            str(body.get("path") or "").strip("/"),
                            body.get("items"),
                            str(body.get("dest") or "").strip("/"))

@action("cloudrmbatch")
def h_cloudrmbatch(body):
    return cloud_rm_batch(str(body.get("remote") or "").strip(),
                          str(body.get("path") or "").strip("/"),
                          body.get("items"), bool(body.get("hard")))

@action("localmkdir")
def h_localmkdir(body):
    return local_mkdir(str(body.get("path") or ""), body.get("name"))

@action("localmove")
def h_localmove(body):
    return local_move_batch(str(body.get("path") or ""), body.get("items"),
                            str(body.get("dest") or ""))

@action("localtrash")
def h_localtrash(body):
    return local_trash_batch(str(body.get("path") or ""), body.get("items"))

@action("localtrashlist")
def h_localtrashlist(body):
    return local_trash_list()

@action("localtrashrestore")
def h_localtrashrestore(body):
    return local_trash_restore(body.get("ids"), bool(body.get("all")))

@action("localtrashpurge")
def h_localtrashpurge(body):
    return local_trash_purge(body.get("ids"), bool(body.get("all")))

@action("localfiles")
def h_localfiles(body):
    return localfiles_listing(str(body.get("path") or ""))

@action("localdirs")
def h_localdirs(body):
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

@action("health")
def h_health(body):
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
def api(action, body):
    fn = HANDLERS.get(action)
    if fn is None:
        raise RuntimeError("未知操作")
    return fn(body)

# ---------------------------------------------------------------------------
# 前端静态资源（./static/，容器内即 /app/static）
# ---------------------------------------------------------------------------
# v3.80：候选里必须带上「脚本所在目录」—— 之前只有 /app 与 /vol1 两个写死的路径，
# 用户把仓库放到 /opt/cloud-shuttle 或任意 bind 目录时，两处都不存在，STATIC_DIR 就
# 落回不存在的 /app/static，整站静态资源 404（表现为打开页面一片白）。
_APP_HOME = APP_HOME
STATIC_DIR_CANDIDATES = ("/app/static", "/vol1/@appshare/zcode/console/static",
                         os.path.join(_APP_HOME, "static"))
STATIC_DIR = next((p for p in STATIC_DIR_CANDIDATES if os.path.isdir(p)), STATIC_DIR_CANDIDATES[-1])
COLLECTOR_PS_CANDIDATES = ("/app/collector.ps1", "/vol1/@appshare/zcode/console/collector.ps1",
                           os.path.join(APP_HOME, "collector.ps1"))
LOGIN_PS_CANDIDATES = ("/app/login-collector.ps1", "/vol1/@appshare/zcode/console/login-collector.ps1",
                       os.path.join(APP_HOME, "login-collector.ps1"))


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

_STATIC_VER_CACHE = {"key": None, "value": ""}


def static_asset_version():
    """v4.00：app.js / app.css 的内容指纹，注入 HTML 的 `?v=` 参数。

    指纹键用 mtime_ns + size：改一行就变；结果缓存起来避免每次开页面都 stat。
    资源 URL 跟着内容变，浏览器就不会再拿旧 JS 配新后端，也不需要 Ctrl+F5。
    """
    parts = []
    for name in ("app.js", "app.css"):
        try:
            st = os.stat(os.path.join(STATIC_DIR, name))
            parts.append("%s:%d:%d" % (name, st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(name + ":missing")
    key = "|".join(parts)
    if _STATIC_VER_CACHE["key"] == key:
        return _STATIC_VER_CACHE["value"]
    value = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    _STATIC_VER_CACHE["key"], _STATIC_VER_CACHE["value"] = key, value
    return value


def render_static_html(data):
    """把 HTML 里的资源占位符换成当前指纹；没有占位符就原样返回。"""
    if b"__CACHE_VER__" not in data:
        return data
    return data.replace(b"__CACHE_VER__", static_asset_version().encode("ascii"))


def static_cache_control(ext):
    """HTML 不进缓存；业务 js/css 每次协商；其余资源按天缓存。"""
    if ext == ".html":
        return "no-store, max-age=0"
    if ext in (".js", ".css"):
        return "no-cache"
    return "max-age=86400"


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
        is_html = ext == ".html"
        try:
            stt = os.stat(full)
            mtime, size = int(stt.st_mtime), stt.st_size
            etag = '"%x-%x"' % (mtime, size)
            # 协商缓存：no-cache 仍然每次询问，命中就 304；HTML 不参与，必须每次给最新
            if not is_html and (self.headers.get("If-None-Match") or "").strip() == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            with open(full, "rb") as f:
                data = f.read()
            if is_html:
                data = render_static_html(data)
            self.send_response(200)
            self.send_header("Content-Type", STATIC_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            if not is_html:
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", self.date_time_string(mtime))
            self.send_header("Cache-Control", static_cache_control(ext))
            if is_html:
                self.send_header("Pragma", "no-cache")
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
            t0 = time.time()
            out = json.dumps(api(action, body)).encode()
            dt = time.time() - t0
            # v3.98：前端有 8 秒看门狗，但到底是"服务端慢"还是"网络慢"看不出来。
            # 超过 2 秒的动作打到容器日志，下次弹「请求超时」时能直接对账。
            if dt >= 2.0:
                print("%s 慢请求 %-12s %5.1fs" % (time.strftime("%F %T"), action or "-", dt), flush=True)
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
    # v4.10：PushPlus 通知巡检（默认关；设置里填 token 并打开开关后生效）
    threading.Thread(target=notify_loop, daemon=True).start()
    # v4.13：失败任务自动重试（默认关；设置里打开后，失败满 N 分钟自动点一次「继续」）
    threading.Thread(target=auto_retry_loop, daemon=True).start()
    # v4.15：孤儿传输巡检（重启/异常退出留下的 rclone 异步 job，没有任务认领却还在吃带宽）
    threading.Thread(target=orphan_guard_loop, daemon=True).start()
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

