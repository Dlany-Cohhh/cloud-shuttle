//! 云梭 CloudShuttle · Rust 原生实现共享库。
//!
//! 同一个业务逻辑库同时服务两个入口：
//! - `cloud-shuttle-rs`：HTTP 服务器版（NAS / Linux，浏览器打开控制台）；
//! - `cloud-shuttle-desktop`：原生窗口桌面版（Windows exe，双击即用，不启动 HTTP）。
//!
//! 两者共用 `api::dispatch` 的全部 action、settings/jobs/rclone 数据格式，
//! 因此桌面版不是「另写一套」，只是换了一个前端。

pub mod api;
pub mod app;
pub mod config;
pub mod localfs;
pub mod rclone;
pub mod store;
pub mod update;
pub mod web;

#[cfg(feature = "gui")]
pub mod gui;
