#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::env;
use std::io::Write;
use std::process::{Command, Stdio};

use serde::Serialize;
use tauri::Manager;

/// Windows：子进程加 CREATE_NO_WINDOW（0x08000000），
/// 根治 GUI 应用每次调用 Python 子进程时闪现/关闭命令行窗口。
#[cfg(target_os = "windows")]
fn hide_console(cmd: &mut Command) {
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
}
#[cfg(not(target_os = "windows"))]
fn hide_console(_cmd: &mut Command) {}

/// 解析 Python 解释器优先级（探测式，进程内缓存一次）：
/// 1) 环境变量 SCORE_PYTHON（调试/覆盖，信任用户）
/// 2) 打包内嵌 python_dist/python.exe —— 校验 import fitz/PIL/numpy 通过才用；
///    历史坏包（目录被压平缺 PIL）自动跳过，避免「假可用」导致 ModuleNotFoundError
/// 3) 系统已装 Python（py -3 / python / python3）—— 同样校验三件套
/// 4) 兜底 "python"（前端显形诊断）
fn resolve_python(app: &tauri::AppHandle) -> String {
    static CACHE: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    CACHE.get_or_init(|| resolve_python_uncached(app)).clone()
}

fn python_ready(cmd: &mut Command) -> bool {
    cmd.arg("-c").arg("import fitz, PIL, numpy");
    hide_console(cmd);
    match cmd.output() {
        Ok(o) => o.status.success(),
        Err(_) => false,
    }
}

fn resolve_python_uncached(app: &tauri::AppHandle) -> String {
    if let Ok(p) = env::var("SCORE_PYTHON") {
        if !p.trim().is_empty() {
            return p;
        }
    }
    let mut bundled: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(res) = app.path().resource_dir() {
        bundled.push(res.join("python_dist").join("python.exe"));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            bundled.push(dir.join("python_dist").join("python.exe"));
        }
    }
    for p in bundled {
        if p.exists() {
            let mut cmd = Command::new(&p);
            if python_ready(&mut cmd) {
                return p.to_string_lossy().to_string();
            }
        }
    }
    let candidates: [(&str, &[&str]); 3] =
        [("py", &["-3"]), ("python", &[]), ("python3", &[])];
    for (c, pre) in candidates {
        let mut cmd = Command::new(c);
        cmd.args(pre);
        if python_ready(&mut cmd) {
            // 解析真实解释器绝对路径（py launcher → C:\...\python.exe），
            // 直连真实 python.exe 以彻底绕开 launcher 的 stdin/stdout 转发层（排查 0/888 关键）
            if let Some(real) = real_python_path(c, pre) {
                return real;
            }
            return c.to_string();
        }
    }
    "python".to_string()
}

/// 解析候选命令对应的真实 python 可执行文件绝对路径（如 `py -3` → C:\Python314\python.exe）。
fn real_python_path(c: &str, pre: &[&str]) -> Option<String> {
    let mut cmd = Command::new(c);
    cmd.args(pre);
    cmd.arg("-c").arg("import sys; print(sys.executable)");
    hide_console(&mut cmd);
    if let Ok(o) = cmd.output() {
        if o.status.success() {
            let p = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if !p.is_empty() {
                return Some(p);
            }
        }
    }
    None
}

/// 解析 sheet_pipeline.py 脚本路径：环境变量 > 内嵌 resource_dir/exe 同级 > 当前目录。
fn resolve_pipeline(app: &tauri::AppHandle) -> String {
    if let Ok(p) = env::var("SCORE_PIPELINE") {
        if !p.trim().is_empty() {
            return p;
        }
    }
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(res) = app.path().resource_dir() {
        candidates.push(res.join("sheet_pipeline.py"));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("sheet_pipeline.py"));
        }
    }
    candidates.push(std::path::PathBuf::from("sheet_pipeline.py"));
    for p in candidates {
        if p.exists() {
            return p.to_string_lossy().to_string();
        }
    }
    "sheet_pipeline.py".to_string()
}

/// 解析 library_ops.py 脚本路径（与 sheet_pipeline.py 同目录，或内嵌目录/exe 同级）。
fn resolve_library_ops(app: &tauri::AppHandle) -> String {
    if let Ok(p) = env::var("SCORE_LIBRARY_OPS") {
        if !p.trim().is_empty() {
            return p;
        }
    }
    // 优先取 sheet_pipeline.py 同目录下的 library_ops.py
    let pipeline = resolve_pipeline(app);
    if let Some(parent) = std::path::Path::new(&pipeline).parent() {
        let sibling = parent.join("library_ops.py");
        if sibling.exists() {
            return sibling.to_string_lossy().to_string();
        }
    }
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(res) = app.path().resource_dir() {
        candidates.push(res.join("library_ops.py"));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("library_ops.py"));
        }
    }
    candidates.push(std::path::PathBuf::from("library_ops.py"));
    for p in candidates {
        if p.exists() {
            return p.to_string_lossy().to_string();
        }
    }
    "library_ops.py".to_string()
}

/// 调用 Python 脚本并返回 stdout 中的 JSON 文本（用于 library_ops）。
fn run_python_json(app: &tauri::AppHandle, script: &str, args: &[&str]) -> String {
    let python = resolve_python(app);
    let mut cmd = Command::new(&python);
    hide_console(&mut cmd);
    if python == "py" {
        cmd.arg("-3");
    }
    cmd.arg(script);
    for a in args {
        cmd.arg(a);
    }
    match cmd.output() {
        Ok(o) => {
            let out = String::from_utf8_lossy(&o.stdout).to_string();
            out.trim().to_string()
        }
        Err(e) => format!("{{\"error\":\"{}\"}}", e),
    }
}

/// 曲库/巡检专用：捕获 stdout + stderr + 退出码，失败时前端可见真实错误（杜绝静默失败）。
/// stderr 字段：即使 ok 也携带 Python 侧诊断输出（如空目录三态诊断），前端显形。
#[derive(Serialize)]
struct PyResult {
    ok: bool,
    out: String,
    error: Option<String>,
    code: i32,
    stderr: Option<String>,
}

/// 带 stdin 输入的完整调用：批量数据走 stdin，规避 Windows 命令行 32767 字符上限
/// （此前巡检数百份时 items JSON 塞命令行 → os error 206 文件名过长）。
fn run_python_full_stdin(app: &tauri::AppHandle, script: &str, args: &[&str], input: &str) -> PyResult {
    let python = resolve_python(app);
    let mut cmd = Command::new(&python);
    hide_console(&mut cmd);
    if python == "py" {
        cmd.arg("-3");
    }
    cmd.arg(script);
    for a in args {
        cmd.arg(a);
    }
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            return PyResult {
                ok: false,
                out: String::new(),
                error: Some(e.to_string()),
                code: -1,
                stderr: None,
            }
        }
    };
    if let Some(mut si) = child.stdin.take() {
        let _ = si.write_all(input.as_bytes());
        let _ = si.flush();
        // si 在此析构关闭 stdin → Python 读到 EOF
    }
    match child.wait_with_output() {
        Ok(o) => {
            let out = String::from_utf8_lossy(&o.stdout).to_string();
            let err = String::from_utf8_lossy(&o.stderr).to_string();
            let code = o.status.code().unwrap_or(-1);
            let ok = o.status.success() && !out.trim().is_empty();
            let err_trim = err.trim().to_string();
            PyResult {
                ok,
                out: out.trim().to_string(),
                error: if ok { None } else { Some(err_trim.clone()) },
                code,
                stderr: if err_trim.is_empty() { None } else { Some(err_trim) },
            }
        }
        Err(e) => PyResult {
            ok: false,
            out: String::new(),
            error: Some(e.to_string()),
            code: -1,
            stderr: None,
        },
    }
}

fn run_python_full(app: &tauri::AppHandle, script: &str, args: &[&str]) -> PyResult {
    let python = resolve_python(app);
    let mut cmd = Command::new(&python);
    hide_console(&mut cmd);
    if python == "py" {
        cmd.arg("-3");
    }
    cmd.arg(script);
    for a in args {
        cmd.arg(a);
    }
    match cmd.output() {
        Ok(o) => {
            let out = String::from_utf8_lossy(&o.stdout).to_string();
            let err = String::from_utf8_lossy(&o.stderr).to_string();
            let code = o.status.code().unwrap_or(-1);
            let ok = o.status.success() && !out.trim().is_empty();
            let err_trim = err.trim().to_string();
            PyResult {
                ok,
                out: out.trim().to_string(),
                error: if ok { None } else { Some(err_trim.clone()) },
                code,
                stderr: if err_trim.is_empty() { None } else { Some(err_trim) },
            }
        }
        Err(e) => PyResult {
            ok: false,
            out: String::new(),
            error: Some(e.to_string()),
            code: -1,
            stderr: None,
        },
    }
}

#[derive(Serialize)]
struct ProcessResult {
    ok: bool,
    path: Option<String>,
    log: String,
    error: Option<String>,
}

/// 调用 Python 曲谱管道（sheet_pipeline.py）。
/// Python 路径与脚本路径可由环境变量覆盖（便于打包时指向内嵌 sidecar）。
#[tauri::command]
fn process_scores(
    app: tauri::AppHandle,
    input: String,
    output_dir: String,
    theme: String,
    name: String,
) -> ProcessResult {
    let python = resolve_python(&app);
    let script = resolve_pipeline(&app);

    let mut cmd = Command::new(&python);
    hide_console(&mut cmd);
    if python == "py" {
        cmd.arg("-3");
    }
    cmd.arg(&script)
        .arg("--input")
        .arg(&input)
        .arg("--output-dir")
        .arg(&output_dir);
    if !theme.is_empty() {
        cmd.arg("--theme").arg(&theme);
    }
    if !name.is_empty() {
        cmd.arg("--name").arg(&name);
    }

    match cmd.output() {
        Ok(o) => {
            let out = String::from_utf8_lossy(&o.stdout).to_string();
            let err = String::from_utf8_lossy(&o.stderr).to_string();
            let combined = format!("{}{}", out, err);
            let ok = o.status.success() && combined.contains("✅");
            let path = if ok {
                combined
                    .split("✅ PDF 已生成：")
                    .nth(1)
                    .and_then(|s| s.lines().next())
                    .map(|s| s.trim().to_string())
            } else {
                None
            };
            ProcessResult {
                ok,
                path,
                log: out,
                error: if ok { None } else { Some(err) },
            }
        }
        Err(e) => ProcessResult {
            ok: false,
            path: None,
            log: String::new(),
            error: Some(e.to_string()),
        },
    }
}

/// 列出输出目录中的 PDF（曲库视图）。
#[tauri::command]
fn list_library(dir: String) -> Vec<String> {
    let mut items = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&dir) {
        for e in entries.flatten() {
            if let Some(ext) = e.path().extension() {
                if ext.eq_ignore_ascii_case("pdf") {
                    if let Some(n) = e.file_name().to_str() {
                        items.push(n.to_string());
                    }
                }
            }
        }
    }
    items.sort();
    items
}

/// 用系统文件管理器打开指定路径。
#[tauri::command]
fn open_path(path: String) {
    let _ = open::that(path);
}

/// 获取曲库元数据（轻量索引：含页数/大小/解析字段，不含缩略图 base64）。
/// 返回 PyResult{ok, out(JSON), error, code, stderr}——失败/空结果时前端可见 stderr，杜绝静默空。
#[tauri::command]
fn get_library(app: tauri::AppHandle, dir: String) -> PyResult {
    let script = resolve_library_ops(&app);
    let dir = dir.trim().to_string();
    run_python_full(&app, &script, &["meta", "--dir", &dir])
}

/// 按需获取单个 PDF 首页缩略图 base64（懒加载，命中文件缓存直接返回）。返回 base64 文本。
#[tauri::command]
fn get_thumb(app: tauri::AppHandle, dir: String, name: String) -> String {
    let script = resolve_library_ops(&app);
    let dir = dir.trim().to_string();
    let name = name.trim().to_string();
    // 用 --key=value 等号形式：rel 可能以 '-' 开头（如 '-1.pdf'），避免 argparse 误判为选项
    let args = [format!("thumb"), format!("--dir={}", dir), format!("--name={}", name)];
    let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
    run_python_json(&app, &script, &refs)
}

/// 批量缩略图：一次 Python 进程渲染多张（滚动加载性能关键，杜绝逐张启进程闪窗口）。
/// rels 为 JSON 数组字符串，如 '["a.pdf","b.pdf"]'。返回 PyResult{ok, out({rel:b64}), ...}。
#[tauri::command]
fn get_thumbs_batch(app: tauri::AppHandle, dir: String, rels: String) -> PyResult {
    let script = resolve_library_ops(&app);
    let dir = dir.trim().to_string();
    let rels = rels.trim().to_string();
    let args = [format!("thumb_batch"), format!("--dir={}", dir), format!("--rels={}", rels)];
    let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
    run_python_full(&app, &script, &refs)
}

/// 扫描曲库并给出命名规范化建议。返回 PyResult{ok, out(JSON), error, code, stderr}。
#[tauri::command]
fn inspect_library(app: tauri::AppHandle, dir: String) -> PyResult {
    let script = resolve_library_ops(&app);
    let dir = dir.trim().to_string();
    run_python_full(&app, &script, &["inspect", "--dir", &dir])
}

/// 执行批量重命名（仅在前端预览确认后调用）。payload 为 JSON 字符串。
#[tauri::command]
fn rename_items(app: tauri::AppHandle, dir: String, payload: String) -> String {
    let script = resolve_library_ops(&app);
    let dir = dir.trim().to_string();
    run_python_json(&app, &script, &["rename", "--dir", &dir, "--payload", &payload])
}

/// 联网补全歌曲的真实专辑归属（iTunes 优先，Wikipedia/本地词库兜底）。返回 JSON 文本。
/// 用 run_python_full：失败时 stderr/退出码随结果回传，前端显形真实原因（杜绝「不可用」哑弹）。
#[tauri::command]
fn album_tag(app: tauri::AppHandle, title: String, artist: String) -> String {
    let script = resolve_library_ops(&app);
    let title = title.trim().to_string();
    let artist = artist.trim().to_string();
    let res = run_python_full(&app, &script, &["albumtag", "--title", &title, "--artist", &artist]);
    serde_json::to_string(&res).unwrap_or_else(|_| "{\"ok\":false,\"error\":\"序列化失败\"}".into())
}

/// 批量并发补全专辑（一次 Python 进程并发处理多首，杜绝逐首 round-trip 卡顿）。
/// items 为 JSON 数组字符串（可能极大）——走 stdin 传参，规避 Windows 命令行 32767 上限。
/// 返回 PyResult{ok, out, error, code, stderr}。
#[tauri::command]
fn album_tag_batch(app: tauri::AppHandle, items: String) -> String {
    let script = resolve_library_ops(&app);
    let res = run_python_full_stdin(&app, &script, &["albumbatch"], &items);
    serde_json::to_string(&res).unwrap_or_else(|_| "{\"ok\":false,\"error\":\"序列化失败\"}".into())
}


/// Windows：将无边框窗口裁剪为圆角矩形，绕过 WebView2 透明兼容性问题。
#[cfg(target_os = "windows")]
fn apply_rounded_corners<R: tauri::Runtime>(window: &tauri::WebviewWindow<R>) {
    if let Ok(hwnd) = window.hwnd() {
        unsafe {
            use windows::Win32::Foundation::RECT;
            use windows::Win32::Graphics::Gdi::{CreateRoundRectRgn, SetWindowRgn};
            use windows::Win32::UI::WindowsAndMessaging::GetClientRect;
            let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
            if GetClientRect(hwnd, &mut rect).is_ok() {
                let w = rect.right - rect.left;
                let h = rect.bottom - rect.top;
                let radius = 16;
                let rgn = CreateRoundRectRgn(0, 0, w, h, radius * 2, radius * 2);
                if !rgn.is_invalid() {
                    SetWindowRgn(hwnd, Some(rgn), true);
                }
            }
        }
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            process_scores,
            list_library,
            open_path,
            get_library,
            get_thumb,
            get_thumbs_batch,
            inspect_library,
            rename_items,
            album_tag,
            album_tag_batch
        ])
        .setup(|app| {
            // Windows：用 SetWindowRgn 把窗体裁剪为圆角，配合 CSS 半径对齐。
            #[cfg(target_os = "windows")]
            if let Some(window) = app.get_webview_window("main") {
                apply_rounded_corners(&window);
                let window_for_events = window.clone();
                let _ = window.on_window_event(move |event| {
                    if let tauri::WindowEvent::Resized(_) = event {
                        apply_rounded_corners(&window_for_events);
                    }
                });
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
