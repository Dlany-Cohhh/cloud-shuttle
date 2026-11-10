//! 云梭 CloudShuttle · 原生窗口桌面版。
//!
//! 双击即用：不开 HTTP 端口、不打开浏览器、不需要 Python / Docker / static 目录。
//! Release 构建隐藏控制台窗口，错误用系统对话框提示。
//!
//! 两种窗口实现共用同一套业务逻辑：
//! - `--features webview`（默认发布）：系统 WebView2 渲染仓库根 `static/`，界面与 Web 版完全一致；
//! - `--features gui`：egui 自绘轻量版，零 Web 依赖。

#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

#[cfg(feature = "webview")]
fn main() {
    if let Err(e) = cloud_shuttle_rs::gui_web::run() {
        cloud_shuttle_rs::platform::fatal("云梭启动失败", &e);
        std::process::exit(1);
    }
}

#[cfg(all(not(feature = "webview"), feature = "gui"))]
fn main() {
    if let Err(e) = cloud_shuttle_rs::gui::run() {
        cloud_shuttle_rs::platform::fatal("云梭启动失败", &e);
        std::process::exit(1);
    }
}

#[cfg(not(any(feature = "webview", feature = "gui")))]
fn main() {
    eprintln!(
        "请用 --features webview（界面与 Web 版一致）或 --features gui（egui 轻量版）构建桌面版。"
    );
}
