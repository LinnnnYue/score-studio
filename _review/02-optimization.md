# 优化执行专员报告（二轮）· Score Studio / 曲谱工坊

执行身份：优化执行专员分身（二轮）
执行日期：2026-08-18
依据：`01-code-review.md`（第一轮评审遗留高价值项）
本轮定位：收尾 🔴#2 与 🟡4/5/6/7 中的高价值项（#2 必做，其余兜底/结论）。

---

## 一、改动文件总览

| 文件 | 归类 | 说明 |
|---|---|---|
| `src-tauri/src/main.rs` | 必做#1 🔴#2 | 4 处子进程调用全部接入 120s 超时看门狗 |
| `sheet_pipeline.py` | 必做#2 兜底 | 清理过期 docstring / `--theme` help 中的「主题来源」语义 |
| `ui-prototype.html` | 必做#2 兜底 | 独立设计稿同步移除「主题来源标注」pill / 设置开关与 JS 块、修正文件名规则 |
| `src/main.js` | 必做#2 兜底 | 删除已确认无调用的死函数 `callLibrary`（评审 #10） |

复扫结果：README.md / docs/ / src/ / library_ops.py / sheet_pipeline.py / run_local.py / ui-prototype.html 中的废弃语义词（主题标签 / 主题来源 / 歌手筛选 / wiki_tag）与禁字（Workbuddy / AI 助手）**已全部清零**（详见第三、五节）。

---

## 二、必做#1 · Rust 子进程超时看门狗（🔴 #2，最高优先）

### 问题
`run_python_json / run_python_full / run_python_full_stdin / process_scores` 原用 `cmd.output()` / `wait_with_output()` **阻塞等待、无超时、无 kill**。网络半开 / 畸形大 PDF / fitz 挂起时，Tauri command 永不返回，前端逐项串行队列会永久停在「处理中」。

### 做了什么（文件 `src-tauri/src/main.rs`）
新增一个**标准库实现**的看门狗辅助函数 `run_child_timeout`（无任何新依赖），并让 4 处调用全部改走它：

```rust
/// 子进程异常结果：code -1 = 启动失败，-2 = 执行超时（已强制终止）。
struct RunErr { code: i32, message: String }

const PROC_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(120);

fn run_child_timeout(cmd: &mut Command, input: Option<&str>)
    -> Result<(std::process::ExitStatus, String, String), RunErr> { ... }
```

核心机制（评审 #2 建议的方案落地，纯 `std`）：
- `spawn()` 后，stdout / stderr 各交给一个 `std::thread::spawn` 线程 `read_to_end` 收集（避免大输出填满 64KB 管道缓冲导致子进程阻塞不退）。
- 主线程每 50ms 轮询 `child.try_wait()`；
- 超过 `PROC_TIMEOUT`(120s) 仍未退出 → `child.kill()` + `child.wait()` 回收 + `join` 读取线程 → 返回 `RunErr{code:-2, message:"子进程执行超时（120s），已强制终止"}`；
- 正常退出 → 返回 `(ExitStatus, stdout, stderr)`。
- 新增小工具 `read_to_end_lossy` 将管道字节按 `String::from_utf8_lossy` 语义转字符串（与原 `output()` 输出解析一致）。
- 顶部 `use std::io::{Read, Write}`、`use std::process::{Child, Command, Stdio}`。

4 处调用点改造（返回值结构/字段名/顺序完全不变）：
- `run_python_json`：`match cmd.output()` → `match run_child_timeout(&mut cmd, None)`，错误分支输出 `{"error":"..."}`（超时时为 `{"error":"子进程执行超时（120s），已强制终止"}`）。
- `run_python_full_stdin`：原手动 spawn + 写 stdin + `wait_with_output` 整体收敛进 `run_child_timeout(&mut cmd, Some(input))`；成功路径组装 `PyResult` 逻辑不变；失败分支 `code: e.code`（-1 启动失败 / -2 超时）。
- `run_python_full`：`run_child_timeout(&mut cmd, None)`，同上。
- `process_scores`：`run_child_timeout(&mut cmd, None)`，成功路径 `ok/path/log/error` 组装不变；失败分支 error 取 `e.message`。

### 旧 → 新
| 项 | 旧 | 新 |
|---|---|---|
| 执行方式 | `cmd.output()` / `wait_with_output()` 阻塞无超时 | `spawn` + 双线程收集 + `try_wait` 轮询 + 120s 看门狗 |
| 卡死时 | 永久挂起（队列卡「处理中」） | 120s 后 kill 并返回明确错误，队列继续处理下一项 |
| 失败 code | 仅 -1（启动失败） | -1 启动失败；-2 执行超时 |
| 依赖 | — | 0 新增（纯标准库） |

### 验证结果
- ✅ **`cargo check`（src-tauri 下）编译通过**：`Finished dev profile ... in 3.40s`，无 error 无 warning（**真实编译验证，非人工审计**）。
- ✅ 命令签名与前端 invoke 契约未改（见第七节）。

---

## 三、必做#2 · 复扫残留（兜底）

### 3.1 全仓 grep（源码/文档，跳过缓存/_review/target/node_modules/.git）

| 检索词 | 复扫结果 |
|---|---|
| `主题标签` / `主题来源` / `歌手筛选` / `wiki_tag` | README / docs / src / run_local / ui-prototype 全清零 ✓（仅命中处见下 3.2/3.3，已修） |
| `Workbuddy` / `AI 助手` / `AI助手` | 全清零 ✓ |
| `themePill` / `themeToggle` | 全清零 ✓（曾残留于 src 与 ui-prototype，均已同步移除） |
| `callLibrary(` 调用点 | 仅定义处、无调用 → 判定死函数，已删（见 3.4） |
| `编配` | 仅出现在 `library_ops.py` 内部说明性注释（第 206/227/243/258/283/349/361/370 行），均为「编配字段已被剔除」的功能说明，语义准确、非残留，**保留**（删除反而损失信息） |

### 3.2 `sheet_pipeline.py`（内部过期文案）
- **L281** docstring `"""文件名：曲名[-歌手][-主题来源].pdf` → `"""文件名：曲名[-歌手][-标签].pdf`（`--theme` 实为追加到文件名的额外标签段，非「主题来源」概念；前端现基本不传该参数）。
- **L415** `--theme` help `"主题来源标注，如：游戏主题曲 / 动漫"` → `"追加到文件名的额外标签（可选）"`。
- 验证：`py_compile` 通过 ✓；`--theme` 参数名/行为未变。

### 3.3 `ui-prototype.html`（README 目录树标注的独立单文件设计稿，含旧概念）
- 删除顶栏 `<div class="pill" id="themePill">主题来源标注 · 开</div>`。
- 设置 · 文件名规则 `曲名-歌手-主题来源` → `曲名-歌手-专辑`。
- 删除设置行 `<div class="row">…主题来源标注…<div class="toggle" id="themeToggle"></div></div>`。
- 删除内联 JS 中引用 `themeToggle`/`themePill` 的整块「主题来源标注开关」（否则删元素后 `getElementById` 返回 null 会报错）。
- 验证：改动区结构完整（topbar / set / JS 区均核对），无残留引用 ✓。

### 3.4 `src/main.js`（死代码 + 元素引用）
- **删除死函数 `callLibrary`**（评审 #10）：全文仅定义、无任何调用点（前端实际全走 `callLibraryMeta`）。本次删除以清理遗留死代码，与上轮删除 `themeToggle` 处理器保持同一标准。
- **元素引用核对**：`main.js` 所有 `$('…')`/`getElementById` 的 id（crumbSub/queue/dirBox/statText/logBox/linkInput/addBtn/clearBtn/drop/runBtn/openOutBtn/dirBtn/libGrid/libSearch/inspWrap/scanBtn/selAllBtn/selNoneBtn/albumAllBtn/applyBtn/applyCount/view-*）与 `index.html` 逐一匹配，**无引用不存在元素导致的运行时报错残留**。
- 验证：`node --check` 通过 ✓。

---

## 四、可选项#3 · 结论（本轮不做改动，给结论）

### A. `run_local.py`：CORS `*` 与 `/api/file` 任意路径读取
- **现状**：`_send` 统一写 `Access-Control-Allow-Origin: *`（L51），`/api/file`（L105-122）直接 `normpath(abspath(p))` 读任意本地文件，无目录白名单；`/api/process`、`/api/rename`、`/api/albumbatch` 亦可被跨域调用。默认绑 `127.0.0.1`。
- **结论**：**值得加基础防护（强烈建议，但本轮不强改）**。理由：
  1. 前端实际从同一 `127.0.0.1:<port>` 加载（index.html 由 `SRC_DIR` 托管），`fetch('/api/…')` 为**同源请求，浏览器不检查 CORS** → `Access-Control-Allow-Origin: *` 纯属多余，反而向任意恶意网页开放了本地任意文件读取 + 写操作。可安全地收敛为「同源」或移除 `*`，不影响本地模式功能。
  2. `/api/file` 建议加「仅允许输出目录树内路径」（拒绝命中 `..` 或不在白名单的绝对路径），或加一次性 token。
- **未做原因**：改动触及本地模式安全行为与测试面，超出本轮「必做」边界；本地模式默认 127.0.0.1、风险面相对可控。已明确结论，可在后续专项实现。

### B. `library_ops.py` `_album_cache_set` 全量落盘
- **现状**：`_album_cache_set`（L810-820）在全局锁内每条命中都 `json.dump` 整个 `_ALBUM_CACHE` 写盘；调用点 L1068 / L1081（`album_tag` 联网命中），`albumbatch` 并发 6 worker 各自持锁串行 dump，批量时有 O(条目数) 次全量磁盘写。
- **结论**：**值得优化，但不做改动**。推荐实现：内存锁内只改 dict + 置 `_dirty` 标志，新增 `_album_cache_flush()`；在 `albumbatch` 主流程 return 前与单条 `album_tag` 结束前统一 flush 一次，把磁盘 I/O 从 O(条目数) 降为每进程 1 次（并发 6 时的锁争用也随之大幅降低）。`_album_cache_load`（L795-799）已存在，读侧无需改。
- **未做原因**：涉及线程安全与多处调用点改造 + 需回归验证，超出本轮「必做」边界；现有实现功能正确（仅性能），放后续专项。

---

## 五、验证结果

| 检查 | 命令/方式 | 结果 |
|---|---|---|
| Rust 编译 | `cargo check`（src-tauri 下） | ✅ 通过（dev profile，3.40s，无 error / 无 warning） |
| Python 语法 | `C:/Python314/python.exe -m py_compile library_ops.py sheet_pipeline.py run_local.py` | ✅ PY_OK |
| JS 语法 | `node --check src/main.js` | ✅ NODE_OK |
| 废弃语义复扫 | README / docs / src / 4 个 .py / ui-prototype | ✅ 主题标签 / 主题来源 / 歌手筛选 / wiki_tag / Workbuddy / AI 助手 全清零 |
| 元素残留 | grep `themePill` / `themeToggle` / `callLibrary(` | ✅ 无残留（引用与调用均为零） |
| UI 结构 | ui-prototype.html 改动区人工核对 | ✅ topbar / set / JS 区完整 |

---

## 六、未做项及原因

| 项 | 说明 | 原因 / 处理 |
|---|---|---|
| 可选项 A（run_local.py CORS / `/api/file` 防护） | 未改 | 见第四节 A：给出明确「值得加固」结论，本轮不强改 |
| 可选项 B（`_album_cache_set` 延迟落盘） | 未改 | 见第四节 B：给出明确「值得优化」结论与方案，本轮不强改 |
| 评审 #5 build_index 无条件回写 | 未做 | 属中优先级性能项，非本轮必做；保持改动最小 |
| 评审 #7 stdin 长度阈值 / 写线程分离 | 未做 | 已确认当前用法（Python 先 `read()` 读光）无死锁窗口；看门狗已消除最坏「永久挂起」后果 |
| 评审 #8 缩略图缓存清理口径 | 未做 | 非本轮必做，留待后续 |

---

## 七、对外行为一致性声明

- **`src-tauri/src/main.rs`**：
  - 4 个 Tauri command 函数签名（参数、返回 `PyResult`/`ProcessResult`/`String`）**完全未变**，前端 invoke 契约不变。
  - 正常完成路径的返回值（`ok/out/error/code/stderr`、`path/log`）组装逻辑**与原来逐字段一致**。
  - **新增行为**：① 子进程超过 120s 强制终止并返回错误（此前是永久挂起）——这是修复而非破坏；② 失败场景多了一种 `code:-2`（超时），原 `code:-1`（启动失败）保留。前端对非 0 退出码仅作展示（`（退出码 n）`），判定主要依赖 `ok` 字段，不受影响。
- **`sheet_pipeline.py`**：仅改 docstring / `--help` 文案，参数名与行为（`--theme` 追加文件名标签段）不变。
- **`ui-prototype.html`**：独立展示设计稿，删除的是已无功能意义的元素与死 JS 块；不再引用被删 id。真实前端在 `src/`，不受影响。
- **`src/main.js`**：删除无调用死函数 `callLibrary`，不影响任何现有调用路径。

## 八、边界合规声明

- 未碰 `_review/`（本报告除外，本身写入其中）、`_train/` 内容；未修改主上个人数据。
- 未 `git commit`、未 `git push`、未打包。
- 改动最小、贴合项目现有风格（标准库看门狗、风格一致的 esc/清理），未引入无关重构。
- 对外文案无任何 `Workbuddy` / `AI 助手` 字样。
