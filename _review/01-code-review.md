# Score Studio · 深度代码评审报告

评审范围：`src-tauri/src/main.rs` · `library_ops.py` · `sheet_pipeline.py` · `run_local.py` · `src/main.js` · `src/index.html`（只读评审，未改动任何源码）
评审时间：2026-08-18 · 评审人：代码评审专员（任务总监麾下）

> 已按主上背景排除的「已知项」不再罗列：UTF-8 STDIO、专辑=中文来源作品、album_tag_batch 超时3s/并发6/前端分批60、封面 A4 剔除。以下仅列「真正值得改」的条目。

---

## 🔴 严重（明显 bug / 正确性）

### 1. `_wiki_search` 被调用却从未定义 → Wikipedia 兜底分类永远静默失效
- **问题描述**：`_wiki_category()` 声称「联网取分类兜底」，但它调用的 `_wiki_search()` 在**整个源文件中不存在**（全文件仅第 955 行一处调用，无任何 `def _wiki_search`）。运行时必然抛 `NameError`，被外层 `except Exception: continue` 吞掉后静默返回 `''`。即：`album_tag` 第 4 步的 Wikipedia 兜底分支实际**永远不会生效**，且无任何报错提示——一个被文档化、写进注释里（"网游取分类兜底"）的功能是死的。已确认该行同样存在于发布版 `Score-Studio-Portable/library_ops.py:955`（用的也是同一个坏源码，`target/debug` 里的旧副本反而有定义，属历史遗留分叉）。
- **具体位置**：`library_ops.py:955`（调用）——函数本体缺失；调用点为 `_wiki_category()`（`library_ops.py:951-963`）。
- **建议改法**（二选一）：
  - **正向修复**：按 MediaWiki API 补一个真实实现，例如
    ```python
    def _wiki_search(query: str, api: str, timeout: float = 3) -> dict:
        params = {'action': 'query', 'list': 'search', 'srsearch': query,
                  'format': 'json', 'utf8': 1, 'srlimit': 3}
        url = api + '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'ignore'))
    ```
  - **或最小化**：若判定该兜底收益低，直接删除 `_wiki_category` 与第 4 步 wiki 分支，并把 `_wiki_category` 调用替换为「仅本地兜底」，消灭死代码并让行为与注释一致。
- **期望收益**：恢复/澄清一个被文档化的兜底路径；避免「注释说有、实际永不工作」的隐性缺陷。稳定性/正确性修复，非偏好。

### 2. 子进程调用无任何超时/看门狗 → 单个卡死会永久冻结整个调用
- **问题描述**：`run_python_json / run_python_full / run_python_full_stdin / process_scores` 全部用 `cmd.output()` / `wait_with_output()` **阻塞等待、无 timeout、无 kill 看门狗**。而 Python 侧存在无上界的等待点：`fetch_html/download_bytes` 网络 timeout=30s 且无整体上限、`local_pdf` 对畸形 PDF 页码、`album_tag` 内 urllib 虽 3s 但多个候选串会翻倍拉长。一旦某个子进程真正卡住（网络半开、恶意大 PDF、fitz 挂起），Tauri command 永不返回，且前端 `runBtn` 是**串行逐项**处理——一个卡死项会让整条队列停在「处理中」，无可取消、无超时提示。
- **具体位置**：`src-tauri/src/main.rs:167`（`cmd.output()`）、`:220`（`wait_with_output`）、`:256`（`cmd.output()`）、`:319`（`cmd.output()`）。
- **建议改法**：给 `run_python_full`/`run_python_full_stdin` 加看门狗——`spawn()` 后用 `std::sync::mpsc`/`crossbeam` 通道 + `recv_timeout`，超时 `child.kill()` 并返回 `PyResult{ok:false, error:"超时已终止", code:-2}`；`process_scores` 同理包一层 `wait_timeout`。`Command` 本身无 timeout，需手动用 `Child` + 线程收尾。
- **期望收益**：杜绝「处理中」永久挂起；失败可诊断、可恢复。稳健性修复。

---

## 🟡 中（健壮性 / 边界 / 安全）

### 3. PDF 首页文字与联网专辑名未经转义进 `innerHTML` → XSS 注入面
- **问题描述**：`renderInspect` 把 `r.text_excerpt`（**任意 PDF 首页提取文本**，`get_text()` 可含 `<script>`、`<img onerror>` 等）直接拼进 `innerHTML`；`renderLibrary` 把 `it.title/artist/album`（专辑名可来自 iTunes/Wikipedia 网络返回）同样未转义。HTML 只对 `title` 属性转义了引号（main.js:647），对可见文本区未做任何 `<`/`>` 转义；`index.html` 无 CSP。由于这是逐条打开用户本地 PDF 的桌面应用，一份被构造的 PDF 首页文字即可在 WebView2 中注入执行脚本。
- **具体位置**：
  - `src/main.js:647`（`excerpt` 可见区直接 `${r.text_excerpt}`）
  - `src/main.js:653-658`（`r.name / r.suggested` 拼入 innerHTML）
  - `src/main.js:560-566`（`it.title / it.artist / it.album / it.name` 拼入 innerHTML）
- **建议改法**：新增一个 `esc()` 转义助手并用于上述所有动态插值：
  ```js
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
    (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  ```
  对 `.textContent` 构建节点的方式更彻底（杜绝注入）；至少对拼接的每一处动态值过 `esc()`。可选：为生产构建加 CSP。
- **期望收益**：封堵恶意 PDF/网络内容的脚本注入面。安全修复。

### 4. 本地服务器 `run_local.py`：CORS `*` + `/api/file` 任意路径读取
- **问题描述**：`Handler` 对所有响应写 `Access-Control-Allow-Origin: *`（run_local.py:51,56,119），而 `/api/file` 端点直接用入参 `path`（`os.path.normpath(os.path.abspath(p))`）读取**任意本地文件**、无目录白名单约束；`/api/process`、`/api/rename`、`/api/albumbatch` 亦可被跨域调用。服务器默认绑定 `127.0.0.1`，但一旦用户开着本地模式跑一个浏览器页面，**任意恶意网页**都能 `fetch` 到本地任意文件内容（甚至配合 `/api/rename` 改文件）。
- **具体位置**：`run_local.py:51`（CORS `*`）、`:105-122`（`/api/file` 任意读）、`:43-53`（`_send` 统一带 CORS）。
- **建议改法**：
  - `/api/file` 增加约束：仅允许来自「当前曲库目录树」内的相对路径，拒绝命中 `..` 或不在白名单的绝对路径；或干脆只在请求带一次性 token 时才放行文件读取。
  - 移除全局 `Access-Control-Allow-Origin: *`，改为仅对本机前端（`http://127.0.0.1:<port>`）或同源放行；对写操作加来源校验。
- **期望收益**：本地模式不再成为任意网页读取/篡改本地文件的跳板。安全修复。

### 5. `_album_cache_set` 每次命中都全量序列化落盘 → 批量并发时脏写放大
- **问题描述**：`_album_cache_set()` 在**每一条**命中结果后，于全局锁内把整个 `_ALBUM_CACHE` dict `json.dump` 写盘（library_ops.py:810-820）。`album_tag_batch` 并发 6、前端分批 60、一次数千行扫描时，会产生**数百次**对整个字典的全量磁盘写；且写盘持锁，6 个 worker 相互排队。词典里每加一条都 O(N) 序列化，N 随会话增长。
- **具体位置**：`library_ops.py:810-820`（调用点 `:1054`、`:1067`）。
- **建议改法**：内存中只加 dict（锁内），**落盘改为延迟/批量/进程结束时一次性写**：加一个 `_dirty` 标志与 `_album_cache_flush()`，在 `albumbatch` 主流程 return 前统一 flush 一次；单条 `albumtag` 命令结束前 flush 一次即可。后续起新进程会因 `_album_cache_load` 读到已 flush 数据而跳过网络。
- **期望收益**：批量补全时磁盘 I/O 从「O(条目数)」降为「每进程 1 次」，显著降低扫描卡顿与锁争用。性能修复。

### 6. `build_index` 每次调用都把整库索引重写落盘（即使无任何变更）
- **问题描述**：`get_library()`（曲库视图每次打开）→ `build_index(with_text=False)` 在扫描结束后**无条件** `json.dump` 全量索引到 `index.json`（library_ops.py:535-540）。对大曲库：只见一次都要做整份 JSON 序列化 + 写盘。
- **具体位置**：`library_ops.py:535-540`（`build_index` 尾部）。
- **建议改法**：记录 `changed` 标志：仅当「有新增/变更/删除条目」或 `force=True` 时才回写索引；全缓存命中时跳过写盘。
- **期望收益**：曲库视图秒开场景不再做无谓全量写盘，降低大库下的 IO 与停顿。性能/稳健性。

### 7. 批量 stdin 传参无长度/进度上限校验，极端大 payload 有管道阻塞风险
- **问题描述**：`album_tag_batch` 的 `items` 经 `run_python_full_stdin` 一次性 `write_all` 到 stdin（main.rs:215-219），写完后才 `wait_with_output`。对 `albumbatch` 而言，Python 侧先 `sys.stdin.read()` 读光再干活，所以当前用法不容易死锁；但**无任何阈值校验**：若未来 items 膨胀或脚本顺序变动（子进程边读边输出），一次性 `write_all` 填满 64KB 管道缓冲而子进程又因输出阻塞不去读 stdin 时，会永久阻塞。属潜在坑 + 缺防护。
- **具体位置**：`src-tauri/src/main.rs:189-243`（`run_python_full_stdin`）。
- **建议改法**：在 Rust 侧加输入大小上限（如 >8MB 直接拒绝并返回明确错误）；若追求彻底稳健，把「写 stdin」放到独立线程，主线程同时读取 stdout/stderr（`wait_with_output` 已并发读两端），消除双向阻塞窗口。
- **期望收益**：对超大输入有明确失败而非静默更复杂的玄学，给该接口加一个可解释的边界契约。稳健性。

---

## 🟢 轻（代码卫生 / 可维护）

### 8. 重命名后缩略图缓存清理失效（basename 带/不带扩展名不一致）
- **问题描述**：缓存文件名是 `f"{basename}_{size}_{mtime}.jpg"`，其中 `basename = os.path.basename(path)` **含 `.pdf`**（render_thumb 第 154 行）；而 `_clean_cache_for` 的输入是 `os.path.basename(os.path.splitext(old)[0])` **不含扩展名**（library_ops.py:1123-1128），再 `f.startswith(basename + '_')` 去匹配——含扩展名的缓存名永远不匹配 `不含扩展名 + '_'` 前缀，**导致改名后旧缩略图缓存永远不会被清理**，`.score-studio-cache/thumbs` 内孤儿文件随每次重命名累积。
- **具体位置**：`library_ops.py:91-101`（`_clean_cache_for`）vs `:154`（`render_thumb` 的 key）。
- **建议改法**：统一 basename 口径——让 `_clean_cache_for` 传入 `os.path.basename(old)`（保留扩展名），或让 `render_thumb` 的 key 用 `os.path.splitext(...)[0]`；并可额外兜底按「去掉 `<ext>_` 后的前缀」扫描。
- **期望收益**：改名后不留孤儿缓存，磁盘不随重命名增长；缩略图指向也更干净。

### 9. `sheet_pipeline.run()` 本地文件夹分支存在恒假三元死代码
- **问题描述**：`im = handle_transparent(Image.open(p)) if False else _open_local(p)`（sheet_pipeline.py:332）——`if False` 分支永不执行，纯死代码，且 `_open_local(p)` 与前者完全等价，徒增阅读负担。
- **具体位置**：`sheet_pipeline.py:332`。
- **建议改法**：删除 `if False else`，直接 `im = _open_local(p)`。
- **期望收益**：消除误导性死代码，提升可读性。

### 10. 巡检重复解析 `split_name` + 未复用的 `callLibrary` 死函数
- **问题描述**：两处小卫生问题：
  - `inspect_library` 已从 `build_index` 拿到 `title/artist/album`，却又对每行 base 再调一次 `split_name`（library_ops.py:640），同一份解析做两遍。
  - `src/main.js` 定义了 `callLibrary`（258-266 行）走 `list_library`，但前端实际全用 `callLibraryMeta`，`callLibrary` 无人调用。
- **具体位置**：`library_ops.py:640`（重复 `split_name`）；`src/main.js:258-266`（未使用 `callLibrary`）。
- **建议改法**：`inspect_library` 直接复用 `e['title']/e['artist']/e['album']`，删掉第 640 行的二次拆分；删除未使用的 `callLibrary`（若本地模式需要，仅保留 `callLibraryMeta` 即可）。
- **期望收益**：减少无效解析、删除死代码，降低维护面。

---

## 评审总评

- **真实可收益条目**：10 条，其中 ≥4 条属稳定性/正确性/安全（#1 恢复死功能、#2 防永久卡死、#3/#4 封堵注入口），其余为性能与卫生。
- **失败回滚覆盖**：已确认 `rename_items` 对「目标已存在」防覆盖跳过（library_ops.py:1116-1118）、`write_pdf_metadata` 用临时文件 + `os.replace` 原子替换（:387-389），批量重命名不会因单个失败覆盖他人——此为已达成的良好回滚基础，未列入改进。
- **建议优先级**：先做 #1（一行级确定性 bug）与 #2（稳定性护栏），再做 #3/#4 安全加固，随后 #5/#6 性能，最后 #7-#10 卫生项。
