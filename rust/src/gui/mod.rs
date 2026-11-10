//! 桌面版入口：原生窗口，不需要 HTTP 端口、static 目录或浏览器。

pub mod bridge;
pub mod platform;
pub mod ui;

use crate::config::{help_text, Config, APP_VERSION};
use eframe::egui;

/// 让中文正常显示：优先系统字体（TTF/TTC），可用 CS_FONT 指定。
fn setup_fonts(ctx: &egui::Context) {
    let Some(path) = platform::find_font() else {
        return;
    };
    let Ok(bytes) = std::fs::read(&path) else {
        return;
    };
    let mut fonts = egui::FontDefinitions::default();
    fonts.font_data.insert(
        "zh".into(),
        std::sync::Arc::new(egui::FontData::from_owned(bytes)),
    );
    for family in [egui::FontFamily::Proportional, egui::FontFamily::Monospace] {
        fonts
            .families
            .entry(family)
            .or_default()
            .insert(0, "zh".into());
    }
    ctx.set_fonts(fonts);
}

pub fn run() -> Result<(), String> {
    // 桌面版默认把数据放用户目录、下载放「下载/CloudShuttle」，
    // 避免把 exe 放桌面或 Program Files 时写不进去。
    if std::env::var_os("CS_DATA_DIR").is_none() {
        std::env::set_var("CS_DATA_DIR", platform::app_data_dir().join("data"));
    }
    if std::env::var_os("CS_DOWNLOAD_ROOT").is_none() {
        std::env::set_var("CS_DOWNLOAD_ROOT", platform::default_download_root());
    }

    let (mut cfg, cli) = Config::from_args();
    if cli.help {
        platform::fatal("云梭 · 用法", help_text());
        return Ok(());
    }
    if cli.version {
        platform::fatal(
            "云梭 · 版本",
            &format!("cloud-shuttle-desktop {}", APP_VERSION),
        );
        return Ok(());
    }

    let (ready, note) = platform::resolve_rclone(&mut cfg);
    cfg.ensure_dirs()
        .map_err(|e| format!("无法创建数据目录：{}", e))?;
    if cli.check {
        platform::fatal(
            "云梭 · 环境自检",
            &format!(
                "数据目录：{}\n下载目录：{}\nrclone：{}\n运行模式：原生窗口（无 HTTP 服务）",
                cfg.data_dir.display(),
                cfg.download_root.display(),
                note
            ),
        );
        return Ok(());
    }

    let data_dir = cfg.data_dir.to_string_lossy().to_string();
    let download_root = cfg.download_root.to_string_lossy().to_string();
    let bridge = bridge::Bridge::start(cfg);

    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1280.0, 820.0])
            .with_min_inner_size([980.0, 640.0])
            .with_title("云梭 CloudShuttle · PikPak 多账号下载器"),
        ..Default::default()
    };
    eframe::run_native(
        "云梭 CloudShuttle",
        options,
        Box::new(move |cc| {
            setup_fonts(&cc.egui_ctx);
            cc.egui_ctx.set_zoom_factor(1.1);
            cc.egui_ctx.set_visuals(egui::Visuals::light());
            Ok(Box::new(ui::GuiApp::new(
                bridge,
                ready,
                note,
                download_root,
                data_dir,
            )))
        }),
    )
    .map_err(|e| e.to_string())
}
