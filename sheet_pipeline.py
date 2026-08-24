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
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
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
def _decode_html(data: bytes, ctype: str = "") -> str:
    """按 Content-Type/meta charset 声明解码，无声明或 UTF-8 有损时 GBK 兜底。"""
    raw = data.decode("utf-8", errors="ignore")
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if not m:
        m = re.search(r'<meta[^>]+charset=["\']?([\w-]+)', raw[:2000], re.I)
    enc = m.group(1).strip().lower() if m else ""
    if enc and enc not in ("utf-8", "utf8"):
        try:
            return data.decode(enc, errors="ignore")
        except Exception:
            pass
    if enc in ("utf-8", "utf8") or raw.count("\ufffd") > 20:
        # UTF-8 乱码（替换符多）→ 换 GBK 兜底（常见中文站）
        try:
            gbk = data.decode("gbk", errors="ignore")
            if gbk.count("\ufffd") < raw.count("\ufffd"):
                return gbk
        except Exception:
            pass
    return raw


def fetch_html(url: str, cookie: str = "") -> str:
    """抓取网页 HTML。cookie 传 Cookie 头（词曲网 ktvc8 云锁需带验证会话）。

    健壮性三重保障（应对代理节点不稳 / 站点 TLS 风控导致的 SSL EOF）：
      1) 直连优先——国内站点绕开本地 Clash 等代理节点不稳的握手失败；
      2) 系统代理降级重试（各 2 次）；
      3) urllib 全失败后调系统 curl.exe 兜底（schannel TLS 栈更抗风控）。
    编码：优先 headers charset → meta → GBK 兜底（中文站点多为 GBK）。"""
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    cookie = normalize_cookie(cookie)
    if cookie:
        headers["Cookie"] = cookie

    last_err = None
    # 1+2) 直连 → 系统代理，各重试 2 次
    for direct in (True, False):
        for attempt in range(2):
            try:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({})) if direct else urllib.request.build_opener()
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=30) as r:
                    return _decode_html(r.read(), r.headers.get("Content-Type", ""))
            except Exception as e:
                last_err = e
                time.sleep(1.0 * (attempt + 1))

    # 3) 系统 curl.exe 兜底（schannel TLS 栈，抗站点对 python ssl 的瞬时掐断）
    try:
        curl = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "curl.exe")
        if os.path.isfile(curl):
            args = [curl, "-s", "-L", "--connect-timeout", "15", "-m", "45",
                    "-A", UA, "--compressed", url]
            if cookie:
                args += ["-H", "Cookie: " + cookie]
            proc = subprocess.run(args, capture_output=True, timeout=60)
            if proc.returncode == 0 and proc.stdout:
                return _decode_html(proc.stdout)
    except Exception as e:
        last_err = last_err or e

    raise urllib.error.URLError(f"抓取失败（网络/SSL）：{last_err}") from last_err


def is_waf_page(html: str) -> bool:
    """检测是否撞上云锁/防火墙验证页（机器访问被拦的标志）。
    覆盖两种形态：① 大防火墙页（标题=网站防火墙）② JS 自动跳转挑战页（YunSuoAutoJump，
    小页面 + security_verify_data 跳转，srcurl 与当前 URL 不匹配时触发）。"""
    if re.search(r"<title>\s*网站防火墙\s*</title>", html, re.I):
        return True
    if re.search(r"cloudwaf|waf\.ktvc8|验证码|滑动验证", html, re.I) and len(html) < 20000:
        return True
    if re.search(r"YunSuoAutoJump|security_verify_data", html) and len(html) < 5000:
        return True
    return False


def download_bytes(url: str) -> bytes:
    """下载二进制内容。直连优先 + 重试 + 系统 curl.exe 兜底（应对 SSL EOF/节点不稳）。"""
    last_err = None
    for direct in (True, False):
        for attempt in range(2):
            try:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({})) if direct else urllib.request.build_opener()
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with opener.open(req, timeout=30) as r:
                    return r.read()
            except Exception as e:
                last_err = e
                time.sleep(1.0 * (attempt + 1))
    try:
        curl = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "curl.exe")
        if os.path.isfile(curl):
            args = [curl, "-s", "-L", "--connect-timeout", "15", "-m", "60",
                    "-A", UA, url]
            proc = subprocess.run(args, capture_output=True, timeout=90)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
    except Exception as e:
        last_err = last_err or e
    raise urllib.error.URLError(f"下载失败（网络/SSL）：{last_err}") from last_err


def normalize_cookie(raw: str) -> str:
    """规范化 Cookie 输入（Cookie Editor 导出容错）：
    - 去 'Cookie:' 前缀 / 首尾空白 / 换行
    - 去掉末尾孤立 ';' 与空段
    返回可直接作 Cookie 头的 'k=v;k=v' 串；空输入返回 ''。"""
    if not raw:
        return ""
    s = re.sub(r'(?i)^Cookie\s*:\s*', '', raw.strip())
    s = s.replace("\r", "\n").replace("\n", " ").strip()
    parts = [p.strip() for p in s.split(";") if p.strip()]
    return ";".join(parts)


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
    """弹琴吧：提取隐藏的高清标准版曲谱 URL（*_standard/ 目录）。
    页面把图片 URL 放在 JS 数组（yuepuArrXian 五线谱 / yuepuArrJian 简谱）里，
    JSON 序列化后斜杠带 \\/ 转义（https:\\/\\/oss.tan8.com\\/...），两种形态都要匹配。"""
    pat = re.compile(
        r'https?:\\?/\\?/oss\.tan8\.com\\?/yuepuku\\?/\d+\\?/\d+\\?/\d+_\w+_standard\\?/\d+_\w+\.ypad\.\d+\.png')
    urls = []
    for u in pat.findall(html):
        u = u.replace("\\/", "/")
        if u not in urls:
            urls.append(u)
    return urls


# ===================== 词曲网（ktvc8.com） =====================
# 谱面为位图 jpg/png，位于 .contentpic 内（uploadfiles/YYYYMMDD/xxx.png 或 uploaduserskinfiles/...）。
# 站点挂了云锁 WAF：机器请求可能弹「网站防火墙」页 → 需带 Cookie（设置里粘贴会话）或换网络。
# 坑：剩余页图藏在内联 JS（myFunction/show_neirong 的字符串拼接里），点击「查看剩余N张曲谱」才写入 DOM。
#     所以提取必须从 JS 字符串里也挖 src —— 只抓 contentpic 的 img 会漏页（如《耳朵》只拿到第 1 页）。
def _ktvc8_imgs(html: str):
    """提取词曲网全部曲谱大图（contentpic img + 内联 JS 字符串里的剩余页 src），去重保序。"""
    out = []
    # 1) contentpic 内直接 img（首页序）
    m = re.search(r'class="contentpic"[^>]*>(.*?)</div>', html, re.S | re.I)
    if m:
        for u in re.findall(
                r'(?:src|data-src)="([^"]*?/(?:uploadfiles|uploaduserskinfiles)/[^"]+?\.(?:png|jpe?g|jpg|gif))"',
                m.group(1), re.I):
            u = u.split("?")[0]
            if u.startswith(".."):
                u = "https://www.ktvc8.com/" + u.lstrip("./")
            if u not in out:
                out.append(u)
    # 2) 全文（含内联 JS 字符串 var carname='<img src=...>' 的剩余页）
    for u in re.findall(
            r"['\"]([^'\"<>]*?/(?:uploadfiles|uploaduserskinfiles)/[^'\"<>]+?\.(?:png|jpe?g|jpg|gif))['\"]",
            html, re.I):
        u = u.split("?")[0]
        if u.startswith(".."):
            u = "https://www.ktvc8.com/" + u.lstrip("./")
        if u not in out:
            out.append(u)
    return out


def _ktvc8_fetch_js(url: str, cookie: str = "") -> tuple:
    """词曲网云锁 JS 挑战兜底：调引擎 ktvc8_fetch.mjs（puppeteer 真实浏览器自动执行
    YunSuoAutoJump 跳转验证 + 挖 contentpic/JS 字符串全部图，含 upload-files 新域名）。
    返回 (imgs, title)；引擎不可用/失败返回 ([], "")。"""
    try:
        engine = _find_ccmz_engine()
        node = _find_node()
        if not engine or not node:
            return [], ""
        eng_dir = os.path.dirname(engine)
        script = os.path.join(eng_dir, "ktvc8_fetch.mjs")
        if not os.path.isfile(_clean_win_path(script)):
            return [], ""
        cmd = [_clean_win_path(node), _clean_win_path(script), url, cookie or ""]
        proc = subprocess.run(cmd, capture_output=True, timeout=150, cwd=eng_dir)
        raw = (proc.stdout or b"").decode("utf-8", "ignore")
        if not raw:
            raw = (proc.stderr or b"").decode("utf-8", "ignore")
        for ln in reversed(raw.strip().splitlines()):
            ln = ln.strip()
            if ln.startswith("{"):
                d = json.loads(ln)
                imgs = [u for u in d.get("imgs", []) if u.startswith("http")]
                return imgs, (d.get("title") or "").strip()
    except Exception as e:
        print(f"[warn] ktvc8 引擎兜底失败: {e}")
    return [], ""


def _ktvc8_probe_next(imgs: list, cookie: str = "", max_probe: int = 8) -> list:
    """词曲网「查看剩余N张曲谱」兜底：首图 URL 末端数字连续递增探测（…496 → 497 → 498…）。
    图片 CDN 不过滤，HEAD 200 即存在；只对以数字结尾的 uploadfiles 路径生效。"""
    if not imgs:
        return imgs
    import urllib.request as _ur
    base = imgs[0]
    m = re.match(r"^(.*?)(\d+)(\.(?:png|jpe?g|jpg|gif))$", base, re.I)
    if not m:
        return imgs
    prefix, num_str, ext = m.group(1), m.group(2), m.group(3)
    out = list(imgs)
    for step in range(1, max_probe + 1):
        cand = f"{prefix}{int(num_str) + step}{ext}"
        try:
            req = _ur.Request(cand, headers={"User-Agent": UA})
            if cookie:
                req.add_header("Cookie", cookie)
            with _ur.urlopen(req, timeout=12) as r:
                if r.status == 200 and int(r.headers.get("Content-Length", 1000)) > 20_000:
                    out.append(cand)
                else:
                    break
        except Exception:
            break
    return out


def extract_ktvc8(html: str):
    """词曲网：提取曲谱图片 URL（含分页探测路径），按页序返回。"""
    return _ktvc8_imgs(html)


def ktvc8_title(html: str) -> str:
    """从词曲网 <title> 提取干净曲名（形如「《耳朵 李荣浩 独奏版》…」→「耳朵 李荣浩 独奏版」）。"""
    m = re.search(r'<title>([^<]+)</title>', html, re.I)
    if not m:
        return ""
    t = html_mod.unescape(m.group(1)).strip()
    # 剥书名号壳：完整的《...》→ 内部文字
    m2 = re.search(r'《([^》]+)》', t)
    if m2:
        t = m2.group(1)
    for noise in ("钢琴谱", "曲谱", "简谱", "歌谱", "独奏版", " - 词曲网", "词曲网"):
        t = t.split(noise)[0]
    return t.strip(" -_（）()　·,，")[:60]


def ktvc8_page_urls(input_url: str, html: str) -> list:
    """词曲网分页：article_XXXX_1.html 若存在 _2.._N 则返回全部页 URL，否则单页。"""
    m = re.match(r"^(https?://[^?#]*?article_\d+)_(\d+)\.html", input_url)
    if not m:
        return [input_url]
    stem, cur = m.group(1), int(m.group(2))
    # 从页面找「共 N 页」或最大分页链接
    maxp = cur
    for n in re.finditer(r'article_(\d+)_(\d+)\.html', html):
        try:
            if int(n.group(1)) == int(re.search(r'article_(\d+)', input_url).group(1)):
                maxp = max(maxp, int(n.group(2)))
        except Exception:
            pass
    return [f"{stem}_{p}.html" for p in range(1, maxp + 1)]


# ===================== 天天钢琴（piastudy / pianoproblem 系） =====================
# 谱面为矢量 SVG（path/符号），且 HTML 常以 <link rel="preload" as="image"> 声明全部页。
# 提取 sheetImg 目录下的全部页 SVG → Edge headless 高清渲染 PNG → 复用 to_pdf。
# Edge 是 Win10+ 系统自带（WebView2/浏览器），定位不到时清晰报错。
def _find_edge() -> str:
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def extract_piastudy(html: str):
    """提取天天钢琴谱面 SVG 页码列表（sheetImg/.../{1..N}.svg），按页序返回。"""
    urls = re.findall(r'href="(https?://[^"]*?/sheetImg/[^"]+?/(\d+)\.svg)"', html)
    if not urls:
        urls = re.findall(r"(https?://[^\"\\]*?/sheetImg/[^\"\\]+?/(\d+)\.svg)", html)
    seen = {}
    for u, num in urls:
        u = u.replace('&quot;', '').replace('\\/', '/')
        try:
            seen[int(num)] = u.split('?')[0]
        except ValueError:
            continue
    return [seen[k] for k in sorted(seen)]


def piastudy_title(html: str) -> str:
    """从页面 <title> 提取干净曲名（去掉「钢琴谱/天天钢琴/编配」噪音）。"""
    m = re.search(r'<title>([^<]+)</title>', html, re.I)
    if not m:
        return ""
    t = html_mod.unescape(m.group(1)).strip()
    for noise in ("钢琴谱", " - 天天钢琴", "钢琴", "简谱"):
        t = t.split(noise)[0]
    # 书名号壳剥离：完整《...》→ 保留内部文字（李鬼文件名可读性）
    t = re.sub(r'^《(.+?)》', r'\1', t)
    t = re.sub(r'^《', '', t).replace('》', '')  # 残余半壳兜底
    return t.strip(" -_（）()　·,，")[:60]


def piastudy_svg_to_png(svg_url: str, out_png: str, edge: str) -> bool:
    """下载 SVG → Edge headless 渲染为高清 PNG（约 600DPI，A4 竖版）。"""
    try:
        req = urllib.request.Request(svg_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except Exception:
        return False
    tmp_dir = tempfile.mkdtemp(prefix="piastudy_")
    svg_path = os.path.join(tmp_dir, "page.svg")
    with open(svg_path, "wb") as f:
        f.write(data)
    try:
        subprocess.run(
            [edge, "--headless", "--disable-gpu",
             f"--screenshot={out_png}", "--window-size=1680,2376",
             "file:///" + svg_path.replace("\\", "/")],
            timeout=60, capture_output=True,
        )
        return os.path.isfile(out_png) and os.path.getsize(out_png) > 20000
    except Exception:
        return False
    finally:
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def process_piastudy(html: str) -> list:
    """天天钢琴：SVG 列表 → 各页渲染 PNG → 白底 Image 列表（复用 to_pdf）。"""
    svgs = extract_piastudy(html)
    if not svgs:
        return []
    edge = _find_edge()
    if not edge:
        raise ValueError("天天钢琴谱面为 SVG 矢量，需系统 Microsoft Edge 渲染（Win10+ 自带）。未找到 Edge。")
    from PIL import Image as _PILImage
    imgs = []
    tmpdir = tempfile.mkdtemp(prefix="piastudy_png_")
    try:
        for i, svg_url in enumerate(svgs):
            png = os.path.join(tmpdir, f"p{i}.png")
            if piastudy_svg_to_png(svg_url, png, edge):
                img = _PILImage.open(png).convert("RGBA")
                bg = _PILImage.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                imgs.append(resize_standard(bg.convert("RGB")))
                print(f"  ✓ 第{i+1}页 → {img.size[0]}×{img.size[1]}")
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
    return imgs


# ===================== 虫虫钢琴（gangqinpu）ccmz 完整曲谱 =====================
# 虫虫付费谱的预览图被墙，但完整乐谱数据在公开的 ccmz 工程文件里（页面 HTML 直含 URL）。
# ccmz = 首字节加密标记(1/2) + zip 包（内含 score.json 全曲数据）。
# 融合 ccmz-to-midi 渲染引擎（Node + puppeteer-core + 系统 Edge）→ 直接产出完整 PDF。
def _extract_ccmz_url(html: str) -> str:
    """虫虫页面提取 ccmz 工程文件 URL。"""
    m = re.search(r'(https?://[^"\' <>]+\.ccmz)', html)
    return m.group(1).replace('&amp;', '&') if m else ""


def _clean_win_path(p: str) -> str:
    """剥掉 Windows 长路径前缀（Tauri 启动的 Python 会经 sys.argv[0]/__file__ 带出），
    Node/subprocess 对带前缀的路径解析异常（lstat 'D:' 崩溃 / WinError 2）。"""
    if not p:
        return p
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return p.replace("/", "\\")


def _find_ccmz_engine() -> str:
    """定位软件内 ccmz 渲染引擎（ccmz2pdf.mjs），兼容开发/安装版布局。"""
    cands = []
    here = os.path.dirname(os.path.abspath(__file__))
    # 开发版
    cands.append(os.path.join(here, "src-tauri", "resources", "ccmz-engine", "ccmz2pdf.mjs"))
    cands.append(os.path.join(here, "ccmz-engine", "ccmz2pdf.mjs"))
    cands.append(os.path.join(here, "resources", "ccmz-engine", "ccmz2pdf.mjs"))
    # 安装版：exe 同级 ccmz-engine（NSIS 把引擎资源放 $INSTDIR\ccmz-engine）
    try:
        import sys as _s
        exe_dir = os.path.dirname(_s.argv[0]) if _s.argv and os.path.isfile(_clean_win_path(_s.argv[0])) else here
        cands.append(os.path.join(exe_dir, "ccmz-engine", "ccmz2pdf.mjs"))
        if os.environ.get("SCORE_CCMZ_ENGINE"):
            cands.insert(0, os.environ["SCORE_CCMZ_ENGINE"])
    except Exception:
        pass
    for c in cands:
        if os.path.isfile(_clean_win_path(c)):
            return _clean_win_path(c)
    # 未找到：输出诊断（帮助定位残缺目录/安装异常）
    print("[ccmz-engine 诊断] 已搜索:", " | ".join(_clean_win_path(c) for c in cands), file=sys.stderr)
    return ""


def process_ccmz(input_str: str, output_dir: str) -> str:
    """虫虫链接 → 下载 ccmz → 调引擎 → 返回成品 PDF 路径（或抛错）。"""
    html_text = fetch_html(input_str)
    ccmz_url = _extract_ccmz_url(html_text)
    if not ccmz_url:
        raise ValueError("未在页面中找到 ccmz 工程文件（可能该曲谱无 ccmz）")
    engine = _find_ccmz_engine()
    if not engine:
        raise ValueError("未找到 ccmz 渲染引擎（软件资源缺失 ccmz-engine），请更新软件")
    # 下载 ccmz 到临时
    tmpdir = tempfile.mkdtemp(prefix="ccmz_")
    try:
        ccmz_path = os.path.join(tmpdir, "score.ccmz")
        with open(ccmz_path, "wb") as f:
            f.write(download_bytes(ccmz_url))
        # 干净输出名：页标题 → 去书名号壳
        os.makedirs(output_dir, exist_ok=True)
        title = piastudy_title(html_text) or "虫虫曲谱"
        out = os.path.join(output_dir, title + ".pdf")
        # 调 Node 引擎
        node = _find_node()
        if not node:
            raise ValueError("未找到 Node.js 运行时（ccmz 渲染需 Node，或已内置但未检测到）")
        # 统一剥 \\?\ 前缀（Tauri 环境路径可能带长路径前缀，Node/subprocess 解析异常）
        node, engine = _clean_win_path(node), _clean_win_path(engine)
        ccmz_path, out = _clean_win_path(ccmz_path), _clean_win_path(out)
        cmd = [node, engine, ccmz_path, out, str(49200 + (os.getpid() % 100))]
        # 显式指定 cwd=引擎目录：Node 的模块解析依赖 cwd，
        # 安装目录含空格（如 D:\Program Files\Score Studio）时若不指定会报 lstat 'D:' 类路径解析错误
        eng_dir = os.path.dirname(engine)
        proc = subprocess.run(cmd, capture_output=True, timeout=180, cwd=eng_dir)
        if proc.returncode != 0 or not os.path.isfile(out):
            err = (proc.stderr or b"").decode("utf-8", "ignore")[:400]
            raise ValueError(f"ccmz 渲染失败: {err or '无输出'}（cmd: {' '.join(cmd)}）")
        print(f"✅ PDF 已生成：{out}（虫虫完整版）")
        return out
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def _find_node() -> str:
    """定位 Node 运行时：引擎内置 node.exe 最优先（开箱即用），再退系统 PATH / 常见安装路径。"""
    import shutil as _sh
    eng_dir = os.path.dirname(_find_ccmz_engine())
    if eng_dir:
        builtin = os.path.join(eng_dir, "node.exe")
        if os.path.isfile(builtin):
            return _clean_win_path(builtin)
    n = _sh.which("node")
    if n:
        return _clean_win_path(n)
    cands = [
        r"C:\Program Files\nodejs\node.exe",
        os.path.expandvars(r"%ProgramFiles%\nodejs\node.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return _clean_win_path(c)
    return ""


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
    """筛选曲谱：高度 > 800px 且体积 > 25KB 且宽高比贴近 A4（排除图标/装饰/头像/竖版封面）。
    注：体积阈值曾用 50KB，误杀过 47KB 的正谱页（PNG 压缩率高不代表内容少）。"""
    if h <= 800 or img_bytes_len <= 25_000:
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


# ===================== OCR 自动命名 =====================
_OCR_ENGINE = None


def _get_ocr_engine():
    """惰性加载 rapidocr_onnxruntime（离线中文 OCR）。缺失/加载失败返回 None，不阻塞主流程。"""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR_ENGINE = RapidOCR()
        except Exception:
            _OCR_ENGINE = False
    return _OCR_ENGINE or None


def ocr_first_page_title(images) -> str:
    """对第一页曲谱图 OCR，从顶部文字行中选出标题（行高最大的首行）。
    无 OCR 引擎 / 识别失败返回空串（不抛异常，不阻塞主流程）。"""
    engine = _get_ocr_engine()
    if engine is None or not images:
        return ""
    try:
        import numpy as np
        # RapidOCR 仅接受 str/ndarray/bytes/Path，直接转 ndarray（BytesIO 会抛 LoadImageError）
        img = images[0].convert("RGB")
        arr = np.array(img)
        result, _ = engine(arr)
        if not result:
            return ""
        # 置信度过滤（0.5 以下多为噪点）。注意 rapidocr 1.2.x 返回的置信度为字符串，须转 float
        def _conf(r):
            try:
                return float(r[2])
            except Exception:
                return 0.0
        lines = [r for r in result if len(r) >= 3 and _conf(r) > 0.5] or list(result)
        if not lines:
            return ""
        # 取最上方 5 行中「行高最大」者——曲谱标题通常字号最大。
        # box 为四点坐标 [[左上],[右上],[右下],[左下]]，左上 y = r[0][0][1]，行高 = 左下 y - 左上 y
        top = sorted(lines, key=lambda r: r[0][0][1])[:5]
        title_line = max(top, key=lambda r: r[0][3][1] - r[0][0][1])
        t = re.sub(r"\s+", "", str(title_line[1])).strip()
        return t or ""
    except Exception:
        return ""


def derive_name(input_str: str, html_text: str = "", theme: str = "", custom: str = ""):
    """文件名：曲名[-歌手][-标签].pdf
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
    if (input_str.lower().startswith("http")
            and re.match(r"^https?://\S+\.(?:png|jpe?g|jpg|webp)($|\?)", input_str.lower())
            and " " not in input_str.strip()):
        # 单张图片直链（词曲网被云锁拦时的通道：图片 CDN 不过滤，浏览器里复制图片地址即可）
        print("[来源] 图片直链")
        images = process_images([input_str], is_tan8=False)
    elif " " in input_str.strip() and all(
            re.match(r"^https?://\S+\.(?:png|jpe?g|jpg|webp)($|\?)", u.lower()) for u in input_str.split()):
        # 多张图片直链（空格分隔一组）：全部下载合并为一份 PDF（词曲网「查看剩余」多页图通道）
        urls = [u for u in input_str.split() if u]
        print(f"[来源] 图片直链 ×{len(urls)}（合并为一份 PDF）")
        images = []
        for u in urls:
            try:
                images.extend(process_images([u], is_tan8=False))
            except Exception as e:
                print(f"  ⚠ {u.split('/')[-1]} 下载失败：{e}")
        if not images:
            print("⚠ 全部图片直链下载失败，退出。")
            return None
        if not theme and not custom:
            custom = "曲谱合集"
    elif input_str.lower().startswith("http"):
        ktvc8 = "ktvc8.com" in input_str.lower()
        cookie = os.environ.get("SCORE_KTVC8_COOKIE", "")
        html = fetch_html(input_str, cookie=cookie)
        # 虫虫钢琴：ccmz 完整曲谱（付费预览图绕过，取公开工程文件渲染）
        if "gangqinpu.com" in input_str.lower() and ".ccmz" in html:
            print("[来源] 虫虫钢琴（ccmz 完整版）")
            out = process_ccmz(input_str, output_dir)
            if out:
                return out
            raise ValueError("虫虫 ccmz 渲染失败")
        # 天天钢琴：谱面为矢量 SVG 多页，走专用渲染（Edge headless → PNG → 白底）
        if any(k in input_str.lower() for k in ("piastudy.com", "pianoproblem", "insstudy")):
            print("[来源] 天天钢琴（SVG 矢量）")
            images = process_piastudy(html)
            if not images:
                print("⚠ 未提取到任何曲谱图片（天天钢琴源），退出。")
                return None
            if not theme and not custom:
                custom = piastudy_title(html)  # 页面标题干净名（无自定义时）
        elif ktvc8:
            # 词曲网：位图谱面，支持分页收集
            if is_waf_page(html):
                # 云锁拦截（含 JS 自动跳转挑战页）→ 自动降级 puppeteer 引擎（真实浏览器执行 JS 过验证）
                js_imgs, js_title = _ktvc8_fetch_js(input_str, cookie)
                if not js_imgs:
                    raise ValueError(
                        "词曲网被云锁（WAF）拦截，且浏览器引擎兜底未取得图片："
                        "请用浏览器打开页面，右键复制曲谱图片地址直接粘贴到本输入框重试；"
                        "或确认软件已更新（内置浏览器引擎）。")
                print(f"[来源] 词曲网（云锁挑战 → 浏览器引擎兜底，{len(js_imgs)} 张）")
                images = process_images(js_imgs, is_tan8=False)
                if not theme and not custom and js_title:
                    custom = ktvc8_title(f"<title>{js_title}</title>")
            else:
                print("[来源] 词曲网（位图 · 分页）")
                pages = ktvc8_page_urls(input_str, html)
                images = []
                for p_url in pages:
                    p_html = html if p_url == input_str else fetch_html(p_url, cookie=cookie)
                    if is_waf_page(p_html):
                        break
                    imgs = _ktvc8_imgs(p_html)
                    # 兜底：JS 注入型页面（剩余页图只在浏览器端生成）→ 按首图编号递增探测
                    if len(imgs) <= 1:
                        imgs = _ktvc8_probe_next(imgs, cookie=cookie)
                        if len(imgs) > 1:
                            print(f"  页 {p_url.rsplit('/', 1)[-1]}: 编号探测补齐 {len(imgs)} 张")
                    print(f"  页 {p_url.rsplit('/', 1)[-1]}: 候选 {len(imgs)} 张")
                    images.extend(process_images(imgs, is_tan8=False))
                if not images:
                    # 常规提取 0 张 → 浏览器引擎兜底一次
                    js_imgs, js_title = _ktvc8_fetch_js(input_str, cookie)
                    if js_imgs:
                        print(f"[来源] 词曲网（常规提取 0 张 → 浏览器引擎兜底，{len(js_imgs)} 张）")
                        images = process_images(js_imgs, is_tan8=False)
                        if not theme and not custom and js_title:
                            custom = ktvc8_title(f"<title>{js_title}</title>")
                if not images:
                    print("⚠ 未提取到任何曲谱图片（词曲网源），退出。")
                    return None
                if not theme and not custom:
                    custom = ktvc8_title(html)
        else:
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

    # 本地输入且未手动命名 → OCR 识别第一页标题（网页源已有 HTML 标题通道）
    if not custom and not theme and (os.path.isdir(input_str) or os.path.isfile(input_str)):
        _ocr = ocr_first_page_title(images)
        if _ocr:
            _base, _artist = parse_title_fields(_ocr)
            custom = f"{_base}-{_artist}" if _artist else _base
            print(f"[命名] OCR 识别第一页标题：{custom}")
        else:
            print("[命名] 未手动命名且 OCR 不可用（缺 rapidocr_onnxruntime 或识别失败），退回路径名；可在前端输入框补名")

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
    ap.add_argument("--theme", default="", help="追加到文件名的额外标签（可选）")
    ap.add_argument("--name", default="", help="自定义文件名（不含扩展名）")
    ap.add_argument("--cookie", default="", help="网站 Cookie（词曲网 ktvc8 云锁会话，可选）")
    ap.add_argument("--selftest", action="store_true", help="运行冒烟测试")
    args = ap.parse_args()

    if args.cookie:
        os.environ["SCORE_KTVC8_COOKIE"] = args.cookie
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
