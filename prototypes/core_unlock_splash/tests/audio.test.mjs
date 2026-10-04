import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { AudioEngine } from '../web/audio.mjs';
import { Timeline } from '../web/timeline.mjs';
import { SplashController } from '../web/controller.mjs';
const manifest = JSON.parse(readFileSync(new URL('../animation_manifest.json', import.meta.url)));

class Param { value = 0; ramps = []; cancelAndHoldAtTime() {} setValueAtTime(v) { this.value = v; } linearRampToValueAtTime(v, t) { this.value = v; this.ramps.push({ v, t }); } setTargetAtTime(v) { this.value = v; } }
class Node { gain = new Param(); pan = new Param(); playbackRate = new Param(); connect() {} disconnect() { this.disconnected = true; } start(...args) { this.started = args; } stop(at) { this.stopped = true; this.stoppedAt = at; } }
class Context {
  currentTime = 10; destination = {}; state = 'running';
  createGain() { return new Node(); } createStereoPanner() { return new Node(); }
  createBufferSource() { return new Node(); }
  createAnalyser() { return { connect() {}, fftSize: 256, getFloatTimeDomainData(a) { a.fill(0); } }; }
  createDynamicsCompressor() { return { ...new Node(), threshold: new Param(), knee: new Param(), ratio: new Param(), attack: new Param(), release: new Param(), connect() {} }; }
  decodeAudioData() { return Promise.resolve({ duration: 4 }); }
  async resume() { this.state = 'running'; } async close() { this.state = 'closed'; }
}
const make = () => {
  const audio = new AudioEngine(manifest); audio.context = new Context(); audio.master = new Node(); audio.loading = Promise.resolve(true);
  for (const id of Object.keys(manifest.sounds)) audio.buffers.set(id, { duration: id.includes('hum') ? 4 : 1 });
  const clock = new Timeline(manifest, () => 0); clock.play(); return { audio, clock };
};

test('silent mode never constructs or decodes audio', async () => {
  const a = new AudioEngine(manifest, { disabled: true, factory: () => { throw Error('Must not initialize'); } });
  assert.equal(await a.initialize(), false); await a.sync({ playing: true }); assert.equal(a.context, null); assert.equal(a.sources.length, 0);
});
test('audio initialization failure is isolated', async () => {
  const a = new AudioEngine(manifest, { factory: () => { throw Error('Device unavailable'); } });
  assert.equal(await a.initialize(), false); assert.ok(a.disabled); assert.ok(a.warnings[0].includes('Device unavailable'));
});
test('missing audio asset is nonfatal and other stems still load', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async url => ({ ok: !String(url).endsWith('/pin_02.wav'), status: 404, arrayBuffer: async () => new ArrayBuffer(8) });
  try {
    const a = new AudioEngine(manifest, { factory: () => new Context() });
    assert.equal(await a.initialize(), true); assert.equal(a.buffers.size, Object.keys(manifest.sounds).length - 1); assert.equal(a.warnings.length, 1); await a.dispose();
  } finally { globalThis.fetch = original; }
});
test('mute selected before Play remains silent when audio initializes', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(8) });
  try {
    const a = new AudioEngine(manifest, { factory: () => new Context() });
    a.setMuted(true); await a.initialize(); assert.equal(a.master.gain.value, 0); await a.dispose();
  } finally { globalThis.fetch = original; }
});
test('all four pin sounds are sample-clock scheduled at their visual timestamps', async () => {
  const { audio, clock } = make(); await audio.sync(clock);
  for (const event of manifest.events.filter(e => e.id.startsWith('pin_'))) {
    const sound = audio.scheduled.find(s => s.id === event.id); assert.equal(sound.when, 10 + event.at); assert.equal(sound.offset, 0);
  }
});
test('seeking skips expired stems and offsets active ones', async () => {
  const { audio, clock } = make(); clock.seek(2.7); await audio.sync(clock);
  assert.ok(!audio.scheduled.some(s => s.id === 'security_start'));
  assert.ok(Math.abs(audio.scheduled.find(s => s.id === 'lock_turn_start').offset - .35) < 1e-9);
});
test('speed controls audio start time and sample playback rate together', async () => {
  const { audio, clock } = make(); clock.setSpeed(.5); await audio.sync(clock);
  const pin = audio.scheduled.find(s => s.id === 'pin_1_release'); assert.equal(pin.when, 16.3); assert.equal(pin.speed, .5);
});
test('no online pulse is scheduled across a closed readiness gate', async () => {
  const { audio, clock } = make(); clock.setGate('ready', false); await audio.sync(clock);
  assert.ok(!audio.scheduled.some(s => s.id === 'online_pulse'));
  clock.setGate('ready', true); await audio.sync(clock); assert.ok(audio.scheduled.some(s => s.id === 'online_pulse'));
});
test('pause and replay remove stale scheduled sources', async () => {
  const { audio, clock } = make(); await audio.sync(clock); const prior = [...audio.sources];
  clock.pause(); await audio.sync(clock); assert.equal(audio.sources.length, 0); assert.ok(prior.every(s => s.source.stopped));
  clock.replay(); await audio.sync(clock); assert.equal(audio.scheduled.filter(s => s.id === 'pin_1_release').length, 1);
});
test('mute changes master gain without affecting timeline', () => {
  const { audio, clock } = make(); audio.setVolume(.45); audio.setMuted(true); assert.equal(audio.master.gain.value, 0); assert.equal(clock.time, 0);
  audio.setMuted(false); assert.equal(audio.master.gain.value, .45);
});
test('a paused transport cannot be resurrected by late audio initialization', async () => {
  const { audio, clock } = make(); let complete;
  audio.loading = new Promise(resolve => { complete = resolve; });
  const pending = audio.sync(clock); clock.pause(); await audio.sync(clock); complete(true); await pending; assert.equal(audio.sources.length, 0);
});
test('full-charge readiness holds loop the charged hum indefinitely, not dormant ambience', async () => {
  const { audio } = make(); let now = 0;
  const clock = new Timeline(manifest, () => now); clock.setGate('ready', false); clock.play(); now = 120000;
  await audio.sync(clock);
  assert.equal(audio.scheduled.length, 1); assert.equal(audio.scheduled[0].id, 'charged_hold_hum');
  assert.equal(audio.sources[0].source.loop, true); assert.ok(!audio.sources[0].source.stopped);
  const old = audio.sources[0].source;
  clock.setGate('ready', true); await audio.sync(clock); assert.ok(old.stopped);
  assert.ok(audio.scheduled.some(s => s.id === 'online_pulse'));
});

test('fault audio replaces future startup cues with a 160ms outgoing ramp', async () => {
  const { audio } = make(); const c = new SplashController(manifest, () => 0); c.seek(7.8); c.play(); await audio.sync(c);
  const outgoing = [...audio.sources]; c.triggerFault(); await audio.sync(c, { fade: .16 });
  assert.ok(outgoing.every(s => s.gain.gain.ramps.at(-1).t === 10.16 && s.source.stoppedAt === 10.162));
  assert.ok(audio.scheduled.some(s => s.id === 'instability_start')); assert.ok(!audio.scheduled.some(s => s.id === 'online_pulse'));
  assert.deepEqual(audio.scheduled.filter(s => s.id.startsWith('pin_')).map(s => s.id), ['pin_3_lock', 'pin_4_lock', 'pin_2_lock', 'pin_1_lock']);
  assert.equal(audio.scheduled.find(s => s.id === 'pin_3_lock').when, 10 + manifest.failure.events.find(e => e.id === 'pin_3_lock').at);
});
test('muted fault never raises the master gain and silent fault never initializes', async () => {
  const { audio } = make(); const c = new SplashController(manifest, () => 0); c.seek(8); c.triggerFault();
  audio.setMuted(true); await audio.sync(c); assert.equal(audio.master.gain.value, 0);
  const silent = new AudioEngine(manifest, { disabled: true, factory: () => { throw Error('Must not run'); } });
  await silent.sync(c); assert.equal(silent.context, null); assert.equal(silent.sources.length, 0);
});
test('missing failure stem leaves remaining containment audio available', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async url => ({ ok: !String(url).endsWith('/emergency_iris_close.wav'), status: 404, arrayBuffer: async () => new ArrayBuffer(8) });
  try {
    const audio = new AudioEngine(manifest, { factory: () => new Context() }); const c = new SplashController(manifest, () => 0); c.seek(8); c.triggerFault();
    await audio.sync(c); assert.equal(audio.warnings.length, 1); assert.ok(!audio.scheduled.some(s => s.id === 'iris_close_start'));
    assert.ok(audio.scheduled.some(s => s.id === 'fault_contained')); await audio.dispose();
  } finally { globalThis.fetch = original; }
});
test('Safe Mode cancels every cue, including the emergency loop', async () => {
  const { audio } = make(); const c = new SplashController(manifest, () => 0); c.seek(8); c.triggerFault(); c.seek(manifest.failure.duration); await audio.sync(c);
  assert.ok(audio.scheduled.some(s => s.id === 'emergency_idle'));
  c.setRecoveryState('SAFE_MODE'); await audio.sync(c); assert.equal(audio.sources.length, 0);
});
