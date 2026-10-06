// ccmz → PDF 自动化：喂 ccmz → 选 pdf/svg2pdf → 开始 → 拦截 blob 下载 → 写 PDF
// 用法: node ccmz2pdf.mjs <ccmz> [out.pdf] [port]
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createServer } from 'node:http';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const input = process.argv[2];
const output = process.argv[3] || path.join(__dirname, 'out.pdf');
const PORT = parseInt(process.argv[4] || '51740', 10);
if (!input || !fs.existsSync(input)) {
  console.error('用法: node ccmz2pdf.mjs <ccmz> [out.pdf] [port]');
  process.exit(1);
}

function findEdge() {
  const cands = [
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  ];
  return cands.find(fs.existsSync) || '';
}

const EDGE = findEdge();
if (!EDGE) {
  console.error('未找到 Microsoft Edge（Win10+ 自带）');
  process.exit(1);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function clickByText(page, text, optional = false) {
  const ok = await page.evaluate((t) => {
    const all = [...document.querySelectorAll('*')];
    const el =
      all.find((e) => e.children.length === 0 && e.textContent.trim() === t) ||
      all.find(
        (e) =>
          (e.tagName === 'LABEL' || e.tagName === 'BUTTON' || e.tagName === 'SPAN' || e.tagName === 'DIV') &&
          e.textContent.trim() === t
      );
    if (el) {
      el.click();
      return true;
    }
    return false;
  }, text);
  if (!ok && !optional) console.warn('⚠ 未找到可点击文本: "' + text + '"');
  return ok;
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.ttf': 'font/ttf',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ico': 'image/x-icon',
};

const distRoot = path.join(__dirname, 'dist');
const server = createServer((req, res) => {
  let p = decodeURIComponent((req.url || '/').split('?')[0]);
  if (p === '/') p = '/index.html';
  const file = path.join(distRoot, p);
  if (!file.startsWith(distRoot)) {
    res.writeHead(403).end();
    return;
  }
  fs.readFile(file, (err, data) => {
    if (err) {
      res.writeHead(404).end('404');
      return;
    }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream' });
    res.end(data);
  });
});

async function main() {
  await new Promise((r) => server.listen(PORT, r));
  console.log('🚀 服务器: http://localhost:' + PORT);

  // 清理可能残留的 puppeteer 临时 profile（上次崩溃留下的锁会导致下次启动失败）
  try {
    const tmp = os.tmpdir();
    const fs2 = fs.readdirSync(tmp);
    for (const f of fs2) {
      if (f.startsWith('puppeteer_dev_chrome_profile') || f.startsWith('score_edge_profile')) {
        fs.rmSync(path.join(tmp, f), { recursive: true, force: true });
      }
    }
  } catch (_) {}

  // 启动浏览器：Edge 153+ 采用「启动器 → 宿主」分离架构——msedge.exe 拉起真正的
  // 浏览器进程后立即退出，puppeteer.launch() 会误判为启动失败（onClose, Code: 0）。
  // 对策：自行 spawn Edge + 等待 CDP 端口就绪，再用 puppeteer.connect() 接管，
  // 完全绕开对浏览器进程生存期的依赖。
  const DBG_PORT = PORT + 1;
  const PROFILE_DIR = path.join(os.tmpdir(), 'score_edge_profile_' + process.pid);
  let browser = null;
  let edgeChild = null;
  let lastErr = null;

  const cdpReady = () =>
    new Promise((resolve) => {
      const req = http.get(
        { host: '127.0.0.1', port: DBG_PORT, path: '/json/version', timeout: 1500 },
        (res) => { res.resume(); resolve(res.statusCode === 200); },
      );
      req.on('error', () => resolve(false));
      req.on('timeout', () => { req.destroy(); resolve(false); });
    });

  for (let attempt = 1; attempt <= 2 && !browser; attempt++) {
    try {
      try { fs.rmSync(PROFILE_DIR, { recursive: true, force: true }); } catch (_) {}
      edgeChild = spawn(EDGE, [
        '--headless=new',
        '--disable-gpu',
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--allow-file-access-from-files',
        '--disable-features=msEdgeSidebarV2',
        '--no-first-run',
        '--no-default-browser-check',
        '--remote-debugging-port=' + DBG_PORT,
        '--user-data-dir=' + PROFILE_DIR,
        'about:blank',
      ], { stdio: 'ignore', detached: false });
      edgeChild.on('error', () => {});

      // 轮询 CDP 端点就绪（最多 ~24s）
      let ready = false;
      for (let i = 0; i < 48; i++) {
        if (await cdpReady()) { ready = true; break; }
        await new Promise((r) => setTimeout(r, 500));
      }
      if (!ready) throw new Error(`CDP 端口 ${DBG_PORT} 未就绪`);

      browser = await puppeteer.connect({
        browserURL: 'http://127.0.0.1:' + DBG_PORT,
        defaultViewport: null,
      });
    } catch (e) {
      lastErr = e;
      console.error(`⚠️ Edge 启动失败（第 ${attempt} 次）: ${String(e && e.message).slice(0, 160)}`);
      try { if (edgeChild) edgeChild.kill(); } catch (_) {}
      edgeChild = null;
      if (attempt === 1) await new Promise((r) => setTimeout(r, 1200));
    }
  }
  if (!browser) {
    const edgVer = (() => { try { return fs.existsSync(EDGE) ? '存在' : '缺失'; } catch (_) { return '?'; } })();
    console.error(`❌ Edge 浏览器无法启动（${edgVer}）。请关闭正在运行的 Edge / 杀软后重试，或重启电脑。`);
    console.error(`   原始错误: ${lastErr ? String(lastErr).slice(0, 300) : '无'}`);
    process.exit(2);
  }
  const page = await browser.newPage();

  // 注入 blob 下载拦截
  await page.evaluateOnNewDocument(() => {
    const origCreate = URL.createObjectURL;
    window.__dlBytes = null;
    window.__dlName = '';
    URL.createObjectURL = (obj) => {
      const url = origCreate.call(URL, obj);
      (async () => {
        try {
          const buf = await obj.arrayBuffer();
          window.__dlBytes = new Uint8Array(buf);
          window.__dlName = obj.name || '';
        } catch (e) {}
      })();
      return url;
    };
  });

  await page.goto('http://localhost:' + PORT, { waitUntil: 'networkidle0', timeout: 60000 });
  await page.waitForSelector('#app-main > *', { timeout: 30000 });
  console.log('✅ 页面加载完成');
  await sleep(800);

  // 喂 ccmz 文件
  const fi = await page.$('input[type=file]');
  if (fi) {
    await fi.uploadFile(input);
    console.log('📤 ccmz 已上传');
  } else {
    throw new Error('找不到文件输入框');
  }
  await sleep(2500);

  // 选 pdf + svg2pdf + 开始
  await clickByText(page, 'pdf');
  await sleep(1000);
  await clickByText(page, 'svg2pdf');
  await sleep(800);
  await clickByText(page, '开始');
  console.log('⏳ 开始转换…');
  await sleep(2000);

  // 等下载按钮并点击
  let got = false;
  for (let i = 0; i < 60; i++) {
    const hasDl = await page.evaluate(() => {
      const all = [...document.querySelectorAll('*')];
      return all.some((e) => e.children.length === 0 && e.textContent.trim() === '下载');
    });
    if (hasDl) {
      await clickByText(page, '下载');
      got = true;
      break;
    }
    await sleep(2000);
  }
  console.log(got ? '✅ 已点下载' : '⏳ 未等到下载按钮（60s）');
  await sleep(4000);

  // 取拦截字节
  let saved = false;
  for (let i = 0; i < 30; i++) {
    const dl = await page.evaluate(() => ({
      len: window.__dlBytes ? window.__dlBytes.length : 0,
      name: window.__dlName || '',
    }));
    if (dl.len > 0) {
      const bytes = await page.evaluate(() => {
        const b = window.__dlBytes;
        let bin = '';
        const CH = 0x8000;
        for (let i = 0; i < b.length; i += CH) {
          bin += String.fromCharCode.apply(null, b.subarray(i, i + CH));
        }
        return btoa(bin);
      });
      fs.writeFileSync(output, Buffer.from(bytes, 'base64'));
      saved = true;
      console.log('📦 捕获下载: ' + (dl.name || path.basename(output)) + ' ' + (dl.len / 1024).toFixed(1) + ' KB');
      break;
    }
    await sleep(2000);
  }
  if (saved) {
    console.log('✅ PDF 已保存: ' + output);
  } else {
    console.log('⚠ 未捕获到下载字节（可能转换失败）');
    await page.screenshot({ path: path.join(__dirname, 'ccmz2pdf_debug.png') });
    process.exitCode = 1;
  }
  // connect 模式：close() 会断开并请求浏览器退出；再补一刀确保 Edge 宿主进程真的走掉，
  // 最后清理本次专用 profile（避免 Temp 目录堆积）。
  try { await browser.close(); } catch (_) {}
  try { if (edgeChild) edgeChild.kill(); } catch (_) {}
  await sleep(600);
  try { fs.rmSync(PROFILE_DIR, { recursive: true, force: true }); } catch (_) {}
}

main()
  .catch((e) => {
    console.error('❌ 转换失败:', e && e.message ? e.message : e);
    process.exitCode = 1;
  })
  .finally(() => server.close());