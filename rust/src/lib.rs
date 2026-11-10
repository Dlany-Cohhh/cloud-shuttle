//! 云梭 CloudShuttle · Rust 原生实现共享库。
//!
//! 同一个业务逻辑库同时服务三个入口：
//! - `cloud-shuttle-rs`：HTTP 服务器版（NAS / Linux，浏览器打开控制台）；
//! - `cloud-shuttle-desktop --features webview`：原生窗口版，复用仓库根 `static/`，
//!   界面与 Web 版完全一致，但不开端口、不弹浏览器；
//! - `cloud-shuttle-desktop --features gui`：egui 自绘轻量窗口版。
//!
//! 三者共用 `api::dispatch` 的全部 action、settings/jobs/rclone 数据格式，
//! 因此桌面版不是「另写一套」，只是换了一个前端载体。

pub mod api;
pub mod app;
pub mod config;
pub mod localfs;
pub mod rclone;
pub mod store;
pub mod update;
pub mod web;

// 平台适配与后台桥接被两种桌面形态共用，因此放在 lib 顶层。
#[path = "gui/bridge.rs"]
pub mod bridge;
#[path = "gui/platform.rs"]
pub mod platform;

#[cfg(feature = "gui")]
pub mod gui;
#[cfg(feature = "webview")]
pub mod gui_web;
