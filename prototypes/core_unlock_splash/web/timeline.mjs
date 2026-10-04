export const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
export const smooth = x => { x = clamp(x); return x * x * (3 - 2 * x); };
export const ramp = (t, start, duration) => smooth((t - start) / duration);
const smoothIntegral = x => x <= 0 ? 0 : x >= 1 ? x - .5 : x ** 3 - .5 * x ** 4;

export function validateManifest(m) {
  if (m?.schemaVersion !== 1 || !Number.isFinite(m.duration) || m.duration <= 0)
    throw new Error('Unsupported timeline');
  for (const group of ['phases', 'gates', 'events']) {
    if (!Array.isArray(m[group]) || !m[group].length) throw new Error(`Missing ${group}`);
    let previous = -1;
    const ids = new Set();
    for (const e of m[group]) {
      if (!e.id || ids.has(e.id) || !Number.isFinite(e.at) || e.at < previous || e.at > m.duration)
        throw new Error(`Invalid ${group} entry`);
      if (e.sound && !m.sounds?.[e.sound]) throw new Error(`Unmapped sound: ${e.id}`);
      if (e.duration !== undefined && !(e.duration > 0)) throw new Error('Invalid duration');
      if (e.gain !== undefined && !(e.gain >= 0 && e.gain <= 1)) throw new Error('Invalid gain');
      if (e.until !== undefined && !(e.until > e.at)) throw new Error('Invalid event end');
      if (e.pan !== undefined && !(Math.abs(e.pan) <= 1)) throw new Error('Invalid pan');
      ids.add(e.id); previous = e.at;
    }
  }
  for (const path of Object.values(m.sounds))
    if (!/^audio\/[a-z0-9_]+\.wav$/.test(path)) throw new Error('Unsafe audio path');
  return m;
}

// All geometry is a pure function of elapsed seconds. Missed frames never change the path.
export function sample(m, seconds, reduced = false, ambientSeconds = seconds) {
  const t = Math.max(0, seconds);
  const e = id => m.events.find(x => x.id === id);
  const progress = id => ramp(t, e(id).at, e(id).duration);
  const mechanical = id => reduced
    ? ramp(t, e(id).at, Math.min(e(id).duration, 0.18)) : progress(id);
  const turn = mechanical('lock_turn_start');
  // A damped 2-degree end-stop recoil, absent in reduced motion.
  const recoil = !reduced && turn > 0.88 && turn < 1
    ? Math.sin((turn - 0.88) / 0.12 * Math.PI * 2) * (1 - turn) * 0.35 : 0;
  const rings = mechanical('ring_rotation_start');
  const irisEvent = e('iris_open_start');
  const charge = progress('core_charge_start');
  const online = e('online_pulse');
  const pulsePhase = clamp((t - online.at) / online.duration);
  const pulse = reduced || pulsePhase === 0 || pulsePhase === 1 ? 0 : Math.sin(pulsePhase * Math.PI) ** 2;
  const chargeEvent = e('core_charge_start');
  const settleAt = m.phases.find(p => p.id === 'stabilization').at;
  const settleDuration = e('stable_online').at - settleAt;
  return {
    t, ambientTime: ambientSeconds, reduced,
    phase: m.phases.findLast(p => p.at <= t),
    security: ramp(t, m.phases.find(p => p.id === 'security').at, 0.8),
    authorization: ramp(t, e('authorization_start').at, 0.7),
    cylinder: (reduced ? 0.22 : Math.PI / 2) * turn + recoil,
    pins: [1, 2, 3, 4].map(i => mechanical(`pin_${i}_release`)),
    rings: reduced ? [0, 0, 0] : [0.6 * rings, -0.85 * rings, 1.1 * rings],
    iris: Array.from({ length: 6 }, (_, i) =>
      ramp(t, irisEvent.at + (reduced ? 0 : i * 0.018), reduced ? 0.2 : 0.78)),
    reveal: ramp(t, irisEvent.at, reduced ? 0.2 : 0.85),
    charge, pulse, pulsePhase,
    brightness: 0.2 + 0.22 * ramp(t, e('authorization_start').at, 0.7) + 0.58 * charge,
    particleCount: reduced ? 0 : 36,
    // Integral of smooth velocity ramps: acceleration then stabilization, no jumps.
    orbit: reduced ? 0.2 : .08 * Math.max(0, t - e('core_visible').at)
      + .23 * chargeEvent.duration * smoothIntegral((t - chargeEvent.at) / chargeEvent.duration)
      - .14 * settleDuration * smoothIntegral((t - settleAt) / settleDuration)
      + .12 * Math.max(0, ambientSeconds - t),
    online: t >= e('stable_online').at
  };
}

export function eventsBetween(m, from, to) {
  return to < from ? [] : m.events.filter(e => e.at > from && e.at <= to);
}

// Clock inputs are performance.now() milliseconds in the browser, injectable in tests.
export class Timeline {
  constructor(manifest, now = () => performance.now()) {
    this.manifest = validateManifest(manifest); this.now = now;
    this.position = 0; this.ambientPosition = 0; this.anchor = now(); this.speed = 1; this.playing = false;
    this.closedGates = new Set();
  }
  get ceiling() {
    return Math.min(Infinity, ...this.manifest.gates.filter(g => this.closedGates.has(g.id)).map(g => g.at - 1e-6));
  }
  get time() {
    return Math.min(this.ceiling, this.position + (this.playing ? Math.max(0, this.now() - this.anchor) / 1000 * this.speed : 0));
  }
  get held() { return this.playing && Number.isFinite(this.ceiling) && this.time >= this.ceiling; }
  get ambientTime() { return this.ambientPosition + (this.playing ? Math.max(0, this.now() - this.anchor) / 1000 * this.speed : 0); }
  rebase() { this.ambientPosition = this.ambientTime; this.position = this.time; this.anchor = this.now(); }
  play() { this.rebase(); this.playing = true; }
  pause() { this.rebase(); this.playing = false; }
  seek(t) {
    if (!Number.isFinite(t)) throw new Error('Invalid seek');
    this.position = clamp(t, 0, Math.min(this.manifest.duration, this.ceiling)); this.ambientPosition = this.position; this.anchor = this.now();
  }
  replay() { this.seek(0); this.play(); }
  setSpeed(value) {
    if (!Number.isFinite(value) || value < 0.25 || value > 2) throw new Error('Speed must be 0.25–2');
    this.rebase(); this.speed = value;
  }
  setGate(id, released) {
    if (!this.manifest.gates.some(g => g.id === id)) throw new Error(`Unknown gate: ${id}`);
    this.rebase();
    if (!released && this.position >= this.manifest.gates.find(g => g.id === id).at)
      throw new Error('Close gates before their boundary; replay before testing a late gate');
    if (released) this.closedGates.delete(id); else this.closedGates.add(id);
  }
}
