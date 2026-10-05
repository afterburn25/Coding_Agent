import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { SplashController, sampleFault, isContained, RECOVERY_STATES } from '../web/controller.mjs';
import { sample, validateManifest } from '../web/timeline.mjs';
const m = JSON.parse(readFileSync(new URL('../animation_manifest.json', import.meta.url)));
const fixture = () => { let ms = 0; const c = new SplashController(m, () => ms); return { c, advance: t => { ms += t * 1000; c.update(); } }; };

test('normal path is unchanged at every phase', () => {
  const { c } = fixture();
  for (const p of m.phases) { c.seek(p.at); assert.deepEqual(c.sample(), sample(m, p.at)); }
});
test('fault from every startup phase preserves exact current component values', () => {
  for (const at of [...m.phases.map(p => p.at), 2.81, 3.4, 5.15, 7.8, 11.799]) {
    const { c } = fixture(); c.seek(at); const before = c.sample(); c.triggerFault(); const after = c.sample();
    for (const field of ['iris', 'pins', 'rings', 'cylinder', 'reveal', 'brightness', 'charge', 'orbit']) assert.deepEqual(after[field], before[field], `${field} at ${at}`);
    c.seek(m.failure.duration); assert.ok(isContained(c.sample()), `not contained from ${at}`);
    assert.deepEqual(c.sample().rings, [0, 0, 0]); assert.equal(c.sample().online, false);
  }
});
test('half-open iris never jumps fully open; closure is monotonic', () => {
  const from = sample(m, 5.15); assert.ok(from.iris[0] > .4 && from.iris[0] < .6);
  let previous = from.iris;
  for (let t = 0; t <= m.failure.duration; t += .01) {
    const s = sampleFault(m, from, t);
    s.iris.forEach((v, i) => { assert.ok(v <= previous[i] + 1e-12); assert.ok(v >= 0); }); previous = s.iris;
  }
});
test('fault phase order, pin order and bounded recovery presentation', () => {
  validateManifest(m); assert.ok(m.failure.duration >= 5 && m.failure.duration <= 6);
  assert.deepEqual(m.failure.events.filter(e => e.id.startsWith('pin_')).map(e => e.id), ['pin_3_lock', 'pin_4_lock', 'pin_2_lock', 'pin_1_lock']);
  assert.equal(sampleFault(m, sample(m, 8), .3).phase.id, 'CORE_INSTABILITY');
  assert.equal(sampleFault(m, sample(m, 8), m.failure.duration).panel, 1);
  const broken = structuredClone(m); broken.failure.events[2].sound = 'missing'; assert.throws(() => validateManifest(broken));
});
test('instability is visible, localized, and suppressed in reduced motion', () => {
  const from = sample(m, 10), s = sampleFault(m, from, .32);
  assert.ok(s.orbitOffsets.some(x => Math.abs(x) > .05)); assert.ok(s.charge !== from.charge || s.brightness !== from.brightness);
  assert.ok(s.warning > 0); assert.equal(s.critical, 0);
  const reduced = sampleFault(m, from, .32, true);
  assert.deepEqual(reduced.orbitOffsets, [0, 0, 0]); assert.equal(reduced.particleCount, 0); assert.equal(reduced.pulse, 0);
});
test('duplicate errors update bounded diagnostics without restarting or changing snapshot', () => {
  const { c, advance } = fixture(); c.seek(7.8); assert.equal(c.triggerFault({ detail: 'first' }, false, .53), true);
  const from = c.faultFrom; advance(.8); const elapsed = c.time;
  for (let i = 0; i < 100; i++) assert.equal(c.triggerFault({ detail: 'latest', message: 'x'.repeat(10000) }), false);
  assert.equal(c.time, elapsed); assert.equal(c.faultFrom, from); assert.equal(c.diagnostics.detail, 'latest');
  assert.equal(c.diagnostics.message.length, 500); assert.equal(c.progressAtFault, .53);
});
test('already locked parts do not schedule fabricated pin or iris motion sounds', () => {
  const { c } = fixture(); c.seek(1.9); c.triggerFault();
  assert.ok(!c.audioEvents.some(e => e.id.startsWith('pin_') || e.id === 'iris_close_start'));
  c.resetNormal(); c.seek(3.3); c.triggerFault();
  assert.deepEqual(c.audioEvents.filter(e => e.id.startsWith('pin_')).map(e => e.id), ['pin_1_lock']);
});
test('recovery hooks retain closure and only host-supplied attempt counters appear', () => {
  const { c } = fixture(); assert.throws(() => c.setRecoveryState('REPAIR_ATTEMPT'));
  c.seek(7.8); c.triggerFault(); c.seek(m.failure.duration);
  for (const mode of Object.keys(RECOVERY_STATES).filter(s => s !== 'SAFE_MODE')) { c.setRecoveryState(mode); assert.ok(isContained(c.sample())); assert.equal(c.attempt, null); }
  c.setRecoveryState('REPAIR_ATTEMPT', { attempt: 2, total: 3 }); assert.deepEqual(c.attempt, { attempt: 2, total: 3 });
  assert.throws(() => c.setRecoveryState('REPAIR_ATTEMPT', { attempt: 4, total: 3 }));
  assert.throws(() => c.setRecoveryState('fake'));
});
test('rollback scans in reverse while mechanical containment stays locked', () => {
  const { c, advance } = fixture(); c.seek(8); c.triggerFault(); advance(m.failure.duration + .05); c.setRecoveryState('ROLLBACK');
  const a = c.sample(); advance(5); const b = c.sample(); assert.ok(b.diagnosticAngle < a.diagnosticAngle);
  assert.deepEqual(a.rings, b.rings); assert.ok(isContained(b));
});
test('Safe Mode immediately skips the cinematic, stops motion and schedules no audio', () => {
  const { c, advance } = fixture(); c.seek(8); c.triggerFault(); c.setRecoveryState('SAFE_MODE');
  const a = c.sample(); assert.ok(isContained(a)); assert.equal(a.panel, 1); assert.equal(c.playing, false);
  c.play(); advance(600); assert.deepEqual(c.sample(), a); assert.deepEqual(c.audioEvents, []); assert.equal(c.repairSuccess(), false);
  c.replay(); assert.equal(c.playing, false); assert.ok(isContained(c.sample()));
});
test('recovery controls can bypass presentation immediately without changing recovery policy', () => {
  const { c } = fixture(); c.seek(7.8); c.triggerFault(); c.showRecoveryImmediately();
  assert.ok(isContained(c.sample())); assert.equal(c.sample().panel, 1); assert.equal(c.recoveryState, 'RECOVERY_ANALYZING');
});
test('early repair success waits for containment then reauthorizes without releasing readiness', () => {
  const { c, advance } = fixture(); c.setGate('ready', false); c.seek(7.8); c.triggerFault();
  assert.equal(c.repairSuccess(), false); advance(m.failure.duration); assert.equal(c.mode, 'repair');
  advance(.55); assert.equal(c.mode, 'normal'); assert.equal(c.time, 1.6);
  advance(100); assert.ok(c.held); assert.equal(c.sample().online, false);
  c.setGate('ready', true); advance(1); assert.ok(c.sample().online);
});
test('a new retry failure is allowed, but only after explicit recovery success', () => {
  const { c, advance } = fixture(); c.seek(8); c.triggerFault(); advance(m.failure.duration); c.repairSuccess(); advance(.55);
  advance(1.3); const before = c.sample(); assert.equal(c.triggerFault(), true); assert.deepEqual(c.sample().cylinder, before.cylinder);
});
test('fault pause, speed and replay remain deterministic', () => {
  const { c, advance } = fixture(); c.seek(5.15); c.triggerFault(); advance(.3); c.pause(); const state = c.sample();
  advance(50); assert.deepEqual(c.sample(), state); c.setSpeed(2); c.play(); advance(.1); assert.ok(Math.abs(c.time - .5) < 1e-9);
  c.replay(); assert.equal(c.time, 0); assert.deepEqual(c.sample().iris, c.faultFrom.iris);
});
