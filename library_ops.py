#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Score Studio · 曲库与巡检后端操作（单一真相源）
=================================================
同时服务于：
  1. Tauri 模式：Rust 通过 shell 调用 `library_ops.py <action> --dir ...`
  2. 本地模式：run_local.py 直接 import 调用

能力：
  - 曲库索引（增量缓存：首次扫描建 index.json，之后仅解析新增/变更 PDF）
  - 缩略图按需获取（懒加载，命中文件缓存直接返回，避免一次性渲染全部）
  - 首页文字提取（PyMuPDF）
  - 文件名解析与统一命名建议
  - Wikipedia 免费无密钥主题曲/影视/动漫/游戏归属识别
  - 安全批量重命名（仅在前端显式确认后执行）

性能要点（回应「不要高占用 / 直播时开很多东西」）：
  - get_library 不再逐一打开 PDF 渲染缩略图并塞进巨型 JSON；改为返回轻量索引，
    缩略图由前端 IntersectionObserver 进入视口时按需单张获取（get_thumb）。
  - inspect 复用索引中已缓存的首页文字，仅变更文件才重新抽取。

依赖：PyMuPDF（fitz）+ Pillow，已随 python_dist 打包。
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

ILLEGAL = r'[\\/:*?"<>|]'
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) ScoreStudio/0.1")

# 文件名分隔符：统一按这些拆分 曲名-歌手-主题
_SPLIT_RE = re.compile(r'[-_·–—|｜]')

# 媒体类型关键词 → 标签（优先级从上到下）
_MEDIA_KEYWORDS = [
    (['主题歌', '主题曲', '片头曲', '片尾曲', '片头主题曲', '片尾主题曲',
      '插曲', 'op', 'ed', 'opening', 'ending', 'theme song'], '主题曲'),
    (['特摄', '特摄剧'], '特摄主题曲'),
    (['动画电影', '动画', '动漫', '日本动画', 'tv动画', '番剧'], '动画主题曲'),
    (['电子游戏', '游戏', '手游', 'galgame', '单机游戏'], '游戏主题曲'),
    (['剧场版', '电影', '院线'], '电影主题曲'),
    (['电视剧', '日剧', '韩剧', '台剧', '大陆剧', '网剧', '连续剧'], '影视主题曲'),
]

# 索引缓存：放在曲库目录下的隐藏目录，避免污染曲谱本身
_INDEX_REL = os.path.join('.score-studio-cache', 'index.json')


# ===================== PyMuPDF 包装（容错） =====================
def _fitz():
    try:
        # PyMuPDF 2.x+ 推荐别名：import pymupdf as fitz
        import pymupdf as fitz
        return fitz
    except Exception:
        try:
            import fitz
            return fitz
        except Exception:
            return None


def _cache_dir(lib_dir: str) -> str:
    return os.path.join(lib_dir, '.score-studio-cache', 'thumbs')


def _index_path(lib_dir: str) -> str:
    return os.path.join(lib_dir, _INDEX_REL)


def _clean_cache_for(lib_dir: str, basename: str):
    """改名后清理旧缓存文件，避免 basename 不变时路径更名不触发 key 变化。"""
    cd = _cache_dir(lib_dir)
    if not os.path.isdir(cd):
        return
    for f in os.listdir(cd):
        if f.startswith(basename + '_') and f.endswith('.jpg'):
            try:
                os.remove(os.path.join(cd, f))
            except Exception:
                pass


def _file_key(path: str) -> str:
    """以 size + mtime_ns 作为变更指纹；任一变动即视为文件已变更。"""
    try:
        st = os.stat(path)
        return f"{st.st_size}_{st.st_mtime_ns}"
    except Exception:
        return ''


def pdf_pages(path: str) -> int:
    fitz = _fitz()
    if not fitz:
        return 0
    try:
        d = fitz.open(path)
        n = d.page_count
        d.close()
        return n
    except Exception:
        return 0


def first_page_text(path: str, max_chars: int = 800) -> str:
    fitz = _fitz()
    if not fitz:
        return ''
    try:
        d = fitz.open(path)
        if d.page_count == 0:
            d.close()
            return ''
        txt = d[0].get_text()
        d.close()
        # 简单清洗：把多行合并，折叠空白
        txt = re.sub(r'\s+', ' ', txt).strip()
        return txt[:max_chars]
    except Exception:
        return ''


def render_thumb(path: str, width: int = 320, cache_dir: str | None = None) -> str:
    """返回 JPEG 缩略图的 base64 字符串；失败返回空串。命中文件缓存则直接返回。"""
    fitz = _fitz()
    if not fitz:
        return ''
    try:
        cache_path = None
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            st = os.stat(path)
            key = f"{os.path.basename(path)}_{st.st_size}_{st.st_mtime_ns}.jpg"
            cache_path = os.path.join(cache_dir, key)
            if os.path.exists(cache_path) and os.path.getmtime(cache_path) >= st.st_mtime:
                with open(cache_path, 'rb') as f:
                    return base64.b64encode(f.read()).decode()

        d = fitz.open(path)
        page = d[0]
        zoom = width / max(page.rect.width, 1.0)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img_bytes = pix.tobytes('jpeg')
        d.close()

        if cache_path:
            try:
                with open(cache_path, 'wb') as f:
                    f.write(img_bytes)
            except Exception:
                pass

        return base64.b64encode(img_bytes).decode()
    except Exception:
        return ''


# ===================== 命名解析与构建 =====================
def split_name(base: str):
    """把 '曲名-歌手-主题' 拆成 (title, artist, theme)。"""
    parts = [p.strip() for p in _SPLIT_RE.split(base) if p.strip()]
    title = parts[0] if len(parts) >= 1 else ''
    artist = parts[1] if len(parts) >= 2 else ''
    theme = parts[2] if len(parts) >= 3 else ''
    return title, artist, theme


def build_name(title: str, artist: str = '', theme: str = '') -> str:
    """文件名（不含扩展名）：曲名[-歌手][-主题]。"""
    segs = [s.strip() for s in [title, artist, theme] if s and s.strip()]
    name = '-'.join(segs)
    name = re.sub(ILLEGAL, '-', name).strip().strip('-')
    return name


def normalize_filename(base: str) -> str:
    """把现有文件名中的下划线/点号/竖线统一成短横线。"""
    return re.sub(ILLEGAL, '-', base).strip().strip('-')


# ===================== 复用 sheet_pipeline 的标题解析 =====================
def _parse_title_fields_local(text: str):
    """安全复用 sheet_pipeline.parse_title_fields。"""
    try:
        sys.path.insert(0, HERE)
        from sheet_pipeline import parse_title_fields
        return parse_title_fields(text)
    except Exception:
        return ('', '')


# ===================== 增量索引（核心性能优化） =====================
def _pdf_walk(dir_path: str):
    """递归收集 dir_path 下所有 PDF 的 (rel, full) 列表。

    排除隐藏目录（以 '.' 开头）与缓存目录 .score-studio-cache。
    指向父目录时也能列出子目录曲谱，彻底避免「顶层空即空」的困惑。
    rel 为相对 dir_path 的路径（'/' 分隔），用于跨子目录唯一标识。
    """
    out = []
    for root, dirs, files in os.walk(dir_path):
        # 原地过滤：排除隐藏目录与缓存目录
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '.score-studio-cache']
        for f in sorted(files):
            if f.lower().endswith('.pdf') and os.path.isfile(os.path.join(root, f)):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, dir_path).replace(os.sep, '/')
                out.append((rel, full))
    out.sort(key=lambda x: x[0])
    return out


def _entry_for(full: str, rel: str, base: str, with_text: bool) -> dict:
    """对单个 PDF 做解析（仅在变更或需文字时调用）。with_text 控制是否抽首页文字。"""
    key = _file_key(full)
    title, artist, theme = split_name(base)
    # 单次 fitz.open 取页数；文字按需（巡检才抽，曲库列表不抽 → 零占用）
    pages = pdf_pages(full)
    text = first_page_text(full) if with_text else ''
    detected = detect_theme(text) if text else ''
    return {
        'rel': rel,
        'name': os.path.basename(full),
        'title': title,
        'artist': artist,
        'theme': theme,
        'pages': pages,
        'size': os.path.getsize(full) if os.path.isfile(full) else 0,
        'key': key,
        'text_excerpt': text[:220],
        'det_theme': detected,
    }


def build_index(dir_path: str, with_text: bool = False, force: bool = False) -> list:
    """生成/增量更新曲库索引。

    返回条目列表（每个含 rel/name/title/artist/theme/pages/size/key/text_excerpt/det_theme）。
    - 递归收集 PDF（支持子目录）。
    - 已存在且 key（size+mtime_ns）未变的条目直接复用，跳过 PDF 解析（零占用）。
    - with_text=True 时（巡检）若缓存无文字则重新抽取；with_text=False（曲库列表）不抽文字。
    - 命中主上诉求：首次扫描建索引，之后直接读索引；新增曲谱因 mtime/size 变化即时入库。
    """
    items = []
    if not dir_path or not os.path.isdir(dir_path):
        return items

    cache_root = os.path.dirname(_index_path(dir_path))
    try:
        os.makedirs(cache_root, exist_ok=True)
    except Exception:
        # 缓存目录创建失败不阻塞扫描（如目录只读/路径异常）
        pass

    old = {}
    idx_path = _index_path(dir_path)
    if os.path.isfile(idx_path):
        try:
            with open(idx_path, 'r', encoding='utf-8') as f:
                old_list = json.load(f)
            if isinstance(old_list, list):
                # 旧格式按 name 缓存；仅顶层文件（rel==name）可安全迁移
                for x in old_list:
                    if isinstance(x, dict) and x.get('name'):
                        old[x['name']] = x
        except Exception:
            old = {}

    result = []
    for rel, full in _pdf_walk(dir_path):
        base = os.path.splitext(os.path.basename(full))[0]
        key = _file_key(full)
        filename = os.path.basename(full)
        cached = old.get(rel)
        if not cached and rel == filename:
            # 旧缓存用 name 作 key，顶层文件可命中
            cached = old.get(filename)
        if cached and cached.get('key') == key and not force:
            if with_text and not cached.get('text_excerpt'):
                # 巡检需要文字但缓存为空 → 重新抽取
                try:
                    result.append(_entry_for(full, rel, base, with_text))
                except Exception:
                    e = dict(cached); e['rel'] = rel; result.append(e)
            else:
                e = dict(cached)
                e['rel'] = rel
                result.append(e)
        else:
            try:
                result.append(_entry_for(full, rel, base, with_text))
            except Exception:
                # 单文件解析失败：放一条最小占位，不拖累整体
                result.append({
                    'rel': rel, 'name': filename, 'title': base, 'artist': '', 'theme': '',
                    'pages': 0, 'size': 0, 'key': key,
                    'text_excerpt': '', 'det_theme': '',
                })
    # 回写索引（仅当前存在的文件）
    try:
        with open(idx_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception:
        pass
    return result


# ===================== 曲库元数据（轻量，不含缩略图 base64） =====================
def _norm_dir(d) -> str:
    """统一清洗目录/路径参数：去首尾空白与引号（防粘贴/对话框带回不可见字符）。"""
    if not d:
        return ''
    return str(d).strip().strip('"').strip("'")


def get_library(dir_path: str) -> list:
    """返回曲库条目列表（rel/name/title/artist/theme/pages/size）。
    不抽 PDF 文字（零占用，秒开）；缩略图由前端按需调用 get_thumb 懒加载。
    目录不存在/非目录返回 []；扫描异常向上抛出（CLI 层写 stderr，前端显形）。
    """
    dir_path = _norm_dir(dir_path)
    if not dir_path or not os.path.isdir(dir_path):
        return []
    return build_index(dir_path, with_text=False)


# ===================== 缩略图按需获取（懒加载端点） =====================
def get_thumb(dir_path: str, rel: str) -> str:
    """返回单个 PDF 首页缩略图的 base64；失败返回空串。命中文件缓存直接返回。
    rel 为相对 dir_path 的路径（支持子目录曲谱）。
    """
    dir_path = _norm_dir(dir_path)
    rel = _norm_dir(rel)
    if not dir_path or not rel:
        return ''
    full = os.path.join(dir_path, rel)
    if not os.path.isfile(full):
        return ''
    try:
        return render_thumb(full, cache_dir=_cache_dir(dir_path))
    except Exception:
        return ''


# ===================== 本地主题关键词检测 =====================
def detect_theme(text: str) -> str:
    """从 PDF 首页文字里识别 动画/游戏/电影/影视/特摄 主题曲标签。"""
    if not text:
        return ''
    low = text.lower()
    for kws, tag in _MEDIA_KEYWORDS:
        if tag == '主题曲':
            continue
        if any(k in low for k in kws):
            return tag
    return ''


# ===================== 巡检（复用索引，增量） =====================
def inspect_library(dir_path: str) -> list:
    """对每个 PDF 给出：当前解析字段、首页文字摘要、检测建议名。
    复用 build_index 已缓存的首页文字，仅变更文件才重新抽取，降低占用。
    目录不存在/非目录返回 []；扫描异常向上抛出（CLI 层写 stderr，前端显形）。
    """
    rows = []
    dir_path = _norm_dir(dir_path)
    if not dir_path or not os.path.isdir(dir_path):
        return rows

    entries = build_index(dir_path, with_text=True)

    for e in entries:
        f = e.get('rel') or e.get('name', '')
        if not f:
            continue
        base = os.path.splitext(os.path.basename(f))[0]
        cur_t, cur_a, cur_th = split_name(base)
        text = e.get('text_excerpt', '') or ''

        det_t, det_a = '', ''
        if text:
            det_t, det_a = _parse_title_fields_local(text)

        sug_t = det_t or cur_t
        sug_a = det_a or cur_a
        detected_theme = e.get('det_theme', '') or (detect_theme(text) if text else '')
        # 优先级：文字检测到的主题 > 文件名已有的主题
        sug_theme = detected_theme or cur_th
        sug_name = build_name(sug_t, sug_a, sug_theme)
        normalized_current = normalize_filename(base)

        # 是否需要改名：建议名与当前规范化名不同，或当前含非法/下划线等分隔符
        needs = (sug_name != normalized_current) and bool(sug_t)

        rows.append({
            'rel': f,
            'name': os.path.basename(f),
            'cur_title': cur_t,
            'cur_artist': cur_a,
            'cur_theme': cur_th,
            'text_excerpt': text,
            'det_title': det_t,
            'det_artist': det_a,
            'sug_title': sug_t,
            'sug_artist': sug_a,
            'sug_theme': sug_theme,
            'suggested': sug_name + '.pdf',
            'needs_rename': needs,
            'has_text': bool(text.strip()),
        })
    return rows


# ===================== Wikipedia 免费无密钥主题曲识别 =====================
WIKI_ZH = 'https://zh.wikipedia.org/w/api.php'
WIKI_EN = 'https://en.wikipedia.org/w/api.php'


def _wiki_search(query: str, api: str):
    params = {
        'action': 'query',
        'list': 'search',
        'srsearch': query,
        'format': 'json',
        'srlimit': 5,
        'srprop': 'snippet',
    }
    url = api + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


def wiki_tag(title: str, artist: str = ''):
    """返回 {'tag': ..., 'source': ..., 'snippet': ...} 或 None。"""
    q = ' '.join(x for x in [title, artist] if x).strip()
    if not q:
        return None

    for api in (WIKI_ZH, WIKI_EN):
        try:
            data = _wiki_search(q, api)
            for item in data.get('query', {}).get('search', []):
                snippet = (item.get('snippet', '') + ' ' + item.get('title', '')).lower()

                # 先判定确实是主题曲相关
                is_theme = any(k in snippet for k in
                               ['主题歌', '主题曲', '片头曲', '片尾曲', 'opening theme',
                                'ending theme', 'theme song', 'title song'])
                if not is_theme:
                    continue

                # 再按优先级命中媒体类型
                for kws, tag in _MEDIA_KEYWORDS:
                    if any(k in snippet for k in kws):
                        clean = re.sub(r'<[^>]+>', '', item.get('snippet', ''))
                        return {
                            'tag': tag,
                            'source': api,
                            'title': item.get('title', ''),
                            'snippet': clean[:180],
                        }
        except Exception:
            continue
    return None


# ===================== 安全批量重命名 =====================
def rename_items(dir_path: str, pairs):
    """
    pairs: [{"old": "原文件名", "new": "新文件名"}, ...]
    返回每条结果；遇到目标已存在则跳过（防止覆盖）。
    """
    results = []
    cleaned_bases = set()

    for p in pairs:
        old = p.get('old', '')
        new = p.get('new', '')
        if not old or not new:
            results.append({'old': old, 'ok': False, 'error': '空参数'})
            continue

        src = os.path.join(dir_path, old)
        dst = os.path.join(dir_path, new)
        if not os.path.exists(src):
            results.append({'old': old, 'ok': False, 'error': '源文件不存在'})
            continue
        if os.path.exists(dst):
            results.append({'old': old, 'ok': False, 'error': '目标已存在，跳过以防覆盖'})
            continue

        try:
            os.rename(src, dst)
            # 清理旧缓存（缓存按 basename 命名；rel 含子目录前缀时须取 basename）
            old_base = os.path.basename(os.path.splitext(old)[0])
            new_base = os.path.basename(os.path.splitext(new)[0])
            _clean_cache_for(dir_path, old_base)
            _clean_cache_for(dir_path, new_base)
            cleaned_bases.add(old_base)
            cleaned_bases.add(new_base)
            results.append({'old': old, 'ok': True, 'new': new})
        except Exception as e:
            results.append({'old': old, 'ok': False, 'error': str(e)})

    return {'results': results}


# ===================== CLI（Tauri 调用入口） =====================
def _write_empty_diag(dir_path: str):
    """空结果诊断：写 stderr（经 Rust run_python_full 带 stderr 字段→前端显形）。

    三态可区分：
      - isdir=false       → 传入路径非目录/不可见字符污染
      - isdir=true, pdf=0 → 目录真实无 PDF
      - isdir=true, pdf>0 → 扫描逻辑异常（应重点排查）
    """
    raw = _norm_dir(dir_path)
    try:
        isdir = bool(raw) and os.path.isdir(raw)
        diag = {
            'dir': repr(raw),
            'isdir': isdir,
            'exists': bool(raw) and os.path.exists(raw),
            'pdf_recursive': len(_pdf_walk(raw)) if isdir else -1,
        }
        sys.stderr.write('[score-studio-diag] ' + json.dumps(diag, ensure_ascii=False) + '\n')
    except Exception:
        pass


def main():
    # 编码免疫：Windows 管道默认 ANSI(GBK) 代码页，文件名含 emoji（非 BMP 字符）时
    # print(json.dumps(..., ensure_ascii=False)) 会抛 UnicodeEncodeError → 整体崩 → 输出 []
    # 强制 stdout/stderr 为 UTF-8，无论环境 locale/PYTHONIOENCODING 如何，JSON 输出无损。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    ap = argparse.ArgumentParser(description='Score Studio 曲库与巡检操作')
    sub = ap.add_subparsers(dest='action')

    m = sub.add_parser('meta', help='获取曲库轻量索引')
    m.add_argument('--dir', required=True)

    th = sub.add_parser('thumb', help='获取单个 PDF 首页缩略图 base64')
    th.add_argument('--dir', required=True)
    th.add_argument('--name', required=True)

    ins = sub.add_parser('inspect', help='扫描曲库并给出命名建议')
    ins.add_argument('--dir', required=True)

    rn = sub.add_parser('rename', help='执行批量重命名')
    rn.add_argument('--dir', required=True)
    rn.add_argument('--payload', required=True,
                    help='JSON 数组：[{"old":"原","new":"新"}]')

    wt = sub.add_parser('wikitag', help='联网识别主题曲归属')
    wt.add_argument('--title', default='')
    wt.add_argument('--artist', default='')

    args = ap.parse_args()

    try:
        if args.action == 'meta':
            items = get_library(args.dir)
            if not items:
                # 空结果时向 stderr 写诊断（前端新契约会显形，三态可区分）
                _write_empty_diag(args.dir)
            print(json.dumps(items, ensure_ascii=False))
        elif args.action == 'thumb':
            # 纯 base64 文本（可能为空），前端按需取；不包裹 JSON，避免巨型负载
            print(get_thumb(args.dir, args.name))
        elif args.action == 'inspect':
            rows = inspect_library(args.dir)
            if not rows:
                _write_empty_diag(args.dir)
            print(json.dumps(rows, ensure_ascii=False))
        elif args.action == 'rename':
            pairs = json.loads(args.payload)
            print(json.dumps(rename_items(args.dir, pairs), ensure_ascii=False))
        elif args.action == 'wikitag':
            print(json.dumps(wiki_tag(args.title, args.artist), ensure_ascii=False))
        else:
            ap.print_help()
    except Exception as e:
        # 任何异常都输出安全 JSON，绝不留空输出（杜绝前端 JSON.parse 崩溃）
        sys.stderr.write(f"[library_ops] action={args.action} error: {e}\n")
        # meta/inspect 返回 []；thumb 返回空串；rename/wikitag 返回 null
        if args.action in ('meta', 'inspect'):
            print('[]')
        elif args.action == 'thumb':
            print('')
        else:
            print('null')


if __name__ == '__main__':
    main()
