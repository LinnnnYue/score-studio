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
import base64 as _b64
import gzip as _gz
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
from xml.sax.saxutils import escape

# 嵌入式 Python（python313._pth）里 "." 指向解释器目录而非脚本目录，
# 导致同级的 library_ops.py（元数据写入）永远 import 不到。
# 显式把脚本所在目录加入 sys.path[0]，保证安装版/便携版都能 import 到同级模块。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

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

# 从整段分享文本里抽链接：手机 App 的「分享」会带上中文前缀与后缀，
# 形如「分享曲谱-萧敬腾、HOYO-MiX·天生鬼才 https://h5.kugou.com/... 」。
# 中文与全角标点一律视为 URL 的终止符。
_URL_IN_TEXT = re.compile(
    r'https?://[^\s\u4e00-\u9fff\u3000-\u303f\uff01-\uff5e<>"\'）】》]+')


def extract_url(text: str) -> str:
    """整段文本 → 第一个 http(s) 链接。无链接时原样返回（兼容本地路径与纯链接）。"""
    if not text:
        return text
    m = _URL_IN_TEXT.search(text)
    return m.group(0).rstrip('.,;') if m else text.strip()


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
                    raw = r.read()
                    # 处理 gzip 压缩（urllib 不自动解压 Content-Encoding: gzip）
                    if r.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = _gz.decompress(raw)
                    return _decode_html(raw, r.headers.get("Content-Type", ""))
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
    """微信公众号：提取 mmbiz.qpic.cn 图片 URL（PNG/JPEG 均匹配），去 query。
    兼容两种形态：
      ① 老式：https://mmbiz.qpic.cn/xxx?wx_fmt=png（带 query 参数）
      ② 新式：https://mmbiz.qpic.cn/mmbiz_jpg/xxx/640（形如 mmbiz_jpg/mmbiz_png/mmbiz_gif，无 query）
    排除 gif 动图与 JS 模板（src 为 .concat 拼接的模板行）。"""
    out = []
    for u in re.findall(r'data-src="(https?://[^"]*?)"', html):
        u = u.split("?")[0]
        if "mmbiz" not in u:
            continue
        # 模板行（'.concat(...)'）无真实 URL，跳过
        if u.startswith("'") or ".concat" in u or "')" in u:
            continue
        # 新式：mmbiz_jpg/mmbiz_png/mmbiz_bmp/mmbiz_webp（去掉 gif 动图）
        if re.search(r"mmbiz_(?:jpg|jpeg|png|bmp|webp|wjpeg)/", u, re.I):
            if u not in out:
                out.append(u)
    return out


def extract_tan8(html: str):
    """弹琴吧：提取隐藏的高清标准版曲谱 URL（*_standard/ 目录）。
    页面把图片 URL 放在 JS 数组（yuepuArrXian 五线谱 / yuepuArrJian 简谱）里，
    JSON 序列化后斜杠带 \\/ 转义（https:\\/\\/oss.tan8.com\\/...），两种形态都要匹配。

    格式变迁（2026-08 实测）：
      老：.../115372_xxx_standard/115372_xxx.ypad.0.png（文件名含 .ypad.）
      新：.../115372_ejjadhjd_standard/prev_115372.0.png（无 .ypad，前缀 prev_）
      另有 _jianpu 目录（简谱），排除。
    → 统一按「_standard 目录 + .png 结尾」匹配，文件名形态不限。"""
    urls = []
    pats = [
        # 主通道：新/老格式通用（_standard 目录下的 png）
        r'https?:\\?/\\?/oss\.tan8\.com\\?/yuepuku\\?/\d+\\?/\d+\\?/\d+_\w+_standard\\?/[^\s"\'<>\\]+\.png',
        # 兜底：老格式（文件名含 .ypad.）
        r'https?:\\?/\\?/oss\.tan8\.com\\?/yuepuku\\?/\d+\\?/\d+\\?/\d+_\w+_standard\\?/\d+_\w+\.ypad\.\d+\.png',
    ]
    for p in pats:
        for u in re.findall(p, html):
            u = u.replace("\\/", "/")
            # 修正重复斜杠（jianpu 目录后可能出现 //）
            u = re.sub(r'(?<!:)//+', '/', u)
            if u not in urls:
                urls.append(u)
        if urls:
            break
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


def _get_captcha_ocr():
    """惰性加载验证码专用 OCR（ddddocr 优先，rapidocr 兜底）。
    ddddocr 专为验证码训练（数字识别率高），但打包体积 +20MB；
    缺失时退回 rapidocr（通用 OCR，对细笔画验证码可能识别失败）。"""
    global _CAPTCHA_OCR
    if _CAPTCHA_OCR is None:
        try:
            import ddddocr
            _CAPTCHA_OCR = ("dddd", ddddocr.DdddOcr(show_ad=False))
        except Exception:
            _CAPTCHA_OCR = ("rapid", _get_ocr_engine())
    return _CAPTCHA_OCR


def _ocr_guess_multi(kind, ocr, b64_bytes):
    """验证码 OCR → 候选答案列表。ddddocr 吃原始字节；rapidocr 需要 ndarray。"""
    import re as _re
    if kind == "dddd":
        try:
            t = str(ocr.classification(b64_bytes)).strip()
            return [t] if len(t) >= 3 else []
        except Exception:
            return []
    try:
        import numpy as np
        from PIL import Image
        img = Image.open(io.BytesIO(b64_bytes)).convert("L")
        arr = np.array(img)
        result, _ = ocr(arr)
        if not result:
            return []
        def _conf(r):
            try:
                return float(r[2])
            except Exception:
                return 0.0
        boxes = [(r[0], str(r[1]), _conf(r)) for r in result if len(r) >= 3]
        boxes.sort(key=lambda b: b[0][0][0])
        text = _re.sub(r"\s+", "", "".join(b[1] for b in boxes)).strip()
        return [text] if len(text) >= 3 else []
    except Exception:
        return []


def _ktvc8_solve_waf(url: str, cookie: str = "", manual_ans: str = "") -> tuple:
    """词曲网云锁 WAF 自动验证：OCR 识别 base64 验证码 → 十六进制编码 → 提交取真实页面。
    通道优先级：
      1) manual_ans 非空（前端弹窗用户手输）→ 用全新会话取验证码图，再用同一会话提交；
      2) ddddocr 自动识别 → rapidocr 兜底 → 失败换新验证码重试（最多 4 轮）；
      3) 全部失败 → 返回 ("__CAPTCHA_REQUIRED__:<path>", "")，前端弹窗让用户手输。
    关键：验证码图与提交必须同一 CookieJar 会话（跨会话提交会失效）。
    返回 (real_html, session_cookie)；彻底失败返回 ("", "")。"""
    import gzip as _gz
    import http.cookiejar as _cj
    try:
        from PIL import Image
    except Exception:
        Image = None

    def _mk_jar():
        jar = _cj.CookieJar()
        ctx = __import__("ssl").create_default_context()
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(jar))
        headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                   "Accept-Encoding": "gzip, deflate"}
        if cookie:
            headers["Cookie"] = normalize_cookie(cookie)
        return jar, opener, headers

    # 单会话内：取 WAF 页 → 候选答案（guesses_source 回调）→ 提交。成功返回 (html, cookie, None)
    def _attempt_round(guesses_source):
        jar, opener, headers = _mk_jar()
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=30) as r:
                waf_data = r.read()
                if r.headers.get("Content-Encoding", "").lower() == "gzip":
                    waf_data = _gz.decompress(waf_data)
            waf_html = waf_data.decode("utf-8", errors="ignore")
        except Exception:
            return (None, None, None)
        if not is_waf_page(waf_html):
            session_cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
            return (waf_html, session_cookie, None)
        m = re.search(r'data:image/bmp;base64,([^"\s]+)', waf_html)
        if not m:
            return (None, None, None)
        b64_str = m.group(1) + "=" * ((4 - len(m.group(1)) % 4) % 4)
        captcha_bytes = _b64.b64decode(b64_str)
        guesses = guesses_source(captcha_bytes, jar, opener, headers)
        # 提交循环（同一 jar）
        for text in guesses:
            hex_text = "".join(f"{ord(c):x}" for c in text)
            sep = "&" if "?" in url else "?"
            verify_url = f"{url}{sep}security_verify_img={hex_text}"
            srcurl_hex = "".join(f"{ord(c):x}" for c in url)
            jar.set_cookie(_cj.Cookie(
                version=0, name='srcurl', value=srcurl_hex,
                port=None, port_specified=False,
                domain='.ktvc8.com', domain_specified=True, domain_initial_dot=True,
                path='/', path_specified=True, secure=True,
                expires=None, discard=True, comment=None, comment_url=None, rest={}, rfc2109=False))
            try:
                req2 = urllib.request.Request(verify_url, headers=headers)
                with opener.open(req2, timeout=30) as r:
                    real_data = r.read()
                    if r.headers.get("Content-Encoding", "").lower() == "gzip":
                        real_data = _gz.decompress(real_data)
                real_html = real_data.decode("utf-8", errors="ignore")
            except Exception:
                continue
            redirect_m = re.search(r'self\.location\s*=\s*["\']([^"\']+)["\']', real_html)
            if redirect_m:
                redirect_url = redirect_m.group(1)
                if redirect_url.startswith("/"):
                    from urllib.parse import urlparse as _up
                    parsed = _up(url)
                    redirect_url = f"{parsed.scheme}://{parsed.netloc}{redirect_url}"
                try:
                    req3 = urllib.request.Request(redirect_url, headers=headers)
                    with opener.open(req3, timeout=30) as r:
                        final_data = r.read()
                        if r.headers.get("Content-Encoding", "").lower() == "gzip":
                            final_data = _gz.decompress(final_data)
                        real_html = final_data.decode("utf-8", errors="ignore")
                except Exception:
                    pass
            if not is_waf_page(real_html):
                session_cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
                print(f"[WAF] 验证码提交成功（{text} → {hex_text}）")
                return (real_html, session_cookie, None)
        # 未破 → 返回验证码字节（供保存弹窗）
        return (None, None, captcha_bytes)

    # ③ @ask 模式：单会话取图 → 打印 __CAPTCHA_SHOW__ 标记 → 等前端写答案文件 → 同会话提交
    if manual_ans == "@ask":
        answer_file = os.path.join(tempfile.gettempdir(), "score_ktvc8_answer.txt")
        try:
            if os.path.exists(answer_file):
                os.remove(answer_file)
        except Exception:
            pass

        def _ask(cap_bytes, jar, opener, headers):
            try:
                cap_path = os.path.join(tempfile.gettempdir(), "score_ktvc8_captcha.png")
                if Image is not None:
                    Image.open(io.BytesIO(cap_bytes)).convert("RGB").save(cap_path)
                else:
                    with open(cap_path, "wb") as f:
                        f.write(cap_bytes)
            except Exception as e:
                print(f"[warn] 验证码保存失败: {e}")
                return []
            # 通知前端弹窗（stdout 会被 Tauri 捕获为 log/error）
            print(f"__KTVC8_CAPTCHA_SHOW__:{cap_path}", flush=True)
            deadline = time.time() + 180
            while time.time() < deadline:
                if os.path.exists(answer_file):
                    try:
                        with open(answer_file, "r", encoding="utf-8") as f:
                            t = f.read().strip()
                        if t:
                            if t == "__CANCEL__":
                                return []
                            return [t]
                    except Exception:
                        pass
                time.sleep(0.5)
            return []
        out = _attempt_round(_ask)
        if out and out[0]:
            return (out[0], out[1])
        return "", ""

    # ① 自动模式（无手动答案）
    if not manual_ans:
        last_cap = None
        for _ in range(4):
            def _auto(cap_bytes, jar, opener, headers):
                kind, ocr = _get_captcha_ocr()
                if ocr is None:
                    return []
                return _ocr_guess_multi(kind, ocr, cap_bytes)
            out = _attempt_round(_auto)
            if out and out[0]:
                return (out[0], out[1])
            if out and out[2]:
                last_cap = out[2]
        cap_bytes = last_cap
    else:
        # ② 手动答案已给出（前端弹窗后重跑）：单会话取图 → 用答案提交
        ans = manual_ans.strip()

        def _manual(cap_bytes, jar, opener, headers):
            return [ans] if len(ans) >= 3 else []
        out = _attempt_round(_manual)
        if out and out[0]:
            return (out[0], out[1])
        cap_bytes = out[2] if out else None
    if cap_bytes is None:
        return "", ""
    try:
        tmp_dir = tempfile.gettempdir()
        cap_path = os.path.join(tmp_dir, "score_ktvc8_captcha.png")
        if Image is not None:
            Image.open(io.BytesIO(cap_bytes)).convert("RGB").save(cap_path)
        else:
            with open(cap_path, "wb") as f:
                f.write(cap_bytes)
        print(f"[WAF] 验证码自动识别失败，已保存到 {cap_path}")
        return (f"__CAPTCHA_REQUIRED__:{cap_path}", "")
    except Exception as e:
        print(f"[warn] 验证码保存失败: {e}")
    return "", ""


def _ktvc8_showvisit_imgs(page_html: str, cookie: str = "") -> list:
    """词曲网新结构（2026-09 起）：谱图不再写进 HTML，而是由页面内联脚本
    `show_neirong(yid)` 动态注入；真正的图片 URL 藏在
    `/showvisitjs.asp?ID=<yid>&uID=<uid>` 返回的 JS 里（同页 <script src> 引用的那个）。

    该子资源受 `/plcms.asp` 滑动验证保护——但验证形同虚设：滑块脚本在拖到底后只发一个
    `GET /plcms.asp?action=verify_pass&t=<ms>`，服务端即回 ok 并下发
    SITE_VERIFY_PASSED cookie。故纯 HTTP 直调即可，无需浏览器。

    返回图片绝对 URL 列表（按页序）；不适用/失败返回 []。
    """
    import http.cookiejar as _cj
    m = re.search(r'showvisitjs\.asp\?ID=(\d+)(?:&(?:amp;)?uID=(\d+))?', page_html)
    if not m:
        return []
    yid, uid = m.group(1), m.group(2) or "0"
    base = "https://www.ktvc8.com"
    try:
        jar = _cj.CookieJar()
        ctx = __import__("ssl").create_default_context()
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(jar))

        def _get(u, referer):
            h = {"User-Agent": UA, "Referer": referer, "Accept": "*/*"}
            if cookie:
                h["Cookie"] = normalize_cookie(cookie)
            with opener.open(urllib.request.Request(u, headers=h), timeout=25) as r:
                return r.read()

        # 过滑动验证（直调 verify_pass，服务端只认这个参数）
        _get(f"{base}/plcms.asp?action=verify_pass&t={int(time.time() * 1000)}",
             referer=f"{base}/article/article_{yid}_1.html")
        js_raw = _get(f"{base}/showvisitjs.asp?ID={yid}&uID={uid}",
                      referer=f"{base}/article/article_{yid}_1.html")
        js = js_raw.decode("gb18030", "ignore")
        # 谱图是 ../uploadfiles/... 相对路径；排除站内图标（../images/*.png）
        rels = re.findall(r"\.\./(?!images/)[^\"'\\\s]+?\.(?:png|jpg|jpeg)", js, re.I)
        out = []
        for r0 in rels:
            full = f"{base}/{r0.replace('../', '').lstrip('/')}"
            if full not in out:
                out.append(full)
        return out
    except Exception as e:
        print(f"[warn] 词曲网 showvisitjs 通道失败: {e}")
        return []


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
    """从词曲网 <title> 提取干净曲名（形如「《耳朵 李荣浩 独奏版》…」→「耳朵 李荣浩 独奏版」）。
    若书名号内只有曲名、歌手写在号外（如「《归来兮》钢琴谱 - 庆庆演唱」，新结构常见），
    则补成「曲名-歌手」。"""
    m = re.search(r'<title>([^<]+)</title>', html, re.I)
    if not m:
        return ""
    raw = html_mod.unescape(m.group(1)).strip()
    t = raw
    # 剥书名号壳：完整的《...》→ 内部文字
    m2 = re.search(r'《([^》]+)》', t)
    if m2:
        t = m2.group(1)
    for noise in ("钢琴谱", "曲谱", "简谱", "歌谱", "独奏版", " - 词曲网", "词曲网"):
        t = t.split(noise)[0]
    t = t.strip(" -_（）()　·,，")[:60]
    # 号外歌手补全：书名号内无空格（未带歌手）时，从 title 的「- XXX演唱」取歌手
    if t and " " not in t:
        ma = re.search(r'[-–—]\s*([^\s\-–—<《》]+?)\s*演唱', raw)
        if ma and ma.group(1):
            t = f"{t}-{ma.group(1)}"
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


# ---------- ccmz → MusicXML（纯 Python，无外部依赖）----------
# 移植自 ccmz-score-convert/scripts/ccmz2mxl.py。要点：
#   * 同 tick 多 elems → 和弦（<chord/>）
#   * 同 staff 多 voice → 顺序输出，之间以 <backup> 复位
#   * 时值以相邻 tick 实差为准（正确覆盖三连音）
#   * note 子元素严格按 MusicXML DTD 顺序（staff 必须在 notations 之前，否则 MuseScore 段错误）
CCMZ_PB = 480
_CCMZ_STEP = {1: 'C', 2: 'D', 3: 'E', 4: 'F', 5: 'G', 6: 'A', 7: 'B'}
_CCMZ_TYPE = {1: 'whole', 2: 'half', 4: 'quarter', 8: 'eighth',
              16: '16th', 32: '32nd', 64: '64th'}


def _ccmz_zip(path: str):
    """ccmz → zipfile：首字节为版本标记（2 = 载荷逐字节 XOR 1）。"""
    import io as _io
    import zipfile as _zip
    raw = open(path, 'rb').read()
    if not raw:
        raise ValueError("ccmz 文件为空")
    payload = bytes(x ^ 1 for x in raw[1:]) if raw[0] == 2 else raw[1:]
    if payload[:2] != b'PK':
        raise ValueError(f"ccmz 解码后不是 ZIP（版本标记={raw[0]}）")
    return _zip.ZipFile(_io.BytesIO(payload))


def _ccmz_note_ticks(type_, dots) -> int:
    base = 4 * CCMZ_PB // type_
    tot = float(base)
    add = base / 2.0
    for _ in range(dots or 0):
        tot += add
        add /= 2.0
    return int(round(tot))


def _ccmz_ticks_to_typenote(t: int):
    best = None
    for type_ in (1, 2, 4, 8, 16, 32):
        for dots in (0, 1, 2):
            d = abs(_ccmz_note_ticks(type_, dots) - t)
            if best is None or d < best[0]:
                best = (d, type_, dots)
    return best[1], best[2]


def _ccmz_measure_total(m: dict) -> int:
    t = m.get('time') or {'beats': 4, 'beatu': 4}
    return int(CCMZ_PB * 4 * t.get('beats', 4) / t.get('beatu', 4))


def _ccmz_tuplet_of(n: dict):
    for e in (n.get('elems') or []):
        for p in (e.get('pairs') or []):
            if p.get('type') == 'tuplet':
                return p.get('value', 3)
    return None


def _ccmz_elem_lyric(e: dict) -> str:
    """elem 上可能挂的歌词（虫虫多数谱无词，字段名做多形态容错）。"""
    for k in ('lyric', 'word', 'text', 'name'):
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _ccmz_notes_xml(ns, dur, type_, dots, tup, staff_no, voice_no):
    """同 tick 的 note 列表 → 一个和弦的 xml 行"""
    x = []
    elems = []
    for n in ns:
        for e in (n.get('elems') or []):
            elems.append(e)
    for i, e in enumerate(elems):
        x.append('<note>')
        if i > 0:
            x.append('<chord/>')
        step = _CCMZ_STEP.get(e.get('step'), 'C')
        alter = e.get('alter', 0) or 0
        octv = e.get('octave', 4)
        x.append(f'<pitch><step>{step}</step>'
                 + (f'<alter>{alter}</alter>' if alter else '')
                 + f'<octave>{octv}</octave></pitch>')
        x.append(f'<duration>{dur}</duration>')
        pairs = e.get('pairs') or []
        t_start = any(p.get('type') == 'tied' for p in pairs)
        t_stop = e.get('tied') == 'end'
        if t_start:
            x.append('<tie type="start"/>')
        if t_stop:
            x.append('<tie type="stop"/>')
        x.append(f'<voice>{voice_no}</voice>')
        x.append(f'<type>{_CCMZ_TYPE.get(type_, "quarter")}</type>')
        for _ in range(dots or 0):
            x.append('<dot/>')
        acc = e.get('acc')
        if acc:
            an = acc.get('acc') if isinstance(acc, dict) else acc
            anm = {'Sharp': 'sharp', 'Flat': 'flat', 'Natural': 'natural',
                   'DoubleSharp': 'double-sharp', 'DoubleFlat': 'flat-flat'}.get(an)
            if anm:
                x.append(f'<accidental>{anm}</accidental>')
        if tup:
            x.append(f'<time-modification><actual-notes>{tup}</actual-notes>'
                     f'<normal-notes>{tup - 1}</normal-notes></time-modification>')
        x.append(f'<staff>{staff_no}</staff>')
        notx = []
        if t_start:
            notx.append('<tied type="start"/>')
        if t_stop:
            notx.append('<tied type="stop"/>')
        if tup:
            notx.append('<tuplet type="start" number="1"/>')
        if notx:
            x.append('<notations>' + ''.join(notx) + '</notations>')
        # 歌词（MusicXML DTD 顺序里 lyric 位于 notations 之后）
        lyr = _ccmz_elem_lyric(e)
        if lyr:
            x.append('<lyric number="1"><syllabic>single</syllabic>'
                     f'<text>{escape(lyr)}</text></lyric>')
        x.append('</note>')
    return x


def _ccmz_rest_xml(dur, type_, dots, staff_no, voice_no, tup=None):
    x = ['<note><rest/>']
    x.append(f'<duration>{dur}</duration>')
    x.append(f'<voice>{voice_no}</voice>')
    x.append(f'<type>{_CCMZ_TYPE.get(type_, "quarter")}</type>')
    for _ in range(dots or 0):
        x.append('<dot/>')
    if tup:
        x.append(f'<time-modification><actual-notes>{tup}</actual-notes>'
                 f'<normal-notes>{tup - 1}</normal-notes></time-modification>')
    x.append(f'<staff>{staff_no}</staff></note>')
    return x


def _ccmz_render_voice(m, notes, staff_no, voice_no):
    """一个 voice 内的全部音符/休止 → xml（空缺补休止，尾部补齐）"""
    total = _ccmz_measure_total(m)
    groups = []
    for n in sorted(notes, key=lambda x: x.get('tick', 0)):
        t = n.get('tick', 0)
        if groups and groups[-1][0] == t:
            groups[-1][1].append(n)
        else:
            groups.append((t, [n]))
    x = []
    cursor = 0
    for gi, (tick, ns) in enumerate(groups):
        if tick > cursor:
            rt, rd = _ccmz_ticks_to_typenote(tick - cursor)
            x.extend(_ccmz_rest_xml(tick - cursor, rt, rd, staff_no, voice_no))
            cursor = tick
        nxt = groups[gi + 1][0] if gi + 1 < len(groups) else total
        dur = nxt - tick
        if dur <= 0:
            continue
        head = ns[0]
        tup = _ccmz_tuplet_of(head)
        if tup:
            type_, dots = head.get('type', 8), head.get('dots', 0)
        else:
            type_, dots = _ccmz_ticks_to_typenote(dur)
        if 'elems' not in head or 'rest' in head:
            x.extend(_ccmz_rest_xml(dur, type_, dots, staff_no, voice_no, tup))
        else:
            x.extend(_ccmz_notes_xml(ns, dur, type_, dots, tup, staff_no, voice_no))
        cursor = tick + dur
    if cursor < total:
        rt, rd = _ccmz_ticks_to_typenote(total - cursor)
        x.extend(_ccmz_rest_xml(total - cursor, rt, rd, staff_no, voice_no))
    return x


def _ccmz_render_measure(m, out, staff_filter, is_first):
    total = _ccmz_measure_total(m)
    out.append(f'<measure number="{escape(str(m.get("num", "1")))}">')
    need = (is_first or m.get('fifths') is not None
            or m.get('time') is not None or m.get('clefs') is not None)
    if need:
        out.append('<attributes>')
        if is_first:
            out.append(f'<divisions>{CCMZ_PB}</divisions>')
        if is_first or m.get('fifths') is not None:
            f = m.get('fifths')
            f = (f.get('fifths') if isinstance(f, dict) else f) or 0
            out.append(f'<key><fifths>{f}</fifths></key>')
        if is_first or m.get('time') is not None:
            t = m.get('time') or {'beats': 4, 'beatu': 4}
            out.append(f'<time><beats>{t.get("beats", 4)}</beats>'
                       f'<beat-type>{t.get("beatu", 4)}</beat-type></time>')
        if is_first and staff_filter is None:
            out.append('<staves>2</staves>')
        for c in ((m.get('clefs') or []) if is_first else []):
            sn = c.get('staff', 1)
            if staff_filter is not None and sn != staff_filter:
                continue
            sign, line = ('G', 2) if c.get('clef') == 'Treble' else ('F', 4)
            ono = 1 if staff_filter is not None else sn
            out.append(f'<clef number="{ono}"><sign>{sign}</sign><line>{line}</line></clef>')
        out.append('</attributes>')
    if is_first:
        for d in (m.get('dirs') or []):
            if d.get('type') == 'metronome':
                bpm = d.get('value', '76')
                out.append(f'<direction placement="above"><direction-type>'
                           f'<metronome><beat-unit>quarter</beat-unit>'
                           f'<per-minute>{bpm}</per-minute></metronome></direction-type>'
                           f'<sound tempo="{bpm}"/></direction>')

    by_staff = {}
    for n in m['notes']:
        st = n.get('staff', 1)
        if staff_filter is not None and st != staff_filter:
            continue
        by_staff.setdefault(st, []).append(n)

    staves = sorted(by_staff.keys()) or [staff_filter or 1]
    first_block = True
    for st in staves:
        by_voice = {}
        for n in by_staff.get(st, []):
            by_voice.setdefault(n.get('v') or 0, []).append(n)
        if not by_voice:
            by_voice = {0: []}
        ono = 1 if staff_filter is not None else st
        first_voice = True
        for vi, v in enumerate(sorted(by_voice.keys())):
            if not (first_block and first_voice):
                out.append(f'<backup><duration>{total}</duration></backup>')
            first_block = False
            first_voice = False
            body = _ccmz_render_voice(m, by_voice[v], ono, vi + 1)
            if not body:
                rt, rd = _ccmz_ticks_to_typenote(total)
                body = _ccmz_rest_xml(total, rt, rd, ono, vi + 1)
            out.extend(body)
    out.append('</measure>')


def ccmz_to_musicxml(ccmz_path: str, staff: int = 1, title: str = "") -> tuple:
    """ccmz → MusicXML 文本。返回 (xml, 元信息 dict)。

    staff=1 即「只留第一行单轨」（虫虫钢琴右手旋律声部）。
    """
    import json as _json
    z = _ccmz_zip(ccmz_path)
    score = _json.loads(z.read('score.json').decode('utf-8'))
    part = score['parts'][0]
    tinfo = score.get('title') or {}
    tname = title or tinfo.get('title') or 'Untitled'
    composer_raw = (tinfo.get('composer') or '').replace('\n', '；')
    composer = composer_raw.replace('；', ' / ')

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
           '"http://www.musicxml.org/dtds/partwise.dtd">',
           '<score-partwise version="4.0">',
           f'<movement-title>{escape(tname)}</movement-title>',
           '<identification>']
    if composer:
        out.append(f'<creator type="composer">{escape(composer)}</creator>')
    out.append('<encoding><software>score-studio ccmz2mxl</software></encoding></identification>')
    out.append('<part-list><score-part id="P1"><part-name>Violin</part-name>'
               '</score-part></part-list>')
    out.append('<part id="P1">')
    for i, mraw in enumerate(part['measures']):
        _ccmz_render_measure(mraw, out, staff, is_first=(i == 0))
    out.append('</part></score-partwise>')

    meta = {
        'title': tname,
        'composer_raw': composer_raw,
        'singer': _ccmz_pick_singer(composer_raw),
        'measures': len(part['measures']),
        'names': z.namelist(),
    }
    return '\n'.join(out), meta


def _ccmz_pick_singer(composer_raw: str) -> str:
    """从「艺术家/歌手：XXX」行提取歌手（虫虫把歌手写在 title.composer 里）。"""
    for line in re.split(r'[；\n]', composer_raw or ''):
        m = re.search(r'(?:艺术家|歌手|演唱|演奏)\s*[/、]?\s*(?:歌手)?\s*[:：]\s*(.+)', line)
        if m:
            return m.group(1).strip(' 　·,，')
    return ""


def _ccmz_pdf_node_engine(ccmz_path: str, out: str) -> bool:
    """降级通道：Node + puppeteer 引擎出「完整双谱表」版（MuseScore 缺失时兜底）。"""
    engine = _find_ccmz_engine()
    node = _find_node() if engine else ""
    if not engine or not node:
        return False
    node, engine = _clean_win_path(node), _clean_win_path(engine)
    ccmz_path, out = _clean_win_path(ccmz_path), _clean_win_path(out)
    cmd = [node, engine, ccmz_path, out, str(49200 + (os.getpid() % 100))]
    eng_dir = os.path.dirname(engine)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180, cwd=eng_dir)
    except Exception as e:
        print(f"[ccmz] Node 引擎调用异常：{e}")
        return False
    if proc.returncode != 0 or not os.path.isfile(out):
        err = (proc.stderr or b"").decode("utf-8", "ignore")[:300]
        print(f"[ccmz] Node 引擎失败：{err or '无输出'}")
        return False
    return True


def process_ccmz(input_str: str, output_dir: str, custom: str = "") -> str:
    """虫虫链接 → 单行单轨（第一行旋律）矢量 PDF，与酷狗 pulu 同规范。

    链路：页面取 ccmz URL → 下载 → 解包 score.json → 转单行 MusicXML（staff 1）
          → MuseScore 无头排版 → PDF
    MuseScore 缺失时降级 Node 引擎（完整双谱表版，并明确提示规范不一致）。
    """
    html_text = fetch_html(input_str)
    ccmz_url = _extract_ccmz_url(html_text)
    if not ccmz_url:
        raise ValueError("未在页面中找到 ccmz 工程文件（可能该曲谱无 ccmz）")
    tmpdir = tempfile.mkdtemp(prefix="ccmz_")
    try:
        ccmz_path = os.path.join(tmpdir, "score.ccmz")
        with open(ccmz_path, "wb") as f:
            f.write(download_bytes(ccmz_url))
        os.makedirs(output_dir, exist_ok=True)
        page_title = piastudy_title(html_text) or "虫虫曲谱"

        ms = find_musescore()
        if not ms:
            print("[ccmz] ⚠ 未找到 MuseScore，降级为 Node 引擎（完整双谱表版，非单行规范）")
            out_full = os.path.join(output_dir, safe_name(page_title) + ".pdf")
            if _ccmz_pdf_node_engine(ccmz_path, out_full):
                print(f"✅ PDF 已生成：{out_full}（虫虫完整版 · 降级通道）")
                return out_full
            raise ValueError(
                "虫虫谱排版失败：未找到 MuseScore（推荐，出单行小提琴版），"
                "Node 渲染引擎兜底也失败。\n"
                "请安装 MuseScore 4：https://musescore.org/zh-hans/download\n"
                "装好后本软件会自动识别，无需配置；也可用环境变量 SCORE_MUSESCORE 指定路径。")
        print(f"[ccmz] 排版引擎：{ms}")

        xml, meta = ccmz_to_musicxml(ccmz_path, staff=1, title=custom)
        song = meta['title']
        singer = meta['singer']
        print(f"[ccmz] 曲名={song} · 歌手={singer or '-'} · 小节={meta['measures']} · 取第 1 行（右手旋律）")

        xml_tmp = os.path.join(tmpdir, "score.musicxml")
        with open(xml_tmp, 'w', encoding='utf-8', newline='') as f:
            f.write(xml)

        stem = f"{song}-{singer}" if singer else song
        out = os.path.join(output_dir, safe_name(f"{stem}-小提琴") + ".pdf")
        if not mxl2pdf(ms, xml_tmp, out):
            raise ValueError(
                "MuseScore 排版失败（未产出 PDF）。\n"
                "请确认 MuseScore 能正常启动（首次运行需初始化音源，耗时较长），然后重试。")

        n_note = len(re.findall(r'<note[ >/]', xml))
        n_lyric = len(re.findall(r'<lyric', xml))
        n_staff2 = len(re.findall(r'<staff>2</staff>', xml))
        cn = _pdf_cjk_count(out)
        print(f"[ccmz] 自检：音符={n_note} 歌词={n_lyric} 第二行残留={n_staff2} PDF汉字={cn}")
        if n_staff2:
            raise ValueError(f"自检未通过：谱面仍含 {n_staff2} 处第 2 行（未成功去掉二轨）")
        if n_lyric == 0:
            print("[ccmz] 说明：虫虫 ccmz 数据不含歌词（纯钢琴谱源），本谱无词可留")
        if cn == 0:
            raise ValueError("自检未通过：PDF 中未检出任何中文（中文字体渲染失败）")
        print(f"✅ PDF 已生成：{out}（{os.path.getsize(out)} 字节 · 虫虫单行小提琴版）")
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


# ===================== 酷狗 Pulu 曲谱（h5.kugou.com） =====================
# 分享链接里的 v-<hash> 是前端构建目录，随酷狗发版轮换、旧目录被 CDN 清理 → 旧链接一律 404。
# 但网关接口与它无关：只要 opernid 有效就能取到谱面 → 直调网关，无视 hash。
# 本链路产出的是「矢量排版 PDF」（MuseScore），不经过位图管线。
import hashlib

PULU_SALT = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
PULU_API = "https://gateway.kugou.com/opern/v1/detail/info"
PULU_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")
PULU_LEVEL = {'0': 'Easy', '2': 'Medium', '1': 'Easy', '3': 'Hard'}
PULU_WANT_LEVEL = '2'          # 主上钦定：只要 Medium 档（无此档时自动降级并回报）

# 根元素上的前端渲染提示属性（OSMD 用）。非标准，不删 → MuseScore 解析器段错误 rc=139。
_PULU_OSMD_ATTR = re.compile(r'\s+osmdScoreType="[^"]*"')
# AI 误识别的「花体踏板」噪声：整块 <direction> 内出现 <pedal> 即删（真人不会 6 层踏板同踩）
_PULU_PEDAL_DIR = re.compile(
    r'[ \t]*<direction>\s*(?:(?!</direction>).)*?<pedal(?:(?!</direction>).)*?</direction>\r?\n?',
    re.S)


def _pulu_opener():
    """酷狗专用 opener：必须禁系统代理（本机代理会拦死请求）。"""
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx))


def _pulu_parse_opernid(url: str) -> str:
    """分享链接 → opernid（形如 863216734_5_0）。hash 段直接无视。"""
    from urllib.parse import unquote
    m = re.search(r'opernid=(\d+_\d+_\d+)', unquote(url or ""))
    return m.group(1) if m else ""


def _pulu_fetch(opern_id: str, instruments: str = '1') -> dict:
    """网关签名请求：salt + 按 key 升序的 k=v 拼接 + salt → MD5。"""
    from urllib.parse import quote
    ct = str(int(time.time() * 1000))
    mid = hashlib.md5(os.urandom(16)).hexdigest()
    params = {
        'appid': '1058', 'clientver': '99999', 'clienttime': ct,
        'mid': mid, 'uuid': mid, 'dfid': '-',
        'opern_id': opern_id, 'userid': '0', 'token': '',
        'instruments': str(instruments), 'srcappid': '2919',
    }
    sign_src = PULU_SALT + ''.join(f'{k}={params[k]}' for k in sorted(params)) + PULU_SALT
    params['signature'] = hashlib.md5(sign_src.encode()).hexdigest()
    qs = '&'.join(f'{k}={quote(str(v), safe="")}' for k, v in params.items())
    req = urllib.request.Request(f'{PULU_API}?{qs}', headers={
        'User-Agent': PULU_UA,
        'Referer': 'https://h5.kugou.com/',
        'Accept': 'application/json, text/plain, */*',
    })
    with _pulu_opener().open(req, timeout=25) as r:
        return json.loads(r.read().decode('utf-8'))


def _pulu_download(url: str) -> bytes:
    """下载谱面直链（带时效，拿到即下，不要存 URL）。"""
    req = urllib.request.Request(url, headers={
        'User-Agent': PULU_UA, 'Referer': 'https://h5.kugou.com/'})
    with _pulu_opener().open(req, timeout=90) as r:
        return r.read()


def _pulu_strip_osmd(x: str):
    """删非标准属性 osmdScoreType。内容零损失，返回 (新文本, 删除数)。"""
    n = len(_PULU_OSMD_ATTR.findall(x))
    return _PULU_OSMD_ATTR.sub('', x), n


def _pulu_strip_pedal(x: str):
    """删花体踏板 <direction> 块。返回 (新文本, 删除数)。"""
    hits = _PULU_PEDAL_DIR.findall(x)
    if not all('<pedal' in h for h in hits):
        raise ValueError("踏板清理正则误命中非 pedal 块，已中止（避免损坏谱面）")
    return _PULU_PEDAL_DIR.sub('', x), len(hits)


def _pulu_set_title(x: str, title: str) -> str:
    """标题规范化：Pulu 原始标题是占位串（如 Piano Solo Score (Medium)），用歌名覆盖。"""
    if '<work-title>' in x:
        x = re.sub(r'<work-title>.*?</work-title>', f'<work-title>{title}</work-title>', x, flags=re.S)
    elif '<work>' in x:
        x = x.replace('<work>', f'<work><work-title>{title}</work-title>', 1)
    if '<movement-title>' in x:
        x = re.sub(r'<movement-title>.*?</movement-title>',
                   f'<movement-title>{title}</movement-title>', x, flags=re.S)
    return x


def _pulu_keep_first_part(x: str):
    """只保留第一个 <part>（旋律声部，带歌词）= 小提琴单行版。返回 (新文本, 裁掉数)。

    MusicXML 没有「隐藏非空谱表」的表达，MuseScore 也不行 ——
    所以要单行只能物理裁掉其余 part（这正是主上要的「只留第一行单轨 + 歌词」）。
    """
    ids = re.findall(r'<score-part id="([^"]+)">', x)
    if len(ids) < 2:
        return x, 0
    for pid in ids[1:]:
        x = re.sub(r'\s*<score-part id="%s">.*?</score-part>' % re.escape(pid), '', x, flags=re.S)
        x = re.sub(r'\s*<part id="%s">.*?</part>' % re.escape(pid), '', x, flags=re.S)
    return x, len(ids) - 1


def find_musescore() -> str:
    """定位 MuseScore（矢量排版引擎）。环境变量 SCORE_MUSESCORE 可强制指定。"""
    exes = ("MuseScore4.exe", "MuseScore3.exe", "MuseScore.exe")
    cands = []
    if os.environ.get("SCORE_MUSESCORE"):
        cands.append(os.environ["SCORE_MUSESCORE"])
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.environ.get("LOCALAPPDATA", "")]
    for r in roots:
        if not r:
            continue
        for ver in ("MuseScore 4", "MuseScore 3", "MuseScore 4 Nightly"):
            for exe in exes:
                cands.append(os.path.join(r, ver, "bin", exe))
        for exe in exes:                       # 便携版布局
            cands.append(os.path.join(r, "MuseScore4", "bin", exe))
    # 与软件同级的 musescore/ 目录（便携分发预留）
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.path.dirname(_clean_win_path(sys.argv[0] or here))):
        for sub in ("musescore", os.path.join("musescore", "bin")):
            for exe in exes:
                cands.append(os.path.join(base, sub, exe))
    for c in cands:
        if c and os.path.isfile(_clean_win_path(c)):
            return _clean_win_path(c)
    return ""


def mxl2pdf(ms: str, xml_path: str, pdf_path: str, timeout: int = 300) -> bool:
    """MusicXML → PDF（MuseScore CLI 无头）。三件套缺一不可，判据只看产物文件。"""
    env = dict(os.environ)
    env['QT_QPA_PLATFORM'] = 'offscreen'     # 缺 → 无 GUI 会话下初始化窗口栈，挂起数分钟
    env['QT_QPA_FONTDIR'] = os.path.join(     # 缺 → 中文歌词在 PDF 里渲染不出来
        os.environ.get('SystemRoot', r'C:\Windows'), 'Fonts')
    if os.path.exists(pdf_path):
        try:
            os.remove(pdf_path)
        except Exception:
            pass
    try:
        subprocess.run([ms, '-o', pdf_path, xml_path], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout, cwd=os.path.dirname(xml_path))
    except subprocess.TimeoutExpired:
        print(f"[pulu] MuseScore 转换超时（{timeout}s）")
    except Exception as e:
        print(f"[pulu] MuseScore 调用异常：{e}")
    return os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 0


def _pdf_cjk_count(pdf_path: str) -> int:
    """PDF 文本层里的汉字数（0 = 字体没配好）。fitz 不可用时返回 -1（跳过该判据）。"""
    try:
        import fitz
        d = fitz.open(pdf_path)
        n = sum(1 for p in d for c in p.get_text() if '\u4e00' <= c <= '\u9fff')
        d.close()
        return n
    except Exception:
        return -1


def process_pulu(input_str: str, output_dir: str, custom: str = "") -> str:
    """酷狗分享链接 → Medium 档「小提琴单行版（含歌词）」矢量 PDF。

    链路：网关签名取直链 → 下载 MusicXML → 清噪（花体踏板 + osmdScoreType）
          → 裁第一个 part（旋律 + 歌词）→ MuseScore 无头排版 → PDF
    """
    opern_id = _pulu_parse_opernid(input_str)
    if not opern_id:
        raise ValueError("未在链接中找到 opernid（酷狗曲谱分享链接形如 …/xml.html?opernid=<ID>_<N>_<N>）")
    instruments = opern_id.split('_')[1] if opern_id.count('_') >= 2 else '1'
    print(f"[pulu] opernid = {opern_id}（instruments={instruments}）")

    try:
        js = _pulu_fetch(opern_id, instruments)
    except Exception as e:
        raise ValueError(f"酷狗网关请求失败：{e}")
    if js.get('errcode') not in (0, '0', None):
        raise ValueError(f"酷狗网关返回错误：errcode={js.get('errcode')} errmsg={js.get('errmsg')!r}")

    data = js.get('data') or {}
    info = data.get('opern_info') or {}
    levels = info.get('opern_level_file') or {}
    if not levels:
        raise ValueError(
            "该曲谱在酷狗侧不存在或已下架（接口 data.opern_info 为空）。\n"
            "换参数无用 —— 请确认分享链接有效，或换一首重新分享。")

    key = PULU_WANT_LEVEL if PULU_WANT_LEVEL in levels else sorted(levels)[0]
    level = PULU_LEVEL.get(key, f"L{key}")
    if key != PULU_WANT_LEVEL:
        print(f"[pulu] ⚠ 该曲无 Medium 档（可用档位：{sorted(levels)}），已降级为 {level}")
    song = (data.get('song_name') or info.get('opern_name') or '').strip() or '酷狗曲谱'
    singer = (data.get('singer_name') or info.get('author_name') or '').strip()
    print(f"[pulu] 曲名={song} · 歌手={singer or '-'} · 档位={level}")

    ms = find_musescore()
    if not ms:
        raise ValueError(
            "未找到 MuseScore（曲谱排版引擎，免费开源）。\n"
            "请先安装 MuseScore 4：https://musescore.org/zh-hans/download\n"
            "装好后本软件会自动识别，无需配置；也可用环境变量 SCORE_MUSESCORE 指定路径。")
    print(f"[pulu] 排版引擎：{ms}")

    tmpdir = tempfile.mkdtemp(prefix="pulu_")
    try:
        raw = _pulu_download(levels[key]).decode('utf-8', 'ignore')
        if '<score-partwise' not in raw and '<score-timewise' not in raw:
            raise ValueError("下载到的内容不是 MusicXML（酷狗直链有时效，可能已过期，请重试）")

        x, n_osmd = _pulu_strip_osmd(raw)      # 必须在写盘前删：交付件要能被 MuseScore 直接打开
        x, n_pedal = _pulu_strip_pedal(x)
        x = _pulu_set_title(x, custom or song)
        x, n_drop = _pulu_keep_first_part(x)
        if n_drop == 0:
            print("[pulu] 提示：该谱只有一个声部，已原样保留（无伴奏可裁）")

        xml_tmp = os.path.join(tmpdir, "score.musicxml")
        with open(xml_tmp, 'w', encoding='utf-8', newline='') as f:
            f.write(x)

        stem = f"{song}-{singer}" if singer else song
        os.makedirs(output_dir, exist_ok=True)
        out = os.path.join(output_dir, safe_name(f"{stem}-小提琴") + ".pdf")
        if not mxl2pdf(ms, xml_tmp, out):
            raise ValueError(
                "MuseScore 排版失败（未产出 PDF）。\n"
                "请确认 MuseScore 能正常启动（首次运行需初始化音源，耗时较长），然后重试。")

        # 自检（交付前必过）
        n_note = len(re.findall(r'<note[ >/]', x))
        n_lyric = len(re.findall(r'<lyric', x))
        n_left = len(_PULU_OSMD_ATTR.findall(x))
        cn = _pdf_cjk_count(out)
        print(f"[pulu] 自检：删osmdScoreType={n_osmd} 清踏板={n_pedal} 裁声部={n_drop} "
              f"音符={n_note} 歌词={n_lyric} 残留属性={n_left} PDF汉字={cn}")
        if n_left:
            raise ValueError(f"自检未通过：谱面仍含 {n_left} 处 osmdScoreType（MuseScore 打开会崩溃）")
        if n_lyric == 0:
            print("[pulu] ⚠ 未提取到歌词（酷狗侧该谱可能本就没有对轴歌词）")
        if cn == 0:
            raise ValueError("自检未通过：PDF 中未检出任何中文（中文字体渲染失败）")
        print(f"✅ PDF 已生成：{out}（{os.path.getsize(out)} 字节 · 酷狗 {level} 小提琴单行版）")
        return out
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


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
_CAPTCHA_OCR = None


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
            return "", ""
        # 置信度过滤（0.5 以下多为噪点）。注意 rapidocr 1.2.x 返回的置信度为字符串，须转 float
        def _conf(r):
            try:
                return float(r[2])
            except Exception:
                return 0.0
        lines = [r for r in result if len(r) >= 3 and _conf(r) > 0.5] or list(result)
        if not lines:
            return "", ""
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
def _run_multi(input_str: str, output_dir: str, theme: str, custom: str):
    """多路径按序合并：队列项内多张图/多个 PDF 合并为一份 PDF。
    input_str 用 \\u001e (ASCII RS) 分隔多个绝对路径，前端拖拽排序后用此编码传参。"""
    paths = [p.strip() for p in input_str.split("\u001e") if p.strip()]
    print(f"[来源] 多文件合并（{len(paths)} 项）")
    images = []
    for i, p in enumerate(paths):
        try:
            if p.lower().endswith(".pdf") and os.path.isfile(p):
                sub = local_pdf(p)
                images.extend(sub)
                print(f"  [{i+1}/{len(paths)}] {os.path.basename(p)} → {len(sub)} 页")
            elif os.path.isfile(p) and p.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                images.append(resize_standard(_open_local(p)))
                print(f"  [{i+1}/{len(paths)}] {os.path.basename(p)} → 1 页")
            else:
                print(f"  ⚠ [{i+1}/{len(paths)}] 跳过未知：{p[:80]}")
        except Exception as e:
            print(f"  ⚠ [{i+1}/{len(paths)}] {os.path.basename(p)} 失败：{e}")
    if not images:
        print("⚠ 多路径模式全部文件处理失败，退出。")
        return None

    # OCR 自动命名（与单文件一致：取第一页最上方字号最大行作标题）
    if not custom and not theme:
        _ocr = ocr_first_page_title(images)
        if _ocr:
            _base, _artist = parse_title_fields(_ocr)
            custom = f"{_base}-{_artist}" if _artist else _base
            print(f"[命名] OCR 识别第一页标题：{custom}")
        else:
            # 多文件模式兜底：取首项基名
            custom = os.path.splitext(os.path.basename(paths[0]))[0] or "曲谱"
            print(f"[命名] 未手动命名且 OCR 不可用，使用首项基名：{custom}")

    os.makedirs(output_dir, exist_ok=True)
    try:
        name = derive_name(paths[0], "", theme, custom)
    except ValueError as e:
        print(f"⚠ {e}")
        return None
    out = os.path.join(output_dir, name)
    to_pdf(images, out)
    try:
        from library_ops import smart_split, write_pdf_metadata
        base = os.path.splitext(name)[0]
        mt, ma, malb = smart_split(base)
        if write_pdf_metadata(out, title=mt or None, artist=ma or None, album=malb or None):
            print(f"[元数据] 已写入：曲={mt or '-'} 歌手={ma or '-'} 专辑={malb or '-'}")
    except Exception as e:
        print(f"[元数据] 写入跳过：{e}")
    print(f"✅ PDF 已生成：{out}（{len(images)} 页 · {TARGET_WIDTH}px · {PDF_DPI}DPI）")
    return out


def write_meta(pdf_path: str):
    """写入 PDF /Info 元数据（曲名/歌手/专辑），等价于 MP3 的 ID3，使曲库与播放器可读。
    曲名/歌手/专辑由 library_ops.smart_split 从文件名解析（编配/乐器描述自动剔除）。"""
    try:
        from library_ops import smart_split, write_pdf_metadata
        base = os.path.splitext(os.path.basename(pdf_path))[0]
        mt, ma, malb = smart_split(base)
        if write_pdf_metadata(pdf_path, title=mt or None, artist=ma or None,
                              album=malb or None):
            print(f"[元数据] 已写入：曲={mt or '-'} 歌手={ma or '-'} 专辑={malb or '-'}")
    except Exception as e:
        print(f"[元数据] 写入跳过：{e}")


def run(input_str: str, output_dir: str, theme: str = "", custom: str = "",
        captcha_ans: str = ""):
    """处理主入口。captcha_ans：词曲网 WAF 验证码手动答案（前端弹窗输入，可选）。"""
    # 多路径合并模式（队列项多文件按序合一份 PDF），优先于所有其他分支
    if "\u001e" in input_str:
        return _run_multi(input_str, output_dir, theme, custom)
    # 整段分享文本 → 抽取链接（手机 App 的「分享」会带上中文前缀与后缀）
    _u = extract_url(input_str)
    if _u != input_str.strip():
        print(f"[来源] 已从分享文本中识别出链接：{_u}")
        input_str = _u
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
        # 酷狗 Pulu：谱面是 MusicXML（结构化），走矢量排版出口，不进位图管线
        if "kugou.com" in input_str.lower():
            out = process_pulu(input_str, output_dir, custom=custom)
            write_meta(out)
            return out
        ktvc8 = "ktvc8.com" in input_str.lower()
        cookie = os.environ.get("SCORE_KTVC8_COOKIE", "")
        # 词曲网移动端 SSL 不稳定 + WAF 严，自动切桌面版
        if ktvc8 and "/mobile/" in input_str:
            desktop_url = re.sub(r'/mobile/(\d+_\d+\.html)', r'/article/article_\1', input_str)
            desktop_url = re.sub(r'[?&]mobile=1', '', desktop_url).rstrip('?')
            print(f"[来源] 词曲网移动端 → 桌面版: {desktop_url}")
            input_str = desktop_url
        html = fetch_html(input_str, cookie=cookie)
        # 虫虫钢琴：ccmz 完整曲谱（付费预览图绕过）→ 与酷狗同规范的单行小提琴版
        if "gangqinpu.com" in input_str.lower() and ".ccmz" in html:
            print("[来源] 虫虫钢琴（ccmz · 单行小提琴版）")
            out = process_ccmz(input_str, output_dir, custom=custom)
            if out:
                write_meta(out)
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
            images = []
            if is_waf_page(html):
                # 云锁拦截 → 优先级：手动答案（重跑）> OCR 自动 > @ask 同会话弹窗
                #   ① 已有手动答案（前端弹窗后重跑）：单会话取图+提交
                if captcha_ans and captcha_ans != "@ask":
                    real_html, waf_cookie = _ktvc8_solve_waf(
                        input_str, cookie, manual_ans=captcha_ans)
                    if real_html and not real_html.startswith("__CAPTCHA"):
                        html = real_html
                        cookie = waf_cookie if waf_cookie else cookie
                        print("[来源] 词曲网（云锁验证码已确认）")
                    else:
                        raise ValueError(
                            "KTVC8_CAPTCHA=RETRY\n"
                            "验证码输入不正确，请重新查看验证码后再试。")
                #   ② 无手动答案：先 OCR 自动（4 轮），失败走 @ask 同会话弹窗
                else:
                    real_html, waf_cookie = _ktvc8_solve_waf(input_str, cookie)
                    if real_html and real_html.startswith("__CAPTCHA_REQUIRED__:"):
                        # 自动失败 → @ask 同会话模式：取图打印标记 → 等答案文件 → 提交
                        cap_path = real_html.split(":", 1)[1]
                        can_ask = os.environ.get("SCORE_KTVC8_INTERACTIVE", "") == "1"
                        if can_ask:
                            real_html, waf_cookie = _ktvc8_solve_waf(
                                input_str, cookie, manual_ans="@ask")
                            if not real_html:
                                raise ValueError(
                                    f"KTVC8_CAPTCHA={cap_path}\n"
                                    "词曲网验证码输入超时或已取消。")
                            html = real_html
                            cookie = waf_cookie if waf_cookie else cookie
                            print("[来源] 词曲网（云锁验证码已确认）")
                        else:
                            raise ValueError(
                                f"KTVC8_CAPTCHA={cap_path}\n"
                                "词曲网被云锁拦截，验证码自动识别失败。"
                                "请查看弹窗中的验证码图片并输入字符后重试。")
                    elif real_html:
                        html = real_html
                        cookie = waf_cookie if waf_cookie else cookie
                        print("[来源] 词曲网（云锁验证已通过）")
                        # WAF 验证成功 → 真实页面是 JS 渲染的，直接用 puppeteer 引擎取图
                        js_imgs, js_title = _ktvc8_fetch_js(input_str, cookie)
                        if js_imgs:
                            images = process_images(js_imgs, is_tan8=False)
                            if not theme and not custom and js_title:
                                custom = ktvc8_title(f"<title>{js_title}</title>")
                        if not images:
                            print("⚠ 浏览器引擎未提取到图片，继续尝试 HTML 提取")
                    else:
                        # OCR + @ask 全失败 → 降级 puppeteer 引擎
                        js_imgs, js_title = _ktvc8_fetch_js(input_str, cookie)
                        if not js_imgs:
                            raise ValueError(
                                "词曲网被云锁（WAF）拦截，自动验证与浏览器引擎兜底均失败："
                                "请用浏览器打开页面，右键复制曲谱图片地址直接粘贴到本输入框重试；"
                                "或确认软件已更新（内置浏览器引擎）。")
                        print(f"[来源] 词曲网（云锁挑战 → 浏览器引擎兜底，{len(js_imgs)} 张）")
                        images = process_images(js_imgs, is_tan8=False)
                        if not theme and not custom and js_title:
                            custom = ktvc8_title(f"<title>{js_title}</title>")
                        if images:
                            pass
                        else:
                            return None
            if not images:
                print("[来源] 词曲网（位图 · 分页）")
                pages = ktvc8_page_urls(input_str, html)
                images = []
                for p_url in pages:
                    p_html = html if p_url == input_str else fetch_html(p_url, cookie=cookie)
                    if is_waf_page(p_html):
                        break
                    imgs = _ktvc8_imgs(p_html)
                    if len(imgs) <= 1:
                        imgs = _ktvc8_probe_next(imgs, cookie=cookie)
                        if len(imgs) > 1:
                            print(f"  页 {p_url.rsplit('/', 1)[-1]}: 编号探测补齐 {len(imgs)} 张")
                    print(f"  页 {p_url.rsplit('/', 1)[-1]}: 候选 {len(imgs)} 张")
                    images.extend(process_images(imgs, is_tan8=False))
                if not images:
                    # 新结构兜底：谱图由 show_neirong() 动态注入，URL 藏在 showvisitjs.asp
                    # 返回的 JS 里（需先过 /plcms.asp 滑动验证）。纯 HTTP，优先于浏览器引擎。
                    sv_imgs = _ktvc8_showvisit_imgs(html, cookie)
                    if sv_imgs:
                        print(f"[来源] 词曲网（showvisitjs 通道 → {len(sv_imgs)} 张）")
                        images = process_images(sv_imgs, is_tan8=False)
                if not images:
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
    write_meta(out)
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
    ap.add_argument("--captcha", default="", help="词曲网 WAF 验证码手动答案（前端弹窗输入，可选）")
    ap.add_argument("--selftest", action="store_true", help="运行冒烟测试")
    args = ap.parse_args()

    if args.cookie:
        os.environ["SCORE_KTVC8_COOKIE"] = args.cookie
    if args.selftest:
        selftest()
        return
    if not args.input:
        ap.error("需提供 --input 或 --selftest")
    out = run(args.input, args.output_dir, theme=args.theme, custom=args.name,
              captcha_ans=args.captcha)
    if out is None:
        print("未生成 PDF：若提示命名失败，请补充 --name 指定曲名后重试。")


if __name__ == "__main__":
    main()
