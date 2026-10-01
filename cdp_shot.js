// Minimal CDP screenshotter: node cdp_shot.js <url> <out.png> [waitMs] [w] [h] [evalJs]
const { spawn } = require('child_process');
const fs = require('fs');

const [url, out, waitMs = '4000', w = '1440', h = '900', evalJs = ''] = process.argv.slice(2);
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const port = 9333;

const chrome = spawn(CHROME, [
  `--remote-debugging-port=${port}`, '--headless=new', '--disable-gpu',
  `--window-size=${w},${h}`, '--user-data-dir=' + require('os').tmpdir() + '/cdp-prof-' + Date.now(),
  'about:blank',
], { stdio: 'ignore' });

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  let ws;
  for (let i = 0; i < 40; i++) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = targets.find(t => t.type === 'page');
      if (page) { ws = new WebSocket(page.webSocketDebuggerUrl); break; }
    } catch {}
    await sleep(250);
  }
  if (!ws) throw new Error('no CDP target');
  let id = 0;
  const pending = new Map();
  const events = [];
  ws.onmessage = m => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    else events.push(msg);
  };
  await new Promise(r => ws.onopen = r);
  const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: +w, height: +h, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url });
  await sleep(+waitMs);
  if (evalJs) {
    const r = await send('Runtime.evaluate', { expression: evalJs, returnByValue: true, awaitPromise: true });
    console.log('EVAL:', JSON.stringify(r.result?.result?.value ?? r.result, null, 0)?.slice(0, 4000));
  }
  const errs = events.filter(e => e.method === 'Runtime.exceptionThrown' || (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error'))
    .map(e => JSON.stringify(e.params).slice(0, 300));
  if (errs.length) console.log('PAGE_ERRORS:', errs.join('\n'));
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(out, Buffer.from(shot.result.data, 'base64'));
  console.log('saved', out);
  ws.close();
}
main().catch(e => { console.error(e); process.exitCode = 1; }).finally(() => { chrome.kill(); process.exit(); });
