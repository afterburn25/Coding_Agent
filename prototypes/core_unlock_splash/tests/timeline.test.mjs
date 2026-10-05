import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Timeline, sample, eventsBetween, validateManifest } from '../web/timeline.mjs';

const manifest = JSON.parse(readFileSync(new URL('../animation_manifest.json', import.meta.url)));
const fixture = () => { let now = 0; const clock = new Timeline(manifest, () => now); return { clock, advance: seconds => { now += seconds * 1000; } }; };

test('manifest orders every phase and event and maps every stem', () => {
  validateManifest(manifest);
  assert.equal(manifest.events.filter(e => e.sound).length, 16);
  for (const file of Object.values(manifest.sounds)) assert.ok(readFileSync(new URL(`../${file}`, import.meta.url)).length > 44);
  const ids = manifest.events.map(e => e.id);
  for (const [first, later] of [['lock_turn_start', 'pin_1_release'], ['pin_4_release', 'ring_rotation_start'], ['ring_rotation_start', 'iris_open_start'], ['core_visible', 'core_charge_start'], ['core_charge_start', 'online_pulse']]) assert.ok(ids.indexOf(first) < ids.indexOf(later));
});
test('invalid ordering, unmapped or unsafe assets fail locally', () => {
  for (const mutate of [m => m.events[1].at = -1, m => m.events[2].id = m.events[1].id, m => m.events[0].sound = 'missing', m => m.sounds.ambient_hum = '../secret.wav']) {
    const m = structuredClone(manifest); mutate(m); assert.throws(() => validateManifest(m));
  }
});
test('same elapsed time gives identical geometry despite missed frames', () => {
  const a = fixture(), b = fixture(); a.clock.play(); b.clock.play();
  for (let i = 0; i < 450; i++) { a.advance(1 / 60); sample(manifest, a.clock.time); }
  b.advance(7.5);
  const sa = sample(manifest, a.clock.time), sb = sample(manifest, b.clock.time);
  assert.ok(Math.abs(sa.t - sb.t) < 1e-9);
  for (const key of ['brightness', 'pulse', 'cylinder', 'charge']) assert.ok(Math.abs(sa[key] - sb[key]) < 1e-9);
  assert.deepEqual(sample(manifest, 5.125), sample(manifest, 5.125));
});
test('dormant geometry stays closed through authorization', () => {
  for (const t of [0, .5, 1.3, 2.3]) {
    const s = sample(manifest, t);
    assert.deepEqual(s.iris, [0, 0, 0, 0, 0, 0]); assert.deepEqual(s.pins, [0, 0, 0, 0]); assert.equal(s.cylinder, 0);
  }
});
test('pins release sequentially and fully before rings turn', () => {
  assert.ok(sample(manifest, 3.28).pins[0] > 0); assert.equal(sample(manifest, 3.28).pins[1], 0);
  const s = sample(manifest, 3.94); assert.deepEqual(s.pins, [1, 1, 1, 1]); assert.deepEqual(s.rings, [0, -0, 0]);
});
test('six iris plates finish opening and remain rigid transform values', () => {
  const mid = sample(manifest, 5.15); assert.equal(mid.iris.length, 6); assert.ok(mid.iris[0] > mid.iris[5]);
  assert.deepEqual(sample(manifest, 5.65).iris, [1, 1, 1, 1, 1, 1]);
});
test('pause freezes time; speed changes preserve continuity', () => {
  const { clock, advance } = fixture(); clock.play(); advance(1); clock.pause(); advance(100); assert.equal(clock.time, 1);
  clock.setSpeed(.5); clock.play(); advance(2); assert.equal(clock.time, 2);
  clock.setSpeed(2); assert.equal(clock.time, 2); advance(1); assert.equal(clock.time, 4);
});
test('replay resets all geometry without stale event state', () => {
  const { clock, advance } = fixture(); clock.play(); advance(manifest.duration + 1); assert.ok(sample(manifest, clock.time).online);
  clock.replay(); assert.equal(clock.time, 0); assert.deepEqual(sample(manifest, clock.time), sample(manifest, 0));
});
test('long holds discard waiting wall time rather than jumping on release', () => {
  for (const gate of manifest.gates) {
    const { clock, advance } = fixture(); clock.setGate(gate.id, false); clock.play(); advance(600);
    assert.ok(clock.held); assert.ok(clock.time < gate.at); const before = clock.time;
    clock.setGate(gate.id, true); assert.equal(clock.time, before); advance(.1); assert.ok(Math.abs(clock.time - before - .1) < 1e-9);
  }
});
test('readiness gate prevents pulse even when animation is fast', () => {
  const { clock, advance } = fixture(); clock.setGate('ready', false); clock.setSpeed(2); clock.play(); advance(90);
  const s = sample(manifest, clock.time); assert.equal(s.pulse, 0); assert.equal(s.online, false);
  assert.throws(() => clock.setGate('unlock', false));
});
test('early readiness still finishes the mechanical sequence', () => {
  const { clock, advance } = fixture(); clock.setGate('ready', false); clock.play(); advance(.2); clock.setGate('ready', true);
  assert.equal(clock.time, .2); assert.equal(sample(manifest, clock.time).online, false);
});
test('event crossing includes skipped frames once, never on backward seek', () => {
  const crossed = eventsBetween(manifest, 3, 4); assert.equal(crossed.filter(e => e.id.startsWith('pin_')).length, 4);
  assert.deepEqual(eventsBetween(manifest, 4, 3), []); assert.deepEqual(eventsBetween(manifest, 3.15, 3.15), []);
});
test('reduced motion keeps meaning and suppresses orbit, particles, and pulse', () => {
  const sealed = sample(manifest, 0, true), online = sample(manifest, 12.6, true);
  assert.equal(sealed.reveal, 0); assert.equal(online.reveal, 1); assert.equal(online.online, true);
  assert.equal(online.particleCount, 0); assert.equal(online.pulse, 0); assert.deepEqual(online.rings, [0, 0, 0]);
  assert.ok(Math.abs(online.cylinder) < Math.abs(sample(manifest, 12.6).cylinder));
});
test('settled mechanics never spin indefinitely', () => {
  const online = sample(manifest, 8), later = sample(manifest, 200);
  for (const key of ['rings', 'iris', 'pins', 'cylinder']) assert.deepEqual(later[key], online[key]);
  assert.equal(later.pulse, 0); assert.ok(later.orbit > online.orbit);
});
test('malformed transport commands cannot corrupt the clock', () => {
  const { clock } = fixture();
  for (const speed of [NaN, Infinity, 0, -1, 3]) assert.throws(() => clock.setSpeed(speed));
  assert.throws(() => clock.seek(NaN)); assert.throws(() => clock.setGate('unknown', false));
});
test('a full-charge wait sustains brightness and slow idle motion without declaring online', () => {
  const { clock, advance } = fixture(); clock.setGate('ready', false); clock.play(); advance(20);
  const first = sample(manifest, clock.time, false, clock.ambientTime);
  advance(100); const later = sample(manifest, clock.time, false, clock.ambientTime);
  assert.equal(first.charge, 1); assert.equal(later.brightness, 1); assert.equal(later.online, false);
  assert.ok(later.orbit > first.orbit); assert.equal(later.pulse, 0);
  clock.pause(); const paused = clock.ambientTime; advance(10); assert.equal(clock.ambientTime, paused);
});
