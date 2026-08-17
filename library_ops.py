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
import threading
import urllib.parse
import urllib.request

# Windows 下强制 STDIO 为 UTF-8：应用内 Python 默认输出是 GBK(cp936)，而 stdin/Rust 传的是 UTF-8 字节——
# 会导致 stdin 读入中文乱码/孤立代理、stdout 输出 ensure_ascii=False 中文报 UnicodeEncodeError，
# 异常后被兜底成空数组。实测复现：PYTHONIOENCODING=gbk 时批量专辑返回 []（即 0/888 根因）。
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))

ILLEGAL = r'[\\/:*?"<>|]'
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) ScoreStudio/0.1")

# 文件名分隔符：统一按这些拆分 曲名-歌手-主题
_SPLIT_RE = re.compile(r'[-_–—|｜]')

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
# 来源作品 / IP 词库（数据驱动自 889 份真实文件名归纳，并经主上复核补全动漫 IP）
# 长串作品/IP 名：高特异，直接子串匹配（顺序：更长的优先，避免「崩坏」抢占「崩坏三」）
# 这些名字本身就是「专辑/来源作品」的真名（如 东方幻想乡 / 夏目友人帐），直接作为专辑字段。
_THEME_LONG = [
    '崩坏三', '崩坏3', '崩坏：星穹铁道', '崩坏', '星穹铁道', '星穹', '东方幻想乡',
    '东方project', '东方', '原神', '宫崎骏', '吉卜力', '爱乐之城', 'la la land',
    '海贼王', '海贼', '火影', '死神', '钢之炼金术师', '钢炼', '进击的巨人', '进击',
    '鬼灭之刃', '鬼灭', '咒术回战', '咒术', '链锯人', '链锯', '电锯人', '间谍过家家',
    '间谍', '葬送的芙莉莲', '葬送', '孤独摇滚', '化物语', '未闻花名',
    '罪恶王冠', 'clannad', '轻音少女', '轻音', 'lovelive', 'love live', '邦邦',
    'bang dream', '偶像梦幻祭', 'fgo', '明日方舟', '碧蓝航线', '碧蓝', '战双帕弥什',
    '战双', '鸣潮', '绝区零', '世界计划', 'プロセカ', 'vocaloid', '初音未来', '初音',
    're:从零', '从零开始的异世界', '异世界', '金牌得主', 'medalist',
    'arcaea', 'phi', '虞美人盛开的山坡', '空洞骑士', 'hollow knight',
    '白猫', '英雄联盟', '王者荣耀', '和平精英', '幻塔', '深空之眼', '流浪地球', '诺亚',
    '梦现', '节奏大师', '赛博朋克', '少女歌剧', '少女革命', 'angel beats', '命运石之门', 'steins gate',
    # —— 主上复核补全的动漫 IP（此前过度剔除，已恢复）——
    '夏目友人帐', '紫罗兰的永恒花园', '紫罗兰永恒花园', '四月是你的谎言', '魔女之旅', '魔女の旅々',
    '我的英雄学院', '辉夜大小姐想让我告白', '辉夜大小姐', '约定的梦幻岛', '灵能百分百',
    '一拳超人', '工作细胞', '罗小黑战记', '刺客伍六七', '非人哉', '镇魂街', '京剧猫',
    '凹凸世界', '干物妹小埋', '青春猪头少年', '埃罗芒阿老师', '路人超能100',
    '你的名字', '秒速五厘米', '千与千寻', '哈尔的移动城堡', '幽灵公主', '起风了', '红猪',
    '尼尔', '最终幻想', '怪物猎人', '只狼', '塞尔达', '超级马力欧', '马力欧',
    '刀剑神域', '可塑性记忆', '末日时在做什么', '情书', '东京爱情故事',
]
# 短后缀：仅保留「主题指示符」（需「非拉丁字母」边界约束，规避 Hope/Open/Lost 误判为 op/ed/ost）。
# 注意：钢琴版/小提琴谱/吉他谱/纯音乐/伴奏 等「编配描述」已移出，归入 _ARRANGEMENT_KEYWORDS ——
# 它们不是专辑/主题，而是乐器/版本描述，必须单独成字段，绝不能再污染专辑。
_THEME_SHORT_SUFFIX = ['op', 'ost', 'tv size', 'tv版', '主题曲', '片头曲',
                        '片尾曲', '插曲', 'insert song', 'im']


def _build_theme_patterns():
    # 专辑词：真实 IP/作品名 → 直接成为 album 字段
    album_pats = [re.compile(re.escape(t), re.IGNORECASE) for t in _THEME_LONG]
    # 主题指示符：仅剥离不入专辑（主题曲/op/ost… 是「这是主题曲」的标记，不是专辑名）
    mark_pats = [
        re.compile(r'(?<![A-Za-z0-9])' + re.escape(t) + r'(?![A-Za-z0-9])',
                   re.IGNORECASE)
        for t in _THEME_SHORT_SUFFIX
    ]
    return album_pats, mark_pats


_ALBUM_PATTERNS, _THEME_MARK_PATTERNS = _build_theme_patterns()
_MULTI_SEP = re.compile(r'[、&＆/／]')

# 编配 / 乐器 / 版本 描述词（主上裁决：不需要，直接剔除丢弃——老公只拉小提琴，钢琴等谱通用）。
# 按长度降序匹配，避免 钢琴版 残留 钢琴、小提琴谱 残留 小提琴。
_ARRANGEMENT_KEYWORDS = [
    '小提琴谱', '钢琴版', '吉他谱', '简谱', '二胡谱', '古筝谱', '八音盒版',
    '小提琴', '钢琴', '吉他', '二胡', '古筝', '八音盒', '口琴', '萨克斯',
    '贝斯', '尤克里里', '竖琴', '长笛', '单簧管', '大提琴', '电子琴',
    '手风琴', '琵琶', '笛子', '箫', '葫芦丝', '架子鼓', '鼓谱',
    '纯音乐', '伴奏', '独奏', '重奏', '协奏曲', '合奏', '独唱', '合唱',
]
_ARRANGEMENT_PATTERNS = [
    re.compile(r'(?<![A-Za-z0-9])' + re.escape(k) + r'(?![A-Za-z0-9一-鿿])', re.IGNORECASE)
    for k in _ARRANGEMENT_KEYWORDS
]


def _strip_arrangement(s: str) -> str:
    """从串中剔除编配/乐器描述词（丢弃，不留字段、不进文件名），返回剩余串。"""
    for pat in _ARRANGEMENT_PATTERNS:
        m = pat.search(s)
        if m:
            s = s[:m.start()] + s[m.end():]
    return s


def _clean_artist(a: str) -> str:
    return re.sub(r'\s+', ' ', a).strip(' -_—–|｜')


def smart_split(base: str):
    """曲名/歌手/专辑(来源作品) 智能解析。

    兼容旧 -/_/·/| 分隔；新增：文件名内嵌来源作品词库、编配描述剔除（丢弃，不留字段）、
    多歌手合并（、&/和）、无分隔时的拉丁↔中日文边界兜底。返回 (title, artist, album)。
    album = 来源作品真名（如 东方幻想乡 / 夏目友人帐）或联网取得的真实专辑。
    """
    if not base:
        return ('', '', '')
    s = base.strip()
    # 1) 抽取文件名内嵌来源作品（专辑真名）→ album；主题指示符(主题曲/op/ost…)仅剥离不入专辑。
    #    ip_at_start：专辑名居原串首段（其后跟分隔符/结尾）——摘除后剩余首段实为「歌手」。
    albums = []
    ip_at_start = False
    for pat in _ALBUM_PATTERNS:
        m = pat.search(s)
        if m:
            albums.append(m.group(0))
            if m.start() == 0:
                after = s[m.end():m.end() + 1] if m.end() < len(s) else ''
                if not after or after in '-_–—|｜、&＆/／ ':
                    ip_at_start = True
            s = s[:m.start()] + s[m.end():]
    for pat in _THEME_MARK_PATTERNS:
        m = pat.search(s)
        if m:
            s = s[:m.start()] + s[m.end():]
    album = ''.join(t.strip(' -_') for t in albums)
    # 2) 剔除编配/乐器描述词（丢弃，不进任何字段——主上裁决：小提琴谱/钢琴版等直接剔除）
    s = _strip_arrangement(s)
    # 3) 标准分隔符拆分
    segs = [p.strip() for p in _SPLIT_RE.split(s) if p.strip()]
    if len(segs) >= 2:
        artist = _clean_artist('、'.join(segs[1:]))  # 多歌手合并为单一歌手串
        return (segs[0], artist, album)
    only = segs[0] if segs else ''
    # 专辑名居首段且被摘除 → 剩余首段实为「歌手」（如 千与千寻-久石让 → 千与千寻/久石让/千与千寻）
    if album and ip_at_start and only:
        return (album, _clean_artist(only), album)
    if not only:
        # 全被专辑/指示符吃光 → 标题兜底为专辑名（如 夏目友人帐-主题曲-小提琴谱）
        return ((album if album else ''), '', album)
    # 4) 含多歌手分隔符 → 以左侧词起点切开，右侧整段作为歌手（合并多歌手）
    m = _MULTI_SEP.search(only)
    if m:
        i = m.start()
        j = i - 1
        cut = i
        found = False
        while j >= 0:
            c = only[j]
            prev = only[j - 1] if j > 0 else ''
            if c.isspace() or c in '、&＆/／':
                cut = j + 1
                found = True
                break
            if re.match(r'[A-Za-z0-9]', c) and not re.match(r'[A-Za-z0-9]', prev):
                cut = j
                found = True
                break
            if re.match(r'[\u4e00-\u9fff\u3040-\u30ff]', c) and not re.match(r'[\u4e00-\u9fff\u3040-\u30ff]', prev):
                cut = j
                found = True
                break
            j -= 1
        if not found or cut == 0:
            # 左侧无清晰词界（粘连，如 一念张紫宁、李鑫一逐玉 / DilemmaNelly、Kelly）：退化到首个「、」处切，保标题非空
            return (only[:i].strip(), _clean_artist(only[m.end():]), album)
        return (only[:cut].strip(), _clean_artist(only[cut:]), album)
    # 5) 拉丁↔中日文直接边界（取末个：歌手通常在末尾）；右侧不得为括号开头，避免误吞标题翻译
    cand = [m.start() for m in re.finditer(r'(?<=[A-Za-z0-9\]\)])(?=[\u4e00-\u9fff\u3040-\u30ff])', only)]
    if cand:
        i = cand[-1]
        title = only[:i].strip()
        if title and not title.endswith('(') and not title.endswith('（'):
            return (title, _clean_artist(only[i:]), album)
    # 6) 拉丁 + 空格 + 中日文歌手（取末个空格边界）
    cand = [m.start() for m in re.finditer(r'(?<=[A-Za-z0-9\]\)])\s+(?=[\u4e00-\u9fff\u3040-\u30ff])', only)]
    if cand:
        i = cand[-1]
        title = only[:i].strip()
        if title:
            return (title, _clean_artist(only[i:].strip()), album)
    # 7) 兜底：整段作曲名
    return (only, '', album)


def split_name(base: str):
    """兼容别名：委托 smart_split。把 '曲名-歌手-专辑' 拆成 (title, artist, album)。"""
    return smart_split(base)


# ===================== PDF 元数据（等价于 MP3 的 ID3） =====================
def read_pdf_metadata(path: str) -> dict:
    """读取 PDF /Info 元数据：曲名(/Title) / 歌手(/Author) / 专辑(/Subject) / 编配(/Keywords)。失败返回空字典。"""
    fitz = _fitz()
    if not fitz:
        return {}
    try:
        doc = fitz.open(path)
        md = doc.metadata or {}
        doc.close()
        return {
            'title': (md.get('title') or '').strip(),
            'author': (md.get('author') or '').strip(),
            'subject': (md.get('subject') or '').strip(),       # 专辑 / 来源作品
            'keywords': (md.get('keywords') or '').strip(),     # 编配 / 乐器
        }
    except Exception:
        return {}


def write_pdf_metadata(path: str, title: str = None, artist: str = None,
                       album: str = None) -> bool:
    """写入 PDF /Info 元数据（等价于 MP3 的 ID3）：
    /Title=曲名  /Author=歌手  /Subject=专辑(来源作品)。编配已按主上裁决剔除，不再写 /Keywords。
    全量保存到临时文件再原子替换，规避加密 PDF 的「增量保存」限制。返回是否成功。
    """
    fitz = _fitz()
    if not fitz:
        return False
    tmp = path + '.meta.tmp'
    try:
        doc = fitz.open(path)
        md = dict(doc.metadata or {})
        if title is not None:
            md['title'] = title
        if artist is not None:
            md['author'] = artist
        if album is not None:
            md['subject'] = album
        doc.set_metadata(md)
        doc.save(tmp)
        doc.close()
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def build_name(title: str, artist: str = '', album: str = '') -> str:
    """文件名（不含扩展名）：曲名[-歌手][-专辑]。"""
    segs = [s.strip() for s in [title, artist, album] if s and s.strip()]
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
    title, artist, album = split_name(base)
    # 单次 fitz.open 取页数；文字按需（巡检才抽，曲库列表不抽 → 零占用）
    pages = pdf_pages(full)
    text = first_page_text(full) if with_text else ''
    detected = detect_theme(text) if text else ''
    # 巡检时读取 PDF /Info 元数据，作为文件名解析的补充数据源（等价于读 ID3）
    meta = read_pdf_metadata(full) if with_text else {}
    return {
        'rel': rel,
        'name': os.path.basename(full),
        'title': title,
        'artist': artist,
        'album': album,
        'pages': pages,
        'size': os.path.getsize(full) if os.path.isfile(full) else 0,
        'key': key,
        'text_excerpt': text[:220],
        'det_theme': detected,
        'meta_title': meta.get('title', ''),
        'meta_artist': meta.get('author', ''),
        'meta_album': meta.get('subject', ''),
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


def get_thumbs_batch(dir_path: str, rels) -> dict:
    """批量缩略图：一次 Python 进程渲染多张，返回 {rel: b64}（命中缓存直接复用）。

    性能质变：滚动加载一屏（~20 张）从「20 次子进程启动」降为「1 次」，
    配合前端节流合并请求，根治曲库滚动卡顿与命令行窗口反复闪现。
    """
    dir_path = _norm_dir(dir_path)
    if not dir_path or not os.path.isdir(dir_path):
        return {}
    cd = _cache_dir(dir_path)
    out = {}
    for rel in rels or []:
        rel = _norm_dir(rel)
        if not rel:
            continue
        full = os.path.join(dir_path, rel)
        if not os.path.isfile(full):
            continue
        try:
            b64 = render_thumb(full, cache_dir=cd)
            if b64:
                out[rel] = b64
        except Exception:
            continue
    return out


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
        cur_t, cur_a, cur_album = split_name(base)
        text = e.get('text_excerpt', '') or ''
        meta_t = e.get('meta_title', '') or ''
        meta_a = e.get('meta_artist', '') or ''
        meta_album = e.get('meta_album', '') or ''

        det_t, det_a = '', ''
        if text:
            det_t, det_a = _parse_title_fields_local(text)

        sug_t = det_t or cur_t or meta_t
        sug_a = det_a or cur_a or meta_a
        # 专辑基线：文件名解析(来源作品) > PDF 元数据(/Subject) > 本地IP真名(离线)
        local_tag = _local_album_tag(sug_t, sug_a)
        local_album = local_tag.get('album', '') if local_tag else ''
        local_cat = (local_tag.get('category', '') if local_tag else '') \
            or (detect_theme(text) if text else '')
        sug_album = cur_album or meta_album or local_album
        sug_name = build_name(sug_t, sug_a, sug_album)
        normalized_current = normalize_filename(base)

        # 是否需要改名：建议名与当前规范化名不同，或当前含非法/下划线等分隔符
        needs = (sug_name != normalized_current) and bool(sug_t)

        rows.append({
            'rel': f,
            'name': os.path.basename(f),
            'cur_title': cur_t,
            'cur_artist': cur_a,
            'cur_album': cur_album,
            'meta_title': meta_t,
            'meta_artist': meta_a,
            'meta_album': meta_album,
            'text_excerpt': text,
            'det_title': det_t,
            'det_artist': det_a,
            'sug_title': sug_t,
            'sug_artist': sug_a,
            'sug_album': sug_album,
            'sug_category': local_cat,
            'suggested': sug_name + '.pdf',
            'needs_rename': needs,
            'has_text': bool(text.strip()),
        })
    return rows


# ===================== 专辑识别：本地词库兜底 + 联网(iTunes/Wikipedia) + 并发 =====================
WIKI_ZH = 'https://zh.wikipedia.org/w/api.php'
WIKI_EN = 'https://en.wikipedia.org/w/api.php'
ITUNES_URL = 'https://itunes.apple.com/search'

# 本地兜底：已知 IP / 作品 → 分类（离线即时）。
# 值 = 分类（动画/游戏/电影/影视）；album 取命中的 IP 真名本身。
# 覆盖 游戏/动画/电影/影视 四大类；主上复核补全的动漫 IP 已恢复（夏目/紫罗兰/四月/魔女…）。
_LOCAL_IP_TAG = {
    '崩坏': '游戏', '原神': '游戏', '星穹铁道': '游戏', '东方': '游戏',
    '明日方舟': '游戏', '碧蓝航线': '游戏', '战双帕弥什': '游戏', '鸣潮': '游戏',
    '绝区零': '游戏', '世界计划': '游戏', 'プロセカ': '游戏', 'vocaloid': '游戏',
    '初音未来': '游戏', 'arcaea': '游戏', 'hollow knight': '游戏', '空洞骑士': '游戏',
    '英雄联盟': '游戏', '王者荣耀': '游戏', '和平精英': '游戏', '幻塔': '游戏',
    '深空之眼': '游戏', '节奏大师': '游戏', 'fgo': '游戏', '白猫': '游戏',
    '尼尔': '游戏', '最终幻想': '游戏', '怪物猎人': '游戏', '只狼': '游戏', '塞尔达': '游戏', '马力欧': '游戏',
    '金牌得主': '动画', 'medalist': '动画', '海贼王': '动画', '火影': '动画',
    '死神': '动画', '钢之炼金术师': '动画', '进击的巨人': '动画', '鬼灭之刃': '动画',
    '咒术回战': '动画', '链锯人': '动画', '间谍过家家': '动画', '葬送的芙莉莲': '动画',
    '孤独摇滚': '动画', '化物语': '动画', '未闻花名': '动画', '罪恶王冠': '动画',
    'clannad': '动画', '轻音少女': '动画', 'lovelive': '动画', 'love live': '动画',
    '邦邦': '动画', 'bang dream': '动画', '偶像梦幻祭': '动画',
    're:从零': '动画', '从零开始的异世界': '动画', '少女歌剧': '动画', '少女革命': '动画',
    'angel beats': '动画', '命运石之门': '动画', 'steins gate': '动画',
    '夏目友人帐': '动画', '紫罗兰的永恒花园': '动画', '紫罗兰永恒花园': '动画', '四月是你的谎言': '动画', '魔女之旅': '动画',
    '我的英雄学院': '动画', '辉夜大小姐': '动画', '约定的梦幻岛': '动画', '灵能百分百': '动画',
    '一拳超人': '动画', '工作细胞': '动画', '罗小黑战记': '动画', '刺客伍六七': '动画',
    '非人哉': '动画', '镇魂街': '动画', '京剧猫': '动画', '凹凸世界': '动画',
    '干物妹小埋': '动画', '青春猪头少年': '动画', '埃罗芒阿老师': '动画', '路人超能100': '动画',
    '刀剑神域': '动画', '可塑性记忆': '动画', '末日时在做什么': '动画',
    '宫崎骏': '电影', '吉卜力': '电影', '爱乐之城': '电影', 'la la land': '电影',
    '你的名字': '电影', '秒速五厘米': '电影', '千与千寻': '电影', '哈尔的移动城堡': '电影',
    '幽灵公主': '电影', '起风了': '电影', '红猪': '电影', '虞美人盛开的山坡': '电影',
    '流浪地球': '电影', '赛博朋克': '影视', '诺亚': '影视', '东京爱情故事': '影视',
}

# 歌曲/主题曲名 → 中文来源作品（主上可读性诉求：专辑必须是中文，绝不显示英文 collectionName）。
# 用于动漫/电影/游戏主题曲：确认《打上花火》属于《烟花》、《红莲华》属于《鬼灭之刃》等。
# 可随主上使用持续扩充。键=歌曲名，值=(中文作品名, 分类)。
_LOCAL_SONG_CN = {
    # 鬼灭之刃
    '红莲华': ('鬼灭之刃', '动画'), '紅蓮華': ('鬼灭之刃', '动画'),
    '残响散歌': ('鬼灭之刃', '动画'),
    # 烟花（打上花火）
    '打上花火': ('烟花', '动画'), '烟花主题曲': ('烟花', '动画'),
    # 你的名字
    '前前前世': ('你的名字', '电影'), '梦灯笼': ('你的名字', '电影'),
    'sparkle': ('你的名字', '电影'), 'スパークル': ('你的名字', '电影'),
    # 咒术回战
    '廻廻奇譚': ('咒术回战', '动画'), '廻廻奇谭': ('咒术回战', '动画'),
    # 未闻花名
    'secret base': ('未闻花名', '动画'),
    # 孤独摇滚（可选）
    '孤独摇滚': ('孤独摇滚', '动画'),
    # ===== 扩充：常见动漫/电影/游戏主题曲 → 中文作品（可读性 + 离线兜底）=====
    # 进击的巨人
    '紅蓮の弓矢': ('进击的巨人', '动画'), '红莲之弓矢': ('进击的巨人', '动画'),
    # 灌篮高手
    '直到世界尽头': ('灌篮高手', '动画'), '世界が终わるまでは': ('灌篮高手', '动画'),
    # 数码宝贝
    'butter-fly': ('数码宝贝', '动画'),
    # EVA
    '残酷な天使のテーゼ': ('新世纪福音战士', '动画'), '残酷天使纲领': ('新世纪福音战士', '动画'),
    # 火影
    '青鸟': ('火影忍者', '动画'), 'ブルーバード': ('火影忍者', '动画'),
    # 千与千寻
    '那个夏天': ('千与千寻', '电影'), 'あの日の川': ('千与千寻', '电影'),
    # 哈尔的移动城堡
    '人生的旋转木马': ('哈尔的移动城堡', '电影'), '人生のメリーゴーランド': ('哈尔的移动城堡', '电影'),
    # 天空之城
    '伴随着你': ('天空之城', '电影'), '君をのせて': ('天空之城', '电影'),
    # 龙猫
    '邻家的龙猫': ('龙猫', '电影'), 'となりのトトロ': ('龙猫', '电影'),
    # 幽灵公主
    'もののけ姫': ('幽灵公主', '电影'), '幽灵公主主题曲': ('幽灵公主', '电影'),
    # 悬崖上的金鱼姬
    '崖上的波妞': ('悬崖上的金鱼姬', '电影'), '崖の上のポニョ': ('悬崖上的金鱼姬', '电影'),
    # CLANNAD
    '团子大家族': ('CLANNAD', '动画'), 'だんご大家族': ('CLANNAD', '动画'),
    # 排球少年
    'imagination': ('排球少年', '动画'), 'イマジネーション': ('排球少年', '动画'),
    # 排球 もう一歩？跳过
    # 约定的梦幻岛
    '不要让夜晚结束': ('约定的梦幻岛', '动画'),
}

# 本地兜底专用「安全媒体关键词」：仅保留无歧义短语，避免误命中英文歌手名。
# 三元组：(关键词, 分类, 专辑标签)。注意不复用 _MEDIA_KEYWORDS 中的裸 op/ed/opening/ending/theme song——
# 它们仅适合扫描 Wikipedia 摘要（上下文保证真实），若对原始串做子串扫描会误命中歌手名（Linked→ed）。
_LOCAL_MEDIA_KEYWORDS = [
    (['主题歌', '主题曲', '片头曲', '片尾曲', '片头主题曲', '片尾主题曲', '插曲'], '动画', '动画主题曲'),
    (['特摄', '特摄剧'], '影视', '特摄主题曲'),
    (['动画电影', '动画', '动漫', '日本动画', 'tv动画', '番剧'], '动画', '动画主题曲'),
    (['电子游戏', '游戏', '手游', 'galgame', '单机游戏'], '游戏', '游戏主题曲'),
    (['剧场版', '电影', '院线'], '电影', '电影主题曲'),
    (['电视剧', '日剧', '韩剧', '台剧', '大陆剧', '网剧', '连续剧'], '影视', '影视主题曲'),
]

# 联网结果磁盘缓存（跨会话命中，避免重复慢请求）
_ALBUM_CACHE = {}
_ALBUM_CACHE_LOCK = threading.Lock()
_MISS = object()
_ALBUM_CACHE_PATH = os.path.join(HERE, '.score-studio-cache', 'album_cache.json')


def _album_cache_load():
    global _ALBUM_CACHE
    try:
        if os.path.isfile(_ALBUM_CACHE_PATH):
            with open(_ALBUM_CACHE_PATH, 'r', encoding='utf-8') as f:
                _ALBUM_CACHE = json.load(f) or {}
    except Exception:
        _ALBUM_CACHE = {}


def _album_cache_get(q):
    with _ALBUM_CACHE_LOCK:
        if not _ALBUM_CACHE:
            _album_cache_load()
        # 仅缓存「成功结果」；命中即返回 dict，未命中返回哨兵
        return _ALBUM_CACHE.get(q, _MISS)


def _album_cache_set(q, val):
    with _ALBUM_CACHE_LOCK:
        _ALBUM_CACHE[q] = val
        try:
            d = os.path.dirname(_ALBUM_CACHE_PATH)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(_ALBUM_CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump(_ALBUM_CACHE, f, ensure_ascii=False)
        except Exception:
            pass


def _local_album_tag(title: str, artist: str = ''):
    """离线兜底：返回 {'album','category','source','title','snippet'} 或 None。

    album = 中文来源作品真名（主上可读性诉求：绝不填英文 collectionName）。
    优先级：歌曲名→中文作品 > 已知 IP/作品名 > 安全媒体关键词（仅分类）。
    """
    s = (title + ' ' + artist).lower()
    if not s.strip():
        return None
    # 0) 歌曲/主题曲名 → 中文来源作品（最高优先：红莲华→鬼灭之刃、打上花火→烟花、前前前世→你的名字）
    for song, (work, cat) in _LOCAL_SONG_CN.items():
        pat = re.escape(song)
        if re.search(r'(?<![A-Za-z0-9])' + pat + r'(?![A-Za-z0-9])', s, re.IGNORECASE):
            return {'album': work, 'category': cat, 'source': 'local',
                    'title': song, 'snippet': f'{song} →《{work}》'}
    # 1) 安全媒体关键词 → 仅给分类（不臆造专辑名）
    for kws, cat, album_label in _LOCAL_MEDIA_KEYWORDS:
        if any(k.lower() in s for k in kws):
            return {'album': '', 'category': cat, 'source': 'local',
                    'title': '', 'snippet': album_label}
    # 2) 已知 IP / 作品 → 中文真名即专辑，并给分类
    for ip, cat in _LOCAL_IP_TAG.items():
        if re.search(re.escape(ip), s, re.IGNORECASE):
            return {'album': ip, 'category': cat, 'source': 'local',
                    'title': ip, 'snippet': ip}
    return None


# 中文昵称/译名 → 拉丁原名，提升 iTunes 命中率（仅作优先尝试，不覆盖原始串）。
_ARTIST_NICKNAME = {
    'a妹': 'Ariana Grande', '阿妹': 'Ariana Grande', '阿里安娜': 'Ariana Grande',
    '刘宪华': 'Henry Lau',
    '邓紫棋': 'G.E.M.', 'g.e.m.': 'G.E.M.',
    '碧梨': 'Billie Eilish', '比莉艾利什': 'Billie Eilish',
    '霉霉': 'Taylor Swift', '泰勒斯威夫特': 'Taylor Swift',
    '周杰伦': 'Jay Chou',
    '林俊杰': 'JJ Lin',
    '陈奕迅': 'Eason Chan',
    '李荣浩': 'Li Ronghao', '薛之谦': 'Joker Xue',
    '毛不易': 'Mao Buyi', '张杰': 'Zhang Jie', '华晨宇': 'Hua Chenyu',
    '五月天': 'Mayday', '孙燕姿': 'Stefanie Sun', '梁静茹': 'Fish Leong',
    '蔡依林': 'Jolin Tsai', '林宥嘉': 'James Yu', '苏打绿': 'Sodagreen',
    '初音': 'Hatsune Miku', '初音未来': 'Hatsune Miku', 'vocaloid': 'Hatsune Miku',
    '米津玄师': 'Kenshi Yonezu',
    'radwimps': 'RADWIMPS', '野田洋次郎': 'RADWIMPS',
    '菅野洋子': 'Yoko Kanno', 'supercell': 'supercell',
    'aimer': 'Aimer', 'lisa': 'LiSA',
}
# iTunes 流派 → 本系统分类（仅当本地无分类时作兜底）
_ITUNES_GENRE_CAT = {
    'anime': '动画', 'childrens music': '动画', 'soundtrack': '影视',
    'jazz': '', 'pop': '', 'rock': '', 'electronic': '', 'classical': '影视',
}
# 「作品类」流派：动漫/影视原声/动画儿歌等——其英文 collectionName 是原声集名，
# 不符合主上「专辑必须中文可读」诉求，遇此类型不把英文专辑名填入 album（留待中文词库/手填）。
_ITUNES_WORK_GENRES = {'anime', 'soundtrack', 'childrens music'}


def _title_sim(a: str, b: str) -> float:
    """曲名相似度 0~1：归一化(去非字母数字)后 全等=1.0，包含=0.85，编辑距离=ratio*0.6。"""
    na = re.sub(r'[^a-z0-9\u4e00-\u9fff]', '', a.lower())
    nb = re.sub(r'[^a-z0-9\u4e00-\u9fff]', '', b.lower())
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.85
    from difflib import SequenceMatcher
    return SequenceMatcher(None, na, nb).ratio() * 0.6


def _itunes_album(q: str, timeout: float = 3, title_hint: str = '',
                  artist_hints=()):
    """iTunes Search 取真实专辑(collectionName)；多结果按「曲名相似度+歌手命中」择优。

    返回最优结果 dict（含内部 '_score'）或 None。keyless、快。
    """
    params = {'term': q, 'entity': 'song', 'limit': 15, 'country': 'US'}
    url = ITUNES_URL + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode('utf-8', 'ignore'))
    except Exception:
        return None
    results = data.get('results') or []
    if not results:
        return None
    hints = [h.lower() for h in artist_hints if h]
    best, best_score = None, -1.0
    for top in results:
        album = (top.get('collectionName') or '').strip()
        if not album:
            continue
        track = (top.get('trackName') or '').strip()
        score = _title_sim(title_hint, track)
        # 翻唱/卡拉OK/伴奏集 降权（优先原唱原专），genre 一并计入
        hay = (album + ' ' + track + ' ' + (top.get('primaryGenreName') or '')).lower()
        if any(k in hay for k in ['karaoke', 'カラオケ', '伴奏', 'cover', 'cover songs',
                                  'tribute', '乐器演奏', 'instrumental']):
            score -= 0.4
        artist = (top.get('artistName') or '').strip()
        if hints:
            alow = artist.lower()
            if any(h in alow or alow in h for h in hints):
                score += 0.9
                # 脚本跨界（中文/日文标题 ↔ 罗马音曲名）且歌手命中 → 视为同一曲补分
                # （如 前前前世 ↔ Zenzenzense，原唱 RADWIMPS 才会吃到此加成）
                has_cjk_t = bool(re.search(r'[\u4e00-\u9fff\u3040-\u30ff]', title_hint))
                has_cjk_r = bool(re.search(r'[\u4e00-\u9fff\u3040-\u30ff]', track))
                if has_cjk_t != has_cjk_r:
                    score += 0.5
        if score > best_score:
            best_score = score
            genre = (top.get('primaryGenreName') or '').strip().lower()
            best = {
                'album': album,
                'category': _ITUNES_GENRE_CAT.get(genre, ''),
                '_is_work': genre in _ITUNES_WORK_GENRES,
                'source': 'itunes',
                'title': track,
                'snippet': f"{artist} — {album} ({top.get('primaryGenreName', '')})".strip(' ()'),
                '_score': score,
            }
    return best


def _wiki_search(query: str, api: str, timeout: float = 3) -> dict:
    """按 MediaWiki 搜索 API 取结果（仅用于分类兜底，不臆造专辑名）。

    此前该函数缺失，导致 _wiki_category 调用处抛 NameError 被外层 except 吞掉、
    Wikipedia 兜底分支静默失效。此处补上真实实现：失败/异常由调用方兜底。
    """
    params = {'action': 'query', 'list': 'search', 'srsearch': query,
              'format': 'json', 'utf8': 1, 'srlimit': 3}
    url = api + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


def _wiki_category(q: str, timeout: float = 3):
    """联网取分类(动画/游戏/电影/影视) 仅作兜底标签；不臆造专辑名。"""
    for api in (WIKI_ZH, WIKI_EN):
        try:
            data = _wiki_search(q, api, timeout)
            for item in data.get('query', {}).get('search', []):
                snippet = (item.get('snippet', '') + ' ' + item.get('title', '')).lower()
                for kws, cat, _ in _LOCAL_MEDIA_KEYWORDS:
                    if any(k.lower() in snippet for k in kws):
                        return cat
        except Exception:
            continue
    return ''


def _expand_artist(artist: str):
    """返回候选歌手串列表（保序去重）：原始 → 全展开 → 逐人展开。提升 iTunes 命中率。

    多歌手串（如「刘宪华、A妹」）整体查询常令 iTunes 失配，故逐段昵称展开后，
    额外产出「仅用单人展开名」的候选（如 'Ariana Grande'），单名更易命中真实专辑。
    """
    if not artist:
        return ['']
    out = [artist.strip()]
    pieces = [p.strip() for p in re.split(r'[、,&＆/]', artist) if p.strip()]
    expanded_pieces = []
    for p in pieces:
        low = p.lower()
        real = ''
        for nick, r in _ARTIST_NICKNAME.items():
            if not r:
                continue
            nlow = nick.lower()
            if nlow == low:
                real = r
                break
            # 拉丁昵称按词边界匹配（防 Elisa→LiSA 误命中）；CJK 昵称子串匹配
            if re.search(r'(?<![a-z0-9])' + re.escape(nlow) + r'(?![a-z0-9])', low):
                real = r
                break
            if re.search(r'[\u4e00-\u9fff]', nick) and nlow in low:
                real = r
                break
        if real:
            # 大小写不敏感替换昵称段（A妹 → Ariana Grande）
            expanded_pieces.append(
                re.sub(re.escape(nick), real, p, flags=re.IGNORECASE))
        else:
            expanded_pieces.append(p)
    out.append('、'.join(expanded_pieces))
    out.extend(expanded_pieces)
    seen = set()
    out2 = []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            out2.append(c)
    return out2


def album_tag(title: str, artist: str = '', timeout: float = 3,
              use_cache: bool = True, use_local: bool = True):
    """返回 {'album','category','source','title','snippet'} 或 None。

    策略：本地词库兜底(即时离线) → iTunes 联网取真实专辑(带磁盘缓存) → Wikipedia 兜底分类。
    album 优先取 iTunes 真实专辑名（即原曲所属专辑/原声集），本地 IP 真名作兜底；
    category 优先取本地 IP 分类，否则 iTunes 流派映射，最后 Wikipedia 兜底。
    绝不返回「假死」：联网失败/无果则回退本地兜底。
    """
    q = ' '.join(x for x in [title, artist] if x).strip()
    if not q:
        return None
    # 1) 本地兜底（即时，不阻塞）
    local = _local_album_tag(title, artist) if use_local else None
    local_cat = local.get('category', '') if local else ''
    # 2) 联网（缓存命中则跳过网络）
    if use_cache:
        cached = _album_cache_get(q)
        if cached is not _MISS:
            res = dict(cached)
            if local_cat and not res.get('category'):
                res['category'] = local_cat
            return res
    # 3) iTunes 优先（昵称展开多候选，跨候选取相似度最优，杜绝「首中即劣质」）
    online = None
    expanded = _expand_artist(artist)
    for art in expanded:
        cand = _itunes_album((title + ' ' + art).strip(), timeout,
                             title_hint=title, artist_hints=expanded)
        if cand and (online is None or cand.get('_score', -1) > online.get('_score', -1)):
            online = cand
    if online:
        res = dict(online)
        res.pop('_score', None)
        res.pop('_is_work', None)
        # 专辑名合并（主上可读性诉求：作品类主题曲绝不显示英文 collectionName）
        if online.get('_is_work'):
            # 动漫/影视/儿歌等原声作品 → album 用中文来源名（本地/文件名）；无则留空待手填
            res['album'] = local.get('album', '') if local else ''
        # 单曲类（Pop/Rock 等）→ 保留 iTunes 真实英文专辑名（如 7 rings → thank u, next）
        res['category'] = local_cat or online.get('category', '')
        res['source'] = 'itunes' if not local_cat else 'itunes+local'
        if use_cache:
            _album_cache_set(q, res)
        return res
    # 4) Wikipedia 兜底分类（不臆造专辑名）
    if local_cat and local:
        return local
    wiki_cat = _wiki_category(q, timeout) if use_local else ''
    if wiki_cat:
        base = local or {'album': '', 'category': '', 'source': 'wiki',
                         'title': '', 'snippet': ''}
        base = dict(base)
        base['category'] = wiki_cat
        base['source'] = 'wiki'
        if use_cache:
            _album_cache_set(q, base)
        return base
    return local


def album_tag_batch(items, max_workers: int = 6, timeout: float = 3,
                    use_cache: bool = True):
    """并发补全一批 (title, artist) 的专辑。items 可为 [dict|tuple]，返回与输入等长的列表。"""
    if not items:
        return []
    norm = []
    for it in items:
        if isinstance(it, dict):
            norm.append((it.get('title', '') or '', it.get('artist', '') or ''))
        else:
            t = it[0] if len(it) > 0 else ''
            a = it[1] if len(it) > 1 else ''
            norm.append((str(t), str(a)))
    from concurrent.futures import ThreadPoolExecutor

    def _one(tup):
        return album_tag(tup[0], tup[1], timeout=timeout, use_cache=use_cache)

    n = max(1, min(max_workers, len(norm)))
    with ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(_one, norm))


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
            # 巡检插入信息：把曲名/歌手/专辑写入 PDF /Info（等价于 MP3 的 ID3）
            try:
                nb = os.path.splitext(new)[0]
                mt, ma, malb = smart_split(os.path.basename(nb))
                if mt or ma or malb:
                    write_pdf_metadata(dst, title=mt or None, artist=ma or None,
                                       album=malb or None)
            except Exception:
                pass
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

    tb = sub.add_parser('thumb_batch', help='批量缩略图 base64（一次进程多张）')
    tb.add_argument('--dir', required=True)
    tb.add_argument('--rels', required=True,
                    help='JSON 数组：["rel1","rel2",...]')

    ins = sub.add_parser('inspect', help='扫描曲库并给出命名建议')
    ins.add_argument('--dir', required=True)

    rn = sub.add_parser('rename', help='执行批量重命名')
    rn.add_argument('--dir', required=True)
    rn.add_argument('--payload', required=True,
                    help='JSON 数组：[{"old":"原","new":"新"}]')

    wt = sub.add_parser('albumtag', help='联网识别真实专辑归属')
    wt.add_argument('--title', default='')
    wt.add_argument('--artist', default='')

    wb = sub.add_parser('albumbatch', help='批量联网补全专辑（并发）')
    wb.add_argument('--items', default='',
                    help='JSON 数组；省略时从 stdin 读取（规避 Windows 命令行 32767 字符上限）')

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
        elif args.action == 'thumb_batch':
            rels = json.loads(args.rels)
            print(json.dumps(get_thumbs_batch(args.dir, rels), ensure_ascii=False))
        elif args.action == 'inspect':
            rows = inspect_library(args.dir)
            if not rows:
                _write_empty_diag(args.dir)
            print(json.dumps(rows, ensure_ascii=False))
        elif args.action == 'rename':
            pairs = json.loads(args.payload)
            print(json.dumps(rename_items(args.dir, pairs), ensure_ascii=False))
        elif args.action == 'albumtag':
            print(json.dumps(album_tag(args.title, args.artist), ensure_ascii=False))
        elif args.action == 'albumbatch':
            raw = args.items
            if not raw.strip():
                raw = sys.stdin.read()  # stdin 传参（规避命令行长度上限）
            # 诊断：把收到的字节数/解析状态写 stderr（前端 PyResult.stderr 可显形，定位 0 命中）
            try:
                items = json.loads(raw) if raw.strip() else []
                diag = f"[albumbatch] stdin_bytes={len(raw.encode('utf-8', 'ignore'))} items={len(items)}"
            except Exception as je:
                items = []
                diag = f"[albumbatch] JSON解析失败({je}) stdin_bytes={len(raw.encode('utf-8', 'ignore'))}"
            sys.stderr.write(diag + "\n")
            print(json.dumps(album_tag_batch(items), ensure_ascii=False))
        else:
            ap.print_help()
    except Exception as e:
        # 任何异常都输出安全 JSON，绝不留空输出（杜绝前端 JSON.parse 崩溃）
        sys.stderr.write(f"[library_ops] action={args.action} error: {e}\n")
        # meta/inspect 返回 []；thumb 返回空串；rename/albumtag 返回 null
        if args.action in ('meta', 'inspect', 'albumbatch'):
            print('[]')
        elif args.action == 'thumb':
            print('')
        else:
            print('null')


if __name__ == '__main__':
    main()
