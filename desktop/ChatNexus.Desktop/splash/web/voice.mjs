import { clamp } from './timeline.mjs';

// Narration channel. Voice is presentation-only: the host schedules lines and
// passes synthesized WAV bytes; this module owns Web Audio playback, ducking
// of the mechanical stem bus, and prompt cancellation for fault narration.
// It never signals readiness and never touches the startup timeline.
export class VoiceChannel {
  constructor(audio) {
    this.audio = audio;      // AudioEngine — its context/bus are shared when available
    this.ownContext = null;  // standalone context when SFX are disabled
    this.active = null;      // { source, gain, shared }
    this.disposed = false;
  }

  async ensureContext() {
    if (this.audio.context) return this.audio.context;
    if (!this.audio.disabled && await this.audio.initialize()) return this.audio.context;
    if (!this.ownContext && !this.disposed) this.ownContext = new AudioContext();
    return this.ownContext;
  }

  get playing() { return this.active !== null; }

  // bytes: WAV file contents. duckLevel: stem-bus gain while speaking.
  // Resolves { started, seconds } once playback actually begins — the host
  // treats `started` as the durable "the user heard this" signal.
  async play(bytes, { duckLevel = .6 } = {}) {
    try {
      if (this.disposed) return { started: false, seconds: 0 };
      const ctx = await this.ensureContext();
      if (!ctx || this.disposed) return { started: false, seconds: 0 };
      await ctx.resume().catch(() => {});
      const buffer = await ctx.decodeAudioData(bytes.slice(0));
      if (this.disposed) return { started: false, seconds: 0 };
      const shared = ctx === this.audio.context;
      // Serialization: a new line (e.g. fault narration) replaces whatever
      // is still playing instead of overlapping it.
      this.stop(.05);
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      const gain = ctx.createGain();
      gain.gain.value = 1;
      source.connect(gain);
      gain.connect(shared ? this.audio.master : ctx.destination);
      let resolveDone;
      const done = new Promise(resolve => { resolveDone = resolve; });
      const token = this.active = { source, gain, shared, resolveDone };
      source.onended = () => {
        if (this.active === token) this.active = null;
        if (shared) this.audio.unduck();
        resolveDone();
      };
      if (shared) this.audio.duck(clamp(duckLevel, .05, 1));
      try {
        source.start(ctx.currentTime + .01);
      } catch {
        if (this.active === token) this.active = null;
        if (shared) this.audio.unduck();
        return { started: false, seconds: 0 };
      }
      return { started: true, seconds: buffer.duration, done };
    } catch {
      return { started: false, seconds: 0 };
    }
  }

  // Fast fade-out so a friendly startup line never plays over a visible fault.
  // Resolves the stopped clip's done promise — a preempted line still owes the
  // host its voice-ended ack or the narrator's queue hangs on the watchdog.
  stop(fade = .18) {
    const token = this.active;
    if (!token) return;
    this.active = null;
    try {
      const ctx = token.source.context;
      token.gain.gain.cancelAndHoldAtTime(ctx.currentTime);
      token.gain.gain.linearRampToValueAtTime(0, ctx.currentTime + fade);
      token.source.onended = null;
      token.source.stop(ctx.currentTime + fade + .002);
      token.source.onended = () => { token.source.disconnect(); token.gain.disconnect(); };
    } catch { /* already stopped */ }
    if (token.shared) this.audio.unduck();
    token.resolveDone?.();
  }

  async dispose() {
    this.disposed = true;
    this.stop(.01);
    if (this.ownContext) await this.ownContext.close().catch(() => {});
    this.ownContext = null;
  }
}
