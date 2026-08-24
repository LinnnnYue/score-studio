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
// HTML 转义：凡拼入 innerHTML 的动态值（PDF 首页文字 / 网络返回的专辑/歌手等）必须先过 esc，
// 封堵恶意 PDF 或联网内容注入脚本（XSS）。
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
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

// ============ 网站 Cookie（词曲网 ktvc8 云锁会话，可选；设置页 password 框，非明文） ============
const COOKIE_KEY = 'scorestudio.cookie';
const cookieInput = $('cookieInput');
if (cookieInput) {
  try {
    const savedCookie = localStorage.getItem(COOKIE_KEY);
    if (savedCookie) cookieInput.value = savedCookie;
  } catch (_) {}
  cookieInput.addEventListener('change', () => {
    try { localStorage.setItem(COOKIE_KEY, cookieInput.value.trim()); } catch (_) {}
  });
  // 显示/隐藏切换（默认非明文，点击小眼睛可临时查看）
  const ct = $('cookieToggle');
  if (ct) {
    ct.onclick = () => {
      cookieInput.type = cookieInput.type === 'password' ? 'text' : 'password';
    };
  }
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

// ============== 队列（一项 = 一份 PDF，含 N 页可排序） ==============
let qid = 0;

function addItem(pages, theme, src) {
  // pages: [{name, path}] 至少 1 项
  const t = pages.map((p) => p.name.replace(/\.[^.]+$/, '')).join('、');
  const el = document.createElement('div');
  el.className = 'q';
  el.draggable = true;
  el.dataset.input = pages.map((p) => p.path).join('\u001e');
  el.dataset.kind = 'multi';
  el.innerHTML = `
    <div class="q-handle" title="拖动调整本项顺序">⋮⋮</div>
    <div class="q-body">
      <div class="q-head">
        <div class="q-title">${esc(t) || '未命名'}</div>
        <div class="q-meta">${pages.length} 页 · 来源 ${esc(src)}${theme ? ` · <span class="q-tag">${esc(theme)}</span>` : ''}</div>
      </div>
      <div class="q-pages" data-pages-host>
        ${pages.map((p, i) => `
          <div class="q-page" draggable="true" data-path="${esc(p.path)}">
            <span class="q-page-num">${i + 1}</span>
            <span class="q-page-name" title="${esc(p.path)}">${esc(p.name)}</span>
            <button class="q-page-del" title="移除该页">✕</button>
          </div>
        `).join('')}
        <button class="q-add-page" title="继续添加文件到该项">+ 添加</button>
      </div>
      <input class="q-name" maxlength="80" placeholder="命名（留空 = 自动识别第一页标题）" />
    </div>
    <div class="q-side">
      <div class="st">待处理</div>
      <div class="bar"><i></i></div>
      <button class="q-del" title="移除整项">✕</button>
    </div>`;
  queueEl.appendChild(el);
  statText.textContent = `队列中 ${queueEl.children.length} 项`;
  return el;
}

// 已有项追加页（拖入/选择更多文件时若用户未单独操作）
function appendPagesToLast(paths) {
  if (!queueEl.lastElementChild) return null;
  const last = queueEl.lastElementChild;
  const host = last.querySelector('[data-pages-host]');
  const existing = (last.dataset.input || '').split('\u001e').filter(Boolean);
  const all = [...existing, ...paths];
  last.dataset.input = all.join('\u001e');
  for (let i = existing.length; i < all.length; i++) {
    const p = all[i];
    const name = p.split(/[\\/]/).pop();
    const page = document.createElement('div');
    page.className = 'q-page';
    page.draggable = true;
    page.dataset.path = p;
    page.innerHTML = `<span class="q-page-num">${i + 1}</span><span class="q-page-name" title="${esc(p)}">${esc(name)}</span><button class="q-page-del" title="移除该页">✕</button>`;
    host.insertBefore(page, host.querySelector('.q-add-page'));
  }
  renumberPages(last);
  return last;
}

function renumberPages(qEl) {
  const nums = qEl.querySelectorAll('.q-page-num');
  nums.forEach((n, i) => { n.textContent = i + 1; });
  const meta = qEl.querySelector('.q-meta');
  if (meta) meta.firstChild.textContent = `${nums.length} 页 · `;
}

function getPages(qEl) {
  return [...qEl.querySelectorAll('.q-page')].map((p) => p.dataset.path);
}

function rebuildInputFromPages(qEl) {
  qEl.dataset.input = getPages(qEl).join('\u001e');
}

$('addBtn').onclick = () => {
  const raw = $('linkInput').value.trim();
  if (!raw) return;
  // 支持多图片直链：空格/换行/逗号分隔的若干图片 URL → 合并为一组处理（词曲网被云锁拦时的通道）
  const parts = raw.split(/[\s,，]+/).filter(Boolean);
  const imgUrls = parts.filter((p) => /^https?:\/\/\S+\.(png|jpe?g|jpg|webp)$/i.test(p));
  if (imgUrls.length > 1) {
    addItem(imgUrls.map((u) => ({ name: u.split('/').pop(), path: u })), '', '链接');
    $('linkInput').value = '';
    statText.textContent = `已收入 ${imgUrls.length} 条图片直链，将合并为一份 PDF`;
    return;
  }
  addItem([{ name: '来自链接的曲谱', path: parts.join(' ') }], '', '链接');
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
drop.addEventListener('dragleave', (ev) => {
  ev.preventDefault();
  // 只有真正离开 drop 元素本身（而非子元素）才移除高亮，避免子元素边界抖动
  if (!drop.contains(ev.relatedTarget)) drop.classList.remove('hover');
});
drop.addEventListener('drop', (ev) => {
  ev.preventDefault();
  drop.classList.remove('hover');
});
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
      addItem(paths.map((pp) => ({ name: pp.split(/[\\/]/).pop(), path: pp })), '', '点选');
    }
    return;
  }
  filePicker.click();
}

filePicker.addEventListener('change', async () => {
  const files = [...filePicker.files];
  filePicker.value = '';
  if (!files.length) return;
  try {
    statText.textContent = '上传中… ' + files.map((f) => f.name).join('、');
    const pages = [];
    for (const f of files) {
      const path = await uploadOne(f);
      pages.push({ name: f.name, path });
    }
    addItem(pages, '', '点选/拖入');
    statText.textContent = '已收入：' + files.map((f) => f.name).join('、');
  } catch (e) {
    statText.textContent = '上传失败：' + e;
  }
});

function fileName(p) { return String(p).split(/[\\/]/).pop().replace(/\.[^.]+$/, ''); }

function addDroppedPaths(paths) {
  // 兼容旧调用：转给新 addItem
  addItem(paths.map((p) => ({ name: p.split(/[\\/]/).pop(), path: p })), '', '拖入');
}

async function tryGetDroppedPaths(ev) {
  const out = [];
  // 1) 标准 FileList：仅收集带真实路径的 File（Tauri 会注入 path）
  const files = [...ev.dataTransfer.files];
  for (const f of files) {
    if (f.path) out.push(f.path);
  }
  if (out.length) return out;

  // 2) dataTransfer.items（WeChat 部分场景会走这里）
  if (ev.dataTransfer.items && ev.dataTransfer.items.length) {
    const items = [...ev.dataTransfer.items];
    const strings = [];
    for (const item of items) {
      if (item.kind === 'file') {
        const file = item.getAsFile();
        if (file && file.path) out.push(file.path);
      } else if (item.kind === 'string') {
        strings.push(new Promise((res) => item.getAsString((s) => res(s))));
      }
    }
    if (out.length) return out;
    const resolved = (await Promise.all(strings)).filter(Boolean);
    for (const s of resolved) {
      const path = uriOrTextToPath(s);
      if (path) out.push(path);
    }
    if (out.length) return out;
  }

  // 3) text/uri-list（file:///C:/...）
  const uriList = ev.dataTransfer.getData('text/uri-list');
  if (uriList) {
    for (let line of uriList.split(/\r?\n/)) {
      line = line.trim();
      if (!line || line.startsWith('#')) continue;
      const path = uriOrTextToPath(line);
      if (path) out.push(path);
    }
    if (out.length) return out;
  }

  // 4) text/plain（可能是路径或 URL）
  const plain = ev.dataTransfer.getData('text/plain');
  if (plain) {
    for (let line of plain.split(/\r?\n/)) {
      line = line.trim();
      if (!line) continue;
      const path = uriOrTextToPath(line);
      if (path) out.push(path);
    }
  }
  return out;
}

function uriOrTextToPath(s) {
  if (!s) return '';
  // file:///C:/Users/... 或 file://C:/...
  if (s.startsWith('file://')) {
    let p = s.slice('file://'.length);
    if (p.startsWith('/')) p = p.slice(1);
    try { p = decodeURIComponent(p); } catch (_) {}
    return p.replace(/\//g, '\\');
  }
  // 普通 URL：先尝试解码，若是本地路径则返回
  if (/^https?:\/\//.test(s)) {
    try {
      const u = new URL(s);
      if (u.protocol === 'file:' || u.pathname) {
        const p = decodeURIComponent(u.pathname).replace(/^\//, '').replace(/\//g, '\\');
        if (/^[A-Za-z]:\\/.test(p)) return p;
      }
    } catch (_) {}
    return s;
  }
  // 已经是 Windows 路径
  if (/^[A-Za-z]:[\\/]/.test(s)) return s.replace(/\//g, '\\');
  // UNC 路径
  if (s.startsWith('\\\\')) return s;
  // 相对路径：若拖拽自微信临时目录，尝试当相对路径补全当前目录（少见）
  return s;
}

drop.addEventListener('drop', async (ev) => {
  ev.preventDefault();
  drop.classList.remove('hover');
  const dt = ev.dataTransfer;
  const fileObjs = [...(dt ? dt.files : [])];
  // 优先真实路径（Tauri 注入 path / 微信 uri-list / 纯文本路径）
  const paths = await tryGetDroppedPaths(ev);
  if (paths.length && IS_TAURI) {
    addItem(paths.map((p) => ({ name: p.split(/[\\/]/).pop(), path: p })), '', '拖入');
    return;
  }
  // 兜底：File 对象（本地服务器/浏览器模式走 b64 上传）
  if (fileObjs.length) {
    if (IS_TAURI) {
      statText.textContent = '未取得文件真实路径，请点「添加文件」选择，或将图片先保存到本地再拖入';
      return;
    }
    try {
      statText.textContent = '上传中… ' + fileObjs.map((f) => f.name).join('、');
      const pages = [];
      for (const f of fileObjs) {
        const path = await uploadOne(f);
        pages.push({ name: f.name, path });
      }
      addItem(pages, '', '拖入');
      statText.textContent = '已收入：' + fileObjs.map((f) => f.name).join('、');
    } catch (e) {
      statText.textContent = '上传失败：' + e;
    }
    return;
  }
  statText.textContent = '未识别到可处理的文件或路径；微信图片若仍失败，请先保存到桌面再拖入';
});
drop.addEventListener('click', pickFiles);
const addFilesBtn = $('addFilesBtn');
if (addFilesBtn) addFilesBtn.addEventListener('click', (ev) => { ev.stopPropagation(); pickFiles(); });

// ============== 队列事件委托（删除/添加页） ==============
queueEl.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('.q-del');
  if (btn) {
    const item = btn.closest('.q');
    if (item) { item.remove(); statText.textContent = queueEl.children.length ? `队列中 ${queueEl.children.length} 项` : '队列已清空'; }
    return;
  }
  const pageDel = ev.target.closest('.q-page-del');
  if (pageDel) {
    const page = pageDel.closest('.q-page');
    const item = pageDel.closest('.q');
    if (item && page) {
      page.remove();
      rebuildInputFromPages(item);
      renumberPages(item);
      if (!item.querySelectorAll('.q-page').length) item.remove();
      statText.textContent = queueEl.children.length ? `队列中 ${queueEl.children.length} 项` : '队列已清空';
    }
    return;
  }
  const addBtn = ev.target.closest('.q-add-page');
  if (addBtn) {
    // 「+ 添加」→ 拉起系统文件选择，新增页追加到本项
    if (!IS_TAURI) { statText.textContent = '本地模式请直接拖入文件到本项'; return; }
    const item = addBtn.closest('.q');
    const { open } = await import('@tauri-apps/plugin-dialog');
    const p = await open({ multiple: true, filters: [{ name: '曲谱', extensions: ['png','jpg','jpeg','pdf','webp','bmp'] }] });
    if (p) {
      const paths = Array.isArray(p) ? p : [p];
      const host = item.querySelector('[data-pages-host]');
      const existing = (item.dataset.input || '').split('\u001e').filter(Boolean);
      const all = [...existing, ...paths];
      item.dataset.input = all.join('\u001e');
      for (let i = existing.length; i < all.length; i++) {
        const pp = all[i];
        const page = document.createElement('div');
        page.className = 'q-page';
        page.draggable = true;
        page.dataset.path = pp;
        page.innerHTML = `<span class="q-page-num">${i + 1}</span><span class="q-page-name" title="${esc(pp)}">${esc(pp.split(/[\\/]/).pop())}</span><button class="q-page-del" title="移除该页">✕</button>`;
        host.insertBefore(page, host.querySelector('.q-add-page'));
      }
      renumberPages(item);
    }
  }
});

// ============== 拖拽排序（队列项 + 子页） ==============
// 队列项整体 draggable，dataTransfer.type='item'；子页 draggable，type='page'
queueEl.addEventListener('dragstart', (ev) => {
  const item = ev.target.closest('.q');
  const page = ev.target.closest('.q-page');
  if (page && item && item.contains(page)) {
    ev.dataTransfer.setData('text/plain', 'page');
    ev.dataTransfer.effectAllowed = 'move';
    page.classList.add('dragging');
    ev.dataTransfer.setData('text/page-idx', [...item.querySelectorAll('.q-page')].indexOf(page).toString());
    ev.dataTransfer.setData('text/parent', [...queueEl.children].indexOf(item).toString());
  } else if (item) {
    ev.dataTransfer.setData('text/plain', 'item');
    ev.dataTransfer.effectAllowed = 'move';
    item.classList.add('dragging');
  }
});
queueEl.addEventListener('dragend', (ev) => {
  const el = ev.target.closest('.dragging');
  if (el) el.classList.remove('dragging');
  queueEl.querySelectorAll('.drop-before, .drop-after').forEach((n) => n.classList.remove('drop-before', 'drop-after'));
});

function getDragInsertPoint(ev, container, childSelector) {
  // 返回 {node, before: bool} —— 鼠标上方 1/3 放前面，下方 2/3 放后面
  const children = [...container.querySelectorAll(childSelector)];
  for (const c of children) {
    if (c.classList.contains('dragging')) continue;
    const r = c.getBoundingClientRect();
    const mid = r.top + r.height / 2;
    if (ev.clientY < mid) return { node: c, before: true };
  }
  return { node: null, before: false };
}

queueEl.addEventListener('dragover', (ev) => {
  const type = ev.dataTransfer.types.includes('text/plain') ? 'text/plain' : null;
  if (!type) return;
  ev.preventDefault();
  ev.dataTransfer.dropEffect = 'move';
  const pageOver = ev.target.closest('.q-page');
  const itemOver = ev.target.closest('.q');
  queueEl.querySelectorAll('.drop-before, .drop-after').forEach((n) => n.classList.remove('drop-before', 'drop-after'));
  if (pageOver) {
    const r = pageOver.getBoundingClientRect();
    const before = ev.clientY < r.top + r.height / 2;
    pageOver.classList.add(before ? 'drop-before' : 'drop-after');
  } else if (itemOver) {
    const r = itemOver.getBoundingClientRect();
    const before = ev.clientY < r.top + r.height / 2;
    itemOver.classList.add(before ? 'drop-before' : 'drop-after');
  }
});

queueEl.addEventListener('drop', (ev) => {
  ev.preventDefault();
  ev.stopPropagation();
  const kind = ev.dataTransfer.getData('text/plain');
  queueEl.querySelectorAll('.drop-before, .drop-after').forEach((n) => n.classList.remove('drop-before', 'drop-after'));

  if (kind === 'item') {
    const dragging = queueEl.querySelector('.q.dragging');
    if (!dragging) return;
    // 决定插入位置：找最近的 .q（非自身）
    const target = ev.target.closest('.q');
    if (!target || target === dragging) {
      // 放到队尾
      queueEl.appendChild(dragging);
      return;
    }
    const r = target.getBoundingClientRect();
    const before = ev.clientY < r.top + r.height / 2;
    queueEl.insertBefore(dragging, before ? target : target.nextSibling);
  } else if (kind === 'page') {
    const parentIdx = Number(ev.dataTransfer.getData('text/parent'));
    const pageIdx = Number(ev.dataTransfer.getData('text/page-idx'));
    const fromItem = queueEl.children[parentIdx];
    if (!fromItem) return;
    const fromPage = fromItem.querySelectorAll('.q-page')[pageIdx];
    if (!fromPage) return;
    // 目标：鼠标下的 .q-page 或 .q（落到其项的末尾）
    const pageTarget = ev.target.closest('.q-page');
    const itemTarget = ev.target.closest('.q');
    if (pageTarget && pageTarget !== fromPage) {
      const r = pageTarget.getBoundingClientRect();
      const before = ev.clientY < r.top + r.height / 2;
      pageTarget.parentNode.insertBefore(fromPage, before ? pageTarget : pageTarget.nextSibling);
    } else if (itemTarget && itemTarget !== fromItem) {
      const host = itemTarget.querySelector('[data-pages-host]');
      const addBtn = host.querySelector('.q-add-page');
      host.insertBefore(fromPage, addBtn);
    } else if (itemTarget === fromItem) {
      // 自身内排序：放到末尾
      const host = fromItem.querySelector('[data-pages-host]');
      const addBtn = host.querySelector('.q-add-page');
      host.insertBefore(fromPage, addBtn);
    }
    // 同步：原项若空了要移除；新项 input 重算
    if (!fromItem.querySelectorAll('.q-page').length) fromItem.remove();
    const newParent = fromPage.closest('.q');
    if (newParent) { rebuildInputFromPages(newParent); renumberPages(newParent); }
  }
});

// Tauri 原生拖拽：从微信等拿不到 dataTransfer.files 的 Shell 拖拽，走官方 tauri://drag-drop 事件
// （Tauri 会把 OS 层拖入的真实文件路径注入该事件；preventDefault 阻止其默认导航行为）
if (IS_TAURI) {
  import('@tauri-apps/api/event').then(({ listen }) => {
    listen('tauri://drag-drop', (ev) => {
      try { ev.preventDefault(); } catch (_) {}
      const payload = ev?.payload || {};
      const paths = Array.isArray(payload) ? payload : (payload.paths || []);
      const real = paths.filter((p) => typeof p === 'string' && /^[A-Za-z]:[\\/]/.test(p) && !/^https?:\/\//.test(p));
      if (real.length) addDroppedPaths(real);
    });
    // 拖入悬停时高亮拖拽区（OS 层事件，HTML5 dragover 不一定触发）
    listen('tauri://drag-enter', () => drop.classList.add('hover'));
    listen('tauri://drag-leave', () => drop.classList.remove('hover'));
  }).catch((e) => {
    console.error('tauri drag-drop listener failed', e);
  });
}

// 队列命名（每项 .q-name 输入框）— runBtn 直接读取 .q-name.value
// 旧的 ttl 双击编辑已弃用（结构改为多页子项后不再需要）

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

// ============ 首次启动引导（选择曲谱目录，仅第一次） ============
// 无 localStorage 记录 = 首次启动 → 弹出引导层强制选目录；选完存 localStorage，此后不再弹。
(function initFirstRun() {
  const overlay = $('setupOverlay');
  if (!overlay) return;
  let hasDir = false;
  try { hasDir = !!localStorage.getItem(DIR_KEY); } catch (_) {}
  if (hasDir) return; // 已配置过，直接进入

  overlay.classList.remove('hidden');
  const setupDirBox = $('setupDirBox');
  const setupDirBtn = $('setupDirBtn');
  const setupConfirm = $('setupConfirm');

  const pickSetupDir = async () => {
    if (IS_TAURI) {
      const { open } = await import('@tauri-apps/plugin-dialog');
      const r = await open({ directory: true, defaultPath: setupDirBox.textContent });
      if (r) setupDirBox.textContent = r;
    }
  };
  setupDirBtn.onclick = pickSetupDir;
  setupDirBox.onclick = pickSetupDir;

  setupConfirm.onclick = () => {
    const chosen = setupDirBox.textContent.trim();
    if (!chosen) return;
    dirBox.textContent = chosen;
    saveDir();
    overlay.classList.add('hidden');
    statText.textContent = '输出目录：' + chosen;
  };
})();

// ============ 后端调用 ============
// 让出渲染循环：长 await 任务前调用，使「扫描中/补全中」等状态能及时绘制，避免 UI 假死感
const yieldToUI = () => new Promise((resolve) => setTimeout(resolve, 0));
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

async function callAlbumTag(title, artist) {
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    const res = await invoke('album_tag', { title, artist });
    const p = safeParse(res);
    if (p && p.ok === false) throw new Error((p.error || '') + (p.stderr ? '\n' + p.stderr : ''));
    return p && p.ok === true ? safeParse(p.out) : p;
  }
  const r = await fetch('/api/albumtag?title=' + encodeURIComponent(title) + '&artist=' + encodeURIComponent(artist));
  return safeParse(await r.text());
}

// 批量并发补全专辑：一次请求后端并发处理（本地兜底 + 联网 iTunes），彻底消除逐行卡顿
// Tauri 模式返回 PyResult{ok,out,error,code,stderr}——失败时携带真实 stderr，前端显形
async function callAlbumTagBatch(items) {
  if (IS_TAURI) {
    const { invoke } = await import('@tauri-apps/api/core');
    const res = await invoke('album_tag_batch', { items: JSON.stringify(items) });
    const p = safeParse(res);
    if (p && p.ok === false) {
      const msg = (p.error || 'CLI 执行失败') + (p.stderr ? ' — ' + p.stderr : '');
      throw new Error(msg);
    }
    if (p && p.ok === true) {
      if (typeof p.out === 'string') {
        const arr = safeParse(p.out);
        if (Array.isArray(arr)) return { tags: arr, stderr: p.stderr || '' };
        throw new Error('后端出参解析失败，out=' + String(p.out).slice(0, 200) + (p.stderr ? ' stderr=' + p.stderr : ''));
      }
      return { tags: Array.isArray(p.out) ? p.out : [], stderr: p.stderr || '' };
    }
    return Array.isArray(p) ? { tags: p, stderr: '' } : { tags: [], stderr: '' };
  } else {
    try {
      const r = await fetch('/api/albumbatch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items }),
      });
      const j = await r.json();
      return { tags: (j && j.albums) || [], stderr: (j && j.error) || '' };
    } catch (_) {
      return { tags: [], stderr: '本地模式调用失败' };
    }
  }
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
  if (!items.length) { statText.textContent = '队列为空，请先收入链接或文件'; return; }
  logBox.classList.remove('hidden');
  logBox.textContent = '';
  let done = 0;
  for (const el of items) {
    const input = el.dataset.input || dirBox.textContent;
    const nameInput = el.querySelector('.q-name');
    const customName = nameInput ? nameInput.value.trim() : '';
    const st = el.querySelector('.st');
    const bar = el.querySelector('.bar i');
    st.textContent = '处理中'; st.style.color = 'var(--gold-2)';
    try {
      const res = await callProcess({
        input,
        outputDir: dirBox.textContent,
        theme: '',
        name: customName,
        cookie: cookieInput ? cookieInput.value.trim() : '',
      });
      if (res && res.ok) {
        st.textContent = '已完成'; st.classList.add('done'); st.classList.remove('err');
        bar.style.width = '100%';
        logBox.textContent += `✓ ${res.path}\n${res.log || ''}\n`;
      } else {
        st.textContent = '失败'; st.classList.add('err'); st.classList.remove('done');
        const errMsg = (res && res.error && res.error.trim())
          ? res.error.trim()
          : ((res && res.log && res.log.trim()) ? res.log.trim() : '未知错误（无后端诊断）');
        logBox.textContent += `✗ ${errMsg}\n`;
        if (res && res.error && res.error.includes('无法自动命名')) {
          statText.textContent = '页面无标题，请在该项命名输入框补名后重试';
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
    const themeChip = it.album ? `<span class="theme-chip">${esc(it.album)}</span>` : '';
    const meta = `<div class="meta-row"><span>${it.pages || '?'} 页</span><span>${fmtBytes(it.size)}</span></div>`;
    c.innerHTML = `
      <div class="cover-wrap" data-name="${rel}">${cover}<div class="cover-overlay"><span class="open-hint">打开 PDF</span></div></div>
      <div class="cap">
        <div class="n">${esc(it.title || it.name.replace(/\.pdf$/i, ''))}</div>
        <div class="a">${esc(it.artist) || '古典'} ${themeChip}</div>
        ${meta}
      </div>`;
    c.addEventListener('click', () => openPdf(rel));
    lib.appendChild(c);
    obs.observe(c.querySelector('.cover-wrap'));
  });
}

function filterLibrary() {
  const q = $('libSearch').value.trim().toLowerCase();
  const filtered = libItems.filter((it) => {
    return !q || [it.title, it.artist, it.album, it.name.replace(/\.pdf$/i, '')]
      .some((s) => s && s.toLowerCase().includes(q));
  });
  renderLibrary(filtered);
}

async function loadLibrary() {
  const lib = $('libGrid');
  lib.innerHTML = '<div class="hero-p">读取中…</div>';
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
let inspUpdated = new Set(); // 本次「联网补全专辑」实际新增的行索引 → 表格高亮反馈

// 专辑改动后按「曲名 - 歌手 - 专辑」规则实时重算建议名
function recalcSuggested(r) {
  const segs = [r.sug_title, r.sug_artist, r.sug_album].filter((s) => s && s.trim());
  r.suggested = segs.join('-') + '.pdf';
  r.needs_rename = true;
}

// 专辑列内联编辑：双击进入输入框，回车/失焦提交并实时重算建议名
function startAlbumEdit(cell) {
  if (cell.querySelector('.album-input')) return; // 已在编辑中
  const idx = Number(cell.dataset.albumIdx);
  const r = inspRows[idx];
  if (!r) return;
  cell.innerHTML = `<input class="album-input" maxlength="80" value="${esc(r.sug_album || '')}">`;
  const inp = cell.querySelector('.album-input');
  inp.dataset.idx = idx;
  inp.focus();
  inp.select();
  inp.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); commitAlbumEdit(inp); }
    else if (ev.key === 'Escape') { ev.preventDefault(); renderInspect(); }
  });
  inp.addEventListener('blur', () => commitAlbumEdit(inp));
  inp.addEventListener('click', (ev) => ev.stopPropagation());
}

function commitAlbumEdit(inp) {
  if (!inp || inp._done) return;
  inp._done = true;
  const idx = Number(inp.dataset.idx);
  const r = inspRows[idx];
  const text = inp.value.replace(/\s+/g, ' ').trim();
  if (r && text !== (r.sug_album || '')) {
    r.sug_album = text;
    recalcSuggested(r);
    inspUpdated.delete(idx);
    inspSelected.delete(idx); // 清掉旧采纳态，避免带着旧名误改
    statText.textContent = '专辑已改「' + (text || '已清空') + '」→ 建议名已同步更新';
  }
  renderInspect();
}

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
  let html = '<div class="insp-head"><span>当前文件名</span><span>解析结果</span><span>建议新名</span><span>专辑（双击改）</span><span>采纳</span></div>';
  html += '<div class="insp-list">';
  inspRows.forEach((r, i) => {
    const checked = inspSelected.has(i) ? 'checked' : '';
    const curParts = [];
    if (r.cur_album) curParts.push('专·' + esc(r.cur_album));
    const cur = `<b>${esc(r.cur_title) || '?'}</b><br><span class="sub">${esc(r.cur_artist) || '未知歌手'}${curParts.length ? ' · ' + curParts.join(' · ') : ''}</span>`;
    const det = r.has_text
      ? `<span class="ok">✓ 文字</span> <b>${esc(r.det_title) || '—'}</b> / ${esc(r.det_artist) || '—'}`
      : '<span class="warn">扫描件/无文字</span>';
    const excerpt = `<div class="excerpt" title="${esc(r.text_excerpt || '—')}">${esc(r.text_excerpt) || '—'}</div>`;
    const albumInner = r.sug_album
      ? `<span class="theme-chip" title="来源：${r.meta_album ? 'PDF元数据' : (r.cur_album ? '文件名' : '识别')}">${esc(r.sug_album)}</span>${r.sug_category ? `<span class="cat-chip">${esc(r.sug_category)}</span>` : ''}`
      : '<span class="muted">未定</span>';
    const updTag = inspUpdated.has(i) ? '<span class="updated-tag" title="本次联网补全新增">新增</span>' : '';
    const album = `${albumInner}${updTag}<span class="album-edit" title="双击修改专辑">✎</span>`;
    const rowClass = `${r.needs_rename ? 'needs' : 'ok'}${inspUpdated.has(i) ? ' updated' : ''}`;
    html += `
      <div class="insp-row ${rowClass}" data-idx="${i}">
        <div class="cell-name" title="${esc(r.name)}">${esc(r.name)}</div>
        <div class="cell-cur">${cur}<br>${det}${excerpt}</div>
        <div class="cell-sug" title="${esc(r.suggested)}">${esc(r.suggested)}</div>
        <div class="cell-album" data-album-idx="${i}">${album}</div>
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

// 专辑列双击编辑（委托到容器，仅绑定一次）
$('inspWrap').addEventListener('dblclick', (ev) => {
  const cell = ev.target.closest('.cell-album');
  if (cell) startAlbumEdit(cell);
});

async function loadInspect() {
  const wrap = $('inspWrap');
  wrap.innerHTML = '<div class="hero-p">扫描中…</div>';
  inspSelected.clear();
  inspUpdated.clear();
  updateApplyButton();
  // 关键：让渲染循环先画「扫描中…」，避免长 await 期间假死
  await yieldToUI();
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

$('selAllBtn').addEventListener('click', () => {
  inspRows.forEach((r, i) => { if (r.needs_rename) inspSelected.add(i); });
  renderInspect();
  updateApplyButton();
});
$('selNoneBtn').addEventListener('click', () => {
  inspSelected.clear();
  renderInspect();
  updateApplyButton();
});

$('albumAllBtn').addEventListener('click', async () => {
  // 保留原始行号，便于「本次新增」高亮
  const targets = inspRows.map((r, i) => ({ r, i })).filter((x) => x.r.sug_title);
  if (!targets.length) {
    statText.textContent = '没有可识别的条目（请先巡检并保留曲名）';
    return;
  }
  inspUpdated.clear();
  statText.textContent = `正在联网补全专辑（共 ${targets.length} 项，实时更新进度）…`;
  const total = targets.length;
  const BATCH = 30; // 收紧批大小，每批网络往返更短，UI 喘息更频繁（根治「多次未响应卡顿」）
  let changed = 0;
  let localHit = 0;
  for (let off = 0; off < total; off += BATCH) {
    const chunk = targets.slice(off, off + BATCH);
    const items = chunk.map((c) => ({ title: c.r.sug_title, artist: c.r.sug_artist || '' }));
    await yieldToUI(); // 让渲染循环刷新进度文字，避免长 await 期间界面假死
    const pct = Math.round((Math.min(off + BATCH, total) / total) * 100);
    statText.textContent = `正在联网补全专辑 ${Math.min(off + BATCH, total)}/${total}（${pct}%）…`;
    let r = null;
    try {
      r = await callAlbumTagBatch(items);
    } catch (e) {
      statText.textContent = '专辑服务失败（真实原因）：' + (e && e.message ? e.message : e);
      renderInspect();
      return;
    }
    await yieldToUI();
    const tags = r ? r.tags : [];
    const backendStderr = r ? r.stderr || '' : '';
    if (!tags || !Array.isArray(tags)) {
      statText.textContent = '专辑服务暂不可用（网络与本地兜底均失败）' + (backendStderr ? ' stderr:' + backendStderr : '');
      renderInspect();
      return;
    }
    if (tags.length === 0) {
      statText.textContent = `⚠ 后端返回空数组（收到 items=${items.length} 条）。后端诊断：${backendStderr || '无'}`;
      renderInspect();
      return;
    }
    chunk.forEach((c, i) => {
      const tag = tags[i];
      if (tag && tag.album) {
        const prev = c.r.sug_album;
        c.r.sug_album = tag.album;
        if (tag.category && !c.r.sug_category) c.r.sug_category = tag.category;
        recalcSuggested(c.r);
        if (prev !== tag.album) inspUpdated.add(c.i); // 本行专辑本次新补到 → 高亮
        changed++;
        if (tag.source === 'local') localHit++;
      }
    });
  }
  renderInspect();
  const extra = localHit ? `（其中本地词库兜底 ${localHit} 项，未联网）` : '';
  statText.textContent = `专辑补全完成：${changed}/${total} 份更新专辑${extra}`;
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
