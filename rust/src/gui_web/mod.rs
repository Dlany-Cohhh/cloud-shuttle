//! 桌面窗口版（与 Web 控制台界面完全一致）。
//!
//! 用系统 WebView2 开一个原生应用窗口，加载**仓库根 `static/` 里的同一份前端**，
//! 资源由进程内自定义协议提供，不监听端口、不打开浏览器、不需要 `static/` 目录。
//!
//! 前端唯一的 `fetch("/api", {method:"POST"})` 由初始化脚本接管：请求经 IPC 交给
//! 后台 tokio runtime 里的 `api::dispatch`，结果再由事件循环回调 resolve。这样
//! 前端源码一行不改，主题、Tabler 图标、ECharts 图表、CSS 动效全部原样保留，
//! 同时慢操作（账号测试、云盘列举）也不会卡住窗口。

pub mod assets;
pub mod server;

use crate::bridge::{self, Reply, Request};
use crate::config::{help_text, Config, APP_VERSION};
use crate::platform;
use serde_json::{json, Value};
use std::sync::Arc;
use tao::{
    dpi::LogicalSize,
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoopBuilder},
    window::WindowBuilder,
};
use wry::WebViewBuilder;

enum UserMsg {
    Reply(Reply),
}

/// 把 `fetch("/api")` 换成 IPC 调用；其余请求全部走原生路径。
/// 返回的 Response 形状与真实 fetch 一致，Vue 侧完全感知不到差异。
const FETCH_SHIM: &str = r#"
(() => {
  const pending = new Map();
  let seq = 0;
  window.__csResolve = (id, payload) => {
    const p = pending.get(String(id));
    if (!p) return;
    pending.delete(String(id));
    if (p.signal && p.signal.aborted) return;
    const text = payload;
    p.resolve({
      ok: true, status: 200, statusText: 'OK',
      headers: { get: () => 'application/json' },
      json: async () => JSON.parse(text),
      text: async () => text,
      clone() { return this; }
    });
  };
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (url, opts) => {
    const u = String(url);
    const method = String((opts && opts.method) || 'GET').toUpperCase();
    if (u.indexOf('/api') === -1 || method !== 'POST') return nativeFetch(url, opts);
    return new Promise((resolve, reject) => {
      const id = ++seq;
      const signal = opts && opts.signal;
      if (signal && signal.aborted) {
        reject(new DOMException('Aborted', 'AbortError'));
        return;
      }
      pending.set(String(id), { resolve, reject, signal });
      if (signal) {
        signal.addEventListener('abort', () => {
          pending.delete(String(id));
          reject(new DOMException('Aborted', 'AbortError'));
        }, { once: true });
      }
      window.ipc.postMessage(JSON.stringify({
        id: id,
        body: String((opts && opts.body) || '{}')
      }));
    });
  };
})();
"#;

/// 首次启动缺 rclone 时的引导条。用 DaisyUI 的 CSS 变量配色，
/// 与页面主题自动一致；只有缺 rclone 才注入，正常界面完全不受影响。
const RCLONE_GUIDE: &str = r#"
(() => {
  const dest = __DEST__;
  const ID = '__cs_rclone_bar';
  const STYLE = 'position:fixed;left:50%;top:14px;transform:translateX(-50%);z-index:9999;' +
    'width:min(760px,92vw);display:flex;align-items:center;gap:10px;flex-wrap:wrap;' +
    'border-radius:12px;padding:10px 14px;font-size:13px;line-height:1.5;' +
    'background:var(--color-base-200,#23262e);color:var(--color-base-content,#e6e6e6);' +
    'border:1px solid var(--color-warning,#f0a020);box-shadow:0 8px 30px rgba(0,0,0,.28)';
  // 运行时插入的 class 不会被浏览器版 Tailwind 扫描到，按钮样式直接内联。
  const BTN = 'padding:6px 12px;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600;' +
    'border:1px solid var(--color-warning,#f0a020);' +
    'background:var(--color-warning,#f0a020);color:#1b1b1b;text-decoration:none;white-space:nowrap';
  const BTN_GHOST = 'padding:6px 12px;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600;' +
    'border:1px solid var(--color-base-content,#888);background:transparent;' +
    'color:var(--color-base-content,#e6e6e6);text-decoration:none;white-space:nowrap';
  function render(html) {
    let el = document.getElementById(ID);
    if (!el) {
      el = document.createElement('div');
      el.id = ID; el.className = 'alert alert-warning shadow-lg';
      el.setAttribute('style', STYLE);
      (document.body || document.documentElement).appendChild(el);
    }
    el.innerHTML = html;
    const go = document.getElementById('__cs_rclone_go');
    if (go) go.onclick = () => {
      render('<span class="loading loading-spinner loading-sm"></span><span>正在从官方源下载 rclone（约 80 MB），请稍候…</span>');
      window.ipc.postMessage(JSON.stringify({
        id: 0, body: JSON.stringify({ action: '__task_rclone', dest: dest })
      }));
    };
  }
  window.__csRcloneDone = (text) => {
    let d = {}; try { d = JSON.parse(text); } catch (e) {}
    if (d.ok) {
      render('<span>rclone 安装完成，现在可以创建下载任务了。</span>' +
        '<button style="' + BTN_GHOST + '" onclick="document.getElementById(\'__cs_rclone_bar\').remove()">知道了</button>');
    } else {
      render('<span>下载失败：' + (d.error || '未知错误') + '</span>' +
        '<a style="' + BTN_GHOST + '" href="https://rclone.org/downloads/" target="_blank">手动下载</a>');
    }
  };
  function boot() {
    render('<span>未检测到 rclone，下载任务无法运行。可一键从官方源安装（约 80 MB），' +
      '或手动把 rclone.exe 放到数据目录的 bin 文件夹。</span>' +
      '<button id="__cs_rclone_go" style="' + BTN + '">一键下载 rclone</button>');
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else { boot(); }
})();
"#;

fn rclone_guide(dest: &str) -> String {
    let literal = serde_json::to_string(dest).unwrap_or_else(|_| "\"\"".into());
    RCLONE_GUIDE.replace("__DEST__", &literal)
}

pub fn run() -> Result<(), String> {
    // 数据放用户目录、下载放「下载/CloudShuttle」，避免 exe 放桌面 / Program Files 写不进去。
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
            &format!("cloud-shuttle-desktop {} (webview)", APP_VERSION),
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
                "数据目录：{}\n下载目录：{}\n内嵌前端资源：{} 个\nrclone：{}\n运行模式：原生窗口（无 HTTP 服务）",
                cfg.data_dir.display(),
                cfg.download_root.display(),
                assets::count(),
                note
            ),
        );
        return Ok(());
    }
    if assets::count() == 0 {
        return Err(
            "这份可执行文件没有内嵌前端资源。请在完整仓库内用 --features webview 重新构建。".into(),
        );
    }
    let download_root = cfg.download_root.clone();
    let rclone_dest = platform::bundled_rclone().to_string_lossy().to_string();

    let event_loop = EventLoopBuilder::<UserMsg>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    let sink: bridge::ReplySink = Arc::new(move |r| {
        let _ = proxy.send_event(UserMsg::Reply(r));
    });
    let (tx, _worker) = bridge::start_core(cfg, sink);

    let window = WindowBuilder::new()
        .with_title("云梭 CloudShuttle · PikPak 多账号下载器")
        .with_inner_size(LogicalSize::new(1360.0, 840.0))
        .with_min_inner_size(LogicalSize::new(1024.0, 640.0))
        .build(&event_loop)
        .map_err(|e| format!("创建窗口失败：{}", e))?;

    let ipc_tx = tx.clone();
    let mut builder = WebViewBuilder::new()
        .with_custom_protocol("cs".into(), move |_id, req| {
            server::respond(&req, &download_root)
        })
        .with_initialization_script(FETCH_SHIM);
    if !ready {
        builder = builder.with_initialization_script(rclone_guide(&rclone_dest));
    }
    let webview = builder
        // target="_blank" 统一交给系统浏览器；桌面版本机没有 NAS 主页服务，
        // 出厂地址点开只会得到连接失败页，这里静默忽略（用户改成自己地址后照常打开）。
        .with_new_window_req_handler(|url: String| {
            let normalized = url.trim_end_matches('/');
            if normalized == "http://127.0.0.1:8080" || normalized == "https://127.0.0.1:8080" {
                return false;
            }
            if url.starts_with("http://") || url.starts_with("https://") {
                platform::open_url(&url);
            }
            false
        })
        .with_ipc_handler(move |msg| {
            let Ok(envelope) = serde_json::from_str::<Value>(msg.body()) else {
                return;
            };
            let id = envelope.get("id").and_then(Value::as_u64).unwrap_or(0);
            let mut body = serde_json::from_str::<Value>(
                envelope
                    .get("body")
                    .and_then(Value::as_str)
                    .unwrap_or("{}"),
            )
            .unwrap_or_else(|_| json!({}));
            if !body.is_object() {
                body = json!({});
            }
            let action = body
                .get("action")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let tag = if action == "__task_rclone" {
                "__rclone".to_string()
            } else {
                id.to_string()
            };
            let _ = ipc_tx.send(Request { action, tag, body });
        })
        .with_url("cs://localhost/")
        .build(&window)
        .map_err(|e| format!("创建 WebView 失败：{}\n\n请确认系统已安装 WebView2 Runtime（Windows 10/11 通常自带）。", e))?;

    let shutdown_tx = tx.clone();
    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            Event::UserEvent(UserMsg::Reply(r)) => {
                if r.action == "__fatal" {
                    platform::fatal("云梭启动失败", &r.data.to_string());
                    *control_flow = ControlFlow::Exit;
                    return;
                }
                let payload = r.data.to_string();
                // serde_json 生成合法的 JS 字符串字面量，中文与换行都安全。
                let literal = serde_json::to_string(&payload).unwrap_or_else(|_| "\"{}\"".into());
                if r.action == "__task_rclone" {
                    let _ = webview.evaluate_script(&format!("window.__csRcloneDone({})", literal));
                    return;
                }
                let script = format!("window.__csResolve({}, {})", r.tag, literal);
                let _ = webview.evaluate_script(&script);
            }
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => {
                let _ = shutdown_tx.send(Request {
                    action: "__shutdown".into(),
                    tag: String::new(),
                    body: json!({}),
                });
                *control_flow = ControlFlow::Exit;
            }
            _ => {}
        }
    })
}
