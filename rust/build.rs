//! 构建时把仓库根目录 `static/` 的全部前端资源编进可执行文件。
//!
//! 桌面窗口版不启动 HTTP 服务，也没有 `static/` 目录：所有 CSS / JS / 图标字体
//! 都通过 `include_bytes!` 变成二进制里的一段静态数据，再由进程内的自定义协议
//! 按 URL 返回。这里生成的表只在 `webview` feature 下被引用。

use std::env;
use std::path::{Path, PathBuf};

fn main() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("rust/ 必须位于仓库目录内")
        .join("static");
    println!("cargo:rerun-if-changed={}", root.display());

    if !root.is_dir() {
        // 单独分发 rust/ 目录时仍能编译（只是没有内嵌前端），不要直接失败。
        let out = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR")).join("embedded_assets.rs");
        std::fs::write(out, "pub static ASSETS: &[(&str, &[u8], &str)] = &[];\n")
            .expect("写入 embedded_assets.rs");
        return;
    }

    let mut files = Vec::new();
    collect(&root, &root, &mut files);
    files.sort_by(|a, b| a.0.cmp(&b.0));

    let mut src = String::from(
        "// 由 build.rs 生成，请勿手改。\n\
         pub static ASSETS: &[(&str, &[u8], &str)] = &[\n",
    );
    for (rel, abs) in &files {
        let url = format!("/static/{}", rel.replace('\\', "/"));
        let mime = mime_of(rel);
        // Windows 路径统一成正斜杠，include_bytes! 在两种平台都能接受。
        let path = abs.to_string_lossy().replace('\\', "/");
        src.push_str(&format!(
            "    ({:?}, include_bytes!({:?}), {:?}),\n",
            url, path, mime
        ));
    }
    src.push_str("];\n");

    let out = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR")).join("embedded_assets.rs");
    std::fs::write(out, src).expect("写入 embedded_assets.rs");
}

fn collect(root: &Path, dir: &Path, out: &mut Vec<(String, PathBuf)>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for e in entries.flatten() {
        let p = e.path();
        if p.is_dir() {
            collect(root, &p, out);
        } else if p.is_file() {
            let rel = p
                .strip_prefix(root)
                .unwrap_or(&p)
                .to_string_lossy()
                .replace('\\', "/");
            println!("cargo:rerun-if-changed={}", p.display());
            out.push((rel, p));
        }
    }
}

fn mime_of(name: &str) -> &'static str {
    let lower = name.to_ascii_lowercase();
    if lower.ends_with(".html") || lower.ends_with(".htm") {
        "text/html; charset=utf-8"
    } else if lower.ends_with(".css") {
        "text/css; charset=utf-8"
    } else if lower.ends_with(".js") || lower.ends_with(".mjs") {
        "application/javascript; charset=utf-8"
    } else if lower.ends_with(".json") || lower.ends_with(".map") {
        "application/json; charset=utf-8"
    } else if lower.ends_with(".woff2") {
        "font/woff2"
    } else if lower.ends_with(".woff") {
        "font/woff"
    } else if lower.ends_with(".ttf") {
        "font/ttf"
    } else if lower.ends_with(".svg") {
        "image/svg+xml"
    } else if lower.ends_with(".png") {
        "image/png"
    } else if lower.ends_with(".jpg") || lower.ends_with(".jpeg") {
        "image/jpeg"
    } else if lower.ends_with(".ico") {
        "image/x-icon"
    } else {
        "application/octet-stream"
    }
}
