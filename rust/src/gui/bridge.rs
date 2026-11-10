//! 桌面窗口与业务逻辑之间的桥。
//!
//! 桌面版不开 HTTP 端口：UI 把 action JSON 丢进无界通道，后台 tokio runtime
//! 直接调用 `api::dispatch`（与 Web 版完全同一套实现），结果回流到 UI。
//! 每个请求独立 spawn，账号测试 / 云盘列举这类慢操作不会卡住界面或彼此排队。
//!
//! egui 版通过 `std::sync::mpsc` 轮询结果；WebView 版通过事件循环代理回调。
//! 两者共用这里的 `start_core`。

use crate::api;
use crate::app::{recover, AppState};
use crate::config::Config;
use crate::platform;
use serde_json::{json, Value};
use std::sync::mpsc::{channel, Receiver};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;
use tokio::sync::mpsc::UnboundedSender;

pub struct Request {
    pub action: String,
    pub tag: String,
    pub body: Value,
}

pub struct Reply {
    pub action: String,
    pub tag: String,
    pub data: Value,
}

/// 后台线程把结果交给 UI 的方式。必须 Send + Sync：tick 和 dispatch 都会回调。
pub type ReplySink = Arc<dyn Fn(Reply) + Send + Sync + 'static>;

/// 启动后台运行时：检测 rclone、恢复中断任务、跑调度 tick、逐个处理请求。
pub fn start_core(cfg: Config, sink: ReplySink) -> (UnboundedSender<Request>, JoinHandle<()>) {
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<Request>();
    let worker = std::thread::Builder::new()
        .name("cloud-shuttle-core".into())
        .spawn(move || {
            let rt = match tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
            {
                Ok(r) => r,
                Err(e) => {
                    sink(Reply {
                        action: "__fatal".into(),
                        tag: String::new(),
                        data: json!({"error": format!("无法启动后台运行时：{}", e)}),
                    });
                    return;
                }
            };
            rt.block_on(async move {
                let mut cfg = cfg;
                let rver = crate::rclone::version(&cfg).await;
                if rver.is_empty() {
                    // rclone 可能稍后才由用户下载，配置里保留可写回的路径
                    cfg.rclone_bin = platform::bundled_rclone().to_string_lossy().to_string();
                }
                let st = AppState::new(cfg);
                *st.rclone_version.lock().unwrap() = if rver.is_empty() {
                    "未检测到 rclone".into()
                } else {
                    rver
                };
                recover(&st);

                let bg = st.clone();
                let ticker = tokio::spawn(async move {
                    api::tick(&bg).await;
                    let mut iv = tokio::time::interval(Duration::from_secs(2));
                    loop {
                        iv.tick().await;
                        api::tick(&bg).await;
                    }
                });

                while let Some(req) = rx.recv().await {
                    if req.action == "__shutdown" {
                        break;
                    }
                    if req.action == "__task_rclone" {
                        let sink = sink.clone();
                        let tag = req.tag.clone();
                        let dest = req
                            .body
                            .get("dest")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                        std::thread::spawn(move || {
                            let result = platform::download_rclone(std::path::Path::new(&dest));
                            let data = match result {
                                Ok(p) => json!({"ok": true, "path": p.to_string_lossy()}),
                                Err(e) => json!({"error": e}),
                            };
                            sink(Reply {
                                action: "__task_rclone".into(),
                                tag,
                                data,
                            });
                        });
                        continue;
                    }
                    let st2 = st.clone();
                    let sink = sink.clone();
                    tokio::spawn(async move {
                        let data = api::dispatch(st2, req.body).await;
                        sink(Reply {
                            action: req.action,
                            tag: req.tag,
                            data,
                        });
                    });
                }
                ticker.abort();
            });
        })
        .expect("无法创建后台线程");
    (tx, worker)
}

pub struct Bridge {
    tx: UnboundedSender<Request>,
    rx: Receiver<Reply>,
    _worker: JoinHandle<()>,
}

impl Bridge {
    pub fn start(cfg: Config) -> Self {
        let (rep_tx, rep_rx) = channel::<Reply>();
        let sink: ReplySink = Arc::new(move |r| {
            let _ = rep_tx.send(r);
        });
        let (tx, worker) = start_core(cfg, sink);
        Bridge {
            tx,
            rx: rep_rx,
            _worker: worker,
        }
    }

    /// action 既是路由键也是回包标识；tag 用于区分同一 action 的多次请求（如切换云盘目录）。
    pub fn send(&self, action: &str, tag: &str, mut body: Value) {
        body["action"] = json!(action);
        let _ = self.tx.send(Request {
            action: action.to_string(),
            tag: tag.to_string(),
            body,
        });
    }

    pub fn poll(&self) -> Vec<Reply> {
        let mut out = Vec::new();
        while let Ok(r) = self.rx.try_recv() {
            out.push(r);
        }
        out
    }

    pub fn shutdown(&self) {
        let _ = self.tx.send(Request {
            action: "__shutdown".into(),
            tag: String::new(),
            body: json!({}),
        });
    }
}
