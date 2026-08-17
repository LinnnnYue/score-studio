"""组装「解压即用」便携版 Score Studio。

产物：项目根目录下的 Score-Studio-Portable_x64.zip
内容：score-studio.exe + python_dist/（内嵌 Python）+ sheet_pipeline.py + README.txt

设计要点：
- 便携布局必须与 src-tauri/src/main.rs 的 resolve_python/resolve_pipeline 对齐：
  exe 同级目录下需存在 python_dist/python.exe 与 sheet_pipeline.py。
- 该脚本不依赖 Tauri 构建产物之外的任何外部工具，纯标准库实现。
"""

import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(ROOT, "src-tauri", "target", "release", "score-studio.exe")
PY_DIST = os.path.join(ROOT, "python_dist")
SCRIPT = os.path.join(ROOT, "sheet_pipeline.py")
LIB_OPS = os.path.join(ROOT, "library_ops.py")
OUT_DIR = os.path.join(ROOT, "Score-Studio-Portable")
ZIP_NAME = os.path.join(ROOT, "Score-Studio-Portable_x64.zip")


def fail(msg):
    print(f"[ERROR] {msg}")
    sys.exit(1)


def main():
    if not os.path.isfile(EXE):
        fail(f"未找到 release 产物：{EXE}\n请先运行 `npx tauri build` 完成编译。")
    if not os.path.isdir(PY_DIST):
        fail(f"未找到内嵌 Python 目录：{PY_DIST}")
    if not os.path.isfile(SCRIPT):
        fail(f"未找到脚本：{SCRIPT}")

    # 清空旧目录
    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR, exist_ok=True)

    # 1) 复制 exe
    print(f"[1/5] 复制主程序 exe ...")
    shutil.copy2(EXE, os.path.join(OUT_DIR, "score-studio.exe"))

    # 2) 复制内嵌 Python 目录
    print(f"[2/5] 复制内嵌 Python 目录（python_dist，约 150MB，请稍候）...")
    dst_py = os.path.join(OUT_DIR, "python_dist")
    shutil.copytree(PY_DIST, dst_py, ignore=shutil.ignore_patterns("__pycache__"))

    # 3) 复制曲谱管道脚本
    print(f"[3/5] 复制 sheet_pipeline.py ...")
    shutil.copy2(SCRIPT, os.path.join(OUT_DIR, "sheet_pipeline.py"))

    # 3.5) 复制曲库/巡检脚本
    if not os.path.isfile(LIB_OPS):
        print(f"[WARN] 未找到 library_ops.py：{LIB_OPS}（巡检/曲库功能将不可用）")
    else:
        print(f"[4/5] 复制 library_ops.py ...")
        shutil.copy2(LIB_OPS, os.path.join(OUT_DIR, "library_ops.py"))

    # 4) 写入说明
    readme = (
        "Score Studio · 曲谱工坊 — 便携版 (Portable)\n"
        "============================================\n"
        "\n"
        "无需安装，解压后双击 `score-studio.exe` 即可运行。\n"
        "本目录已自带完整 Python 运行时（python_dist/），不依赖系统安装 Python。\n"
        "\n"
        "目录结构：\n"
        "  score-studio.exe     主程序\n"
        "  python_dist/         内嵌 Python 3.13 + Pillow/numpy/PyMuPDF\n"
        "  sheet_pipeline.py    曲谱标准化管道脚本\n"
        "  library_ops.py       曲库浏览/封面缩略图/命名巡检脚本\n"
        "\n"
        "如需手动指定 Python 或脚本路径，可设置环境变量：\n"
        "  SCORE_PYTHON     指向任意 python 解释器\n"
        "  SCORE_PIPELINE   指向自定义 sheet_pipeline.py\n"
    )
    with open(os.path.join(OUT_DIR, "README.txt"), "w", encoding="utf-8") as f:
        f.write(readme)

    # 打包为 zip
    print(f"[5/5] 压缩为 {os.path.basename(ZIP_NAME)} ...")
    if os.path.exists(ZIP_NAME):
        os.remove(ZIP_NAME)
    with zipfile.ZipFile(ZIP_NAME, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(OUT_DIR):
            for name in files:
                full = os.path.join(base, name)
                rel = os.path.relpath(full, OUT_DIR)
                z.write(full, rel)

    size_mb = os.path.getsize(ZIP_NAME) / (1024 * 1024)
    print(f"[OK] 便携包已生成：{ZIP_NAME}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
