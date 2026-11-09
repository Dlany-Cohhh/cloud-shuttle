#!/bin/bash
# PikPak token 自动续期（多账号版，建议 root crontab 每小时调用）
# 用法：RCLONE_CONF=/path/to/rclone.conf TOKEN_LOG=/path/to/refresh.log \
#           bash scripts/refresh-pikpak-token.sh
# 原理：refresh_token 单次轮换——谁刷新谁必须立即保存。本脚本逐账号刷新并
# 逐个写回 rclone.conf，rclone 端 TokenSource 过期时会重读文件拿到新鲜 token。
CONF="${RCLONE_CONF:-/etc/rclone/rclone.conf}"
LOG="${TOKEN_LOG:-/var/log/pikpak-token.log}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
export RCLONE_CONF="$CONF"

python3 << 'PYEOF' >> "$LOG" 2>&1
import json, os, re, time, datetime, urllib.request, urllib.parse, urllib.error

CONF = os.environ.get("RCLONE_CONF", "/etc/rclone/rclone.conf")
now = datetime.datetime.now().strftime("%F %T")
lines = open(CONF).read().splitlines()

# 解析所有 [section]
sections = {}
order = []
cur = None
for ln in lines:
    m = re.match(r'^\[(.+)\]\s*$', ln)
    if m:
        cur = m.group(1)
        sections[cur] = {}
        order.append(cur)
        continue
    if cur and "=" in ln and not ln.strip().startswith("#"):
        k, v = ln.split("=", 1)
        sections[cur][k.strip()] = v.strip()

def refresh(rt):
    # GFW 偶发把连接掐断（连接层失败=服务器没收到请求，轮换未发生），此时安全重试；
    # 一旦收到服务器响应（含错误 JSON），说明轮换状态已明确，绝不重试以免丢链
    last_err = None
    for attempt in range(3):
        data = urllib.parse.urlencode({
            "client_id": "YUMx5nI8ZU8Ap8pm",
            "grant_type": "refresh_token",
            "refresh_token": rt,
        }).encode()
        req = urllib.request.Request("https://user.mypikpak.com/v1/auth/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode()), None
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode()[:180]
            except Exception:
                body = ""
            return None, "HTTP%s %s" % (e.code, body)
        except Exception as e:
            last_err = str(e)
            time.sleep(2 + attempt * 3)
    return None, last_err

changed = False
for name in order:
    sec = sections[name]
    if sec.get("type") != "pikpak" or not sec.get("refresh_token"):
        continue
    d, err = refresh(sec["refresh_token"])
    if err:
        print("%s [%s] 错误：%s" % (now, name, err))
        continue
    if "access_token" not in d:
        print("%s [%s] 刷新被拒：%s" % (now, name, json.dumps(d, ensure_ascii=False)[:180]))
        continue
    new_rt = d.get("refresh_token") or sec["refresh_token"]
    exp = datetime.datetime.utcnow() + datetime.timedelta(seconds=d.get("expires_in", 7200))
    sec["token"] = json.dumps({"access_token": d["access_token"],
        "token_type": d.get("token_type", "Bearer"), "refresh_token": new_rt,
        "expiry": exp.strftime("%Y-%m-%dT%H:%M:%SZ")})
    sec["refresh_token"] = new_rt
    changed = True
    print("%s [%s] 成功：有效期至 %s，RT新前缀 %s..." % (now, name, exp.strftime("%F %T"), new_rt[:8]))
    # 逐个立即落盘，防止中途崩溃丢轮换
    if changed:
        out = []
        cur = None
        for ln in lines:
            m = re.match(r'^\[(.+)\]\s*$', ln)
            if m:
                cur = m.group(1)
                out.append(ln)
                continue
            mm = re.match(r'^(refresh_token|token) = ', ln)
            if cur and mm and cur in sections and mm.group(1) in sections[cur]:
                out.append("%s = %s" % (mm.group(1), sections[cur][mm.group(1)]))
                continue
            out.append(ln)
        open(CONF, "w").write("\n".join(out) + "\n")
        lines = out
PYEOF
chown "${RCLONE_CONF_UID:-1000}:${RCLONE_CONF_GID:-1000}" "$CONF" 2>/dev/null || true
