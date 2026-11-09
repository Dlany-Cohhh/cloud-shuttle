#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建用于「在线更新」的发行包。

用法（仓库根目录执行）：
    python3 scripts/build_release.py

产物：
    dist/cloud-shuttle.zip             固定文件名，后端 UPDATE_ASSET 默认指向它
    dist/cloud-shuttle.zip.sha256      整包 sha256，供后端下载前校验

包内结构：
    release-manifest.json             {"version": "v3.54", "files": {路径: sha256, ...}}
    download-console.py
    static/index.html ...             只含后端 UPDATE_FILES 白名单里的程序文件

白名单直接从 download-console.py 的 UPDATE_FILES 解析，避免两边漂移。
包里**不含**任何配置、任务记录、账号凭证、TLS 私钥与下载数据。
"""
import ast
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "download-console.py"
DIST = ROOT / "dist"
ASSET = "cloud-shuttle.zip"


def source_constants():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = getattr(target, "id", None)
            if name == "APP_VERSION":
                out["version"] = ast.literal_eval(node.value)
            elif name in ("UPDATE_FILES", "UPDATE_REQUIRED"):
                expr = node.value
                if isinstance(expr, ast.Call):          # frozenset((...)) / tuple(...)
                    expr = expr.args[0]
                out[name] = tuple(ast.literal_eval(expr))
            elif name == "UPDATE_ASSET":
                # os.environ.get("UPDATE_ASSET", "cloud-shuttle.zip") → 取默认值
                expr = node.value
                out["asset"] = ast.literal_eval(
                    expr.args[1] if isinstance(expr, ast.Call) and len(expr.args) > 1
                    else expr)
    for key in ("version", "UPDATE_FILES", "UPDATE_REQUIRED"):
        if key not in out:
            raise SystemExit("无法从源码解析 " + key)
    return out


def main():
    c = source_constants()
    version = c["version"]
    asset = c.get("asset") or ASSET
    files, required = c["UPDATE_FILES"], c["UPDATE_REQUIRED"]

    payload = {}
    for rel in files:
        p = ROOT / rel
        if not p.is_file():
            if rel in required:
                raise SystemExit("缺少必需文件：" + rel)
            continue
        payload[rel] = p.read_bytes()
    for rel in required:
        if rel not in payload:
            raise SystemExit("缺少必需文件：" + rel)

    # 提交前顺手确认主程序语法正确，避免把坏包发成 Release
    compile(payload["download-console.py"].decode("utf-8"), "download-console.py", "exec")

    manifest = {
        "version": version,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": "Dlany-Cohhh/cloud-shuttle",
        "files": {k: hashlib.sha256(v).hexdigest() for k, v in payload.items()},
    }

    DIST.mkdir(exist_ok=True)
    zpath = DIST / asset
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in sorted(payload):
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, payload[rel])
        z.writestr("release-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    # 显式 LF：Windows 下 write_text 会翻译成 CRLF，导致 `sha256sum -c` 找不到文件
    with open(DIST / (asset + ".sha256"), "w", encoding="utf-8", newline="\n") as f:
        f.write(digest + "  " + asset + "\n")
    print("版本 %s · %d 个文件 · %s (%d bytes)" % (version, len(payload), asset, zpath.stat().st_size))
    print("sha256 " + digest)
    print("发布时把 dist/%s 与 dist/%s.sha256 一起上传到 Release" % (asset, asset))
    return 0


if __name__ == "__main__":
    sys.exit(main())
