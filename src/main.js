// Score Studio · 前端逻辑
// 同时服务于两种运行时：
//   1. Tauri（无边框窗口）：调用 invoke('process_scores') / plugin-dialog
//   2. 本地 Python 服务器（run_local.py）：调用 /api/process、/api/library
// 通过 window.__TAURI_INTERNALS__ 自动判别，单一代码源。

const IS_TAURI = typeof window !== 'undefined' && !!window.__TAURI_INTERNALS__;
// Tauri 运行时铺满窗口；本地服务器模式以全屏 app 形态呈现
if (IS_TAURI) document.body.classList.add('tauri');
if (!IS_TAURI) document.body.classList.add('appmode');

const $ = (id) => document.getElementById(id);
const crumbSub = $('crumbSub');
const queueEl = $('queue');
const dirBox = $('dirBox');
const statText = $('statText');
const logBox = $('logBox');

// ============ 目录持久化（排除「重启重置默认目录」的混淆源） ============
const DIR_KEY = 'scorestudio.dir';
try {
  const saved = localStorage.getItem(DIR_KEY);
  if (saved) dirBox.textContent = saved;
} catch (_) {}
function saveDir() {
  try { localStorage.setItem(DIR_KEY, dirBox.textContent); } catch (_) {}
}

let themeOn = true;

// ============ 视图切换 ============
document.querySelectorAll('.nav-item').forEach((n) => {
  n.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach((x) => x.classList.remove('active'));
    n.classList.add('active');
    const v = n.dataset.view;
    ['process', 'library', 'inspect', 'settings'].forEach((id) => {
      $('view-' + id).classList.toggle('hidden', id !== v);
    });
    const crumbMap = {
      process: '提取与标准',
      library: '已归档曲谱',
      inspect: '命名规范化',
      settings: '工坊形制',
    };
    crumbSub.textContent = crumbMap[v] || '工坊形制';
    if (v === 'library') loadLibrary();
  });
});

// ============ 窗口控制（仅 Tauri） ============
if (IS_TAURI) {
  import('@tauri-apps/api/window').then(({ getCurrentWindow }) => {
    const w = getCurrentWindow();
    document.querySelector('.btn.min').onclick = () => w.minimize();
    document.querySelector('.btn.max').onclick = () => w.toggleMaximize();
    document.querySelector('.btn.close').onclick = () => w.close();
  });
}

// ============ 队列 ============
let qid = 0;
function addItem(t, s, src, theme, input) {
  const el = document.createElement('div');
  el.className = 'q';
  el.dataset.input = input || '';
  el.innerHTML = `
    <div class="thumb">谱</div>
    <div class="meta">
      <div class="ttl">${t}${theme ? `<span class="tag">${theme}</span>` : ''}</div>
      <div class="src">${s} · 来源 ${src}</div>
    </div>
    <div class="st">待处理</div>
    <div class="bar"><i></i></div>`;
  queueEl.appendChild(el);
  statText.textContent = `队列中 ${queueEl.children.length} 项`;
  return el;
}

$('addBtn').onclick = () => {
  const v = $('linkInput').value.trim();
  if (!v) return;
  addItem('来自链接的曲谱', '解析中…', '链接', '', v);
  $('linkInput').value = '';
};
$('clearBtn').onclick = () => {
  if (!queueEl.children.length) return;
  queueEl.innerHTML = '';
  statText.textContent = '队列已清空';
};

// 拖拽
const drop = $('drop');
['dragover', 'dragenter'].forEach((e) =>
  drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add('hover'); }));
['dragleave', 'drop'].forEach((e) =>
  drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove('hover'); }));
// ============ 本地文件上传（本地模式无真实路径 → b64 上传到服务器临时目录） ============
const filePicker = document.createElement('input');
filePicker.type = 'file';
filePicker.multiple = true;
filePicker.accept = 'image/*,application/pdf';
filePicker.style.display = 'none';
document.body.appendChild(filePicker);

async function readB64(f) {
  return await new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result);
    r.onerror = () => rej(r.error);
    r.readAsDataURL(f);
  });
}

async function uploadOne(f) {
  if (IS_TAURI) {
    // Tauri 模式：原生对话框直接拿真实路径，无需上传
    return null;
  }
  const b64 = await readB64(f);
  const r = await fetch('/api/upload', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: f.name, data: b64 }),
  });
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || '上传失败');
  return j.path;
}

async function pickFiles() {
  if (IS_TAURI) {
    const { open } = await import('@tauri-apps/plugin-dialog');
    const p = await open({
      multiple: true,
      filters: [{ name: '曲谱', extensions: ['png', 'jpg', 'jpeg', 'pdf', 'webp', 'bmp'] }],
    });
    if (p) {
      const paths = Array.isArray(p) ? p : [p];
      paths.forEach((f) =>
        addItem(f.split(/[\\/]/).pop().replace(/\.[^.]+$/, ''), '本地文件 · 真实路径', '点选', '', f));
    }
    return;
  }
  filePicker.click();
}

filePicker.addEventListener('change', async () => {
  const files = [...filePicker.files];
  filePicker.value = '';
  for (const f of files) {
    try {
      statText.textContent = '上传中… ' + f.name;
      const path = await uploadOne(f);
      addItem(f.name.replace(/\.[^.]+$/, ''), '本地文件 · 已上传', '点选/拖入', '', path);
      statText.textContent = '已收入：' + f.name;
    } catch (e) {
      statText.textContent = '上传失败：' + f.name + ' · ' + e;
    }
  }
});

drop.addEventListener('drop', async (ev) => {
  ev.preventDefault();
  drop.classList.remove('hover');
  const files = [...ev.dataTransfer.files];
  if (IS_TAURI) {
    files.forEach((f) => addItem(f.name.replace(/\.[^.]+$/, ''), '本地文件', '拖入', '', f.path || f.name));
  } else {
    for (const f of files) {
      try {
        statText.textContent = '上传中… ' + f.name;
        const path = await uploadOne(f);
        addItem(f.name.replace(/\.[^.]+$/, ''), '本地文件 · 已上传', '拖入', '', path);
        statText.textContent = '已收入：' + f.name;
      } catch (e) {
        statText.textContent = '上传失败：' + f.name + ' · ' + e;
      }
    }
  }
});
drop.addEventListener('click', pickFiles);

// ============ 队列曲名双击编辑（无标题链接时可补名） ============
const finishEdit = (ttl, q) => {
  ttl.contentEditable = 'false';
  ttl.innerText = ttl.innerText.replace(/\s+/g, ' ').trim() || '未命名';
  q.dataset.edited = '1';
  statText.textContent = '曲名已改：' + ttl.innerText;
};
queueEl.addEventListener('dblclick', (ev) => {
  const ttl = ev.target.closest('.ttl');
  if (!ttl) return;
  ttl.querySelectorAll('.tag').forEach((t) => t.remove());
  ttl.contentEditable = 'true';
  ttl.focus();
  const sel = window.getSelection();
  sel.selectAllChildren(ttl);
});
queueEl.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' || ev.key === 'Escape') {
    const ttl = ev.target.closest('.ttl');
    if (ttl && ttl.isContentEditable) { ev.preventDefault(); finishEdit(ttl, ttl.closest('.q')); ttl.blur(); }
  }
});
queueEl.addEventListener('focusout', (ev) => {
  const ttl = ev.target.closest('.ttl');
  if (ttl && ttl.isContentEditable) finishEdit(ttl, ttl.closest('.q'));
});

// ============ 目录选择 ============
const DIR_PICKS = ['G:\\Lin_File\\Documents\\曲谱', 'G:\\Lin_File\\Documents\\曲谱\\归档_已发送给老公', 'D:\\Music\\小提琴谱', 'G:\\Lin_File\\Documents\\曲谱\\归档'];
$('dirBtn').onclick = async () => {
  if (IS_TAURI) {
    const { open } = await import('@tauri-apps/plugin-dialog');
    const r = await open({ directory: true, defaultPath: dirBox.textContent });
    if (r) dirBox.textContent = r;
  } else {
    const next = DIR_PICKS[(DIR_PICKS.indexOf(dirBox.textContent) + 1) % DIR_PICKS.length];
    dirBox.textContent = next;
  }
  saveDir();
  statText.textContent = '输出目录：' + dirBox.textContent;
  // 正在曲库/巡检视图时，切换目录后立即按新目录刷新
  if (!$('view-library').classList.contains('hidden')) loadLibrary();
  if (!$('view-inspect').classList.contains('hidden')) loadInspect();
};

// ============ 主题来源标注开关 ============
$('themeToggle').onclick = () => {
  themeOn = !themeOn;
  $('themeToggle').style.background = themeOn ? 'rgba(201,169,97,0.6)' : 'rgba(143,214,160,0.6)';
  $('themePill').textContent = '主题来源标注 · ' + (themeOn ? '开' : '关');
};

// ============ 后端调用 ============
// 安全解析：后端异常时可能返回空串，直接 JSON.parse 会崩；此处兜底返回 null。
function safeParse(s) {
  if (typeof s !== 'string') return s;
  const t = s.trim();
  if (!t) return null;
  return JSON.parse(t);
}

async function callProcess(payload) {
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    return await invoke('process_scores', payload);
  }
  const r = await fetch('/api/process', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return await r.json();
}

async function callLibrary(dir) {
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    return await invoke('list_library', { dir });
  }
  const r = await fetch('/api/library?dir=' + encodeURIComponent(dir));
  const j = await r.json();
  return j.items || [];
}

async function callLibraryMeta(dir) {
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    const r = await invoke('get_library', { dir });
    // 新契约：{ok, out, error, code, stderr}——失败/空结果时前端可见 stderr 诊断，杜绝静默空
    if (r && r.ok === false) {
      throw new Error((r.error || '后端异常') + (r.code != null ? '（退出码 ' + r.code + '）' : ''));
    }
    const items = safeParse(r && r.out != null ? r.out : r);
    return { items: Array.isArray(items) ? items : [], stderr: (r && r.stderr) || '' };
  }
  const res = await (await fetch('/api/library_meta?dir=' + encodeURIComponent(dir))).json();
  const parsed = safeParse(res);
  return { items: Array.isArray(parsed) ? parsed : [], stderr: '' };
}

// 按需获取单张缩略图（懒加载）：Tauri 走 get_thumb（返回 base64 串），本地走 /api/thumb（纯文本 base64）。
async function callThumb(dir, name) {
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    const res = await invoke('get_thumb', { dir, name });
    return typeof res === 'string' ? res : '';
  }
  const r = await fetch('/api/thumb?dir=' + encodeURIComponent(dir) + '&name=' + encodeURIComponent(name));
  const t = await r.text();
  return t.trim();
}

// 批量缩略图：一次后端进程渲染多张（滚动加载性能关键，杜绝逐张启 Python 子进程闪窗口）。
// Tauri 走 get_thumbs_batch（返回 {ok,out:{rel:b64},...}），本地走 POST /api/thumb_batch。
async function callThumbsBatch(dir, relsArr) {
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    const r = await invoke('get_thumbs_batch', { dir, rels: JSON.stringify(relsArr) });
    if (r && r.ok === false) throw new Error((r.error || '后端异常') + (r.code != null ? '（退出码 ' + r.code + '）' : ''));
    const items = safeParse(r && r.out != null ? r.out : r);
    return (items && typeof items === 'object') ? items : {};
  }
  const r = await fetch('/api/thumb_batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dir, rels: relsArr }),
  });
  const j = await r.json();
  return (j && typeof j === 'object') ? j : {};
}

async function callInspectLibrary(dir) {
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    const r = await invoke('inspect_library', { dir });
    if (r && r.ok === false) {
      throw new Error((r.error || '后端异常') + (r.code != null ? '（退出码 ' + r.code + '）' : ''));
    }
    const items = safeParse(r && r.out != null ? r.out : r);
    return { items: Array.isArray(items) ? items : [], stderr: (r && r.stderr) || '' };
  }
  const res = await (await fetch('/api/inspect?dir=' + encodeURIComponent(dir))).json();
  const parsed = safeParse(res);
  return { items: Array.isArray(parsed) ? parsed : [], stderr: '' };
}

async function callRenameItems(dir, pairs) {
  let res;
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    res = await invoke('rename_items', { dir, payload: JSON.stringify(pairs) });
  } else {
    const r = await fetch('/api/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir, pairs }),
    });
    res = await r.json();
  }
  return safeParse(res) || { results: [] };
}

async function callWikiTag(title, artist) {
  let res;
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    res = await invoke('wiki_tag', { title, artist });
  } else {
    const r = await fetch('/api/wikitag?title=' + encodeURIComponent(title) + '&artist=' + encodeURIComponent(artist));
    res = await r.json();
  }
  return safeParse(res);
}

async function openOutputDir() {
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    try { await invoke('open_path', { path: dirBox.textContent }); } catch (_) {}
  } else {
    statText.textContent = '本地模式请用文件管理器打开：' + dirBox.textContent;
  }
}
$('openOutBtn').onclick = openOutputDir;

// ============ 开始处理 ============
$('runBtn').onclick = async () => {
  const items = [...queueEl.children];
  if (!items.length) { statText.textContent = '队列为空，请先收入链接'; return; }
  logBox.classList.remove('hidden');
  logBox.textContent = '';
  let done = 0;
  for (const el of items) {
    const input = el.dataset.input || dirBox.textContent;
    const ttlEl = el.querySelector('.ttl');
    const customName = el.dataset.edited
      ? (ttlEl ? ttlEl.innerText.replace(/\s+/g, ' ').trim() : '')
      : '';
    const st = el.querySelector('.st');
    const bar = el.querySelector('.bar i');
    st.textContent = '处理中'; st.style.color = 'var(--gold-2)';
    try {
      const res = await callProcess({
        input,
        outputDir: dirBox.textContent,
        theme: themeOn ? (el.querySelector('.tag') ? el.querySelector('.tag').textContent : '') : '',
        name: customName,
      });
      if (res && res.ok) {
        st.textContent = '已完成'; st.classList.add('done'); st.classList.remove('err');
        bar.style.width = '100%';
        logBox.textContent += `✓ ${res.path}\n${res.log || ''}\n`;
      } else {
        st.textContent = '失败'; st.classList.add('err'); st.classList.remove('done');
        logBox.textContent += `✗ ${res && res.error ? res.error : '未知错误'}\n${res && res.log ? res.log : ''}\n`;
        if (res && res.error && res.error.includes('无法自动命名')) {
          statText.textContent = '页面无标题，请双击该曲目名称补名后重试';
        }
      }
    } catch (e) {
      st.textContent = '失败'; st.classList.add('err');
      logBox.textContent += `✗ 调用异常：${e}\n`;
    }
    done++;
    statText.textContent = `已完成 ${done}/${items.length}`;
  }
};

// ============ 曲库 ============
let libItems = [];
let activeArtist = '全部';

function fmtBytes(b) {
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
  return (b / (1024 * 1024)).toFixed(2) + ' MB';
}

// rel 为相对目录的路径（'/' 分隔，可能含子目录）；拼成本机完整路径
function fullPath(rel) {
  const d = dirBox.textContent.replace(/[\\/]+$/, '');
  const sep = d.includes('/') ? '/' : '\\';
  return d + sep + rel.replace(/\//g, sep);
}

async function openPdf(rel) {
  const p = fullPath(rel);
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    await invoke('open_path', { path: p });
  } else {
    window.open('/api/file?path=' + encodeURIComponent(p), '_blank');
  }
}

// ============ 懒加载缩略图（批次 + 节流 + 内存缓存） ============
// 性能关键：旧实现逐张 callThumb（每张 = 1 次 Python 子进程启动 → 疯狂闪窗口 + 卡顿）。
// 新实现：进入视口并入队，150ms 节流合并为一次批请求（20 张/批分片，单进程渲染），
// 命中内存缓存直接填充，滚回不再重取。
const THUMB_BATCH = 20;
const THUMB_CACHE = new Map(); // rel -> b64
let thumbObserver = null;
let thumbQueue = new Set(); // 待加载的 cover-wrap 元素
let thumbTimer = null;

function ensureThumbObserver() {
  if (thumbObserver) return thumbObserver;
  thumbObserver = new IntersectionObserver((entries, obs) => {
    entries.forEach((en) => {
      if (!en.isIntersecting) return;
      const wrap = en.target;
      obs.unobserve(wrap);
      queueThumb(wrap);
    });
  }, { rootMargin: '240px' });
  return thumbObserver;
}

function fillThumb(wrap, b64) {
  const img = document.createElement('img');
  img.className = 'cover-img';
  img.loading = 'lazy';
  img.alt = '';
  img.src = 'data:image/jpeg;base64,' + b64;
  const ph = wrap.querySelector('.cover');
  if (ph) wrap.replaceChild(img, ph);
}

function queueThumb(wrap) {
  const rel = wrap.dataset.name;
  if (!rel || wrap.dataset.loaded) return;
  if (THUMB_CACHE.has(rel)) {
    wrap.dataset.loaded = '1';
    fillThumb(wrap, THUMB_CACHE.get(rel));
    return;
  }
  wrap.dataset.loaded = '1'; // 防重复入队
  thumbQueue.add(wrap);
  if (thumbTimer) return;
  thumbTimer = setTimeout(flushThumbs, 150);
}

async function flushThumbs() {
  thumbTimer = null;
  const wraps = [...thumbQueue];
  thumbQueue.clear();
  if (!wraps.length) return;
  const rels = [...new Set(wraps.map((w) => w.dataset.name))];
  // 按 20 张分片串行请求，滚动到底也渐进填充
  for (let i = 0; i < rels.length; i += THUMB_BATCH) {
    const slice = rels.slice(i, i + THUMB_BATCH);
    let map = {};
    try {
      map = await callThumbsBatch(dirBox.textContent, slice);
    } catch (_) {
      map = {};
    }
    wraps.forEach((w) => {
      const rel = w.dataset.name;
      if (!slice.includes(rel) || !map[rel]) return;
      THUMB_CACHE.set(rel, map[rel]);
      fillThumb(w, map[rel]);
    });
  }
}

function renderLibrary(items) {
  const lib = $('libGrid');
  if (!items.length) {
    lib.innerHTML = '<div class="hero-p">没有匹配的曲谱。</div>';
    return;
  }
  lib.innerHTML = '';
  const obs = ensureThumbObserver();
  items.forEach((it) => {
    const rel = it.rel || it.name; // 兼容旧索引缺 rel 的场景
    const c = document.createElement('div');
    c.className = 'card';
    c.title = rel;
    c.dataset.name = rel;
    // 占位封面（真实缩略图由懒加载填充）
    const cover = '<div class="cover">谱</div>';
    const themeChip = it.theme ? `<span class="theme-chip">${it.theme}</span>` : '';
    const meta = `<div class="meta-row"><span>${it.pages || '?'} 页</span><span>${fmtBytes(it.size)}</span></div>`;
    c.innerHTML = `
      <div class="cover-wrap" data-name="${rel}">${cover}<div class="cover-overlay"><span class="open-hint">打开 PDF</span></div></div>
      <div class="cap">
        <div class="n">${it.title || it.name.replace(/\.pdf$/i, '')}</div>
        <div class="a">${it.artist || '古典'} ${themeChip}</div>
        ${meta}
      </div>`;
    c.addEventListener('click', () => openPdf(rel));
    lib.appendChild(c);
    obs.observe(c.querySelector('.cover-wrap'));
  });
}

function buildChips() {
  const artists = Array.from(new Set(libItems.map((i) => i.artist).filter(Boolean))).sort();
  const wrap = $('libChips');
  wrap.innerHTML = '';
  const allBtn = document.createElement('button');
  allBtn.className = 'chip' + (activeArtist === '全部' ? ' active' : '');
  allBtn.textContent = '全部';
  allBtn.onclick = () => { activeArtist = '全部'; buildChips(); filterLibrary(); };
  wrap.appendChild(allBtn);
  artists.forEach((a) => {
    const b = document.createElement('button');
    b.className = 'chip' + (activeArtist === a ? ' active' : '');
    b.textContent = a;
    b.onclick = () => { activeArtist = a; buildChips(); filterLibrary(); };
    wrap.appendChild(b);
  });
}

function filterLibrary() {
  const q = $('libSearch').value.trim().toLowerCase();
  const filtered = libItems.filter((it) => {
    const hit = !q || [it.title, it.artist, it.theme, it.name.replace(/\.pdf$/i, '')]
      .some((s) => s && s.toLowerCase().includes(q));
    const artistOk = activeArtist === '全部' || it.artist === activeArtist;
    return hit && artistOk;
  });
  renderLibrary(filtered);
}

async function loadLibrary() {
  const lib = $('libGrid');
  lib.innerHTML = '<div class="hero-p">读取中…</div>';
  $('libChips').innerHTML = '';
  const dir = dirBox.textContent;
  try {
    const raw = await callLibraryMeta(dir);
    libItems = raw.items;
    if (!libItems.length) {
      // 诊断显形：显示后端 stderr 三态诊断（isdir / pdf_recursive）而非静默占位
      const diag = raw.stderr
        ? `<br><span class="muted">后端诊断：<br>${String(raw.stderr).replace(/</g, '&lt;')}</span>`
        : '';
      lib.innerHTML = `<div class="hero-p">「${dir}」暂无 PDF。<br><span class="muted">已启用递归扫描（子目录内 PDF 也会列出）。若确认此处确有 PDF 却仍为空，请把本诊断截图回传：<br>返回条数：${libItems.length}${diag}</span></div>`;
      statText.textContent = '曲库为空：' + dir;
      return;
    }
    activeArtist = '全部';
    buildChips();
    filterLibrary();
  } catch (e) {
    lib.innerHTML = `<div class="hero-p">曲库读取失败（后端异常已显形）：<br>${e && e.message ? e.message : e}<br><span class="muted">目录：${dir}<br>若反复失败请截图本诊断回传。</span></div>`;
    statText.textContent = '曲库读取失败：' + (e && e.message ? e.message : e);
  }
}

$('libSearch').addEventListener('input', filterLibrary);

// ============ 巡检 ============
let inspRows = [];
let inspSelected = new Set();

// rel 可能含子目录前缀；改名必须保留原目录，否则会把文件挪到根目录
function renamePair(r) {
  if (!r || !r.needs_rename || !r.suggested) return null;
  const rel = r.rel || r.name;
  if (!rel || r.suggested === rel) return null;
  const i = rel.lastIndexOf('/');
  const newRel = i >= 0 ? rel.slice(0, i + 1) + r.suggested : r.suggested;
  return { old: rel, new: newRel };
}

function updateApplyButton() {
  const pairs = [];
  inspSelected.forEach((idx) => {
    const p = renamePair(inspRows[idx]);
    if (p) pairs.push(p);
  });
  $('applyCount').textContent = pairs.length;
  $('applyBtn').disabled = pairs.length === 0;
}

function renderInspect() {
  const wrap = $('inspWrap');
  if (!inspRows.length) {
    wrap.innerHTML = '<div class="hero-p">点击「扫描曲库」开始巡检。</div>';
    return;
  }
  let html = '<div class="insp-head"><span>当前文件名</span><span>解析结果</span><span>首页文字摘要</span><span>建议新名</span><span>主题标签</span><span>采纳</span></div>';
  html += '<div class="insp-list">';
  inspRows.forEach((r, i) => {
    const checked = inspSelected.has(i) ? 'checked' : '';
    const cur = `<b>${r.cur_title || '?'}</b><br><span class="sub">${r.cur_artist || '未知歌手'}${r.cur_theme ? ' · ' + r.cur_theme : ''}</span>`;
    const det = r.has_text
      ? `<span class="ok">✓ 检测到文字</span><br><b>${r.det_title || '—'}</b> / ${r.det_artist || '—'}`
      : '<span class="warn">扫描件/无文字</span>';
    const excerpt = `<div class="excerpt" title="${(r.text_excerpt || '').replace(/"/g, '&quot;')}">${r.text_excerpt || '—'}</div>`;
    const tag = r.sug_theme
      ? `<span class="theme-chip">${r.sug_theme}</span>`
      : '<span class="muted">无</span>';
    const rowClass = r.needs_rename ? 'needs' : 'ok';
    html += `
      <div class="insp-row ${rowClass}" data-idx="${i}">
        <div class="cell-name" title="${r.name}">${r.name}</div>
        <div class="cell-cur">${cur}</div>
        <div class="cell-text">${det}<br>${excerpt}</div>
        <div class="cell-sug" title="${r.suggested}">${r.suggested}</div>
        <div class="cell-tag" data-tag-idx="${i}">${tag}</div>
        <div class="cell-check"><input type="checkbox" ${checked} ${!r.needs_rename ? 'disabled' : ''}></div>
      </div>`;
  });
  html += '</div>';
  wrap.innerHTML = html;

  wrap.querySelectorAll('.insp-row input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener('change', (ev) => {
      const idx = Number(ev.target.closest('.insp-row').dataset.idx);
      if (ev.target.checked) inspSelected.add(idx);
      else inspSelected.delete(idx);
      updateApplyButton();
    });
  });
}

async function loadInspect() {
  const wrap = $('inspWrap');
  wrap.innerHTML = '<div class="hero-p">扫描中…</div>';
  inspSelected.clear();
  updateApplyButton();
  try {
    const raw = await callInspectLibrary(dirBox.textContent);
    inspRows = raw.items;
    if (!inspRows.length && raw.stderr) {
      wrap.innerHTML = `<div class="hero-p">该目录无可巡检条目。<br><span class="muted">后端诊断：<br>${String(raw.stderr).replace(/</g, '&lt;')}</span></div>`;
      statText.textContent = '巡检为空：' + dirBox.textContent;
      return;
    }
    renderInspect();
    statText.textContent = `巡检完成：${inspRows.length} 份，待改名 ${inspRows.filter((r) => r.needs_rename).length} 份`;
  } catch (e) {
    wrap.innerHTML = `<div class="hero-p">巡检失败：${e && e.message ? e.message : e}</div>`;
    statText.textContent = '巡检失败';
  }
}

$('scanBtn').addEventListener('click', loadInspect);

$('wikiAllBtn').addEventListener('click', async () => {
  statText.textContent = '正在联网识别主题曲归属…';
  let changed = 0;
  for (let i = 0; i < inspRows.length; i++) {
    const r = inspRows[i];
    if (!r.sug_title) continue;
    try {
      const tag = await callWikiTag(r.sug_title, r.sug_artist);
      if (tag && tag.tag) {
        r.sug_theme = tag.tag;
        const segs = [r.sug_title, r.sug_artist, r.sug_theme].filter((s) => s && s.trim());
        r.suggested = segs.join('-') + '.pdf';
        r.needs_rename = true;
        changed++;
      }
    } catch (_) {
      // 单个失败继续
    }
  }
  renderInspect();
  statText.textContent = `联网识别完成：${changed} 份更新主题标签`;
});

$('applyBtn').addEventListener('click', async () => {
  const pairs = [];
  inspSelected.forEach((idx) => {
    const p = renamePair(inspRows[idx]);
    if (p) pairs.push(p);
  });
  if (!pairs.length) return;
  if (!confirm(`确认对 ${pairs.length} 个文件执行重命名？此操作不可撤销。\n\n${pairs.map((p) => p.old + ' → ' + p.new).join('\n')}`)) return;

  $('applyBtn').disabled = true;
  statText.textContent = '正在重命名…';
  try {
    const res = await callRenameItems(dirBox.textContent, pairs);
    const okCount = res.results.filter((x) => x.ok).length;
    const failCount = res.results.length - okCount;
    statText.textContent = `重命名完成：成功 ${okCount}，失败 ${failCount}`;
    if (failCount) {
      console.error(res.results.filter((x) => !x.ok));
    }
    // 刷新
    await loadInspect();
    if ($('view-library').classList.contains('hidden') === false) await loadLibrary();
  } catch (e) {
    statText.textContent = '重命名失败：' + e;
  }
});
