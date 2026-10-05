import { Timeline, sample, clamp, ramp } from './timeline.mjs';

export const RECOVERY_STATES = Object.freeze({
  RECOVERY_ANALYZING: ['RECOVERY · ANALYZING', 'Diagnosing startup failure.'],
  REPAIR_ATTEMPT: ['REPAIR · IN PROGRESS', 'Attempting automatic recovery.'],
  RESTARTING: ['PREPARING TO RESTART', 'Containment remains secured.'],
  ROLLBACK: ['RESTORING · LAST KNOWN GOOD', 'Rolling back to a verified system state.'],
  SAFE_MODE: ['SAFE MODE · INITIALIZING', 'Starting essential systems only.'],
  HUMAN_INTERVENTION_REQUIRED: ['NEXUS CORE · COULD NOT START', 'Automatic recovery was unable to restore core services.']
});
const mix = (a, b, t) => a + (b - a) * t;
const closed = s => s.iris.every(v => v === 0) && s.pins.every(v => v === 0) && s.cylinder === 0;

// Pure interruption sampler: every moving component starts at its captured value.
// Emergency motion has its own easing/order; it never runs normal frames backwards.
export function sampleFault(manifest, from, seconds, reduced = false) {
  const f = manifest.failure, t = Math.max(0, seconds);
  const event = id => f.events.find(e => e.id === id);
  const progress = id => { const e = event(id); return ramp(t, e.at, e.duration); };
  const drop = progress('power_drop_start');
  const dropEvent = event('power_drop_start'), closeEvent = event('iris_close_start');
  const instability = reduced ? 0 : ramp(t, .08, .45) * (1 - drop);
  const wobble = -instability * (.035 + .19 * (.5 + .5 * Math.sin(t * 6.4 + .4 * Math.sin(t * 3.8))));
  const iris = from.iris.map((v, i) => v * (1 - ramp(t, closeEvent.at + (reduced ? 0 : i * .008), reduced ? .28 : closeEvent.duration - .04)));
  const alignment = event('containment_alignment');
  const rings = from.rings.map((v, i) => { const p = ramp(t, alignment.at + [.13, .21, 0][i], alignment.duration - .21); return p === 1 ? 0 : v * (1 - p); });
  if (!reduced) rings[2] += Math.sin(t * 53) * .005 * instability;
  const pins = from.pins.map((v, i) => v * (1 - progress(`pin_${i + 1}_lock`)));
  const lock = progress('ring_lock');
  const cylinder = from.cylinder * (1 - lock) + (reduced ? 0 : from.cylinder * .045 * Math.sin(lock * Math.PI * 2) * lock * (1 - lock));
  const phase = f.phases.findLast(p => p.at <= t);
  return { ...from, t, ambientTime: t, reduced, phase, iris, rings, pins, cylinder,
    reveal: from.reveal * (1 - ramp(t, closeEvent.at, reduced ? .28 : closeEvent.duration)),
    charge: clamp(from.charge * (1 - drop) * (1 + wobble)),
    brightness: clamp(mix(from.brightness, .06, drop) * (1 + wobble)),
    orbit: from.orbit + (reduced ? 0 : .16 * Math.min(t, dropEvent.at) + .16 * dropEvent.duration * (() => { const u = clamp((t - dropEvent.at) / dropEvent.duration); return u - u ** 3 + .5 * u ** 4; })()),
    orbitOffsets: reduced ? [0, 0, 0] : [instability * .36 * Math.sin(t * 4.1), -instability * .46 * Math.sin(t * 2.6 + .5), instability * .22 * Math.sin(t * 5.2)],
    energyScale: 1 - .86 * drop, instability,
    pulse: from.pulse * (1 - ramp(t, 0, .12)),
    particleCount: reduced ? 0 : Math.round(from.particleCount * (1 - drop)),
    warning: ramp(t, .1, .35), critical: ramp(t, event('warning_active').at, .3) * (1 - ramp(t, event('fault_contained').at, .3)),
    pinFlash: pins.map((_, i) => { const e = event(`pin_${i + 1}_lock`); return from.pins[i] > .01 ? Math.max(0, 1 - Math.abs(t - e.at - e.duration) / .07) : 0; }),
    fault: true, online: false, contained: t >= event('fault_contained').at,
    panel: ramp(t, event('recovery_ui').at, event('recovery_ui').duration),
    diagnosticAngle: 0, diagnosticActive: false
  };
}

export class SplashController {
  constructor(manifest, now = () => performance.now()) {
    this.manifest = manifest; this.now = now; this.normal = new Timeline(manifest, now);
    this.transport = this.normal; this.mode = 'normal'; this.revision = 0;
    this.recoveryState = 'RECOVERY_ANALYZING'; this.diagnostics = { count: 0, message: '', detail: '' };
    this.pendingSuccess = false; this.progressAtFault = 0;
  }
  get time() { return this.transport.time; }
  get ambientTime() { return this.transport.ambientTime; }
  get speed() { return this.transport.speed; }
  get playing() { return this.transport.playing; }
  get held() { return this.mode === 'normal' && this.normal.held; }
  get ceiling() { return this.transport.ceiling; }
  get duration() { return this.transport.manifest.duration; }
  get activeFault() { return this.mode === 'fault'; }
  sequence(spec) { return new Timeline({ schemaVersion: 1, ...spec, gates: [], sounds: this.manifest.sounds }, this.now); }
  sample(reduced = false) {
    if (this.mode === 'normal') return sample(this.manifest, this.normal.time, reduced, this.normal.ambientTime);
    if (this.mode === 'repair') {
      const p = ramp(this.time, 0, this.manifest.repair.duration);
      const target = sample(this.manifest, this.resumeAt, reduced);
      return { ...this.repairFrom, phase: this.manifest.repair.phases[0], warning: 1 - p,
        critical: 0, panel: 1 - p, security: mix(this.repairFrom.security, target.security, p),
        authorization: mix(this.repairFrom.authorization, target.authorization, p),
        fault: p < 1, diagnosticActive: false, reduced };
    }
    const s = sampleFault(this.manifest, this.faultFrom, this.time, reduced);
    if (s.contained) {
      const safe = this.recoveryState === 'SAFE_MODE';
      s.recoveryState = this.recoveryState;
      s.diagnosticActive = !safe && ['RECOVERY_ANALYZING', 'REPAIR_ATTEMPT', 'ROLLBACK'].includes(this.recoveryState);
      s.diagnosticAngle = reduced || safe ? 0 : Math.max(0, this.time - this.manifest.failure.events.find(e => e.id === 'fault_contained').at) * (this.recoveryState === 'ROLLBACK' ? -.32 : .22);
      if (this.time >= this.manifest.failure.duration || safe)
        s.phase = { id: this.recoveryState, label: RECOVERY_STATES[this.recoveryState][0] };
      if (safe) { s.panel = 1; s.particleCount = 0; s.reduced = true; }
    }
    return s;
  }
  triggerFault(details = {}, reduced = false, progress = 0) {
    this.diagnostics = { count: Math.min(9999, this.diagnostics.count + 1),
      message: String(details.message ?? 'Core initialization could not complete.').slice(0, 500),
      detail: String(details.detail ?? '').slice(0, 12000) };
    if (this.activeFault) return false; // More diagnostics, never another cinematic loop.
    this.faultFrom = structuredClone(this.sample(reduced));
    this.normal.pause(); this.transport.pause(); this.mode = 'fault';
    this.transport = this.sequence(this.manifest.failure); this.transport.play();
    this.progressAtFault = clamp(progress); this.recoveryState = 'RECOVERY_ANALYZING';
    this.attempt = null; this.pendingSuccess = false; this.revision++; return true;
  }
  setRecoveryState(state, { attempt, total, message } = {}) {
    if (!Object.hasOwn(RECOVERY_STATES, state)) throw new Error('Unknown recovery state');
    if (!this.activeFault) throw new Error('Recovery requires a startup fault');
    if (attempt !== undefined || total !== undefined) {
      if (!Number.isInteger(attempt) || !Number.isInteger(total) || attempt < 1 || total < attempt || total > 999) throw new Error('Invalid recovery attempt');
      this.attempt = { attempt, total };
    } else this.attempt = null;
    if (message !== undefined) this.diagnostics.message = String(message).slice(0, 500);
    this.recoveryState = state;
    // Recovery work may report any state immediately. Only Safe Mode bypasses
    // the cosmetic closure so diagnostics become available without delay.
    if (state === 'SAFE_MODE') { this.transport.seek(this.manifest.failure.duration); this.transport.pause(); this.pendingSuccess = false; }
    this.revision++;
  }
  repairSuccess(reduced = false) {
    if (!this.activeFault || this.recoveryState === 'SAFE_MODE') return false;
    if (!this.sample(reduced).contained) { this.pendingSuccess = true; return false; }
    this.repairFrom = this.sample(reduced); this.pendingSuccess = false;
    this.transport.pause(); this.mode = 'repair';
    // Preserve readiness gates. Presentation success never releases them.
    this.resumeAt = Math.min(this.manifest.phases.find(p => p.id === 'authorization').at, this.normal.ceiling);
    this.transport = this.sequence(this.manifest.repair); this.transport.play(); this.revision++; return true;
  }
  showRecoveryImmediately() {
    if (!this.activeFault) return;
    this.transport.seek(this.manifest.failure.duration); this.pendingSuccess = false; this.revision++;
  }
  update(reduced = false) {
    if (this.activeFault && this.pendingSuccess && this.time >= this.manifest.failure.duration) this.repairSuccess(reduced);
    if (this.mode === 'repair' && this.time >= this.manifest.repair.duration) {
      this.transport.pause(); this.mode = 'normal'; this.transport = this.normal;
      this.normal.seek(this.resumeAt); this.normal.play(); this.diagnostics.count = 0; this.revision++;
    }
  }
  get audioEvents() {
    if (this.mode === 'normal') return this.manifest.events;
    if (this.mode === 'repair') return this.manifest.repair.events;
    if (this.recoveryState === 'SAFE_MODE') return [];
    return this.manifest.failure.events.filter(e => {
      if (e.id.startsWith('pin_')) return this.faultFrom.pins[Number(e.id[4]) - 1] > .01;
      if (e.id === 'iris_close_start') return this.faultFrom.iris.some(v => v > .01);
      return true;
    }).map(e => ['instability_start', 'electrical_instability', 'power_drop_start'].includes(e.id)
      ? { ...e, gain: e.gain * (.15 + .85 * this.faultFrom.reveal * this.faultFrom.brightness) } : e);
  }
  play() { if (this.recoveryState !== 'SAFE_MODE' || !this.activeFault) this.transport.play(); }
  pause() { this.transport.pause(); }
  seek(t) { this.transport.seek(t); }
  setSpeed(v) { this.transport.setSpeed(v); }
  setGate(id, released) { this.normal.setGate(id, released); }
  replay() {
    this.pendingSuccess = false;
    if (this.activeFault && this.recoveryState === 'SAFE_MODE') { this.transport.seek(this.manifest.failure.duration); this.transport.pause(); }
    else this.transport.replay();
    this.revision++;
  }
  resetNormal() {
    this.transport.pause(); this.mode = 'normal'; this.transport = this.normal;
    this.normal.seek(0); this.recoveryState = 'RECOVERY_ANALYZING'; this.diagnostics.count = 0;
    this.pendingSuccess = false; this.revision++;
  }
}

export { closed as isContained };
