"""组装「解压即用」便携版 Score Studio（免中间目录，流式直写 zip）。

产物：项目根目录下的 Score-Studio-Portable_x64.zip
内容：score-studio.exe + python_dist/（内嵌 Python）+ ccmz-engine/（虫虫渲染引擎）
      + sheet_pipeline.py + library_ops.py + README.txt

设计要点：
- 便携布局必须与 src-tauri/src/main.rs 的 resolve_python/resolve_pipeline 对齐：
  exe 同级目录下需存在 python_dist/python.exe 与 sheet_pipeline.py。
- 不落中间目录（避免大体积临时副本），直接 zipfile 流式打包。
- 该脚本不依赖 Tauri 构建产物之外的任何外部工具，纯标准库实现。
"""

import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(ROOT, "src-tauri", "target", "release", "score-studio.exe")
PY_DIST = os.path.join(ROOT, "python_dist")
SCRIPT = os.path.join(ROOT, "sheet_pipeline.py")
LIB_OPS = os.path.join(ROOT, "library_ops.py")
CCMZ_ENGINE = os.path.join(ROOT, "src-tauri", "resources", "ccmz-engine")
ZIP_NAME = os.path.join(ROOT, "Score-Studio-Portable_x64.zip")


def fail(msg):
    print(f"[ERROR] {msg}")
    sys.exit(1)


def _skip_rel(rel):
    """zip 内跳过缓存与 git 元数据（pycache 是运行期产物，不需要分发）。"""
    parts = rel.replace("\\", "/").split("/")
    return any(p in ("__pycache__", ".git") for p in parts)


def _add_tree(z, src, prefix):
    n = 0
    for base, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for name in files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, src)
            z.write(full, os.path.join(prefix, rel))
            n += 1
    return n


def main():
    if not os.path.isfile(EXE):
        fail(f"未找到 release 产物：{EXE}\n请先运行 `npx tauri build` 完成编译。")
    if not os.path.isdir(PY_DIST):
        fail(f"未找到内嵌 Python 目录：{PY_DIST}")
    if not os.path.isfile(SCRIPT):
        fail(f"未找到脚本：{SCRIPT}")

    if os.path.exists(ZIP_NAME):
        os.remove(ZIP_NAME)

    readme = (
        "Score Studio · 曲谱工坊 — 便携版 (Portable)\n"
        "============================================\n"
        "\n"
        "无需安装，解压后双击 `score-studio.exe` 即可运行。\n"
        "本目录已自带完整 Python 运行时（python_dist/），不依赖系统安装 Python。\n"
        "本目录已自带 ccmz 渲染引擎（ccmz-engine/，含 Node.js），虫虫钢琴完整曲谱开箱即用。\n"
        "\n"
        "目录结构：\n"
        "  score-studio.exe     主程序\n"
        "  python_dist/         内嵌 Python 3.13 + Pillow/numpy/PyMuPDF\n"
        "  ccmz-engine/         虫虫钢琴 ccmz 渲染引擎（含内嵌 Node.js）\n"
        "  sheet_pipeline.py    曲谱标准化管道脚本\n"
        "  library_ops.py       曲库浏览/封面缩略图/命名巡检脚本\n"
        "\n"
        "如需手动指定 Python 或脚本路径，可设置环境变量：\n"
        "  SCORE_PYTHON     指向任意 python 解释器\n"
        "  SCORE_PIPELINE   指向自定义 sheet_pipeline.py\n"
    )

    print("[1/5] 流式打包 score-studio.exe ...")
    with zipfile.ZipFile(ZIP_NAME, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(EXE, "score-studio.exe")
        n_py = _add_tree(z, PY_DIST, "python_dist")
        print(f"[2/5] python_dist/ 已入包（{n_py} 文件）...")
        z.write(SCRIPT, "sheet_pipeline.py")
        if os.path.isfile(LIB_OPS):
            z.write(LIB_OPS, "library_ops.py")
        else:
            print("[WARN] 未找到 library_ops.py（巡检/曲库功能将不可用）")
        if os.path.isdir(CCMZ_ENGINE):
            n_en = _add_tree(z, CCMZ_ENGINE, "ccmz-engine")
            print(f"[3/5] ccmz-engine/ 已入包（{n_en} 文件）...")
        else:
            print("[WARN] 未找到 ccmz 渲染引擎（虫虫 ccmz 功能将不可用）")
        z.writestr("README.txt", readme)

    size_mb = os.path.getsize(ZIP_NAME) / (1024 * 1024)
    print(f"[OK] 便携包已生成：{ZIP_NAME}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()