#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Score Studio · 曲谱处理后端管道
=================================
将散落的曲谱皈依为统一形制：
    提取（多源链接 / 本地） → 透明底→白底 → LANCZOS 缩放至 2009px 宽
    → 300 DPI PDF（无损封装，质量等价于 95 级 JPEG） → 输出至自定义目录。

复用自「曲谱处理 v4.0」skill 的成熟算法，去除微信发送步骤，改为可调用模块 / CLI。
依赖：Pillow（唯一必需第三方库）。已有 PDF 重处理可选 pymupdf。

用法：
    python sheet_pipeline.py --input <链接或本地路径> --output-dir <目录> [--theme "游戏主题曲"] [--name "自定义名"]
    python sheet_pipeline.py --selftest            # 冒烟测试（生成透明图→白底→PDF）
"""

import argparse
import html as html_mod
import io
import os
import re
import sys
import urllib.request

# Windows 下强制 STDIO 为 UTF-8（应用内 Python 默认 GBK/cp936，会导致中文日志/文件名输出乱码或 UnicodeEncodeError）
for _s in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ===================== 配置 =====================
TARGET_WIDTH = 2009          # 标准宽度
PDF_DPI = 300                # 输出分辨率
PDF_QUALITY = 95            # 质量参照（PDF 为无损封装，等效此级）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")
ILLEGAL = r'[\\/:*?"<>|]'    # Windows 非法文件名字符


# ===================== 网络 =====================
def fetch_html(url: str) -> str:
    """抓取网页 HTML（绕过微信 robots 限制：服务端 HTML 已含 data-src 全部图片 URL）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def download_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def download_image(url: str):
    """下载图片为 PIL.Image（去 query 参数取干净 URL）。"""
    from PIL import Image
    clean = url.split("?")[0]
    data = download_bytes(clean)
    return Image.open(io.BytesIO(data))


# ===================== 提取 =====================
def extract_wechat(html: str):
    """微信公众号：提取 mmbiz.qpic.cn 图片 URL（PNG/JPEG 均匹配），去 query。"""
    pat = re.compile(r'data-src="(https?://[^"]*?mmbiz[^"]*?wx_fmt=(?:png|jpeg)[^"]*)"')
    urls = [u.split("?")[0] for u in pat.findall(html)]
    return list(dict.fromkeys(urls))  # 去重保序


def extract_tan8(html: str):
    """弹琴吧：提取隐藏的高清标准版曲谱 URL（*_standard/ 目录）。"""
    pat = re.compile(r'https://oss\.tan8\.com/yuepuku/\d+/\d+/\d+_\w+_standard/\d+_\w+\.ypad\.\d+\.png')
    return list(dict.fromkeys(pat.findall(html)))


def extract_generic(html: str):
    """通用兜底：抓取所有图片 URL，交由尺寸/体积筛选。"""
    pat = re.compile(r'src="(https?://[^"]+\.(?:png|jpe?g))"', re.I)
    return list(dict.fromkeys(u.split("?")[0] for u in pat.findall(html)))


def extract_urls(input_str: str):
    """按来源分派提取器，返回候选图片 URL 列表。"""
    if input_str.lower().startswith("http"):
        html = fetch_html(input_str)
        if "mp.weixin.qq.com" in input_str:
            return extract_wechat(html), "微信公众号"
        if "tan8.com" in input_str:
            return extract_tan8(html), "弹琴吧"
        return extract_generic(html), "网页"
    return [], "本地"


# ===================== 图像处理 =====================
def handle_transparent(img):
    """透明底（RGBA / LA / P）→ 白底（通用，所有来源先执行）。"""
    from PIL import Image
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    if img.mode == "P":
        return handle_transparent(img.convert("RGBA"))
    return img.convert("RGB")


def remove_green(img):
    """弹琴吧绿底（约 RGB(71,112,76)）→ 白底。"""
    import numpy as np
    from PIL import Image
    arr = np.array(img.convert("RGB"))
    mask = np.all((arr >= [60, 100, 65]) & (arr <= [85, 125, 90]), axis=2)
    arr[mask] = [255, 255, 255]
    return Image.fromarray(arr)


def resize_standard(img):
    """统一缩放至 2009px 宽（LANCZOS，保持纵横比）。"""
    from PIL import Image
    w, h = img.size
    if w == TARGET_WIDTH:
        return img
    new_h = int(h * (TARGET_WIDTH / w))
    return img.resize((TARGET_WIDTH, new_h), Image.LANCZOS)


def is_score_candidate(img_bytes_len: int, w: int, h: int) -> bool:
    """筛选曲谱：高度 > 800px 且体积 > 50KB 且宽高比贴近 A4（排除图标/装饰/头像/竖版封面）。"""
    if h <= 800 or img_bytes_len <= 50_000:
        return False
    ratio = w / h
    # A4：竖版 0.707 / 横版 1.414，放宽邻域防轻微变形误滤
    return 0.65 <= ratio <= 0.80 or 1.30 <= ratio <= 1.50


def drop_cover_page(images: list) -> list:
    """多页一致性：首页比例与其余页（中位数）差异 >5% 时视为封面插画剔除。
    根治微信文章「封面图混入第一页」问题（如 7rings 0.912 / 过海 1.439 / 归舟 0.75 vs 真谱 0.706）。"""
    if len(images) < 2:
        return images
    rest = [im.width / im.height for im in images[1:]]
    base = sorted(rest)[len(rest) // 2]  # 中位数，抗噪声
    first = images[0].width / images[0].height
    if abs(first - base) / base > 0.05:
        print(f"  ✗ 剔除封面（首页比例 {first:.3f} vs 曲谱 {base:.3f}）")
        return images[1:]
    return images


def process_images(urls, is_tan8=False, max_pages=12):
    """下载→筛选→透明/绿底→缩放→封面剔除，返回 RGB 图列表。"""
    from PIL import Image
    out = []
    for url in urls[:max_pages]:
        try:
            data = download_bytes(url.split("?")[0])
            img = Image.open(io.BytesIO(data))
            w, h = img.size
            if not is_score_candidate(len(data), w, h):
                continue
            img = handle_transparent(img)
            if is_tan8:
                img = remove_green(img)
            img = resize_standard(img)
            out.append(img)
            print(f"  ✓ {os.path.basename(url)[:34]:34} → {img.size[0]}×{img.size[1]}")
        except Exception as e:
            print(f"  ✗ 跳过 {url[:40]}: {e}")
    return drop_cover_page(out)


# ===================== 本地 =====================
def local_images(folder: str):
    exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(exts))
    return [os.path.join(folder, f) for f in files]


def local_pdf(path: str):
    """已有 PDF：逐页转图 → 透明转白底 → 缩放。需 pymupdf。"""
    import fitz
    from PIL import Image
    doc = fitz.open(path)
    imgs = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        img = handle_transparent(img)
        imgs.append(resize_standard(img))
    doc.close()
    return imgs


# ===================== PDF =====================
def to_pdf(images, output_path: str):
    """RGB 图列表 → 300 DPI 标准 PDF（无损封装）。"""
    if not images:
        raise ValueError("无有效曲谱图片")
    rgb = [im.convert("RGB") for im in images]
    first, rest = rgb[0], rgb[1:]
    save_kw = dict(save_all=True, append_images=rest, resolution=PDF_DPI) if rest else dict(resolution=PDF_DPI)
    first.save(output_path, "PDF", **save_kw)
    return output_path


# ===================== 命名 =====================
def safe_name(s: str) -> str:
    return re.sub(ILLEGAL, "-", s).strip().strip("-")


# 标题噪音词（搬运/制谱标识、格式词），不进入文件名
_NOISE_WORDS = ("揉揉酱", "自制", "钢琴谱", "小提琴谱", "吉他谱", "尤克里里谱",
                "双手简谱", "简谱", "五线谱", "弹唱谱", "指弹谱", "歌谱", "曲谱",
                "乐谱", "谱子", "微信公众平台", "公众号", "伴奏", "教学", "翻唱")


def extract_title(html_text: str) -> str:
    """多通道提取文章标题：og:title → activity-name → rich_media_title → msg_title → h1 → title。
    返回清洗后的标题；不可得时返回空串（绝不退回链接 ID）。"""
    if not html_text:
        return ""
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:title["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\'](.*?)["\']',
        r'id=["\']activity-name["\'][^>]*>(.*?)<',
        r'class=["\'][^"\']*rich_media_title[^"\']*["\'][^>]*>(.*?)<',
        r'var\s+msg_title\s*=\s*["\'](.*?)["\']',
        r'<h1[^>]*>(.*?)</h1>',
        r'<title[^>]*>(.*?)</title>',
    )
    for pat in patterns:
        m = re.search(pat, html_text, re.I | re.S)
        if not m:
            continue
        t = re.sub(r"<[^>]+>", "", m.group(1))      # 去内嵌标签
        t = html_mod.unescape(t).strip()             # HTML 实体反转义
        t = re.sub(r"\s+", " ", t).strip()           # 折叠空白
        if t:
            return t
    return ""


def parse_title_fields(title: str):
    """从标题解析出 (曲名, 歌手)：
      1) 截断『揉揉酱/自制』等搬运标识之前的部分
      2) 按 |｜_·；;，, 拆字段 → 前两段为曲名/歌手
      3) 形如『曲名（歌手）』『曲名-歌手』的兜底解析
      4) 剥离曲谱格式噪音词
    """
    t = title.strip()
    for marker in ("揉揉酱", "自制"):
        idx = t.find(marker)
        if idx > 0:
            t = t[:idx]
            break
    t = t.strip(" |｜·-—_")
    fields = [f.strip() for f in re.split(r"[|｜_·,，;；]", t) if f.strip()]
    if len(fields) >= 2:
        title_part, artist_part = fields[0], fields[1]
    else:
        m = re.search(r"^(.*?)[（(]([^（）()]{1,40})[）)]$", fields[0])
        if m:
            title_part, artist_part = m.group(1).strip(), m.group(2).strip()
        else:
            m = re.search(r"^(.*?)[-\s—–]{1,2}([^-—–\s]{2,40})$", fields[0])
            if m:
                title_part, artist_part = m.group(1).strip(), m.group(2).strip()
            else:
                title_part, artist_part = fields[0], ""
    # 剥噪音词（保留【八仙】等前缀标签）
    for w in _NOISE_WORDS:
        title_part = title_part.replace(w, "")
    title_part = re.sub(r"\s+", " ", title_part).strip(" -_—–|｜·")
    return title_part or "曲谱", artist_part.strip()


def derive_name(input_str: str, html_text: str = "", theme: str = "", custom: str = ""):
    """文件名：曲名[-歌手][-主题来源].pdf
    优先级：custom > 页面标题解析 > 本地路径 basename。
    网页无标题且未提供 custom → 抛 ValueError（拒绝用链接 ID 冒充曲名）。"""
    if custom:
        base = custom
    else:
        title = extract_title(html_text)
        if title:
            base, artist = parse_title_fields(title)
            if artist:
                base = f"{base}-{artist}"
        elif os.path.isdir(input_str) or os.path.isfile(input_str):
            base = os.path.basename(input_str.rstrip(os.sep))
            if base.lower().endswith(".pdf"):
                base = os.path.splitext(base)[0]
            base = base or "曲谱"
        elif re.match(r"^[A-Za-z]:[\\/]", input_str) or input_str.startswith(("/", "\\")):
            base = os.path.basename(input_str.rstrip("\\/"))
            if base.lower().endswith(".pdf"):
                base = os.path.splitext(base)[0]
            base = base or "曲谱"
        else:
            raise ValueError(
                "无法自动命名：页面未提供标题。请用 --name 指定曲名（格式：曲名 或 曲名-歌手）。")
    base = safe_name(base)
    if theme:
        base = f"{base}-{safe_name(theme)}"
    return base + ".pdf"


# ===================== 编排 =====================
def run(input_str: str, output_dir: str, theme: str = "", custom: str = ""):
    # 编码免疫：Windows 管道默认 ANSI(GBK)，路径/曲名含 emoji 时 print 会 UnicodeEncodeError → 处理整体失败
    # 强制 stdout/stderr 为 UTF-8（StringIO 场景无 reconfigure，异常忽略即可）
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    os.makedirs(output_dir, exist_ok=True)
    is_tan8 = "tan8.com" in input_str
    html = ""
    if input_str.lower().startswith("http"):
        html = fetch_html(input_str)
        urls, src = extract_urls_dispatch(input_str, html)
        print(f"[来源] {src} · 候选图片 {len(urls)} 张")
        images = process_images(urls, is_tan8=is_tan8)
    elif os.path.isdir(input_str):
        print("[来源] 本地图片文件夹")
        images = []
        for p in local_images(input_str):
            im = handle_transparent(Image.open(p)) if False else _open_local(p)
            images.append(resize_standard(im))
    elif input_str.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")) and os.path.isfile(input_str):
        print("[来源] 本地单张曲谱图片")
        im = _open_local(input_str)
        images = [resize_standard(im)]
    elif input_str.lower().endswith(".pdf"):
        print("[来源] 本地 PDF（重处理）")
        images = local_pdf(input_str)
    else:
        raise ValueError("无法识别的输入：需为链接 / 图片文件夹 / PDF 路径")

    if not images:
        print("⚠ 未提取到任何曲谱图片，退出。")
        return None

    try:
        name = derive_name(input_str, html, theme, custom)
    except ValueError as e:
        print(f"⚠ {e}")
        return None
    out = os.path.join(output_dir, name)
    to_pdf(images, out)
    # 自动写入 PDF /Info 元数据（曲名/歌手/专辑），等价于 MP3 的 ID3，使曲库与播放器可读
    try:
        from library_ops import smart_split, write_pdf_metadata
        base = os.path.splitext(name)[0]
        mt, ma, malb = smart_split(base)
        if write_pdf_metadata(out, title=mt or None, artist=ma or None,
                              album=malb or None):
            print(f"[元数据] 已写入：曲={mt or '-'} 歌手={ma or '-'} 专辑={malb or '-'}")
    except Exception as e:
        print(f"[元数据] 写入跳过：{e}")
    print(f"✅ PDF 已生成：{out}（{len(images)} 页 · {TARGET_WIDTH}px · {PDF_DPI}DPI）")
    return out


def extract_urls_dispatch(input_str, html):
    if "mp.weixin.qq.com" in input_str:
        return extract_wechat(html), "微信公众号"
    if "tan8.com" in input_str:
        return extract_tan8(html), "弹琴吧"
    return extract_generic(html), "网页"


def _open_local(p):
    from PIL import Image
    return handle_transparent(Image.open(p))


# ===================== 冒烟测试 =====================
def selftest():
    from PIL import Image, ImageDraw
    print("== 冒烟测试：透明底→白底 → LANCZOS 2009px → PDF ==")
    # 生成一张 RGBA 透明底测试图（中心一个深色音符，四周透明）
    im = Image.new("RGBA", (800, 1142), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([300, 300, 500, 900], fill=(40, 30, 20, 255))
    d.line([380, 250, 380, 950], fill=(40, 30, 20, 255), width=14)
    tmp = os.path.join(os.path.dirname(__file__), "_selftest_in.png")
    im.save(tmp)

    out_dir = os.path.join(os.path.dirname(__file__), "_selftest_out")
    os.makedirs(out_dir, exist_ok=True)
    img = handle_transparent(Image.open(tmp))
    img = resize_standard(img)
    assert img.size[0] == TARGET_WIDTH, f"宽度应为 {TARGET_WIDTH}，实际 {img.size[0]}"
    out = os.path.join(out_dir, "selftest.pdf")
    to_pdf([img], out)

    # 校验 PDF 宽度
    with open(out, "rb") as f:
        head = f.read(60)
    print(f"  ✓ 输出尺寸 {img.size[0]}×{img.size[1]}")
    print(f"  ✓ PDF 已写出：{out}")
    print("自检通过。")


# ===================== CLI =====================
def main():
    ap = argparse.ArgumentParser(description="Score Studio 曲谱处理管道")
    ap.add_argument("--input", help="链接 / 本地图片文件夹 / 本地 PDF")
    ap.add_argument("--output-dir", default=r"G:\Lin_File\Documents\曲谱")
    ap.add_argument("--theme", default="", help="主题来源标注，如：游戏主题曲 / 动漫")
    ap.add_argument("--name", default="", help="自定义文件名（不含扩展名）")
    ap.add_argument("--selftest", action="store_true", help="运行冒烟测试")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if not args.input:
        ap.error("需提供 --input 或 --selftest")
    out = run(args.input, args.output_dir, theme=args.theme, custom=args.name)
    if out is None:
        print("未生成 PDF：若提示命名失败，请补充 --name 指定曲名后重试。")


if __name__ == "__main__":
    main()
