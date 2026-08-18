#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::env;
use std::io::{Read, Write};
use std::process::{Child, Command, Stdio};

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
/// 3) 系统已装 Python（py -3 / python / python3）—— 同样校验三件套；
///    跳过 WindowsApps 的「商店假 python stub」（重定向器，启动即 9009）
/// 4) 兜底 "python"（前端显形诊断）
fn resolve_python(app: &tauri::AppHandle) -> String {
    static CACHE: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    let p = CACHE.get_or_init(|| resolve_python_uncached(app)).clone();
    if p == "python" {
        // 兜底命中：记录诊断，供 spawn 失败时前端显形真实原因
        set_py_diag("未找到可用的 Python 运行时（内置 python_dist 校验失败，系统探测亦无可用解释器，已跳过 Windows 商店假 stub）");
    }
    p
}

/// 全局 Python 探测诊断（进程内最后一次解析的说明），spawn 失败时拼进错误消息。
static PY_DIAG: std::sync::OnceLock<std::sync::Mutex<String>> = std::sync::OnceLock::new();
fn set_py_diag(msg: &str) {
    let m = PY_DIAG.get_or_init(|| std::sync::Mutex::new(String::new()));
    if let Ok(mut g) = m.lock() {
        *g = msg.to_string();
    }
}
fn get_py_diag() -> String {
    PY_DIAG
        .get()
        .and_then(|m| m.lock().ok())
        .map(|g| g.clone())
        .unwrap_or_default()
}

fn python_ready(cmd: &mut Command) -> bool {
    cmd.arg("-c").arg("import fitz, PIL, numpy");
    hide_console(cmd);
    match cmd.output() {
        Ok(o) => o.status.success(),
        Err(_) => false,
    }
}

/// 是否为 Windows 商店「假 python stub」：位于 WindowsApps 目录的可执行文件，
/// 实际是重定向到商店的 AppInstallerPythonRedirector.exe，启动必失败/退出码 9009。
fn is_windowsapps_stub(path: &std::path::Path) -> bool {
    #[cfg(target_os = "windows")]
    {
        if let Some(s) = path.to_str() {
            let lower = s.to_lowercase();
            if lower.contains("windowsapps") {
                return true;
            }
        }
    }
    false
}

fn resolve_python_uncached(app: &tauri::AppHandle) -> String {
    if let Ok(p) = env::var("SCORE_PYTHON") {
        if !p.trim().is_empty() {
            return p;
        }
    }
    // ① 内嵌 python_dist：exe 同级优先（NSIS 版 python_dist 在 $INSTDIR\python_dist），
    //    resource_dir 兜底（未来若改用 tauri resources 打包）
    let mut bundled: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            bundled.push(dir.join("python_dist").join("python.exe"));
        }
    }
    if let Ok(res) = app.path().resource_dir() {
        bundled.push(res.join("python_dist").join("python.exe"));
    }
    for p in bundled {
        if p.exists() {
            let mut cmd = Command::new(&p);
            if python_ready(&mut cmd) {
                return p.to_string_lossy().to_string();
            }
        }
    }
    // ② 系统已装 Python：跳过 WindowsApps 假 stub；校验三件套通过才用
    let candidates: [(&str, &[&str]); 3] =
        [("py", &["-3"]), ("python", &[]), ("python3", &[])];
    for (c, pre) in candidates {
        // 先解析候选命令的真实绝对路径（如 `py -3` → C:\Python314\python.exe），
        // 以判定并跳过 WindowsApps stub
        if let Some(real) = real_python_path(c, pre) {
            if is_windowsapps_stub(std::path::Path::new(&real)) {
                continue; // 商店假 stub，跳过
            }
            let mut cmd = Command::new(&real);
            if python_ready(&mut cmd) {
                return real;
            }
        } else {
            let mut cmd = Command::new(c);
            cmd.args(pre);
            if python_ready(&mut cmd) {
                return c.to_string();
            }
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

/// 子进程异常结果：code -1 = 启动失败，-2 = 执行超时（已强制终止）。
struct RunErr {
    code: i32,
    message: String,
}

/// 子进程超时阈值（看门狗）：网络半开 / 畸形输入 / 引擎挂起时，超时即 kill。
const PROC_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(120);

/// spawn + 带超时的进程执行（标准库实现，无新增依赖）：
/// - stdout / stderr 交由独立线程收集，主线程每 50ms 轮询 `try_wait()`；
/// - 超过 PROC_TIMEOUT 仍未退出 → `child.kill()` 并返回超时错误；
/// - 正常退出 → 返回 `(ExitStatus, stdout, stderr)`。
/// 避免 `cmd.output()` 阻塞等待导致「处理中」永久卡死。
fn run_child_timeout(
    cmd: &mut Command,
    input: Option<&str>,
) -> Result<(std::process::ExitStatus, String, String), RunErr> {
    let mut child: Child = cmd
        .stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| {
            let diag = get_py_diag();
            let msg = if diag.is_empty() {
                format!("子进程启动失败（退出码 9009）：{}", e)
            } else {
                format!("子进程启动失败（退出码 9009）：{}. {}", diag, e)
            };
            RunErr { code: 9009, message: msg }
        })?;

    // 需要 stdin 时先写入并关闭（Python 侧 `sys.stdin.read()` 读光，当前用法不会死锁）。
    if let (Some(text), Some(mut si)) = (input, child.stdin.take()) {
        let _ = si.write_all(text.as_bytes());
        let _ = si.flush();
        // si 在此析构关闭 stdin → Python 读到 EOF
    }

    let out_pipe = child.stdout.take().expect("stdout pipe");
    let err_pipe = child.stderr.take().expect("stderr pipe");
    let out_handle = std::thread::spawn(move || {
        let mut buf = String::new();
        let _ = read_to_end_lossy(out_pipe, &mut buf);
        buf
    });
    let err_handle = std::thread::spawn(move || {
        let mut buf = String::new();
        let _ = read_to_end_lossy(err_pipe, &mut buf);
        buf
    });

    let start = std::time::Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break st,
            Ok(None) => {
                if start.elapsed() >= PROC_TIMEOUT {
                    let _ = child.kill();
                    let _ = child.wait(); // 回收进程；随后读取线程读至 EOF 自然结束
                    let _ = out_handle.join();
                    let _ = err_handle.join();
                    return Err(RunErr {
                        code: -2,
                        message: "子进程执行超时（120s），已强制终止".to_string(),
                    });
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(e) => return Err(RunErr { code: -1, message: e.to_string() }),
        }
    };
    let out = out_handle.join().unwrap_or_default();
    let err = err_handle.join().unwrap_or_default();
    Ok((status, out, err))
}

/// 把管道读到底并转成 UTF-8 损失字符串（与 `String::from_utf8_lossy` 语义一致）。
fn read_to_end_lossy(mut pipe: impl Read, buf: &mut String) -> std::io::Result<()> {
    let mut bytes = Vec::new();
    pipe.read_to_end(&mut bytes)?;
    buf.push_str(&String::from_utf8_lossy(&bytes));
    Ok(())
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
    match run_child_timeout(&mut cmd, None) {
        Ok((_status, out, _err)) => out.trim().to_string(),
        Err(e) => format!("{{\"error\":\"{}\"}}", e.message),
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
    match run_child_timeout(&mut cmd, Some(input)) {
        Ok((status, out, err)) => {
            let code = status.code().unwrap_or(-1);
            let ok = status.success() && !out.trim().is_empty();
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
            error: Some(e.message),
            code: e.code,
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
    match run_child_timeout(&mut cmd, None) {
        Ok((status, out, err)) => {
            let code = status.code().unwrap_or(-1);
            let ok = status.success() && !out.trim().is_empty();
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
            error: Some(e.message),
            code: e.code,
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
/// async：PDF 处理在后台线程执行，主线程不阻塞 → 根治 Windows「窗口未响应/泛白/转圈」。
#[tauri::command]
async fn process_scores(
    app: tauri::AppHandle,
    input: String,
    output_dir: String,
    theme: String,
    name: String,
) -> ProcessResult {
    tauri::async_runtime::spawn_blocking(move || {
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

        match run_child_timeout(&mut cmd, None) {
            Ok((status, out, err)) => {
                let combined = format!("{}{}", out, err);
                let ok = status.success() && combined.contains("✅");
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
                    error: if ok { None } else { Some(combined.trim().to_string()) },
                }
            }
            Err(e) => ProcessResult {
                ok: false,
                path: None,
                log: String::new(),
                error: Some(e.message),
            },
        }
    })
    .await
    .unwrap_or(ProcessResult {
        ok: false,
        path: None,
        log: String::new(),
        error: Some("后台任务调度失败".to_string()),
    })
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
async fn get_library(app: tauri::AppHandle, dir: String) -> PyResult {
    tauri::async_runtime::spawn_blocking(move || {
        let script = resolve_library_ops(&app);
        let dir = dir.trim().to_string();
        run_python_full(&app, &script, &["meta", "--dir", &dir])
    })
    .await
    .unwrap_or(PyResult {
        ok: false,
        out: String::new(),
        error: Some("后台任务调度失败".to_string()),
        code: -1,
        stderr: None,
    })
}

/// 按需获取单个 PDF 首页缩略图 base64（懒加载，命中文件缓存直接返回）。返回 base64 文本。
/// async：后台线程渲染，主线程不阻塞。
#[tauri::command]
async fn get_thumb(app: tauri::AppHandle, dir: String, name: String) -> String {
    tauri::async_runtime::spawn_blocking(move || {
        let script = resolve_library_ops(&app);
        let dir = dir.trim().to_string();
        let name = name.trim().to_string();
        // 用 --key=value 等号形式：rel 可能以 '-' 开头（如 '-1.pdf'），避免 argparse 误判为选项
        let args = [format!("thumb"), format!("--dir={}", dir), format!("--name={}", name)];
        let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
        run_python_json(&app, &script, &refs)
    })
    .await
    .unwrap_or_default()
}

/// 批量缩略图：一次 Python 进程渲染多张（滚动加载性能关键，杜绝逐张启进程闪窗口）。
/// rels 为 JSON 数组字符串，如 '["a.pdf","b.pdf"]'。返回 PyResult{ok, out({rel:b64}), ...}。
/// async：后台线程渲染多张，主线程不阻塞。
#[tauri::command]
async fn get_thumbs_batch(app: tauri::AppHandle, dir: String, rels: String) -> PyResult {
    tauri::async_runtime::spawn_blocking(move || {
        let script = resolve_library_ops(&app);
        let dir = dir.trim().to_string();
        let rels = rels.trim().to_string();
        let args = [format!("thumb_batch"), format!("--dir={}", dir), format!("--rels={}", rels)];
        let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
        run_python_full(&app, &script, &refs)
    })
    .await
    .unwrap_or(PyResult {
        ok: false,
        out: String::new(),
        error: Some("后台任务调度失败".to_string()),
        code: -1,
        stderr: None,
    })
}

/// 扫描曲库并给出命名规范化建议。返回 PyResult{ok, out(JSON), error, code, stderr}。
/// async：Python 子进程在后台线程执行，主线程不阻塞 → 根治 Windows「窗口未响应/泛白/转圈」。
#[tauri::command]
async fn inspect_library(app: tauri::AppHandle, dir: String) -> PyResult {
    tauri::async_runtime::spawn_blocking(move || {
        let script = resolve_library_ops(&app);
        let dir = dir.trim().to_string();
        run_python_full(&app, &script, &["inspect", "--dir", &dir])
    })
    .await
    .unwrap_or(PyResult {
        ok: false,
        out: String::new(),
        error: Some("后台任务调度失败".to_string()),
        code: -1,
        stderr: None,
    })
}

/// 执行批量重命名（仅在前端预览确认后调用）。payload 为 JSON 字符串。
/// async：后台线程执行。
#[tauri::command]
async fn rename_items(app: tauri::AppHandle, dir: String, payload: String) -> String {
    tauri::async_runtime::spawn_blocking(move || {
        let script = resolve_library_ops(&app);
        let dir = dir.trim().to_string();
        run_python_json(&app, &script, &["rename", "--dir", &dir, "--payload", &payload])
    })
    .await
    .unwrap_or_else(|_| "{\"error\":\"后台任务调度失败\"}".into())
}

/// 联网补全歌曲的真实专辑归属（iTunes 优先，Wikipedia/本地词库兜底）。返回 JSON 文本。
/// 用 run_python_full：失败时 stderr/退出码随结果回传，前端显形真实原因（杜绝「不可用」哑弹）。
/// async：后台线程联网，主线程不阻塞。
#[tauri::command]
async fn album_tag(app: tauri::AppHandle, title: String, artist: String) -> String {
    tauri::async_runtime::spawn_blocking(move || {
        let script = resolve_library_ops(&app);
        let title = title.trim().to_string();
        let artist = artist.trim().to_string();
        let res = run_python_full(&app, &script, &["albumtag", "--title", &title, "--artist", &artist]);
        serde_json::to_string(&res).unwrap_or_else(|_| "{\"ok\":false,\"error\":\"序列化失败\"}".into())
    })
    .await
    .unwrap_or_else(|_| "{\"ok\":false,\"error\":\"后台任务调度失败\"}".into())
}

/// 批量并发补全专辑（一次 Python 进程并发处理多首，杜绝逐首 round-trip 卡顿）。
/// items 为 JSON 数组字符串（可能极大）——走 stdin 传参，规避 Windows 命令行 32767 上限。
/// 返回 PyResult{ok, out, error, code, stderr}。async：后台线程执行，主线程不阻塞。
#[tauri::command]
async fn album_tag_batch(app: tauri::AppHandle, items: String) -> String {
    tauri::async_runtime::spawn_blocking(move || {
        let script = resolve_library_ops(&app);
        let res = run_python_full_stdin(&app, &script, &["albumbatch"], &items);
        serde_json::to_string(&res).unwrap_or_else(|_| "{\"ok\":false,\"error\":\"序列化失败\"}".into())
    })
    .await
    .unwrap_or_else(|_| "{\"ok\":false,\"error\":\"后台任务调度失败\"}".into())
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
