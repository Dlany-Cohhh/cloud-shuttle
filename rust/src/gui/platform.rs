//! 桌面版平台适配：数据目录、中文字体、rclone 定位 / 下载、系统交互。
//!
//! 只用标准库 + 系统自带命令（PowerShell / explorer / xdg-open），
//! 不引入新的第三方依赖，保证依赖树仍是纯 Rust。

use crate::config::Config;
use std::path::{Path, PathBuf};
use std::process::Command;

pub fn exe_dir() -> PathBuf {
    crate::config::exe_dir()
}

/// 配置与数据放用户目录，避免把 exe 放在桌面 / Program Files 时没写权限。
#[cfg(windows)]
pub fn app_data_dir() -> PathBuf {
    std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(exe_dir)
        .join("CloudShuttle")
}

#[cfg(not(windows))]
pub fn app_data_dir() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))
        .unwrap_or_else(exe_dir)
        .join("cloud-shuttle")
}

/// 默认下载目录：用户能找到的地方，而不是藏在 %APPDATA% 里。
pub fn default_download_root() -> PathBuf {
    #[cfg(windows)]
    if let Some(p) = std::env::var_os("USERPROFILE") {
        return PathBuf::from(p).join("Downloads").join("CloudShuttle");
    }
    #[cfg(not(windows))]
    if let Some(h) = std::env::var_os("HOME") {
        return PathBuf::from(h).join("Downloads").join("CloudShuttle");
    }
    app_data_dir().join("downloads")
}

/// 中文字体候选：优先 TTF（解析器兼容性最好），TTC 兜底。
pub fn font_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(p) = std::env::var("CS_FONT") {
        out.push(PathBuf::from(p));
    }
    #[cfg(windows)]
    {
        let windir = std::env::var_os("WINDIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
        let fonts = windir.join("Fonts");
        for n in [
            "msyh.ttc",
            "msyhl.ttc",
            "simhei.ttf",
            "simsun.ttc",
            "Deng.ttf",
            "Dengb.ttf",
            "simkai.ttf",
            "simfang.ttf",
        ] {
            out.push(fonts.join(n));
        }
    }
    #[cfg(not(windows))]
    {
        for p in [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/arphic/uming.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ] {
            out.push(PathBuf::from(p));
        }
    }
    out
}

pub fn find_font() -> Option<PathBuf> {
    font_candidates().into_iter().find(|p| p.is_file())
}

pub fn open_folder(path: &Path) {
    #[cfg(windows)]
    {
        let _ = Command::new("explorer.exe").arg(path).spawn();
    }
    #[cfg(target_os = "linux")]
    {
        let _ = Command::new("xdg-open").arg(path).spawn();
    }
    #[cfg(target_os = "macos")]
    {
        let _ = Command::new("open").arg(path).spawn();
    }
}

pub fn open_url(url: &str) {
    #[cfg(windows)]
    {
        let script = format!("Start-Process {}", ps_quote(url));
        let mut cmd = Command::new("powershell.exe");
        cmd.args(["-NoProfile", "-NonInteractive", "-Command", &script]);
        hide_window(&mut cmd);
        let _ = cmd.spawn();
    }
    #[cfg(target_os = "linux")]
    {
        let _ = Command::new("xdg-open").arg(url).spawn();
    }
    #[cfg(target_os = "macos")]
    {
        let _ = Command::new("open").arg(url).spawn();
    }
}

fn rclone_name() -> &'static str {
    if cfg!(windows) {
        "rclone.exe"
    } else {
        "rclone"
    }
}

/// 桌面版自带的 rclone 位置（自动下载后放这里）。
pub fn bundled_rclone() -> PathBuf {
    app_data_dir().join("bin").join(rclone_name())
}

fn works(bin: &str) -> bool {
    let mut cmd = Command::new(bin);
    cmd.arg("version");
    hide_window(&mut cmd);
    cmd.output().map(|o| o.status.success()).unwrap_or(false)
}

#[cfg(windows)]
fn hide_window(cmd: &mut Command) {
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
}

#[cfg(not(windows))]
fn hide_window(_cmd: &mut Command) {}

/// 按「用户指定 > 已下载 > exe 同目录 > PATH」的顺序找 rclone。
/// 找到时把绝对路径写回 cfg，返回给 UI 的状态文案。
pub fn resolve_rclone(cfg: &mut Config) -> (bool, String) {
    let explicit = PathBuf::from(&cfg.rclone_bin);
    let mut candidates: Vec<PathBuf> = Vec::new();
    if explicit.is_absolute() || explicit.components().count() > 1 {
        candidates.push(explicit.clone());
    }
    candidates.push(bundled_rclone());
    candidates.push(exe_dir().join(rclone_name()));
    for c in &candidates {
        if c.is_file() && works(&c.to_string_lossy()) {
            cfg.rclone_bin = c.to_string_lossy().to_string();
            return (true, format!("已找到 rclone：{}", c.display()));
        }
    }
    if works(&cfg.rclone_bin) {
        return (
            true,
            format!("使用系统 PATH 中的 rclone：{}", cfg.rclone_bin),
        );
    }
    (
        false,
        format!(
            "未找到 {}。下载任务需要 rclone，可在「设置」页一键下载或手动指定路径。",
            rclone_name()
        ),
    )
}

#[cfg(windows)]
fn ps_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

/// 下载官方 rclone 并解压出可执行文件（用系统 PowerShell，不引入 HTTP / 解压库）。
#[cfg(windows)]
pub fn download_rclone(dest: &Path) -> Result<PathBuf, String> {
    let dir = dest.parent().unwrap_or(dest);
    std::fs::create_dir_all(dir).map_err(|e| format!("创建目录失败：{}", e))?;
    let zip = dir.join("rclone-download.zip");
    let extract = dir.join("rclone-extract");
    let bin = dest.to_path_buf();
    let script = format!(
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; \
         Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri 'https://downloads.rclone.org/rclone-current-windows-amd64.zip' -OutFile {zip}; \
         Expand-Archive -Path {zip} -DestinationPath {ext} -Force; \
         $exe = Get-ChildItem -Path {ext} -Recurse -Filter rclone.exe | Select-Object -First 1; \
         if (-not $exe) {{ throw '压缩包里没有找到 rclone.exe' }}; \
         Copy-Item -Force $exe.FullName {bin}; \
         try {{ Remove-Item -Force {zip} -ErrorAction SilentlyContinue }} catch {{}}; \
         try {{ Remove-Item -Recurse -Force {ext} -ErrorAction SilentlyContinue }} catch {{}}",
        zip = ps_quote(&zip.to_string_lossy()),
        ext = ps_quote(&extract.to_string_lossy()),
        bin = ps_quote(&bin.to_string_lossy()),
    );
    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-NonInteractive", "-Command", &script]);
    hide_window(&mut cmd);
    let out = cmd
        .output()
        .map_err(|e| format!("启动 PowerShell 失败：{}", e))?;
    // 清理临时文件失败（例如被安全软件拦截）不影响安装结果，只要 exe 已就位就算成功。
    if !bin.is_file() {
        let err = String::from_utf8_lossy(&out.stderr);
        return Err(format!(
            "下载 rclone 失败：{}",
            err.chars().take(300).collect::<String>()
        ));
    }
    Ok(bin)
}

#[cfg(not(windows))]
pub fn download_rclone(_dest: &Path) -> Result<PathBuf, String> {
    Err("Linux / macOS 请用系统包管理器安装 rclone（例如 sudo apt install rclone）".into())
}

/// GUI 子系统下没有控制台，用系统对话框报告致命错误。
#[cfg(windows)]
pub fn fatal(title: &str, msg: &str) {
    use std::ffi::c_void;
    #[link(name = "user32")]
    extern "system" {
        fn MessageBoxW(hwnd: *mut c_void, text: *const u16, caption: *const u16, typ: u32) -> i32;
    }
    let t: Vec<u16> = msg.encode_utf16().chain(std::iter::once(0)).collect();
    let c: Vec<u16> = title.encode_utf16().chain(std::iter::once(0)).collect();
    unsafe {
        MessageBoxW(std::ptr::null_mut(), t.as_ptr(), c.as_ptr(), 0x10);
    }
}

#[cfg(not(windows))]
pub fn fatal(title: &str, msg: &str) {
    eprintln!("{}: {}", title, msg);
}
