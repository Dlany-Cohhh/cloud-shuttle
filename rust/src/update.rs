//! 版本信息：Python / Docker 版与 Rust 原生版共用一个仓库、一个 Release。
//!
//! 统一 Release（tag `v3.xx`）里同时带 `cloud-shuttle.zip`（Python 版在线更新用）
//! 和 `cloud-shuttle-rs-*.zip|tar.gz`（原生版安装包）。原生版按**资产名**识别
//! 自己的安装包，不再依赖 `rs-v*` 标签，也就不会和 Python 版抢 `releases/latest`，
//! NAS 上的在线更新永远不会读到一个没有 `cloud-shuttle.zip` 的 Release。
//!
//! 出站 HTTPS 交给系统自带的 curl（Windows 回退 PowerShell），
//! 这样依赖树保持纯 Rust，交叉编译时不需要 C 工具链 / OpenSSL / cmake。

use crate::config::APP_VERSION;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::process::Command;

/// CI 构建时通过环境变量写入对应的统一 Release 标签；本地手工编译为空。
fn build_release_tag() -> &'static str {
    option_env!("CS_RELEASE_TAG").unwrap_or("")
}

pub fn state() -> Value {
    json!({
        "ok": true,
        "current": APP_VERSION,
        "rustVersion": APP_VERSION,
        "buildRelease": build_release_tag(),
        "runtime": "rust",
        "state": {
            "lastCheck": 0,
            "lastApply": 0,
            "error": ""
        }
    })
}

fn hide(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.as_std_mut().creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    #[cfg(not(windows))]
    let _ = cmd;
}

async fn curl_fetch(url: &str) -> Result<String, String> {
    let mut cmd = Command::new("curl");
    cmd.args([
        "-fsSL",
        "--max-time",
        "12",
        "-H",
        "User-Agent: cloud-shuttle-rs",
        "-H",
        "Accept: application/vnd.github+json",
        url,
    ]);
    hide(&mut cmd);
    let out = tokio::time::timeout(Duration::from_secs(15), cmd.output())
        .await
        .map_err(|_| "curl 调用超时".to_string())?
        .map_err(|e| format!("curl 不可用：{}", e))?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).to_string())
    } else {
        Err(format!("curl 返回 {}", out.status))
    }
}

#[cfg(windows)]
async fn ps_fetch(url: &str) -> Result<String, String> {
    let script = format!(
        "(Invoke-WebRequest -UseBasicParsing -Headers @{{'User-Agent'='cloud-shuttle-rs'}} -Uri '{}').Content",
        url
    );
    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-NonInteractive", "-Command", &script]);
    hide(&mut cmd);
    let out = tokio::time::timeout(Duration::from_secs(20), cmd.output())
        .await
        .map_err(|_| "PowerShell 请求超时".to_string())?
        .map_err(|e| e.to_string())?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).to_string())
    } else {
        Err("PowerShell 请求失败".into())
    }
}

async fn fetch(url: &str) -> Result<String, String> {
    match curl_fetch(url).await {
        Ok(s) => Ok(s),
        Err(e1) => {
            #[cfg(windows)]
            {
                ps_fetch(url).await.map_err(|e2| format!("{}；{}", e1, e2))
            }
            #[cfg(not(windows))]
            {
                Err(format!("{}（检查更新需要系统已安装 curl）", e1))
            }
        }
    }
}

/// 统一 Release 里属于原生版的安装包。
fn is_native_asset(name: &str) -> bool {
    name.starts_with("cloud-shuttle-rs-")
        && (name.ends_with(".zip") || name.ends_with(".tar.gz"))
        && !name.ends_with(".sha256")
}

pub async fn check(repo: &str) -> Value {
    let url = format!("https://api.github.com/repos/{}/releases/latest", repo);
    let mut base = json!({
        "ok": false, "current": APP_VERSION, "rustVersion": APP_VERSION,
        "buildRelease": build_release_tag(), "latest": "", "hasUpdate": false,
        "notes": "", "runtime": "rust", "pythonLatest": "", "error": "",
        "assets": []
    });
    let data: Value = match fetch(&url).await {
        Ok(s) => match serde_json::from_str::<Value>(&s) {
            Ok(v) => v,
            Err(e) => {
                base["error"] = json!(format!("解析 GitHub 响应失败：{}", e));
                return base;
            }
        },
        Err(e) => {
            base["error"] = json!(e);
            return base;
        }
    };
    let tag = data
        .get("tag_name")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    base["ok"] = json!(true);
    base["latest"] = json!(tag);
    base["pythonLatest"] = json!(tag);
    base["notes"] = json!(data
        .get("body")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .chars()
        .take(600)
        .collect::<String>());

    let assets: Vec<String> = data
        .get("assets")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default()
        .iter()
        .filter_map(|a| a.get("name").and_then(|v| v.as_str()))
        .filter(|n| is_native_asset(n))
        .map(str::to_string)
        .collect();
    base["assets"] = json!(assets);

    if assets.is_empty() {
        base["notes"] = json!(
            "这个 Release 尚未附带原生版安装包；可到 Releases 查看，或用 rust/scripts 自行编译。"
        );
    } else {
        // 只有 CI 构建（写入了 CS_RELEASE_TAG）才能可靠判断「有更新」，
        // 本地手工编译不猜版本，避免每次都误报升级。
        let built = build_release_tag();
        base["hasUpdate"] = json!(!built.is_empty() && tag != built);
    }
    base
}

pub fn apply_unsupported() -> Value {
    json!({
        "error": "原生版暂不支持在线替换自身：请到 Releases 下载新的可执行文件覆盖后重启进程（数据目录不受影响）",
        "unsupported": true,
        "current": APP_VERSION
    })
}
