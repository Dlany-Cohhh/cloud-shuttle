//! 云梭 CloudShuttle · Windows / Linux 原生窗口桌面版。
//!
//! 双击即用：不开 HTTP 端口、不需要浏览器、不需要 Python / Docker。
//! Release 构建隐藏控制台窗口，错误用系统对话框提示。

#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

fn main() {
    if let Err(e) = cloud_shuttle_rs::gui::run() {
        cloud_shuttle_rs::gui::platform::fatal("云梭启动失败", &e);
        std::process::exit(1);
    }
}
