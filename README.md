# Score Studio · 曲谱工坊

将散落的曲谱皈依为统一形制：**提取（多源链接 / 本地）→ 透明底转白底 → LANCZOS 缩放至 2009px 宽 → 300DPI·95 PDF → 输出至自定义目录**。

外壳主题：**古典 · 巴洛克 & 教堂**，无边框高级感 Flutter 风窗口（风格为自研基线）。

---

## 功能总览

四大视图（左侧导航切换）：

1. **处理（Process）**：从多源链接 / 本地文件提取曲谱，统一为 2009px·300DPI·白底 PDF（核心管道）。
2. **曲库（Library）**：浏览输出目录中的 PDF 曲谱墙。
   - **真实封面**：用 PyMuPDF 渲染 PDF 首页缩略图（按 `basename+size+mtime` 缓存）。
   - **关键词搜索**：曲名 / 歌手 / 专辑任意组合关键词即时过滤。
   - **点击开 PDF**：卡片点击直接调用系统默认程序打开对应 PDF（Tauri 走 `open_path`，本地走 `/api/file` 下载）。
   - **深色滚动条**：与首页一致的全局滚动条风格。
3. **巡检（Inspect · 命名规范化）**：统一「曲名[-歌手][-专辑]」文件名结构。
   - 数字 PDF：用 PyMuPDF 抽首页文字补名；扫描件：回退仅解析文件名 + 标记待人工补名。
   - **预览优先、绝不静默改**：先展示预览表（当前文件名 / 解析结果 / 建议新名 / 专辑 / 采纳勾选）→ 勾选 → 点执行 → `confirm()` 二次确认 → 才真正重命名（不可逆操作红线）。
   - **联网补全专辑（可选增强）**：iTunes 取证真实专辑 + 本地中文来源作品兜底，离线时优雅降级为仅本地识别，不阻塞主流程。
4. **设置（Settings）**：主题与运行偏好。

> 全部前端逻辑（`src/main.js`）自动判别 Tauri / 本地服务器两种后端，单一代码源同时服务两种运行时。

## 目录结构

```
score-studio/
├─ sheet_pipeline.py      # 后端管道（曲谱标准化：缩放/白底/DPI）
├─ library_ops.py         # 曲库/巡检后端（缩略图/命名解析/巡检/专辑补全/重命名）
├─ run_local.py           # 本地运行服务器（标准库，无需 Node/Rust）
├─ vite.config.js         # 前端构建配置（root=src，out=dist）
├─ package.json           # 前端依赖与脚本
├─ _build_python_dist.py  # 生成内嵌 Python 发行包（embeddable Python + 依赖）
├─ make_portable.py       # 组装「解压即用」便携 zip
├─ python_dist/           # 内嵌 Python 运行时（构建生成，~150MB，不入库）
├─ icon-source.png        # 应用图标源图（1024×1024 巴洛克风）
├─ src/
│  ├─ index.html          # 巴洛克无边框 UI（Tauri / 本地通用）
│  ├─ styles.css          # 主题样式（含真·无边框形态）
│  └─ main.js             # 前端逻辑：自动判别 Tauri / 本地，调用对应后端
├─ ui-prototype.html      # 设计稿（独立单文件可视化原型，可双击直接看效果）
├─ score-studio-launch-proof.png  # 真·无边框窗口首启截图证据
├─ 启动曲谱工坊.bat       # 一键启动（ASCII 编码，自动配置 Python 环境）
├─ src-tauri/             # Tauri v2 工程（无边框窗体 + Rust 命令桥接 Python）
│  ├─ tauri.conf.json     # decorations:false + transparent:false + shadow:false
│  ├─ Cargo.toml
│  ├─ build.rs
│  ├─ src/main.rs         # process_scores / list_library / open_path / get_library / inspect_library / rename_items / album_tag / album_tag_batch 命令 + Python 自动探测
│  ├─ capabilities/default.json
│  └─ icons/              # 编译期生成的图标全套（png/ico/icns）
```

---

## 路径一 · 当下即可运行（本地服务器 · 验证管线）

无需安装 Node / Rust，只要 Python + Pillow：

```bash
# 1. 准备环境（一次性）
python -m venv .venv
.venv\Scripts\activate
pip install Pillow

# 2. 启动本地服务器
python run_local.py
# 打开 http://127.0.0.1:8765/
```

此形态以**浏览器窗口**呈现 UI（全屏 app 形态），用于端到端验证「前端 → 管道 → PDF」闭环。真正的「无边框高级感窗口」由路径二提供。

---

## 路径二 · 真·无边框桌面软件（Tauri v2）

需要：Rust 工具链 + Node 22+ +（Windows）WebView2 运行时。

```bash
# 1. 安装前端依赖
npm install

# 2. 生成应用图标（首次需要）
npm run tauri icon icon-source.png     # 会写入 src-tauri/icons/ 全套

# 3. 编译（首次约 4 分钟拉取 tauri 全家桶依赖）
npm run tauri build -- --no-bundle
# 产出：src-tauri\target\release\score-studio.exe

# 4. 双击启动曲谱工坊.bat，或直接运行
src-tauri\target\release\score-studio.exe
```

**实测验证**：首启进程仅占 30MB 内存，标题 `Score Studio · 曲谱工坊`；截屏见 `score-studio-launch-proof.png`。

### 无边框窗口要点（已固化）
- `tauri.conf.json` → `app.windows[0]`：`decorations: false`（去原生标题栏/边框）、`shadow: false`（避免 DWM inset）、`transparent: false`（不透桌面，避免 WebView2 透明兼容黑角）、`backgroundColor: "#16130e"`（深墨底与 .win 同色系）
- 前端 `main.js` 必须给 `body` 加 `tauri` 类：`if (IS_TAURI) document.body.classList.add('tauri')`，触发 `styles.css` 中的「真·无边框形态」：
  - `.win` 铺满 `100vw/100vh`（无 max-width/max-height 限制）
  - `.win` `border-radius: 16px` + `box-shadow: 0 0 0 1px rgba(201,169,97,0.22)`（金箔细边）+ 弥散阴影
  - `body` 背景 `#16130e`（与 `.win` 同色），圆角外不露深色底
- Rust 侧在 `.setup` 中用 `SetWindowRgn(CreateRoundRectRgn(...))` 把窗体裁剪为 16px 圆角矩形；监听 `WindowEvent::Resized` 在缩放时重新裁剪（见 `src/main.rs`）
- 窗口控制（最小化/最大化/关闭）由 `main.js` 通过 `getCurrentWindow()` 调用，标题栏区域以 `-webkit-app-region: drag` 实现拖拽

### 黑框问题的最终根因（v12）
**真因**：`main.js` 判别 Tauri 后，只给非 Tauri 模式加 `appmode` 类，Tauri 运行时 `body` 没有 `tauri` 类。

结果：
- `.win` 走默认样式：`width:1020px; height:680px; max-width:96vw; max-height:94vh; border-radius:20px`（居中卡片）
- `body` 背景是 `radial-gradient`（深色，边缘 #0e0c09）
- 圆角 UI 卡片浮在中央，四角外露出 body 深色背景 → 呈现「方角黑框框起圆角界面」

**修复**（`src/main.js`）：
```javascript
const IS_TAURI = typeof window !== 'undefined' && !!window.__TAURI_INTERNALS__;
if (IS_TAURI) document.body.classList.add('tauri');
if (!IS_TAURI) document.body.classList.add('appmode');
```

修复后 Tauri 运行时 `.win` 铺满窗口，`body` 背景与 `.win` 同色，圆角外无对比深色带。PrintWindow 1020x680 截屏 + 像素扫描验证边缘 ≈ #16130e，无黑框（见 `score-studio-launch-proof.png`）。

### 踩坑历史（引以为戒）
1. **v5-v6 误判**：以为是 WebView2 transparent 窗口 22px inset → 改为 `transparent:false + backgroundColor:#16130e`。方向对了一半，但不是真因。
2. **v8-v9 误判**：以为是 WebView2 controller bounds 未同步 → 引入 `webview2-com` + `windows` crate 做 `SetBounds`。实际上多余的 Rust 介入会干扰 wry 的 frontendDist 加载。
3. **v9 中间真相**：`tauri.conf.json` 的 `devUrl` 字段会让 release exe navigate 到 localhost，导致错误页 + 黑框。已删除 `devUrl` / `beforeDevCommand` / `beforeBuildCommand`，只保留 `frontendDist: "../dist"`。
4. **v11 二次误判**：以为是 `.win` 圆角外 webview 背景色差 → 改同色后自认为已修复，但实测仍有黑框。
5. **v12 真因**：`body.tauri` 类缺失。此类错误极隐蔽：本地测试或某些路径下 `.win` 恰好铺满，容易让诊断人误以为 CSS 已生效。
6. **v13 透明窗圆角未遂**：尝试 `transparent:true + backgroundColor:#00000000 + body 透明 + border-radius` 让圆角外透桌面。在部分 WebView2/GPU 组合上透明不生效，仍显直角黑底，故弃用。
7. **v14 最终方案**：Rust 侧 `SetWindowRgn` 直接把窗体裁剪为圆角矩形，不依赖 WebView2 透明，与 CSS `border-radius:16px` 对齐。截图见 `score-studio-rounded-proof.png`。

### Python 管线接入（内嵌自包含，免系统 Python）
Rust 命令 `process_scores` 通过 `std::process::Command` 调用 `sheet_pipeline.py`。

打包后应用**自带完整 Python 运行时**（`python_dist/`，基于官方 embeddable Python 3.13 + Pillow / numpy / PyMuPDF），用户机器无需预装 Python。

**探测顺序**（`main.rs` 的 `resolve_python` / `resolve_pipeline`）：
1. 环境变量 `SCORE_PYTHON`（强制覆盖）
2. 内嵌 `python_dist/python.exe`：先查 `resource_dir()`，再查 exe 同级目录（同时兼容 NSIS 安装版与便携版两种布局）
3. 系统 `py` / `python` / `python3` 兜底

`SCORE_PIPELINE` 同理优先解析内嵌 `sheet_pipeline.py`。构建内嵌 Python 见 `_build_python_dist.py`（`python_dist/` 不入库，构建时生成）。

## 打包与发布（GitHub 风格双产物）
仿照主流 GitHub 桌面应用作者的做法，提供两种分发形态：

1. **安装版（NSIS 安装器 `.exe`）**
   ```bash
   npx tauri build        # bundle.targets=["nsis"]
   # 产出：src-tauri/target/release/bundle/nsis/Score Studio_0.1.0_x64-setup.exe
   ```
   双击安装，自动写入开始菜单 / 卸载项；Python 运行时（`python_dist/`、`sheet_pipeline.py`、`library_ops.py`）随 `bundle.resources` 直接铺入**安装根目录**（实测无解压到 `resources/` 子目录，故 `main.rs` 以 exe 同级路径回退命中）；WebView2 运行时采用默认 `downloadBootstrapper`——安装时若目标机未预装 WebView2 会联网拉取并就地安装（需联网；Windows 10/11 绝大多数已自带）。

2. **便携版（解压即用 `.zip`）**
   ```bash
   python make_portable.py
   # 产出：Score-Studio-Portable_x64.zip（exe + python_dist + sheet_pipeline.py + library_ops.py）
   ```
   解压后直接双击 `score-studio.exe`，无需安装、不写注册表，Python 与脚本随 exe 同级 `python_dist/` 一起携带。

> 两种形态均已接入内嵌 Python，`main.rs` 通过 `resource_dir()` 与 exe 同级回退双路径解析，确保安装版与便携版都能找到运行时。

### WebView2 分发形态与前置依赖
当前 `tauri.conf.json` 的 `bundle.windows` 未显式配置 `webviewInstallMode`，即采用 Tauri 默认 `downloadBootstrapper`：安装器运行时若探测到目标机未安装 WebView2，会自动联网下载并就地安装。**前提**：目标机需联网（一次）；Windows 10/11 自带 WebView2 的占比极高，多数用户无需额外步骤。

若需「完全离线自包含」（无网也能装好 WebView2），可在 `bundle.windows.webviewInstallMode` 二选一：
- `{ "type": "offlineInstaller", "silent": true }`：把约 127MB 的 WebView2 离线安装器内嵌进安装包，安装时静默装好。代价是安装包体积增大。
- `{ "type": "fixedRuntime", "path": "C:/.../解压后的固定版WV2目录" }`：内嵌固定版本 WV2 运行时目录（`Microsoft.WebView2.FixedVersionRuntime`），约 180MB，应用自带、不依赖系统安装。代价是体积最大、需先从官方 CAB 解压该目录。

> 离线方案曾因本机 C: 盘空间耗尽（`os error 112`）未能在本机完成验证；默认 `downloadBootstrapper` 模式已在本机通过干净静默安装实测（exit 0、文件正确落地），故当前默认形态稳定可用。

---

## 处理参数（默认值，一般无需改动）

| 项 | 值 |
|---|---|
| 目标宽度 | 2009 px |
| 缩放算法 | LANCZOS |
| 输出分辨率 | 300 DPI |
| PDF 质量 | 95（无损封装等效） |
| 透明底→白底 | 开 |
| 文件名规则 | 曲名-歌手-专辑 |

## 支持来源
- 微信公众号（`mp.weixin.qq.com`，提取 `mmbiz` 高清图）
- 弹琴吧（`tan8.com`，提取标准版曲谱 + 绿底转白底）
- 通用网页（尺寸/体积筛选）
- 本地图片文件夹 / 本地 PDF（重处理为统一形制）
- 抖音图文等（走通用提取，后续可补专用规则）

> 使用预览与支持作者：见 [docs/SUPPORT.md](docs/SUPPORT.md)（界面实拍 / 微信收款 / 爱发电）。
