# 优化执行报告 · Score Studio / 曲谱工坊

执行身份：优化执行专员（任务总监麾下）
执行日期：2026-08-18
依据：`01-code-review.md`（清单 A）、`02-publish-review.md`（清单 B）
范围：仅落地「必做 3 项」，并顺带完成可选项 A#3；A#2 仅作建议未实现。

---

## 一、改动文件总览

| 文件 | 归类 | 说明 |
|---|---|---|
| `README.md` | 必做#1 / #2 | 废弃语义全面同步 + 内部口吻清理 + 补 SUPPORT 链接 + 目录树缩进 |
| `docs/SUPPORT.md` | 必做#2 | 删除含被禁字的第 44 行 |
| `src/index.html` | 必做#1 | 删除「主题来源标注」pill 与开关、剔除「编配」，命名规则改「曲名-歌手-专辑」 |
| `src/main.js` | 必做#1 + 可选项 A#3 | 移除已删除元素的失效开关处理器；新增 `esc()` 转义并应用到曲库/巡检 innerHTML |
| `library_ops.py` | 必做#3 | 补上缺失的 `_wiki_search`，恢复 Wikipedia 分类兜底死功能 |

> Rust 侧（`src-tauri/src/main.rs`）**未改动、未编译**（见未做项）。

---

## 二、逐文件改动明细（旧 → 新）

### 1. `README.md`

对外形象修复（B #1~#6）：

| 位置 | 旧文案 | 新文案 |
|---|---|---|
| 曲库「搜索」 | `三向模糊搜索：曲名 + 歌手 + 主题标签任意组合关键词即时过滤（吸顶工具栏 + 歌手筛选 chips）` | `关键词搜索：曲名 / 歌手 / 专辑任意组合关键词即时过滤` |
| 巡检命名规则 | `统一「曲名[-歌手][-主题]」文件名结构` | `统一「曲名[-歌手][-专辑]」文件名结构` |
| 巡检预览表 | `当前名 / 解析 / 文字摘要 / 建议名 / 主题标签 / 采纳勾选` | `当前文件名 / 解析结果 / 建议新名 / 专辑 / 采纳勾选` |
| 巡检增强段 | `联网主题标签（可选增强）：Wikipedia …识别主题曲/影视/动漫/游戏归属并加标签` | `联网补全专辑（可选增强）：iTunes 取证真实专辑 + 本地中文来源作品兜底，离线时优雅降级为仅本地识别` |
| 目录树 `library_ops.py` | `…/inspect/wiki_tag/rename` | `…/巡检/专辑补全/重命名` |
| 目录树 `src/main.rs` | `…/rename_items / wiki_tag 命令…` | `…/rename_items / album_tag / album_tag_batch 命令…` |
| 参数表 | `文件名规则 | 曲名-歌手-主题来源` | `文件名规则 | 曲名-歌手-专辑` |

发布质量（B #9~#11，属「对外形象」范畴一并处理）：
- 目录树缩进：`   └─ src-tauri/` 及子行整体左移一格，与 `src/` 平级（现为 `├─ src-tauri/`），子项改用 `│  ` 对齐。
- 结尾补链接：`> 使用预览与支持作者：见 [docs/SUPPORT.md](docs/SUPPORT.md)（界面实拍 / 微信收款 / 爱发电）。`
- 清理内部口吻：L5 `（锚定基线，见 `ui-design-director` skill）` → `（风格为自研基线）`；L31 `复用「曲谱处理 v4.0」` → `缩放/白底/DPI`；L113 `主上看到的…` → `呈现…`；L128 `我自以为好了，但主上实际仍有黑框` → `自认为已修复，但实测仍有黑框`。

### 2. `docs/SUPPORT.md`

- **删除第 44 行**：`> 署名非「Workbuddy」等任何 AI 助手 —— 由 小凛酱丷 个人创作。`
  - 该行原文含「Workbuddy」「AI 助手」两个被禁字，属合规红线问题。整句删除；保留第 40 行「由 **小凛酱丷** 独立开发与维护」作为正向署名（与 B #8 建议一致）。

### 3. `src/index.html`

- 顶栏：删除 `<div class="pill" id="themePill">主题来源标注 · 开</div>`（旧概念「主题标签→专辑」后的冗余开关，B #7）。
- 处理台命名规则：`命名规则：曲名 - 歌手 - 专辑 - 编配（专辑/编配可选…按 MP3 网络音乐元数据规范）` → `命名规则：<b>曲名 - 歌手 - 专辑</b>（专辑可选，无则省略）`（编配字段已整体剔除，UI 不再展示，B #7）。
- 设置 · 文件名规则：`曲名-歌手-专辑-编配` → `曲名-歌手-专辑`。
- 设置：删除 `<div class="row"><span class="k">主题来源标注</span><div class="toggle" id="themeToggle"></div></div>`（对应开关整体移除，B #7）。

> 巡检区 / 曲库搜索占位符本身已是「联网补全专辑」「搜索 曲名 / 歌手 / 专辑…」，与实现一致，未改。

### 4. `src/main.js`

- **Remove 失效处理器**：删除 `// ==== 主题来源标注开关 ====` 整块（`$('themeToggle').onclick` 及 `$('themePill')` 引用）。因 index.html 已删 `themeToggle`/`themePill` 元素，若不删则页面加载即对 null 取 `.onclick` 抛错；骨干 `let themeOn = true;` 保留（`runBtn` 处 `theme` 参数逻辑不变，行为保持原有）。
- **可选项 A#3（已做）innerHTML 转义**：新增
  ```js
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  ```
  并应用于：
  - `renderLibrary`：`it.album`（主题 chip）、`it.title || it.name…`（卡片标题）、`it.artist`（歌手行）；
  - `renderInspect`：`cur_album`、`cur_title`、`cur_artist`、`det_title`、`det_artist`、`text_excerpt`（可见区 + title 属性）、`sug_album`、`sug_category`、`name`（单元格 + title 属性）、`suggested`。
  - 封堵恶意 PDF 首页文字 / iTunes·Wikipedia 网络返回内容注入脚本（A #3）。
  - 注：`rel`（卡片封面的 `data-name` 查询键）未转义——它被用作缩略图批量请求的匹配 key，转义会破坏取图；其来源是本机文件名，风险低于 PDF 首面/网络文本，故有意保留。

### 5. `library_ops.py`（必做#3，确定性 bug）

- **问题**（A #1）：`_wiki_category()`（调用于 `album_tag` 第 4 步）调用 `_wiki_search()`，但该函数全文件从未定义 → 每次 `setUp` 抛 `NameError`，被外层 `except Exception: continue` 吞掉，Wikipedia 分类兜底分支永远静默失效。
- **修复（正向最小）**：在 `_wiki_category` 前补上真实实现：
  ```python
  def _wiki_search(query: str, api: str, timeout: float = 3) -> dict:
      params = {'action': 'query', 'list': 'search', 'srsearch': query,
                'format': 'json', 'utf8': 1, 'srlimit': 3}
      url = api + '?' + urllib.parse.urlencode(params)
      req = urllib.request.Request(url, headers={'User-Agent': UA})
      with urllib.request.urlopen(req, timeout=timeout) as r:
          return json.loads(r.read().decode('utf-8', 'ignore'))
  ```
  - 完全复用文件已导入的 `json` / `urllib.parse` / `urllib.request` 与常量 `UA`，无新增依赖。
  - 修复后该兜底分支逻辑（`data.get('query',{}).get('search',[])` 遍历匹配 `_LOCAL_MEDIA_KEYWORDS`）与注释一致，行为与文档对齐。

---

## 三、验证结果

| 检查 | 命令/方式 | 结果 |
|---|---|---|
| Python 语法 | `C:/Python314/python.exe -m py_compile library_ops.py` | ✅ 通过（PY_COMPILE_OK） |
| Python 运行时修复 | import 后 `hasattr(_wiki_search)` + mock `urlopen` 调 `_wiki_category('测试')` | ✅ `has _wiki_search: True`，返回 `动画`（死功能已恢复，无 NameError） |
| JS 语法 | `node --check src/main.js` | ✅ 通过（NODE_OK） |
| HTML 标签闭合 | 脚本统计 `div`/`span`/`button` 开闭数 | ✅ 全部平衡（64/64、27/27、13/13） |
| 禁字/废弃语义清零 | 全局检索 4 文件 | ✅ 无残留命中（Workbuddy / AI 助手 / 主题来源 / 主题标签 / 编配 / wiki_tag / 歌手筛选 / ui-design-director / v4.0） |
| main.rs | 未改动 | 不适用（Rust 未编译，见下） |

---

## 四、未做项及原因

| 项 | 说明 | 原因 / 处理 |
|---|---|---|
| **A #2 Rust 子进程看门狗**（`main.rs` 超时 kill） | 未实现 | 改动较大、需引入通道/线程并触碰多处调用（`:167/220/256/319`），超出本次「必做」边界。**仅给建议**：给 `run_python_full`/`run_python_full_stdin`/`process_scores` 用 `spawn()` + `mpsc.recv_timeout` 或 `crossbeam` 做看门狗，超时 `child.kill()` 并返回 `PyResult{ok:false,error:"超时已终止",code:-2}`。Rust **未编译**，如后续实现请以 `cargo check` 验证。 |
| **A #4 run_local.py CORS `/api/file` 任意读** | 未做 | 属安全加固，不在本次必做清单；本地模式默认绑 127.0.0.1，风险可控，留待后续专项。 |
| **A #5 / #6 / #7 / #8 / #9 / #10**（性能与卫生） | 未做 | 超出本次「必做 3 项」范围；保持改动最小，未引入无关重构。 |

> 已顺带完成的可选项：**A #3（innerHTML 转义 XSS）**——见上文「main.js」条。

---

## 五、主上红线确认

- `docs/SUPPORT.md` 与 `README.md` 中 **已无任何「Workbuddy」「AI 助手」字样**（含曾被 B #8 点名、原文自带禁字的 SUPPORT.md:44，已整句删除）。
- 所有对外文案均未引入新的工具/AI 提及，保留正向署名「由 **小凛酱丷** 独立开发与维护」。

## 六、边界合规声明

- 未改动主上个人数据；未推 GitHub、未 `git commit`。
- 改动保持最小、贴合项目现有风格；未引入无关重构。
- 未编译 Rust（耗时大），`main.rs` 无改动故无需语法审计。
