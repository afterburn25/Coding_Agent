const { chromium } = require(process.env.NEXUS_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');
const http = require('http');
const crypto = require('crypto');
const { spawn } = require('child_process');
const { once } = require('events');
const config = require('./error-continuation.json');
const recoveryConfig = require('./recovery.json');
const failedConfig = require('./recovery-failed.json');
const isFailed = process.argv.includes('--recovery-failed');
const isRecovery = process.argv.includes('--recovery') || isFailed;
const isStartup = process.argv.includes('--startup');
const root = path.resolve(__dirname, '../..');
const out = path.join(root, 'review/videos');
fs.mkdirSync(out, { recursive: true });
const startup = path.join(out, 'NexusCore-Startup-Glow-Only.mp4');
const startupHash = crypto.createHash('sha256').update(fs.readFileSync(startup)).digest('hex');
const finalStartupTime = config.startupSeconds - 1 / config.fps;

// Apply export-only timing changes in memory. The installed app and startup
// source/movie stay unchanged. Closure events and their audio share this offset.
const setup = `
  const exportConfig = ${JSON.stringify(config)};
  let lastExportState;
  const extendedFailure = manifest.failure;
  extendedFailure.duration += exportConfig.extraInstabilitySeconds;
  for (const phase of extendedFailure.phases) if (phase.at >= 2.8) phase.at += exportConfig.extraInstabilitySeconds;
  for (const event of extendedFailure.events) {
    if (event.at >= 2.8) event.at += exportConfig.extraInstabilitySeconds;
    if (event.id === 'instability_start') event.until += exportConfig.extraInstabilitySeconds;
  }
`;
const hook = `exportStartupFrame(t) {
  cancelAnimationFrame(raf);
  clock.resetNormal(); clock.pause();
  for (const gate of manifest.gates) clock.setGate(gate.id, true);
  clock.normal.position = t; clock.normal.ambientPosition = t;
  externalProgress = null; reduced = false;
  document.body.classList.add('capture');
  for (const id of ['fill', 'track', 'status', 'detail']) $(id).removeAttribute('style');
  paint(); audio.stop();
  return { t, status: $('status').textContent, fill: $('fill').style.transform };
},
    exportErrorFrame(t) {
  cancelAnimationFrame(raf);
  if (!clock.exportSample) {
    clock.exportSample = clock.sample.bind(clock);
    clock.sample = minimal => {
      const state = clock.exportSample(minimal);
      if (state.fault) state.ambientTime = clock.faultFrom.ambientTime + clock.time;
      return state;
    };
  }
  clock.resetNormal(); clock.pause();
  for (const gate of manifest.gates) clock.setGate(gate.id, true);
  const elapsed = t - exportConfig.onlineLeadSeconds;
  const normalTime = ${finalStartupTime} + Math.min(t, exportConfig.onlineLeadSeconds);
  clock.normal.position = normalTime; clock.normal.ambientPosition = normalTime;
  externalProgress = null; reduced = false;
  document.body.classList.add('capture');
  for (const id of ['fill', 'track', 'status', 'detail']) $(id).removeAttribute('style');
  if (elapsed >= 0) {
    clock.triggerFault({ message: 'Core synchronization lost.', detail: 'Video demonstration of emergency containment. No actual startup failure was triggered.' }, false, 1);
    clock.pause(); clock.transport.position = elapsed; clock.transport.ambientPosition = elapsed;
    if (elapsed >= manifest.failure.events.find(event => event.id === 'fault_contained').at)
      clock.setRecoveryState('HUMAN_INTERVENTION_REQUIRED');
  }
  paint(); audio.stop();
  const state = clock.sample(false);
  lastExportState = state;
  if (elapsed >= 0) {
    const pulse = .5 + .5 * Math.cos(2 * Math.PI * elapsed / 1.2);
    $('fill').style.transform = 'scaleX(1)';
    $('fill').style.background = 'linear-gradient(90deg,#cb1832,#ff5258)';
    $('fill').style.opacity = String(.38 + .62 * pulse);
    $('fill').style.boxShadow = '0 0 9px #ff243d';
    $('track').style.borderColor = '#ee5964';
    $('track').style.boxShadow = '0 0 ' + (4 + 7 * pulse) + 'px #ff203d80';
    $('status').style.color = '#ff6974';
    $('status').style.opacity = String(.64 + .36 * pulse);
    $('status').style.textShadow = '0 0 ' + (4 + 7 * pulse) + 'px #ff274a80';
    $('status').style.letterSpacing = '.16em';
    $('status').style.whiteSpace = 'nowrap';
    $('detail').style.color = '#c9828d';
    if (!state.contained) setText('status', exportConfig.messages.findLast(message => message.at <= elapsed).text);
    else {
      setText('status', exportConfig.terminalStatus);
      setText('detail', 'CORE SECURED · SELECT RETRY TO ATTEMPT RECOVERY');
      setText('recovery-title', 'Nexus Core startup failure');
      setText('recovery-message', 'Startup could not complete.');
      setText('recovery-state', exportConfig.terminalStatus);
      setText('recovery-description', 'Choose Retry to attempt recovery, or select another recovery option.');
    }
  }
  return {
    t, faultElapsed: elapsed, charge: state.charge, iris: state.iris,
    rings: state.rings, cylinder: state.cylinder, orbit: state.orbit,
    ambientTime: state.ambientTime, contained: Boolean(state.contained),
    status: $('status').textContent, fill: $('fill').style.transform,
    color: getComputedStyle($('status')).color,
    statusFits: $('status').scrollWidth <= $('readout').clientWidth
  };
},
    capture(t, minimal = false) {`;
const recoveryHook = `exportRecoveryFrame(t) {
  const recovery = ${JSON.stringify(recoveryConfig)};
  const lastErrorTime = exportConfig.seconds - 1 / exportConfig.fps;
  this.exportErrorFrame(lastErrorTime);
  const from = clock.sample(false);
  const smoothProgress = (at, duration) => { const u = clamp((t - at) / duration); return u * u * (3 - 2 * u); };
  const mix = (a, b, amount) => a + (b - a) * amount;
  const color = (a, b, amount) => 'rgb(' + a.map((channel, i) => Math.round(mix(channel, b[i], amount))).join(',') + ')';
  let sourceTime = recovery.motion[0][1];
  for (let i = 1; i < recovery.motion.length; i++) {
    const [time, value] = recovery.motion[i], [before, prior] = recovery.motion[i - 1];
    if (t <= time) { sourceTime = mix(prior, value, clamp((t - before) / (time - before))); break; }
    sourceTime = value;
  }
  const normal = sample(manifest, sourceTime, false, sourceTime);
  const fade = smoothProgress(recovery.fadeAt, recovery.fadeSeconds);
  const restoring = smoothProgress(8.8, 1.6);
  const online = t >= recovery.onlineAt;
  const text = recovery.messages.findLast(message => message.at <= t).text;
  const state = {
    ...from, t: from.t + t, ambientTime: from.ambientTime + t,
    phase: { id: online ? 'recovered_online' : 'recovery', label: text },
    security: 1, authorization: 1,
    cylinder: normal.cylinder, rings: normal.rings, pins: normal.pins, iris: normal.iris,
    reveal: normal.reveal, charge: normal.charge, pulse: normal.pulse,
    brightness: mix(from.brightness, normal.brightness, restoring),
    orbit: from.orbit + normal.orbit, orbitOffsets: [0, 0, 0],
    energyScale: mix(from.energyScale, 1, smoothProgress(8.8, 6.2)),
    particleCount: Math.round(normal.particleCount * normal.reveal),
    warning: from.warning * (1 - fade), critical: 0, instability: 0,
    pinFlash: [0, 0, 0, 0], fault: fade < 1, online,
    contained: t < 13.5, panel: 1 - fade,
    diagnosticActive: t < 10.4,
    diagnosticAngle: from.diagnosticAngle + t * .22
  };
  const originalSample = clock.sample;
  lastExportState = state;
  clock.sample = () => state;
  paint();
  clock.sample = originalSample;
  setText('status', text);
  setText('detail', online ? 'CONTAINMENT RELEASED · ENERGY STABLE' : t >= 18.6 ? 'INTEGRITY CHECK COMPLETE · ENERGY STABLE' : t >= 15.8 ? 'CONTAINMENT RELEASED · VERIFYING INTEGRITY' : t >= 12.6 ? 'STABILITY RESTORING · CONTAINMENT CONTROLLED' : 'CORE SECURED · RECOVERY IN PROGRESS');
  const pulse = .5 + .5 * Math.cos(2 * Math.PI * (from.t + t) / 1.2);
  $('fill').style.transform = 'scaleX(1)';
  $('fill').style.background = 'linear-gradient(90deg,' + color([203,24,50],[76,107,255],fade) + ',' + color([255,82,88],[86,237,255],fade) + ')';
  $('fill').style.opacity = String(mix(.38 + .62 * pulse, 1, fade));
  $('fill').style.boxShadow = '0 0 9px ' + color([255,36,61],[68,204,255],fade);
  $('track').style.borderColor = color([238,89,100],[78,114,154],fade);
  $('track').style.boxShadow = '0 0 ' + mix(4 + 7 * pulse,4,fade) + 'px ' + color([180,22,45],[20,86,135],fade);
  $('status').style.color = online ? '#72ffb3' : color([255,105,116],[238,247,255],fade);
  $('status').style.opacity = String(online ? .65 + .35 * (.5 + .5 * Math.cos(2 * Math.PI * (t - recovery.onlineAt) / 2.4)) : mix(.64 + .36 * pulse,1,fade));
  $('status').style.textShadow = '0 0 9px ' + (online ? '#49e59490' : color([148,24,45],[84,124,156],fade));
  $('detail').style.color = color([201,130,141],[142,180,207],fade);
  setText('recovery-title', 'Nexus Core recovery');
  setText('recovery-message', text);
  setText('recovery-state', t < 4.4 ? 'CONTAINMENT ACTIVE' : t < 8.8 ? 'DIAGNOSTIC ANALYSIS' : 'RESTORING CORE STABILITY');
  setText('recovery-description', t < 4.4 ? 'Core isolated. Essential systems remain protected.' : t < 8.8 ? 'Recovery analysis is locating the synchronization fault.' : 'Rebuilding the core and verifying stable operation.');
  return { t, sourceTime, charge: state.charge, iris: state.iris,
    rings: state.rings, pins: state.pins, cylinder: state.cylinder,
    orbit: state.orbit, ambientTime: state.ambientTime,
    status: text, color: getComputedStyle($('status')).color,
    fill: $('fill').style.transform, barColor: $('fill').style.background,
    fade, online, statusFits: $('status').scrollWidth <= $('readout').clientWidth };
},
    capture(t, minimal = false) {`;
const failedHook = `exportFailedFrame(t) {
  const failure = ${JSON.stringify(failedConfig)};
  this.exportRecoveryFrame(failure.branchAt);
  const from = structuredClone(lastExportState);
  const elapsed = Math.max(0, t - failure.onlineLeadSeconds);
  const message = failure.messages.findLast(message => message.at <= elapsed).text;
  const warning = clamp(elapsed / .5);
  const state = { ...from, t: from.t + t, ambientTime: from.ambientTime + t,
    phase: { id: 'recovery_failed', label: message },
    diagnosticAngle: from.diagnosticAngle + .22 * Math.min(t, 3.2),
    diagnosticActive: t < 3.2, warning: 1, critical: .8 * warning,
    contained: true, panel: 1, online: false, fault: true };
  const originalSample = clock.sample;
  clock.sample = () => state;
  paint(); clock.sample = originalSample; lastExportState = state;
  const pulse = .5 + .5 * Math.cos(2 * Math.PI * state.t / 1.2);
  $('fill').style.transform = 'scaleX(1)';
  $('fill').style.background = 'linear-gradient(90deg,#cb1832,#ff5258)';
  $('fill').style.opacity = String(.38 + .62 * pulse);
  $('fill').style.boxShadow = '0 0 9px #ff243d';
  $('track').style.borderColor = '#ee5964';
  $('track').style.boxShadow = '0 0 ' + (4 + 7 * pulse) + 'px #ff203d80';
  $('status').style.color = '#ff6974';
  $('status').style.opacity = String(.64 + .36 * pulse);
  $('status').style.textShadow = '0 0 9px #94182d';
  $('detail').style.color = '#c9828d';
  setText('status', t < failure.onlineLeadSeconds ? 'CORE RECONSTRUCTION · IN PROGRESS' : message);
  setText('detail', t < failure.onlineLeadSeconds ? 'CORE SECURED · RECOVERY IN PROGRESS' : elapsed < 8.1 ? 'CORE SECURED · AUTOMATIC RECOVERY HALTED' : 'CORE SECURED · MANUAL RECOVERY REQUIRED');
  setText('recovery-title', t < failure.onlineLeadSeconds ? 'Nexus Core recovery' : 'Nexus Core recovery failed');
  setText('recovery-message', t < failure.onlineLeadSeconds ? 'CORE RECONSTRUCTION · IN PROGRESS' : message);
  setText('recovery-state', t < failure.onlineLeadSeconds ? 'RESTORING CORE STABILITY' : elapsed < 8.1 ? 'AUTOMATIC RECOVERY HALTED' : 'YOUR ATTENTION IS REQUIRED');
  setText('recovery-description', t < failure.onlineLeadSeconds ? 'Rebuilding the core and verifying stable operation.' : 'Automatic recovery has stopped. Review the details or choose a recovery action.');
  return { t, charge: state.charge, iris: state.iris, rings: state.rings, cylinder: state.cylinder,
    ambientTime: state.ambientTime, orbit: state.orbit, contained: state.contained, online: state.online,
    status: $('status').textContent, color: getComputedStyle($('status')).color,
    fill: $('fill').style.transform, statusFits: $('status').scrollWidth <= $('readout').clientWidth };
},
    capture(t, minimal = false) {`;
const mime = { '.html': 'text/html', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.wav': 'audio/wav' };
const server = http.createServer((req, res) => {
  const name = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  const file = path.resolve(root, '.' + name);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
  res.setHeader('Content-Type', mime[path.extname(file)] || 'application/octet-stream');
  if (name === '/web/app.mjs') {
    let script = fs.readFileSync(file, 'utf8');
    const marker = 'manifest = validateManifest(await response.json());';
    const capture = 'capture(t, minimal = false) {';
    if (!script.includes(marker) || !script.includes(capture)) throw new Error('Export hook not found');
    script = script.replace(marker, marker + setup).replace(capture, hook.replace(capture, recoveryHook.replace(capture, failedHook)));
    res.end(script);
  } else fs.createReadStream(file).pipe(res);
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ ...(process.platform === 'win32' ? { channel: 'msedge' } : {}), headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1024, height: 576 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:' + server.address().port + '/web/index.html?silent');
    await page.waitForFunction(() => Boolean(window.preview?.exportErrorFrame));
    await page.evaluate(() => document.fonts.ready);
    await page.locator('#chamber').evaluate(img => img.decode());
    const capturedStates = [];
    for (const [name, t] of [['start', 0], ['warning', .7], ['failing', 2.5], ['cascade', 4.3], ['imminent', 6.1], ['closing', 8.1], ['contained', 10.5]]) {
      const state = await page.evaluate(t => window.preview.exportErrorFrame(t), t);
      if (state.fill !== 'scaleX(1)' || !state.statusFits) throw new Error('Invalid full bar or clipped status: ' + JSON.stringify(state));
      capturedStates.push({ name, ...state });
      await page.screenshot({ path: path.join(out, 'NexusCore-Error-Continuation-' + name + '.png') });
    }
    const expected = await page.evaluate(async end => {
      const { sample } = await import('./timeline.mjs');
      const manifest = await (await fetch('../animation_manifest.json')).json();
      const state = sample(manifest, end);
      return { charge: state.charge, iris: state.iris, rings: state.rings, cylinder: state.cylinder, orbit: state.orbit, ambientTime: state.ambientTime };
    }, finalStartupTime);
    for (const key of Object.keys(expected)) {
      if (JSON.stringify(capturedStates[0][key]) !== JSON.stringify(expected[key])) throw new Error('Startup continuity mismatch: ' + key);
    }
    if (capturedStates[0].charge !== 1 || capturedStates[0].iris.some(value => value !== 1)) throw new Error('Error must begin fully powered and open');
    if (capturedStates.at(-1).status !== config.terminalStatus || !capturedStates.at(-1).contained) throw new Error('First failure must stop at user intervention');
    for (let i = 0; i < config.messages.length; i++) {
      if (capturedStates[i + 1].status !== config.messages[i].text) throw new Error('Wrong message order');
    }
    const recoveryStates = [];
    if (isRecovery) {
      for (const [i, message] of recoveryConfig.messages.entries()) {
        const t = message.at + (i === 0 ? 0 : .2);
        const state = await page.evaluate(t => window.preview.exportRecoveryFrame(t), t);
        if (state.status !== message.text || !state.statusFits || state.fill !== 'scaleX(1)') throw new Error('Recovery caption/bar mismatch: ' + JSON.stringify(state));
        recoveryStates.push(state);
        await page.screenshot({ path: path.join(out, 'NexusCore-Recovery-' + i + '.png') });
      }
      const prior = await page.evaluate(t => window.preview.exportErrorFrame(t), config.seconds - 1 / config.fps);
      for (const key of ['charge', 'iris', 'rings', 'cylinder', 'orbit', 'ambientTime']) {
        if (JSON.stringify(prior[key]) !== JSON.stringify(recoveryStates[0][key])) throw new Error('Recovery continuity mismatch: ' + key);
      }
      const white = await page.evaluate(() => window.preview.exportRecoveryFrame(15));
      if (white.color !== 'rgb(238, 247, 255)' || white.fade !== 1) throw new Error('Recovery white transition failed');
      const end = await page.evaluate(() => window.preview.exportRecoveryFrame(26.9));
      if (end.color !== 'rgb(114, 255, 179)' || !end.online || end.charge !== 1 || end.iris.some(value => value !== 1)) throw new Error('Recovery online state failed');
    }
    const failedStates = [];
    if (isFailed) {
      const branch = await page.evaluate(t => window.preview.exportRecoveryFrame(t), failedConfig.branchAt);
      const start = await page.evaluate(() => window.preview.exportFailedFrame(0));
      for (const key of ['charge', 'iris', 'rings', 'cylinder', 'ambientTime', 'orbit']) {
        if (JSON.stringify(branch[key]) !== JSON.stringify(start[key])) throw new Error('Failed recovery continuity mismatch: ' + key);
      }
      for (const [i, message] of failedConfig.messages.entries()) {
        const state = await page.evaluate(t => window.preview.exportFailedFrame(t), message.at + failedConfig.onlineLeadSeconds + .2);
        if (state.status !== message.text || !state.contained || state.online || state.iris.some(value => value !== 0) || state.charge !== 0 || !state.statusFits || state.fill !== 'scaleX(1)') throw new Error('Invalid failed recovery state: ' + JSON.stringify(state));
        failedStates.push(state);
        await page.screenshot({ path: path.join(out, 'NexusCore-Recovery-Failed-' + i + '.png') });
      }
    }
    if (!process.argv.includes('--stills')) {
      const target = path.join(__dirname, isStartup ? 'Startup-silent.mp4' : isFailed ? 'Recovery-Failed-silent.mp4' : isRecovery ? 'Recovery-silent.mp4' : 'Error-Continuation-silent.mp4');
      const encoder = spawn('ffmpeg', ['-y', '-hide_banner', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(config.fps), '-vcodec', 'png', '-i', 'pipe:0', '-an', '-vf', 'scale=1280:720:flags=lanczos', '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', target], { windowsHide: true });
      const done = once(encoder, 'close');
      encoder.stderr.on('data', chunk => process.stderr.write(chunk));
      const frames = Math.round((isStartup ? config.startupSeconds : isFailed ? failedConfig.seconds : isRecovery ? recoveryConfig.seconds : config.seconds) * config.fps);
      for (let i = 0; i < frames; i++) {
        const state = await page.evaluate(({ t, startup, recovery, failed }) => startup ? window.preview.exportStartupFrame(t) : failed ? window.preview.exportFailedFrame(t) : recovery ? window.preview.exportRecoveryFrame(t) : window.preview.exportErrorFrame(t), { t: i / config.fps, startup: isStartup, recovery: isRecovery, failed: isFailed });
        if (!isStartup && state.fill !== 'scaleX(1)') throw new Error('Progress bar emptied');
        const png = await page.screenshot({ type: 'png', animations: 'allow' });
        if (!encoder.stdin.write(png)) await once(encoder.stdin, 'drain');
        if (i % 90 === 0) console.log(i + '/' + frames + ' frames: ' + state.status);
      }
      encoder.stdin.end();
      const [code] = await done;
      if (code !== 0) throw new Error('Video encoder failed: ' + code);
      console.log('Video rendered: ' + target);
    }
    if (errors.length) throw new Error(errors.join('\n'));
    const hashNow = crypto.createHash('sha256').update(fs.readFileSync(startup)).digest('hex');
    if (hashNow !== startupHash) throw new Error('Startup changed');
    fs.writeFileSync(path.join(__dirname, isFailed ? 'recovery-failed-render-report.json' : isRecovery ? 'recovery-render-report.json' : 'error-continuation-render-report.json'), JSON.stringify({ config, recoveryConfig: isRecovery ? recoveryConfig : undefined, startupHash, continuityPassed: true, capturedStates, recoveryStates, failedStates, errors }, null, 2));
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
