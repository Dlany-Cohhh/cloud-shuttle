//! 内嵌前端资源表。内容由 `build.rs` 在编译时从仓库根 `static/` 生成。

include!(concat!(env!("OUT_DIR"), "/embedded_assets.rs"));

/// 按 URL 路径取资源，返回 (内容, Content-Type)。
pub fn get(url_path: &str) -> Option<(&'static [u8], &'static str)> {
    let p = url_path.split('?').next().unwrap_or(url_path);
    ASSETS
        .iter()
        .find(|(u, _, _)| *u == p)
        .map(|(_, b, m)| (*b, *m))
}

pub fn index_html() -> &'static [u8] {
    get("/static/index.html")
        .map(|(b, _)| b)
        .unwrap_or(b"<!doctype html><meta charset='utf-8'><body>Frontend assets were not embedded. Rebuild from the full repository.</body>")
}

pub fn count() -> usize {
    ASSETS.len()
}
