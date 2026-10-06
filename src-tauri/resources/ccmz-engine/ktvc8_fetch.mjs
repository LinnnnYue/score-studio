// ktvc8 页面全图提取（词曲网云锁通道）
// 用法: node ktvc8_fetch.mjs <pageUrl> <cookie> [port]
// 输出 JSON: { ok, title, imgs: ["https://..."], error }
// 原理: puppeteer 系统 Edge 无头，注入完整 Cookie 过云锁 → 渲染后从 myFunction/JS 字符串挖全部页图
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import http from 'node:http';
import { spawn } from 'node:child_process';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pageUrl = process.argv[2];
const cookie = process.argv[3] || '';
if (!pageUrl) { console.error('用法: node ktvc8_fetch.mjs <pageUrl> <cookie>'); process.exit(1); }

const EDGE_CANDS = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
];

function cli() {
  return JSON.stringify({ ok: false, title: '', imgs: [], error: '未找到系统 Edge，请安装 Microsoft Edge 后再试' });
}

// Cookie 解析：支持 Cookie Editor 的 "k=v;k=v" 或带 Cookie: 前缀
function parseCookies(raw) {
  const s = raw.replace(/^Cookie:\s*/i, '').trim();
  if (!s) return [];
  return s.split(';').map((kv) => {
    const i = kv.indexOf('=');
    if (i < 0) return null;
    return {
      name: kv.slice(0, i).trim(),
      value: kv.slice(i + 1).trim(),
      domain: new URL(pageUrl).hostname,
      path: '/',
    };
  }).filter(Boolean);
}

const edge = EDGE_CANDS.find((c) => fs.existsSync(c));
if (!edge) { console.log(cli()); process.exit(0); }

// Edge 153+ 改为「启动器 → 宿主」分离：msedge.exe 拉起真浏览器后立刻退出，
// puppeteer.launch() 的子进程 onClose 检测会误判为启动失败（Code: 0，stderr 空）。
// 对策：自行 spawn + 轮询 CDP 端口就绪 + puppeteer.connect() 接管（与 ccmz2pdf.mjs 同方案）。
const DBG_PORT = 9300 + (process.pid % 300);
const PROFILE_DIR = path.join(os.tmpdir(), 'ktvc8_edge_profile_' + process.pid);

const cdpReady = () =>
  new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port: DBG_PORT, path: '/json/version', timeout: 1500 },
      (res) => { res.resume(); resolve(res.statusCode === 200); },
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });

async function launchEdge() {
  try { fs.rmSync(PROFILE_DIR, { recursive: true, force: true }); } catch (_) {}
  const child = spawn(edge, [
    '--headless=new', '--disable-gpu', '--no-sandbox',
    '--disable-setuid-sandbox', '--disable-dev-shm-usage',
    '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=' + DBG_PORT,
    '--user-data-dir=' + PROFILE_DIR,
    'about:blank',
  ], { stdio: 'ignore', detached: false });
  child.on('error', () => {});
  for (let i = 0; i < 48; i++) {          // 最多 ~24s
    if (await cdpReady()) {
      const browser = await puppeteer.connect({
        browserURL: 'http://127.0.0.1:' + DBG_PORT,
        defaultViewport: null,
      });
      return { browser, child };
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  try { child.kill(); } catch (_) {}
  throw new Error(`Edge CDP 端口 ${DBG_PORT} 未就绪（启动失败）`);
}

async function run() {
  let browser;
  let child = null;
  try {
    const launched = await launchEdge();
    browser = launched.browser;
    child = launched.child;
    const page = await browser.newPage();
    await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36');
    const cookies = parseCookies(cookie);
    if (cookies.length) await page.setCookie(...cookies);
    // 词曲网 /plcms.asp 滑动验证：滑块拖到底只发 verify_pass，服务端即放行。
    // 先进这个地址把验证 cookie 写进本会话，否则后续页面会被换成「访问验证」页。
    try {
      await page.goto(
        'https://www.ktvc8.com/plcms.asp?action=verify_pass&t=' + Date.now(),
        { waitUntil: 'domcontentloaded', timeout: 20000 },
      );
    } catch (_) {}
    await page.goto(pageUrl, { waitUntil: 'networkidle2', timeout: 45000 });
    await new Promise((r) => setTimeout(r, 2500));

    const title = await page.title();

    // 从 contentpic img + myFunction/show_neirong 源码挖全部 uploadfiles 图
    // （注意：不能先点击「查看剩余」按钮——按钮 onclick 会重写 contentpic DOM，
    //  但函数源码中的全部图 URL 不受影响，直接从 JS 字符串挖即可全量拿到）
    const imgs = await page.evaluate(() => {
      const out = [];
      const push = (u) => {
        if (!u) return;
        let clean = u.split('?')[0].trim();
        if (clean.startsWith('..')) clean = clean.replace(/^\.\.?\/+/, 'https://www.ktvc8.com/');
        else if (clean.startsWith('/')) clean = 'https://www.ktvc8.com' + clean;
        else if (!/^https?:/.test(clean)) clean = 'https://www.ktvc8.com/' + clean;
        if (clean.startsWith('http') && !out.includes(clean)) out.push(clean);
      };
      // DOM 图片
      document.querySelectorAll('.contentpic img').forEach((i) => push(i.getAttribute('src') || i.src));
      // 全局函数源码（词曲网把剩余页图写在 myFunction/show_neirong 的字符串里）
      // 注意：匹配 /uploadfiles/YYYYMMDD/YYYYMMDDHHMMSSXXXXXX.png 格式（至少 20 位数字 + .png）
      const UP = '(?:uploadfiles|upload-files|uploaduserskinfiles)';
      const UP_RE = new RegExp(UP + '\\/\\d{8}\\/\\d{14,20}[a-zA-Z0-9]+\\.(?:png|jpe?g|gif)', 'g');
      try {
        const t1 = typeof myFunction === 'function' ? myFunction.toString() : '';
        const m1 = t1.match(UP_RE);
        if (m1) m1.forEach((u) => push(u));
      } catch (_) {}
      try {
        const t2 = typeof show_neirong === 'function' ? show_neirong.toString() : '';
        const m2 = t2.match(UP_RE);
        if (m2) m2.forEach((u) => push(u));
      } catch (_) {}
      // 全量 script 字符串兜底（更宽松的匹配）
      document.querySelectorAll('script').forEach((s) => {
        const t = s.textContent || '';
        const m = t.match(UP_RE) || t.match(new RegExp(UP + '\\/[^\\s\'"<>]+', 'g'));
        if (m) m.forEach((u) => push(u));
      });
      return out;
    });

    // 过滤封面/头像类（contentpic 外的 uploadfiles 才可能是谱面；JS 挖的默认全收）
    const result = { ok: true, title: title.trim(), imgs, error: '' };
    console.log(JSON.stringify(result));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, title: '', imgs: [], error: String(e).slice(0, 300) }));
  } finally {
    if (browser) try { await browser.close(); } catch (_) {}
    if (child) try { child.kill(); } catch (_) {}
    await new Promise((r) => setTimeout(r, 500));
    try { fs.rmSync(PROFILE_DIR, { recursive: true, force: true }); } catch (_) {}
  }
}

run();