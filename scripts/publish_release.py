#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地一键发布 Release：构建发行包 → 校验 → 上传（或新建）GitHub Release。

不依赖 GitHub Actions，也不需要 workflow 权限，只要本机 `gh auth login` 过即可。
若 rust/dist/ 下已有 Rust 原生版安装包，会一并上传到同一个 Release。
用法（仓库根目录）：
    python3 scripts/publish_release.py            # 用源码里的 APP_VERSION 作为 tag
    python3 scripts/publish_release.py --tag v3.55
    python3 scripts/publish_release.py --draft   # 先建草稿，检查后再发布
"""
import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
ASSET = "cloud-shuttle.zip"
RUST_DIST = ROOT / "rust" / "dist"
RUST_ASSETS = (
    "cloud-shuttle-rs-windows-x86_64.zip",
    "cloud-shuttle-rs-linux-x86_64.tar.gz",
    "cloud-shuttle-desktop.exe",
    "cloud-shuttle-desktop.exe.sha256",
)


def run(args, **kw):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", help="版本 tag，默认取源码 APP_VERSION")
    ap.add_argument("--draft", action="store_true", help="创建为草稿")
    ap.add_argument("--notes", default="", help="发行说明文本")
    opt = ap.parse_args()

    tag = opt.tag
    if not tag:
        import re
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"',
                      (ROOT / "download-console.py").read_text(encoding="utf-8"))
        if not m:
            raise SystemExit("无法从源码读取 APP_VERSION，请用 --tag 指定")
        tag = m.group(1)
    if not tag.startswith("v"):
        raise SystemExit("tag 必须以 v 开头，例如 v3.55")

    # 1. 构建
    r = run([sys.executable, "scripts/build_release.py"])
    print(r.stdout.strip())
    if r.returncode:
        print(r.stderr.strip())
        return r.returncode

    z, sha_file = DIST / ASSET, DIST / (ASSET + ".sha256")
    want = sha_file.read_text(encoding="utf-8").split()[0]
    got = hashlib.sha256(z.read_bytes()).hexdigest()
    if got != want:
        raise SystemExit("整包 sha256 不一致，已中止发布")
    with zipfile.ZipFile(z) as f:
        mani = json.loads(f.read("release-manifest.json"))
    if mani.get("version") != tag:
        raise SystemExit("包内版本 %s 与 tag %s 不一致，请先升 APP_VERSION"
                         % (mani.get("version"), tag))
    print("发行包校验通过：%s · %d 个文件" % (tag, len(mani["files"])))

    # 2. 已存在则覆盖资源，否则新建 Release
    native = [RUST_DIST / n for n in RUST_ASSETS if (RUST_DIST / n).is_file()]
    files = [str(z), str(sha_file)] + [str(p) for p in native]
    print("本次上传资源：" + ", ".join(Path(f).name for f in files))
    exists = run(["gh", "release", "view", tag]).returncode == 0
    if exists:
        cmd = ["gh", "release", "upload", tag] + files + ["--clobber"]
        action = "更新已有 Release 资源"
    else:
        cmd = ["gh", "release", "create", tag] + files + [
               "--title", "云梭 CloudShuttle " + tag, "--generate-notes"]
        if opt.notes:
            cmd += ["--notes", opt.notes]
        if opt.draft:
            cmd += ["--draft"]
        action = "创建 Release"
    r = run(cmd)
    if r.returncode:
        print("%s 失败：%s" % (action, (r.stdout + r.stderr).strip()[:500]))
        return r.returncode
    print("%s 完成：%s" % (action, (r.stdout or "").strip()[:300]))

    info = run(["gh", "repo", "view", "--json", "url", "--jq", ".url"])
    print("仓库：%s/releases/tag/%s" % (info.stdout.strip(), tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
