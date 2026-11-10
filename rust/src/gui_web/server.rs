//! 进程内的「虚拟 HTTP 服务」。
//!
//! WebView 用 `cs://localhost/` 自定义协议加载页面：所有请求都由这个模块在
//! 内存里响应，**不监听任何端口**，浏览器地址栏、CORS、跨进程通信都不存在。
//! 前端的 `/static/*` 就是仓库根目录那一份原文件，`/media/*` 直接读本地下载目录。

use super::assets;
use crate::localfs;
use std::borrow::Cow;
use std::path::{Path, PathBuf};
use wry::http::{header, Method, Request, Response, StatusCode};

pub type Body = Cow<'static, [u8]>;

/// 1×1 透明 PNG：缩略图代理在桌面版不可用，用它避免破图。
const BLANK_PNG: &[u8] = &[
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4,
    0x89, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44, 0x41, 0x54, 0x08, 0xd7, 0x63, 0xf8, 0xcf, 0xc0, 0xf0,
    0x1f, 0x00, 0x05, 0x00, 0x01, 0xff, 0x89, 0x99, 0x3d, 0x1d, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45,
    0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
];

pub fn respond(req: &Request<Vec<u8>>, download_root: &Path) -> Response<Body> {
    let path = req.uri().path();
    let method = req.method();

    if path == "/api" {
        // 正常情况下 POST /api 已被注入的 fetch shim 接管，走到这里说明脚本没生效。
        return json(
            StatusCode::BAD_GATEWAY,
            r#"{"ok":false,"error":"桌面版 API 桥接未就绪，请重启应用"}"#,
        );
    }
    if method != Method::GET && method != Method::HEAD {
        return text(StatusCode::METHOD_NOT_ALLOWED, "method not allowed");
    }
    if path == "/thumb" {
        return bytes(StatusCode::OK, Cow::Borrowed(BLANK_PNG), "image/png", None);
    }
    if path == "/login-collector.bat" {
        return text(
            StatusCode::OK,
            "@echo off\r\necho 桌面窗口版不需要采集脚本，请在「设置」里直接填写账号凭据。\r\npause\r\n",
        );
    }
    if path.starts_with("/media/") {
        let range = req
            .headers()
            .get(header::RANGE)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        return media(
            download_root,
            path,
            range.as_deref(),
            method == Method::HEAD,
        );
    }

    let url = normalize(path);
    let Some((data, mime)) = assets::get(&url) else {
        return text(StatusCode::NOT_FOUND, "404 Not Found");
    };
    bytes(StatusCode::OK, Cow::Borrowed(data), mime, None)
}

/// 兼容 `/`、`/index.html`、`/static/x`、`/x` 四种写法，和服务器版 web.rs 一致。
fn normalize(path: &str) -> String {
    let raw = percent_decode(path.trim_start_matches('/'));
    if raw.is_empty() || raw == "index.html" {
        return "/static/index.html".into();
    }
    if raw.starts_with("static/") {
        return format!("/{}", raw);
    }
    format!("/static/{}", raw)
}

fn media(root: &Path, path: &str, range: Option<&str>, head: bool) -> Response<Body> {
    use std::io::{Read, Seek, SeekFrom};
    let rel = percent_decode(path.trim_start_matches("/media/"));
    let full: PathBuf = match localfs::safe_path(root, &rel) {
        Ok(p) => p,
        Err(e) => return text(StatusCode::NOT_FOUND, &e),
    };
    if !full.is_file() {
        return text(StatusCode::NOT_FOUND, "404 Not Found");
    }
    let mut f = match std::fs::File::open(&full) {
        Ok(f) => f,
        Err(_) => return text(StatusCode::NOT_FOUND, "404 Not Found"),
    };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let mime = mime_of(&full);
    let (status, start, end) = match parse_range(range, len) {
        Some((a, b)) => (StatusCode::PARTIAL_CONTENT, a, b),
        None if range.is_some() => {
            return Response::builder()
                .status(StatusCode::RANGE_NOT_SATISFIABLE)
                .header(header::CONTENT_RANGE, format!("bytes */{}", len))
                .body(Cow::Owned(Vec::new()))
                .unwrap();
        }
        None => (StatusCode::OK, 0u64, len.saturating_sub(1)),
    };
    let mut buf = Vec::new();
    if !head {
        if start > 0 && f.seek(SeekFrom::Start(start)).is_err() {
            return text(StatusCode::NOT_FOUND, "seek failed");
        }
        let want = (end - start + 1).min(8 * 1024 * 1024) as usize;
        buf.resize(want, 0);
        let mut got = 0;
        while got < want {
            match f.read(&mut buf[got..]) {
                Ok(0) | Err(_) => break,
                Ok(n) => got += n,
            }
        }
        buf.truncate(got);
    }
    let last = start + buf.len() as u64;
    let last = last.saturating_sub(1);
    let mut b = Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, mime)
        .header(header::ACCEPT_RANGES, "bytes")
        .header(header::CONTENT_LENGTH, buf.len())
        .header(header::CACHE_CONTROL, "no-store")
        .header(header::ACCESS_CONTROL_ALLOW_ORIGIN, "*");
    if status == StatusCode::PARTIAL_CONTENT {
        b = b.header(
            header::CONTENT_RANGE,
            format!("bytes {}-{}/{}", start, last, len),
        );
    }
    b.body(Cow::Owned(buf)).unwrap()
}

fn parse_range(h: Option<&str>, len: u64) -> Option<(u64, u64)> {
    let spec = h?.strip_prefix("bytes=")?;
    let mut it = spec.splitn(2, '-');
    let a = it.next()?.trim();
    let b = it.next().unwrap_or("").trim();
    if len == 0 {
        return Some((0, 0));
    }
    if a.is_empty() {
        let suffix: u64 = b.parse().ok()?;
        if suffix == 0 {
            return None;
        }
        Some((len.saturating_sub(suffix), len - 1))
    } else {
        let start: u64 = a.parse().ok()?;
        if start >= len {
            return None;
        }
        let end = if b.is_empty() {
            len - 1
        } else {
            b.parse::<u64>().ok()?.min(len - 1)
        };
        (end >= start).then_some((start, end))
    }
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let (Some(h), Some(l)) = (hex(b[i + 1]), hex(b[i + 2])) {
                out.push(h * 16 + l);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

fn mime_of(p: &Path) -> &'static str {
    match p
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase()
        .as_str()
    {
        "mp4" => "video/mp4",
        "mkv" => "video/x-matroska",
        "webm" => "video/webm",
        "mov" => "video/quicktime",
        "avi" => "video/x-msvideo",
        "mp3" => "audio/mpeg",
        "flac" => "audio/flac",
        "m4a" | "aac" => "audio/mp4",
        "wav" => "audio/wav",
        "jpg" | "jpeg" => "image/jpeg",
        "png" => "image/png",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "srt" | "ass" | "vtt" => "text/plain; charset=utf-8",
        "pdf" => "application/pdf",
        "txt" | "log" | "nfo" => "text/plain; charset=utf-8",
        "html" | "htm" => "text/html; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "js" => "application/javascript; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        _ => "application/octet-stream",
    }
}

fn bytes(
    status: StatusCode,
    body: Body,
    mime: &str,
    extra: Option<(header::HeaderName, String)>,
) -> Response<Body> {
    let n = body.len();
    let mut b = Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, mime)
        .header(header::CONTENT_LENGTH, n)
        .header(header::CACHE_CONTROL, "no-cache")
        .header(header::ACCESS_CONTROL_ALLOW_ORIGIN, "*");
    if let Some((k, v)) = extra {
        b = b.header(k, v);
    }
    b.body(body).unwrap()
}

fn text(status: StatusCode, s: &str) -> Response<Body> {
    bytes(
        status,
        Cow::Owned(s.as_bytes().to_vec()),
        "text/plain; charset=utf-8",
        None,
    )
}

fn json(status: StatusCode, s: &str) -> Response<Body> {
    bytes(
        status,
        Cow::Owned(s.as_bytes().to_vec()),
        "application/json; charset=utf-8",
        None,
    )
}
