//! 原生窗口界面（egui）。所有数据都来自 `api::dispatch`，没有任何内置 HTTP 请求。

use super::bridge::Bridge;
use super::platform;
use crate::config::APP_VERSION;
use eframe::egui;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Page {
    Overview,
    Jobs,
    Cloud,
    Accounts,
    Local,
    Settings,
    About,
}

impl Page {
    fn label(self) -> &'static str {
        match self {
            Page::Overview => "概览",
            Page::Jobs => "下载任务",
            Page::Cloud => "云盘浏览",
            Page::Accounts => "账号管理",
            Page::Local => "本地文件",
            Page::Settings => "设置",
            Page::About => "关于",
        }
    }
    const ALL: [Page; 7] = [
        Page::Overview,
        Page::Jobs,
        Page::Cloud,
        Page::Accounts,
        Page::Local,
        Page::Settings,
        Page::About,
    ];
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}
fn n(v: &Value, k: &str) -> f64 {
    v.get(k).and_then(|x| x.as_f64()).unwrap_or(0.0)
}
fn arr(v: &Value, k: &str) -> Vec<Value> {
    v.get(k)
        .and_then(|x| x.as_array())
        .cloned()
        .unwrap_or_default()
}
fn human(b: f64) -> String {
    if b < 1024.0 {
        format!("{} B", b as u64)
    } else if b < 1024.0 * 1024.0 {
        format!("{:.1} KB", b / 1024.0)
    } else if b < 1024.0 * 1024.0 * 1024.0 {
        format!("{:.1} MB", b / 1024.0 / 1024.0)
    } else {
        format!("{:.2} GB", b / 1024.0 / 1024.0 / 1024.0)
    }
}
fn ok_color() -> egui::Color32 {
    egui::Color32::from_rgb(0x18, 0x80, 0x4a)
}
fn err_color() -> egui::Color32 {
    egui::Color32::from_rgb(0xc0, 0x35, 0x2f)
}
fn dim_color(ctx: &egui::Context) -> egui::Color32 {
    if ctx.style().visuals.dark_mode {
        egui::Color32::from_gray(150)
    } else {
        egui::Color32::from_gray(110)
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ToastKind {
    Info,
    Ok,
    Err,
}

pub struct GuiApp {
    bridge: Bridge,
    page: Page,
    dark: bool,

    jobs: Value,
    stats: Value,
    health: Value,
    settings: Value,
    accounts: Value,
    remotes: Value,
    cloud: Value,
    local: Value,
    version: Value,
    update: Value,

    toast: String,
    toast_kind: ToastKind,
    toast_at: Instant,
    events: Vec<String>,
    last_poll: Instant,
    rclone_ready: bool,
    rclone_note: String,
    downloading: bool,
    download_root: String,
    data_dir: String,

    // 下载任务 / 新建表单
    new_remote: String,
    new_path: String,
    new_folder: String,
    confirm_remove: Option<String>,

    // 云盘浏览
    cloud_remote: String,
    cloud_path: String,
    cloud_sel: HashSet<String>,

    // 账号管理
    acct_name: String,
    acct_type: String,
    acct_user: String,
    acct_pass: String,
    acct_token: String,
    acct_rename_from: String,
    acct_rename_to: String,
    acct_results: Vec<(String, bool, String)>,

    // 本地文件
    local_path: String,
    local_target: String,
    local_new_name: String,
    batch_prefix: String,
    batch_suffix: String,
    batch_find: String,
    batch_replace: String,

    // 设置
    set_max: i32,
    set_disk_min: f64,
    set_proxy: String,
    set_bw_on: bool,
    set_bw_limit: String,
    manual_rclone: String,
}

impl GuiApp {
    pub fn new(
        bridge: Bridge,
        rclone_ready: bool,
        rclone_note: String,
        download_root: String,
        data_dir: String,
    ) -> Self {
        let app = GuiApp {
            bridge,
            page: Page::Overview,
            dark: false,
            jobs: Value::Null,
            stats: Value::Null,
            health: Value::Null,
            settings: Value::Null,
            accounts: Value::Null,
            remotes: Value::Null,
            cloud: Value::Null,
            local: Value::Null,
            version: Value::Null,
            update: Value::Null,
            toast: "欢迎使用云梭 · 原生桌面版".into(),
            toast_kind: ToastKind::Info,
            toast_at: Instant::now(),
            events: Vec::new(),
            last_poll: Instant::now(),
            rclone_ready,
            rclone_note,
            downloading: false,
            download_root,
            data_dir,
            new_remote: String::new(),
            new_path: String::new(),
            new_folder: String::new(),
            confirm_remove: None,
            cloud_remote: String::new(),
            cloud_path: String::new(),
            cloud_sel: HashSet::new(),
            acct_name: String::new(),
            acct_type: "pikpak".into(),
            acct_user: String::new(),
            acct_pass: String::new(),
            acct_token: String::new(),
            acct_rename_from: String::new(),
            acct_rename_to: String::new(),
            acct_results: Vec::new(),
            local_path: String::new(),
            local_target: String::new(),
            local_new_name: String::new(),
            batch_prefix: String::new(),
            batch_suffix: String::new(),
            batch_find: String::new(),
            batch_replace: String::new(),
            set_max: 2,
            set_disk_min: 20.0,
            set_proxy: String::new(),
            set_bw_on: false,
            set_bw_limit: "5M".into(),
            manual_rclone: String::new(),
        };
        app.refresh(Page::Overview);
        app.refresh(Page::Settings);
        app.refresh(Page::Cloud);
        app.refresh(Page::Accounts);
        app.bridge.send("localfiles", "", json!({"path": ""}));
        app
    }

    // ── 请求 ────────────────────────────────────────────────────────────────

    fn act(&self, action: &str, body: Value) {
        self.bridge.send(action, "", body);
    }

    fn poll_fast(&self) {
        self.act("jobs", json!({}));
        self.act("stats", json!({}));
        self.act("health", json!({}));
    }

    fn refresh(&self, page: Page) {
        match page {
            Page::Overview => {
                self.poll_fast();
                self.act("version", json!({}));
            }
            Page::Jobs => {
                self.act("jobs", json!({}));
                self.act("remotes", json!({}));
            }
            Page::Cloud => {
                self.act("remotes", json!({}));
                if !self.cloud_remote.is_empty() {
                    self.load_cloud();
                }
            }
            Page::Accounts => {
                self.act("accounts", json!({}));
                self.act("remotes", json!({}));
            }
            Page::Local => self.act("localfiles", json!({"path":self.local_path.clone()})),
            Page::Settings => {
                self.act("settings", json!({}));
                self.act("version", json!({}));
            }
            Page::About => {
                self.act("version", json!({}));
                self.act("health", json!({}));
            }
        }
    }

    fn load_cloud(&self) {
        self.bridge.send(
            "dirs",
            &format!("{}:{}", self.cloud_remote, self.cloud_path),
            json!({"remote": self.cloud_remote.clone(), "path": self.cloud_path.clone()}),
        );
    }

    // ── 回包 ────────────────────────────────────────────────────────────────

    fn note(&mut self, text: impl Into<String>) {
        let t = text.into();
        self.events.insert(0, t);
        self.events.truncate(40);
    }

    fn toast(&mut self, kind: ToastKind, text: impl Into<String>) {
        let t = text.into();
        self.toast = t.clone();
        self.toast_kind = kind;
        self.toast_at = Instant::now();
        self.note(t);
    }

    fn sync_settings(&mut self, v: &Value) {
        let st = v.get("settings").cloned().unwrap_or_else(|| v.clone());
        self.set_max = n(&st, "maxConcurrent") as i32;
        self.set_disk_min = n(&st, "diskMinGB");
        self.set_proxy = s(&st, "proxyUrl");
        self.set_bw_on = st
            .get("bwLimitOn")
            .and_then(|x| x.as_bool())
            .unwrap_or(false);
        self.set_bw_limit = {
            let x = s(&st, "bwLimit");
            if x.is_empty() {
                "5M".into()
            } else {
                x
            }
        };
    }

    fn drain(&mut self) {
        for r in self.bridge.poll() {
            let action = r.action.as_str();
            let err = s(&r.data, "error");
            match action {
                "__fatal" => self.toast(ToastKind::Err, format!("后台错误：{}", err)),
                "__task_rclone" => {
                    self.downloading = false;
                    if r.data.get("ok").and_then(|v| v.as_bool()) == Some(true) {
                        self.rclone_ready = true;
                        self.rclone_note = format!("已安装 rclone：{}", s(&r.data, "path"));
                        self.toast(ToastKind::Ok, "rclone 下载完成，现在可以创建下载任务了");
                        self.act("version", json!({}));
                    } else {
                        self.toast(ToastKind::Err, err.clone());
                    }
                }
                "jobs" => {
                    self.jobs = r.data.clone();
                    self.sync_settings(r.data.get("settings").unwrap_or(&Value::Null));
                }
                "stats" => self.stats = r.data,
                "health" => self.health = r.data,
                "settings" => {
                    self.settings = r.data.clone();
                    self.sync_settings(&r.data);
                }
                "version" => self.version = r.data,
                "updatestate" | "updatecheck" => self.update = r.data,
                "remotes" => {
                    let list: Vec<String> = arr(&r.data, "remotes")
                        .iter()
                        .map(|v| v.as_str().unwrap_or("").to_string())
                        .filter(|x| !x.is_empty())
                        .collect();
                    if self.cloud_remote.is_empty() {
                        self.cloud_remote = list.first().cloned().unwrap_or_default();
                        if !self.cloud_remote.is_empty() {
                            self.load_cloud();
                        }
                    }
                    if self.new_remote.is_empty() {
                        self.new_remote = self.cloud_remote.clone();
                    }
                    self.remotes = r.data;
                }
                "accounts" => self.accounts = r.data,
                "dirs" => {
                    let e = s(&r.data, "listError");
                    if !e.is_empty() {
                        self.toast(ToastKind::Err, e);
                    }
                    self.cloud = r.data;
                }
                "localfiles" => {
                    let e = s(&r.data, "error");
                    if !e.is_empty() {
                        self.toast(ToastKind::Err, e);
                    }
                    self.local = r.data;
                }
                "accttest" => {
                    let name = if r.tag.is_empty() {
                        s(&r.data, "name")
                    } else {
                        r.tag.clone()
                    };
                    let ok = r.data.get("ok").and_then(|v| v.as_bool()) == Some(true);
                    self.acct_results.push((
                        name.clone(),
                        ok,
                        if ok { "正常".into() } else { err.clone() },
                    ));
                    self.acct_results.truncate(30);
                    if !ok {
                        self.toast(ToastKind::Err, format!("账号 {} 测试失败：{}", name, err));
                    }
                }
                "start" => {
                    if err.is_empty() {
                        self.toast(ToastKind::Ok, "下载任务已创建");
                        self.page = Page::Jobs;
                        self.act("jobs", json!({}));
                    } else {
                        self.toast(ToastKind::Err, err.clone());
                    }
                }
                "localrenamebatch" => {
                    if err.is_empty() {
                        let ok = n(&r.data, "ok") as i64;
                        let failed = arr(&r.data, "failed").len();
                        self.toast(
                            ToastKind::Ok,
                            format!("已完成 {} 项改名，失败 {} 项", ok, failed),
                        );
                        self.local_target.clear();
                    } else {
                        self.toast(ToastKind::Err, err.clone());
                    }
                    self.act("localfiles", json!({"path":self.local_path.clone()}));
                }
                "savesettings" => {
                    self.settings =
                        json!({"settings": r.data.get("settings").cloned().unwrap_or(Value::Null)});
                    self.toast(ToastKind::Ok, "设置已保存");
                }
                _ => {
                    if !err.is_empty() {
                        self.toast(ToastKind::Err, err.clone());
                    } else if r.data.get("ok").and_then(|v| v.as_bool()) == Some(true) {
                        self.note(format!("{} 完成", action));
                    }
                }
            }
            if action == "acctdel"
                || action == "acctadd"
                || action == "acctrename"
                || action == "acctpurge"
            {
                if err.is_empty() {
                    self.toast(ToastKind::Ok, "账号配置已更新");
                    self.act("accounts", json!({}));
                    self.act("remotes", json!({}));
                } else {
                    self.toast(ToastKind::Err, err.clone());
                }
            }
            if matches!(action, "stop" | "resume" | "remove" | "clearfinished") {
                self.act("jobs", json!({}));
            }
        }
    }

    // ── 主框架 ──────────────────────────────────────────────────────────────

    fn header(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.heading(egui::RichText::new("云梭 CloudShuttle").strong());
            ui.label(egui::RichText::new("PikPak 多账号下载器 · 桌面版").color(dim_color(ctx)));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let label = if self.dark { "浅色" } else { "深色" };
                if ui.button(label).clicked() {
                    self.dark = !self.dark;
                    ctx.set_visuals(if self.dark {
                        egui::Visuals::dark()
                    } else {
                        egui::Visuals::light()
                    });
                }
                ui.label(
                    egui::RichText::new(format!("v{}", APP_VERSION.trim_start_matches("rs-v")))
                        .color(dim_color(ctx)),
                );
                let ready = if self.rclone_ready {
                    "rclone 就绪"
                } else {
                    "rclone 未就绪"
                };
                ui.colored_label(
                    if self.rclone_ready {
                        ok_color()
                    } else {
                        err_color()
                    },
                    ready,
                );
            });
        });
    }

    fn nav(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        ui.add_space(8.0);
        let mut changed: Option<Page> = None;
        for p in Page::ALL {
            let resp = ui.add_sized(
                [150.0, 34.0],
                egui::Button::new(p.label()).selected(self.page == p),
            );
            if resp.clicked() {
                self.page = p;
                changed = Some(p);
            }
        }
        if let Some(p) = changed {
            self.refresh(p);
        }
        ui.with_layout(egui::Layout::bottom_up(egui::Align::LEFT), |ui| {
            ui.add_space(8.0);
            if ui.button("打开下载目录").clicked() {
                platform::open_folder(std::path::Path::new(&self.download_root));
            }
            if ui.button("退出程序").clicked() {
                self.bridge.shutdown();
                ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            }
            ui.label(egui::RichText::new("本地运行 · 无需浏览器").color(dim_color(ctx)));
        });
    }

    fn banner(&mut self, ui: &mut egui::Ui) {
        if self.rclone_ready {
            return;
        }
        egui::Frame::NONE
            .fill(egui::Color32::from_rgb(0xff, 0xf3, 0xe0))
            .inner_margin(10.0)
            .corner_radius(6.0)
            .show(ui, |ui| {
                ui.horizontal_wrapped(|ui| {
                    ui.colored_label(err_color(), "缺少 rclone：");
                    ui.label(
                        egui::RichText::new(&self.rclone_note)
                            .color(egui::Color32::from_rgb(0x55, 0x40, 0x20)),
                    );
                    let btn = egui::Button::new("一键下载 rclone");
                    if ui.add_enabled(!self.downloading, btn).clicked() {
                        self.downloading = true;
                        self.toast(ToastKind::Info, "正在下载 rclone，请稍候…");
                        self.act(
                            "__task_rclone",
                            json!({"dest": platform::bundled_rclone().to_string_lossy()}),
                        );
                    }
                    if ui.button("官网手动下载").clicked() {
                        platform::open_url("https://rclone.org/downloads/");
                    }
                });
            });
        ui.add_space(6.0);
    }

    // ── 页面 ────────────────────────────────────────────────────────────────

    fn card(ui: &mut egui::Ui, title: &str, value: String, sub: &str) {
        egui::Frame::group(ui.style())
            .inner_margin(12.0)
            .show(ui, |ui| {
                ui.set_min_width(140.0);
                ui.label(egui::RichText::new(title).small());
                ui.heading(value);
                ui.label(egui::RichText::new(sub).small().color(egui::Color32::GRAY));
            });
    }

    fn overview(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        self.banner(ui);
        let jobs = arr(&self.jobs, "jobs");
        let running = jobs.iter().filter(|j| s(j, "state") == "running").count();
        let done = jobs.iter().filter(|j| s(j, "state") == "done").count();
        ui.horizontal_wrapped(|ui| {
            Self::card(
                ui,
                "当前速度",
                format!("{}/s", human(n(&self.stats, "speed"))),
                "所有下载任务合计",
            );
            Self::card(ui, "进行中", running.to_string(), "rclone 子进程");
            Self::card(
                ui,
                "已完成",
                done.to_string(),
                &format!("共 {} 个任务", jobs.len()),
            );
            let disk = self
                .jobs
                .get("state")
                .and_then(|v| v.get("diskFree"))
                .and_then(|v| v.as_f64());
            Self::card(
                ui,
                "磁盘剩余",
                disk.map(|d| format!("{:.1} GB", d))
                    .unwrap_or_else(|| "--".into()),
                "保护阈值在设置页调整",
            );
        });
        ui.add_space(12.0);
        ui.horizontal(|ui| {
            ui.heading("正在传输");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("新建下载").clicked() {
                    self.page = Page::Cloud;
                    self.refresh(Page::Cloud);
                }
            });
        });
        ui.separator();
        let transferring = arr(&self.stats, "transferring");
        if transferring.is_empty() {
            ui.label(egui::RichText::new("暂无正在传输的文件").color(dim_color(ctx)));
        } else {
            egui::Grid::new("ov-transfer")
                .striped(true)
                .spacing([12.0, 6.0])
                .show(ui, |ui| {
                    ui.label(egui::RichText::new("文件").strong());
                    ui.label(egui::RichText::new("已下载").strong());
                    ui.label(egui::RichText::new("速度").strong());
                    ui.end_row();
                    for t in transferring {
                        ui.label(s(&t, "name"));
                        ui.label(human(n(&t, "bytes")));
                        ui.label(format!("{}/s", human(n(&t, "speed"))));
                        ui.end_row();
                    }
                });
        }
        ui.add_space(14.0);
        ui.heading("运行状态");
        ui.separator();
        egui::Grid::new("ov-health")
            .spacing([16.0, 4.0])
            .show(ui, |ui| {
                ui.label("运行模式");
                ui.label("原生窗口（无 HTTP 端口、无浏览器）");
                ui.end_row();
                ui.label("rclone 版本");
                ui.label(s(&self.version, "version"));
                ui.end_row();
                ui.label("下载目录");
                ui.label(&self.download_root);
                ui.end_row();
                ui.label("数据目录");
                ui.label(&self.data_dir);
                ui.end_row();
                ui.label("运行时长");
                ui.label(format!("{} 秒", n(&self.health, "uptime") as u64));
                ui.end_row();
            });
    }

    fn state_label(state: &str) -> (String, egui::Color32) {
        let (text, color) = match state {
            "running" => ("下载中", ok_color()),
            "queued" => ("排队中", egui::Color32::from_rgb(0x1f, 0x6f, 0xeb)),
            "done" => ("已完成", egui::Color32::from_rgb(0x1f, 0x6f, 0xeb)),
            "error" => ("出错", err_color()),
            "stopped" => ("已暂停", egui::Color32::from_rgb(0xb0, 0x6a, 0x00)),
            _ => (state, egui::Color32::GRAY),
        };
        (text.to_string(), color)
    }

    fn jobs_page(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        self.banner(ui);
        ui.heading("新建下载任务");
        egui::Frame::group(ui.style())
            .inner_margin(10.0)
            .show(ui, |ui| {
                egui::Grid::new("new-job")
                    .num_columns(2)
                    .spacing([10.0, 8.0])
                    .show(ui, |ui| {
                        ui.label("账号");
                        let remotes: Vec<String> = arr(&self.remotes, "remotes")
                            .iter()
                            .map(|v| v.as_str().unwrap_or("").to_string())
                            .filter(|x| !x.is_empty())
                            .collect();
                        egui::ComboBox::from_id_salt("new-remote")
                            .selected_text(if self.new_remote.is_empty() {
                                "请选择账号"
                            } else {
                                &self.new_remote
                            })
                            .width(220.0)
                            .show_ui(ui, |ui| {
                                for r in &remotes {
                                    ui.selectable_value(&mut self.new_remote, r.clone(), r);
                                }
                            });
                        ui.end_row();
                        ui.label("云盘路径");
                        ui.add(
                            egui::TextEdit::singleline(&mut self.new_path)
                                .hint_text("留空为根目录，或填 分类/子目录")
                                .desired_width(320.0),
                        );
                        ui.end_row();
                        ui.label("保存目录名");
                        ui.add(
                            egui::TextEdit::singleline(&mut self.new_folder)
                                .hint_text("留空自动按云盘目录名命名")
                                .desired_width(320.0),
                        );
                        ui.end_row();
                    });
                ui.add_space(6.0);
                if ui.button("开始下载").clicked() {
                    if self.new_remote.is_empty() {
                        self.toast(ToastKind::Err, "请先选择一个账号");
                    } else {
                        self.act(
                            "start",
                            json!({
                                "remote": self.new_remote.clone(),
                                "path": self.new_path.clone(),
                                "folder": self.new_folder.clone(),
                                "mode": ""
                            }),
                        );
                    }
                }
                ui.add_space(2.0);
                if ui.button("从云盘挑选…").clicked() {
                    self.page = Page::Cloud;
                    self.refresh(Page::Cloud);
                }
            });

        ui.add_space(10.0);
        ui.horizontal(|ui| {
            ui.heading("任务列表");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("清理已完成").clicked() {
                    self.act("clearfinished", json!({}));
                }
                if ui.button("刷新").clicked() {
                    self.act("jobs", json!({}));
                }
            });
        });
        ui.separator();
        let jobs = arr(&self.jobs, "jobs");
        if jobs.is_empty() {
            ui.label(
                egui::RichText::new("还没有下载任务，去「云盘浏览」挑几个目录吧")
                    .color(dim_color(ctx)),
            );
            return;
        }
        egui::ScrollArea::vertical()
            .auto_shrink([false, false])
            .show(ui, |ui| {
                for j in jobs.iter().rev() {
                    let id = s(j, "id");
                    let state = s(j, "state");
                    let (label, color) = Self::state_label(&state);
                    let total = n(j, "total");
                    let bytes = n(j, "bytes");
                    let frac = if total > 0.0 {
                        (bytes / total).clamp(0.0, 1.0) as f32
                    } else {
                        0.0
                    };
                    egui::Frame::group(ui.style())
                        .inner_margin(9.0)
                        .show(ui, |ui| {
                            ui.horizontal(|ui| {
                                ui.label(egui::RichText::new(s(j, "name")).strong());
                                ui.colored_label(color, label);
                                ui.label(
                                    egui::RichText::new(s(j, "src"))
                                        .small()
                                        .color(dim_color(ui.ctx())),
                                );
                                ui.with_layout(
                                    egui::Layout::right_to_left(egui::Align::Center),
                                    |ui| {
                                        if ui.small_button("删除").clicked() {
                                            self.confirm_remove = Some(id.clone());
                                        }
                                        if ui.small_button("打开").clicked() {
                                            platform::open_folder(std::path::Path::new(&s(
                                                j, "hostDest",
                                            )));
                                        }
                                        if state == "done" {
                                            ui.add_enabled(false, egui::Button::new("已完成"));
                                        } else {
                                            let label = if state == "running" || state == "queued" {
                                                "暂停"
                                            } else {
                                                "继续"
                                            };
                                            if ui.small_button(label).clicked() {
                                                let act = if state == "running" || state == "queued"
                                                {
                                                    "stop"
                                                } else {
                                                    "resume"
                                                };
                                                self.act(act, json!({"id": id.clone()}));
                                            }
                                        }
                                    },
                                );
                            });
                            ui.add_space(4.0);
                            ui.add(egui::ProgressBar::new(frac).text(format!(
                                "{} / {}",
                                human(bytes),
                                if total > 0.0 {
                                    human(total)
                                } else {
                                    "?".into()
                                }
                            )));
                            ui.horizontal(|ui| {
                                ui.label(
                                    egui::RichText::new(format!("{}/s", human(n(j, "speed"))))
                                        .small(),
                                );
                                let eta = n(j, "eta");
                                if eta > 0.0 {
                                    ui.label(
                                        egui::RichText::new(format!("剩余 {} 秒", eta as u64))
                                            .small(),
                                    );
                                }
                                let cur = s(j, "curfile");
                                if !cur.is_empty() {
                                    ui.label(
                                        egui::RichText::new(cur).small().color(dim_color(ui.ctx())),
                                    );
                                }
                                let e = s(j, "error");
                                if !e.is_empty() {
                                    ui.colored_label(err_color(), egui::RichText::new(e).small());
                                }
                            });
                        });
                    ui.add_space(4.0);
                }
            });
        if let Some(id) = self.confirm_remove.clone() {
            egui::Window::new("确认删除")
                .collapsible(false)
                .resizable(false)
                .anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0])
                .show(ctx, |ui| {
                    ui.label("删除这个任务？已下载的文件不会被删除。");
                    ui.horizontal(|ui| {
                        if ui.button("确认删除").clicked() {
                            self.act("remove", json!({"id": id}));
                            self.confirm_remove = None;
                        }
                        if ui.button("取消").clicked() {
                            self.confirm_remove = None;
                        }
                    });
                });
        }
    }

    fn cloud_page(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        self.banner(ui);
        ui.horizontal(|ui| {
            ui.heading("云盘浏览");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("刷新").clicked() {
                    self.load_cloud();
                }
                if ui.button("上一级").clicked() {
                    let p = self.cloud_path.clone();
                    self.cloud_path = p
                        .rsplit_once('/')
                        .map(|(a, _)| a.to_string())
                        .unwrap_or_default();
                    self.cloud_sel.clear();
                    self.load_cloud();
                }
            });
        });
        let remotes: Vec<String> = arr(&self.remotes, "remotes")
            .iter()
            .map(|v| v.as_str().unwrap_or("").to_string())
            .filter(|x| !x.is_empty())
            .collect();
        ui.horizontal(|ui| {
            ui.label("账号");
            let before = self.cloud_remote.clone();
            egui::ComboBox::from_id_salt("cloud-remote")
                .selected_text(if self.cloud_remote.is_empty() {
                    "请选择账号"
                } else {
                    &self.cloud_remote
                })
                .width(180.0)
                .show_ui(ui, |ui| {
                    for r in &remotes {
                        ui.selectable_value(&mut self.cloud_remote, r.clone(), r);
                    }
                });
            if before != self.cloud_remote {
                self.cloud_path.clear();
                self.cloud_sel.clear();
                self.load_cloud();
            }
            ui.label("路径");
            let path = self.cloud_path.clone();
            if ui
                .add(egui::TextEdit::singleline(&mut self.cloud_path).desired_width(260.0))
                .lost_focus()
                && ui.input(|i| i.key_pressed(egui::Key::Enter))
                && path != self.cloud_path
            {
                self.load_cloud();
            }
            if ui.button("前往").clicked() {
                self.load_cloud();
            }
        });
        ui.label(
            egui::RichText::new(format!("{}:{}", self.cloud_remote, self.cloud_path))
                .small()
                .color(dim_color(ctx)),
        );
        let list_e = s(&self.cloud, "listError");
        if !list_e.is_empty() {
            ui.colored_label(err_color(), &list_e);
        }
        ui.separator();
        let items = arr(&self.cloud, "dirs");
        if items.is_empty() {
            ui.label(egui::RichText::new("目录为空，或还未配置可访问的账号").color(dim_color(ctx)));
        }
        egui::ScrollArea::vertical()
            .auto_shrink([false, false])
            .show(ui, |ui| {
                egui::Grid::new("cloud-list")
                    .striped(true)
                    .spacing([10.0, 5.0])
                    .show(ui, |ui| {
                        ui.label("");
                        ui.label(egui::RichText::new("名称").strong());
                        ui.label(egui::RichText::new("大小").strong());
                        ui.label(egui::RichText::new("修改时间").strong());
                        ui.label("");
                        ui.end_row();
                        for it in &items {
                            let name = s(it, "name");
                            let isdir = it.get("isdir").and_then(|v| v.as_bool()).unwrap_or(false);
                            let mut selected = self.cloud_sel.contains(&name);
                            if isdir && ui.checkbox(&mut selected, "").changed() {
                                if selected {
                                    self.cloud_sel.insert(name.clone());
                                } else {
                                    self.cloud_sel.remove(&name);
                                }
                            } else if !isdir {
                                ui.label("·");
                            }
                            let label = if isdir {
                                format!("📁 {}", name)
                            } else {
                                name.clone()
                            };
                            if isdir {
                                if ui.small_button(&label).clicked() {
                                    let base = self.cloud_path.clone();
                                    self.cloud_path = if base.is_empty() {
                                        name.clone()
                                    } else {
                                        format!("{}/{}", base, name)
                                    };
                                    self.cloud_sel.clear();
                                    self.load_cloud();
                                }
                            } else {
                                ui.label(&label);
                            }
                            ui.label(if isdir {
                                "目录".into()
                            } else {
                                human(n(it, "size"))
                            });
                            ui.label(s(it, "mtime"));
                            if isdir {
                                if ui.small_button("进入").clicked() {
                                    let base = self.cloud_path.clone();
                                    self.cloud_path = if base.is_empty() {
                                        name.clone()
                                    } else {
                                        format!("{}/{}", base, name)
                                    };
                                    self.cloud_sel.clear();
                                    self.load_cloud();
                                }
                            } else if it.get("downloaded").and_then(|v| v.as_bool()) == Some(true) {
                                ui.colored_label(ok_color(), "已下载");
                            } else {
                                ui.label("");
                            }
                            ui.end_row();
                        }
                    });
            });
        ui.add_space(8.0);
        ui.horizontal(|ui| {
            let count = self.cloud_sel.len();
            if ui.add_enabled(count > 0, egui::Button::new(format!("下载选中的 {} 个文件夹", count))).clicked() {
                let remote = self.cloud_remote.clone();
                let base = self.cloud_path.clone();
                for name in self.cloud_sel.iter() {
                    let path = if base.is_empty() { name.clone() } else { format!("{}/{}", base, name) };
                    self.act("start", json!({"remote": remote.clone(), "path": path, "folder": name.clone(), "mode": ""}));
                }
                self.cloud_sel.clear();
                self.toast(ToastKind::Info, "已提交下载任务，可在「下载任务」页查看进度");
            }
            if ui.button("下载当前目录").clicked() {
                let remote = self.cloud_remote.clone();
                let path = self.cloud_path.clone();
                let folder = path.rsplit('/').next().unwrap_or("").to_string();
                self.act("start", json!({"remote": remote, "path": path, "folder": folder, "mode": ""}));
            }
            ui.label(egui::RichText::new("双击文件夹进入；rclone copy 以目录为单位下载").small().color(dim_color(ctx)));
        });
    }

    fn accounts_page(&mut self, ui: &mut egui::Ui) {
        self.banner(ui);
        ui.heading("账号管理");
        ui.horizontal(|ui| {
            if ui.button("刷新").clicked() {
                self.act("accounts", json!({}));
                self.act("remotes", json!({}));
            }
            if ui.button("一键测试全部").clicked() {
                self.acct_results.clear();
                for a in arr(&self.accounts, "accounts") {
                    let name = s(&a, "name");
                    self.bridge.send("accttest", &name, json!({"name": name}));
                }
            }
            if ui.button("清理无效残留").clicked() {
                self.act("acctpurge", json!({}));
            }
        });
        ui.separator();
        let list = arr(&self.accounts, "accounts");
        if list.is_empty() {
            ui.label("还没有账号。在下方添加，或把现有 rclone.conf 放到数据目录后刷新。");
        }
        egui::ScrollArea::vertical()
            .max_height(260.0)
            .show(ui, |ui| {
                egui::Grid::new("acct-list")
                    .striped(true)
                    .spacing([12.0, 5.0])
                    .show(ui, |ui| {
                        ui.label(egui::RichText::new("名称").strong());
                        ui.label(egui::RichText::new("类型").strong());
                        ui.label(egui::RichText::new("账号").strong());
                        ui.label("操作");
                        ui.end_row();
                        for a in &list {
                            let name = s(a, "name");
                            ui.label(&name);
                            ui.label(s(a, "type"));
                            ui.label(s(a, "email"));
                            ui.horizontal(|ui| {
                                if ui.small_button("测试").clicked() {
                                    self.bridge.send(
                                        "accttest",
                                        &name,
                                        json!({"name": name.clone()}),
                                    );
                                }
                                if ui.small_button("改名").clicked() {
                                    self.acct_rename_from = name.clone();
                                    self.acct_rename_to = name.clone();
                                }
                                if ui.small_button("删除").clicked() {
                                    self.act("acctdel", json!({"name": name.clone()}));
                                }
                            });
                            ui.end_row();
                        }
                    });
            });
        if !self.acct_results.is_empty() {
            ui.add_space(6.0);
            ui.label(egui::RichText::new("测试结果").strong());
            for (name, ok, msg) in self.acct_results.iter().rev().take(8) {
                ui.horizontal(|ui| {
                    ui.colored_label(
                        if *ok { ok_color() } else { err_color() },
                        if *ok { "✓" } else { "✗" },
                    );
                    ui.label(name);
                    ui.label(msg);
                });
            }
        }
        ui.add_space(10.0);
        ui.heading("添加账号");
        egui::Frame::group(ui.style())
            .inner_margin(10.0)
            .show(ui, |ui| {
                egui::Grid::new("acct-add")
                    .num_columns(2)
                    .spacing([10.0, 6.0])
                    .show(ui, |ui| {
                        ui.label("名称");
                        ui.add(
                            egui::TextEdit::singleline(&mut self.acct_name).hint_text("例如 pik1"),
                        );
                        ui.end_row();
                        ui.label("类型");
                        egui::ComboBox::from_id_salt("acct-type")
                            .selected_text(&self.acct_type)
                            .show_ui(ui, |ui| {
                                for t in ["pikpak", "webdav", "sftp", "local"] {
                                    ui.selectable_value(&mut self.acct_type, t.to_string(), t);
                                }
                            });
                        ui.end_row();
                        ui.label("账号 / 邮箱");
                        ui.add(
                            egui::TextEdit::singleline(&mut self.acct_user).desired_width(280.0),
                        );
                        ui.end_row();
                        ui.label("密码");
                        ui.add(
                            egui::TextEdit::singleline(&mut self.acct_pass)
                                .password(true)
                                .desired_width(280.0),
                        );
                        ui.end_row();
                        ui.label("refresh_token");
                        ui.add(
                            egui::TextEdit::singleline(&mut self.acct_token)
                                .hint_text("PikPak 可选，留空则用账号密码")
                                .desired_width(280.0),
                        );
                        ui.end_row();
                    });
                if ui.button("添加").clicked() {
                    if self.acct_name.trim().is_empty() {
                        self.toast(ToastKind::Err, "请填写账号名称");
                    } else {
                        let mut params = serde_json::Map::new();
                        for (k, v) in [
                            ("username", self.acct_user.clone()),
                            ("user", self.acct_user.clone()),
                            ("password", self.acct_pass.clone()),
                            ("pass", self.acct_pass.clone()),
                            ("refresh_token", self.acct_token.clone()),
                        ] {
                            if !v.trim().is_empty() {
                                params.insert(k.into(), json!(v));
                            }
                        }
                        self.act(
                            "acctadd",
                            json!({
                                "name": self.acct_name.trim(),
                                "type": self.acct_type.clone(),
                                "params": Value::Object(params)
                            }),
                        );
                        self.acct_name.clear();
                        self.acct_pass.clear();
                    }
                }
            });
        if !self.acct_rename_from.is_empty() {
            ui.add_space(8.0);
            ui.horizontal(|ui| {
                ui.label(format!("把 {} 改名为", self.acct_rename_from));
                ui.add(egui::TextEdit::singleline(&mut self.acct_rename_to).desired_width(160.0));
                if ui.button("确认改名").clicked() {
                    self.act(
                        "acctrename",
                        json!({
                            "name": self.acct_rename_from.clone(),
                            "newName": self.acct_rename_to.clone()
                        }),
                    );
                    self.acct_rename_from.clear();
                }
                if ui.button("取消").clicked() {
                    self.acct_rename_from.clear();
                }
            });
        }
        let ghosts = arr(&self.accounts, "ghosts").len();
        if ghosts > 0 {
            ui.colored_label(
                err_color(),
                format!("检测到 {} 个无效残留段，可点「清理无效残留」", ghosts),
            );
        }
    }

    fn local_page(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        ui.heading("本地文件");
        ui.horizontal(|ui| {
            ui.label("目录");
            ui.add(
                egui::TextEdit::singleline(&mut self.local_path)
                    .hint_text("留空为下载根目录")
                    .desired_width(300.0),
            );
            if ui.button("前往").clicked() {
                self.act("localfiles", json!({"path":self.local_path.clone()}));
            }
            if ui.button("上一级").clicked() {
                let parent = s(&self.local, "parent");
                self.local_path = parent.clone();
                self.act("localfiles", json!({"path":parent}));
            }
            if ui.button("打开目录").clicked() {
                let root = std::path::PathBuf::from(&self.download_root);
                platform::open_folder(&if self.local_path.is_empty() {
                    root
                } else {
                    root.join(&self.local_path)
                });
            }
        });
        let e = s(&self.local, "error");
        if !e.is_empty() {
            ui.colored_label(err_color(), e);
        }
        ui.separator();
        let dirs = arr(&self.local, "dirs");
        let files = arr(&self.local, "files");
        if dirs.is_empty() && files.is_empty() {
            ui.label(egui::RichText::new("这个目录还是空的").color(dim_color(ctx)));
        }
        egui::ScrollArea::vertical()
            .max_height(280.0)
            .show(ui, |ui| {
                egui::Grid::new("local-list")
                    .striped(true)
                    .spacing([10.0, 4.0])
                    .show(ui, |ui| {
                        ui.label(egui::RichText::new("名称").strong());
                        ui.label(egui::RichText::new("大小 / 项数").strong());
                        ui.label("操作");
                        ui.end_row();
                        for d in &dirs {
                            let name = s(d, "name");
                            ui.label(format!("📁 {}", name));
                            ui.label(format!("{} 项", n(d, "count") as u64));
                            ui.horizontal(|ui| {
                                if ui.small_button("进入").clicked() {
                                    let base = self.local_path.clone();
                                    self.local_path = if base.is_empty() {
                                        name.clone()
                                    } else {
                                        format!("{}/{}", base, name)
                                    };
                                    self.act("localfiles", json!({"path":self.local_path.clone()}));
                                }
                                if ui.small_button("重命名").clicked() {
                                    self.local_target = name.clone();
                                    self.local_new_name = name.clone();
                                }
                            });
                            ui.end_row();
                        }
                        for f in &files {
                            let name = s(f, "name");
                            ui.label(&name);
                            ui.label(human(n(f, "size")));
                            if ui.small_button("重命名").clicked() {
                                self.local_target = name.clone();
                                self.local_new_name = name.clone();
                            }
                            ui.end_row();
                        }
                    });
            });
        if !self.local_target.is_empty() {
            ui.add_space(6.0);
            egui::Frame::group(ui.style()).inner_margin(8.0).show(ui, |ui| {
                ui.label(format!("重命名：{}", self.local_target));
                ui.horizontal(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut self.local_new_name).desired_width(320.0));
                    if ui.button("确认").clicked() {
                        self.act("localrenamebatch", json!({
                            "path": self.local_path.clone(),
                            "items": [{"from": self.local_target.clone(), "to": self.local_new_name.clone()}]
                        }));
                    }
                    if ui.button("取消").clicked() {
                        self.local_target.clear();
                    }
                });
            });
        }
        ui.add_space(8.0);
        ui.heading("批量重命名");
        egui::Frame::group(ui.style())
            .inner_margin(8.0)
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    ui.label("前缀");
                    ui.add(egui::TextEdit::singleline(&mut self.batch_prefix).desired_width(90.0));
                    ui.label("后缀");
                    ui.add(egui::TextEdit::singleline(&mut self.batch_suffix).desired_width(90.0));
                    ui.label("查找");
                    ui.add(egui::TextEdit::singleline(&mut self.batch_find).desired_width(90.0));
                    ui.label("替换为");
                    ui.add(egui::TextEdit::singleline(&mut self.batch_replace).desired_width(90.0));
                });
                if ui.button("应用到当前目录的全部文件").clicked() {
                    let mut items = Vec::new();
                    for f in &files {
                        let name = s(f, "name");
                        let (stem, ext) = match name.rsplit_once('.') {
                            Some((a, b)) if !a.is_empty() => (a.to_string(), format!(".{}", b)),
                            _ => (name.clone(), String::new()),
                        };
                        let replaced = if self.batch_find.is_empty() {
                            stem
                        } else {
                            stem.replace(&self.batch_find, &self.batch_replace)
                        };
                        let to = format!(
                            "{}{}{}{}",
                            self.batch_prefix, replaced, self.batch_suffix, ext
                        );
                        if to != name {
                            items.push(json!({"from": name, "to": to}));
                        }
                    }
                    if items.is_empty() {
                        self.toast(ToastKind::Info, "没有需要改名的文件");
                    } else {
                        self.act(
                            "localrenamebatch",
                            json!({"path": self.local_path.clone(), "items": items}),
                        );
                    }
                }
                ui.label(
                    egui::RichText::new("整批预检 + 两阶段临时名 + 失败自动回滚，目录同样适用")
                        .small()
                        .color(dim_color(ctx)),
                );
            });
    }

    fn settings_page(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        ui.heading("设置");
        egui::Frame::group(ui.style())
            .inner_margin(10.0)
            .show(ui, |ui| {
                ui.label(egui::RichText::new("下载参数").strong());
                ui.add_space(4.0);
                egui::Grid::new("set-grid")
                    .num_columns(2)
                    .spacing([12.0, 8.0])
                    .show(ui, |ui| {
                        ui.label("最大并发任务数");
                        ui.add(egui::Slider::new(&mut self.set_max, 1..=6));
                        ui.end_row();
                        ui.label("磁盘剩余保护阈值（GB）");
                        ui.add(egui::Slider::new(&mut self.set_disk_min, 0.0..=500.0).step_by(1.0));
                        ui.end_row();
                        ui.label("自定义代理地址");
                        ui.add(
                            egui::TextEdit::singleline(&mut self.set_proxy)
                                .hint_text("留空用默认；填 off 为直连；如 http://127.0.0.1:7890")
                                .desired_width(320.0),
                        );
                        ui.end_row();
                        ui.label("限速");
                        ui.horizontal(|ui| {
                            ui.checkbox(&mut self.set_bw_on, "启用");
                            ui.add(
                                egui::TextEdit::singleline(&mut self.set_bw_limit)
                                    .desired_width(80.0),
                            );
                            ui.label(
                                egui::RichText::new("如 5M / 500K")
                                    .small()
                                    .color(dim_color(ctx)),
                            );
                        });
                        ui.end_row();
                    });
                if ui.button("保存设置").clicked() {
                    self.act(
                        "savesettings",
                        json!({
                            "maxConcurrent": self.set_max,
                            "diskMinGB": self.set_disk_min,
                            "proxyUrl": self.set_proxy.clone(),
                            "bwLimitOn": self.set_bw_on,
                            "bwLimit": self.set_bw_limit.clone()
                        }),
                    );
                }
                ui.add_space(2.0);
                if ui.button("恢复默认").clicked() {
                    self.act("resetsettings", json!({}));
                    self.act("settings", json!({}));
                }
            });

        ui.add_space(10.0);
        ui.heading("rclone 引擎");
        egui::Frame::group(ui.style())
            .inner_margin(10.0)
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    ui.colored_label(
                        if self.rclone_ready {
                            ok_color()
                        } else {
                            err_color()
                        },
                        if self.rclone_ready {
                            "● 就绪"
                        } else {
                            "● 未就绪"
                        },
                    );
                    ui.label(&self.rclone_note);
                });
                ui.horizontal(|ui| {
                    let dest = platform::bundled_rclone();
                    if ui
                        .add_enabled(
                            !self.downloading,
                            egui::Button::new("一键下载 / 更新 rclone"),
                        )
                        .clicked()
                    {
                        self.downloading = true;
                        self.act("__task_rclone", json!({"dest": dest.to_string_lossy()}));
                    }
                    ui.label(
                        egui::RichText::new(format!("安装位置：{}", dest.display()))
                            .small()
                            .color(dim_color(ctx)),
                    );
                });
                ui.horizontal(|ui| {
                    ui.label("手动指定");
                    ui.add(
                        egui::TextEdit::singleline(&mut self.manual_rclone)
                            .hint_text("rclone.exe 的完整路径")
                            .desired_width(320.0),
                    );
                    if ui.button("应用").clicked() {
                        let src = std::path::PathBuf::from(self.manual_rclone.trim());
                        let dest = platform::bundled_rclone();
                        match std::fs::create_dir_all(dest.parent().unwrap_or(&dest))
                            .and_then(|_| std::fs::copy(&src, &dest))
                        {
                            Ok(_) => {
                                self.rclone_ready = true;
                                self.rclone_note = format!("已安装 rclone：{}", dest.display());
                                self.toast(ToastKind::Ok, "已应用，现在可以创建下载任务了");
                                self.act("version", json!({}));
                            }
                            Err(e) => self.toast(ToastKind::Err, format!("复制失败：{}", e)),
                        }
                    }
                });
            });

        ui.add_space(10.0);
        ui.heading("路径");
        egui::Grid::new("set-paths")
            .spacing([14.0, 4.0])
            .show(ui, |ui| {
                ui.label("下载目录");
                ui.label(&self.download_root);
                ui.end_row();
                ui.label("数据目录");
                ui.label(&self.data_dir);
                ui.end_row();
            });
        if ui.button("打开数据目录").clicked() {
            platform::open_folder(std::path::Path::new(&self.data_dir));
        }
    }

    fn about_page(&mut self, ctx: &egui::Context, ui: &mut egui::Ui) {
        ui.heading("关于云梭");
        ui.add_space(6.0);
        egui::Grid::new("about-grid")
            .spacing([16.0, 5.0])
            .show(ui, |ui| {
                ui.label("产品");
                ui.label("PikPak 多账号下载器 · 云梭 CloudShuttle（桌面版）");
                ui.end_row();
                ui.label("版本");
                ui.label(APP_VERSION);
                ui.end_row();
                ui.label("运行方式");
                ui.label("Rust 原生窗口进程；不启动 HTTP 服务、不依赖浏览器 / Docker / Python");
                ui.end_row();
                ui.label("下载引擎");
                ui.label(s(&self.version, "version"));
                ui.end_row();
                ui.label("后端");
                ui.label(s(&self.version, "backend"));
                ui.end_row();
                ui.label("许可");
                ui.label("MIT");
                ui.end_row();
            });
        ui.add_space(10.0);
        ui.horizontal(|ui| {
            if ui.button("检查更新").clicked() {
                self.act("updatecheck", json!({}));
            }
            if ui.button("打开项目主页").clicked() {
                platform::open_url("https://github.com/Dlany-Cohhh/cloud-shuttle");
            }
        });
        if self.update != Value::Null {
            ui.add_space(8.0);
            ui.label(format!(
                "最新版本：{}    当前构建：{}    有更新：{}",
                s(&self.update, "latest"),
                s(&self.update, "buildRelease"),
                if self.update.get("hasUpdate").and_then(|v| v.as_bool()) == Some(true) {
                    "是"
                } else {
                    "否"
                }
            ));
            let e = s(&self.update, "error");
            if !e.is_empty() {
                ui.colored_label(err_color(), e);
            }
            ui.label(
                egui::RichText::new(
                    "桌面版不能在线替换自身，请到 Releases 下载新 exe 覆盖即可，数据目录不变。",
                )
                .small()
                .color(dim_color(ctx)),
            );
        }
        ui.add_space(12.0);
        ui.heading("运行日志");
        ui.separator();
        egui::ScrollArea::vertical()
            .max_height(180.0)
            .show(ui, |ui| {
                for line in &self.events {
                    ui.label(egui::RichText::new(line).small());
                }
                if self.events.is_empty() {
                    ui.label(
                        egui::RichText::new("暂无日志")
                            .small()
                            .color(dim_color(ctx)),
                    );
                }
            });
    }
}

impl eframe::App for GuiApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.drain();
        if self.last_poll.elapsed() >= Duration::from_secs(2) {
            self.last_poll = Instant::now();
            self.poll_fast();
        }
        ctx.request_repaint_after(Duration::from_millis(400));

        egui::TopBottomPanel::top("header").show(ctx, |ui| self.header(ctx, ui));

        egui::SidePanel::left("nav")
            .resizable(false)
            .exact_width(172.0)
            .show(ctx, |ui| self.nav(ctx, ui));

        egui::TopBottomPanel::bottom("status").show(ctx, |ui| {
            ui.horizontal(|ui| {
                let color = match self.toast_kind {
                    ToastKind::Info => dim_color(ctx),
                    ToastKind::Ok => ok_color(),
                    ToastKind::Err => err_color(),
                };
                ui.colored_label(
                    color,
                    if self.toast_at.elapsed() > Duration::from_secs(12) {
                        "就绪"
                    } else {
                        &self.toast
                    },
                );
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.label(
                        egui::RichText::new("本地进程 · 数据保存在用户目录")
                            .small()
                            .color(dim_color(ctx)),
                    );
                });
            });
        });

        egui::CentralPanel::default().show(ctx, |ui| {
            egui::ScrollArea::vertical()
                .auto_shrink([false, false])
                .show(ui, |ui| {
                    ui.add_space(4.0);
                    match self.page {
                        Page::Overview => self.overview(ctx, ui),
                        Page::Jobs => self.jobs_page(ctx, ui),
                        Page::Cloud => self.cloud_page(ctx, ui),
                        Page::Accounts => self.accounts_page(ui),
                        Page::Local => self.local_page(ctx, ui),
                        Page::Settings => self.settings_page(ctx, ui),
                        Page::About => self.about_page(ctx, ui),
                    }
                    ui.add_space(10.0);
                });
        });
    }

    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        self.bridge.shutdown();
    }
}
