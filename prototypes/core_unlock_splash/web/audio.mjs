import { clamp } from './timeline.mjs';

// A transport rebuilds sources on seek/pause/speed/gate changes. Normal playback is
// scheduled on the AudioContext clock, not on render callbacks or setTimeout.
export class AudioEngine {
  constructor(manifest, { disabled = false, factory = () => new AudioContext() } = {}) {
    this.manifest = manifest; this.disabled = disabled; this.factory = factory;
    this.volume = manifest.defaultVolume; this.muted = false; this.sources = [];
    this.buffers = new Map(); this.warnings = []; this.context = null;
    this.loading = null; this.generation = 0; this.peak = 0; this.scheduled = [];
  }
  async initialize() {
    if (this.disabled) return false;
    if (this.loading) return this.loading;
    this.loading = this.load(); return this.loading;
  }
  async load() {
    try {
      this.context = this.factory();
      this.master = this.context.createGain(); this.master.gain.value = this.muted ? 0 : this.volume;
      this.analyser = this.context.createAnalyser(); this.analyser.fftSize = 256;
      this.wave = new Float32Array(this.analyser.fftSize);
      // Worst-case mix is tested below 0 dBFS even at volume 1. This limiter is a guard.
      this.limiter = this.context.createDynamicsCompressor();
      this.limiter.threshold.value = -3; this.limiter.knee.value = 3; this.limiter.ratio.value = 12;
      this.limiter.attack.value = .003; this.limiter.release.value = .15;
      this.master.connect(this.limiter); this.limiter.connect(this.analyser); this.analyser.connect(this.context.destination);
      await Promise.all(Object.entries(this.manifest.sounds).map(async ([id, path]) => {
        try {
          const response = await fetch(new URL(`../${path}`, import.meta.url));
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          this.buffers.set(id, await this.context.decodeAudioData(await response.arrayBuffer()));
        } catch (e) { this.warnings.push(`${id}: ${e.message}`); }
      }));
      return true;
    } catch (e) {
      this.warnings.push(`Audio disabled: ${e.message}`); this.disabled = true;
      if (this.context) await this.context.close().catch(() => {});
      this.context = null; return false;
    }
  }
  stop(fade = .008) {
    this.generation++;
    for (const { source, gain, pan } of this.sources) {
      try {
        const now = this.context.currentTime;
        gain.gain.cancelAndHoldAtTime(now); gain.gain.linearRampToValueAtTime(0, now + fade);
        source.onended = () => { source.disconnect(); gain.disconnect(); pan.disconnect(); };
        source.stop(now + fade + .002);
      } catch { source.disconnect(); gain.disconnect(); pan.disconnect(); }
    }
    this.sources = []; this.scheduled = [];
  }
  setVolume(volume) {
    this.volume = clamp(volume);
    if (this.master) this.master.gain.setTargetAtTime(this.muted ? 0 : this.volume, this.context.currentTime, .012);
  }
  setMuted(muted) { this.muted = Boolean(muted); this.setVolume(this.volume); }
  async sync(clock, { fade = .008 } = {}) {
    this.stop(fade); const version = this.generation;
    if (!clock.playing || this.disabled) return;
    if (!await this.initialize() || version !== this.generation) return;
    try {
      await this.context.resume();
      if (version !== this.generation) return;
      const t = clock.time, speed = clock.speed, limit = clock.ceiling;
      if (clock.held) {
        const charge = this.manifest.events.find(e => e.id === 'core_charge_start');
        const sustain = this.manifest.events.find(e => e.id === 'charged_sustain');
        // A loaded reactor must not fall back to the dormant hum while readiness lags.
        // Ignore the nominal sustain end at this gate; it loops until release/disposal.
        const heldEvent = t >= charge.at + charge.duration
          ? { ...sustain, id: 'charged_hold_hum', until: undefined, fadeIn: .015 }
          : { id: 'hold_hum', sound: 'ambient_hum', at: 0, gain: .26, loop: true, fadeIn: .08 };
        this.schedule(heldEvent, clock.ambientTime ?? t, speed, Infinity);
      } else {
        for (const event of clock.audioEvents ?? this.manifest.events)
          if (event.sound && event.at < limit) this.schedule(event, t, speed, limit);
      }
    } catch (e) { this.warnings.push(`Playback unavailable: ${e.message}`); this.stop(); }
  }
  schedule(event, t, speed, limit) {
    const buffer = this.buffers.get(event.sound); if (!buffer) return;
    const end = Math.min(event.until ?? (event.loop ? Infinity : event.at + buffer.duration), limit);
    if (end <= t || end <= event.at) return;
    const context = this.context, start = context.currentTime + Math.max(0, event.at - t) / speed;
    const elapsed = Math.max(0, t - event.at), remaining = (end - Math.max(t, event.at)) / speed;
    const source = context.createBufferSource(), gain = context.createGain(), pan = context.createStereoPanner();
    source.buffer = buffer; source.loop = Boolean(event.loop); source.playbackRate.value = speed;
    pan.pan.value = event.pan ?? 0;
    const level = event.gain ?? .6, fadeIn = event.fadeIn ?? .006;
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime(level, start + Math.min(elapsed > 0 ? .01 : fadeIn / speed, remaining / 2));
    if (Number.isFinite(remaining)) {
      const fadeOut = Math.min((event.fadeOut ?? .015) / speed, remaining / 2);
      gain.gain.setValueAtTime(level, start + remaining - fadeOut);
      gain.gain.linearRampToValueAtTime(0, start + remaining);
    }
    source.connect(gain); gain.connect(pan); pan.connect(this.master);
    source.start(start, event.loop ? elapsed % buffer.duration : elapsed);
    if (Number.isFinite(remaining)) source.stop(start + remaining);
    const entry = { source, gain, pan }; this.sources.push(entry);
    source.onended = () => { source.disconnect(); gain.disconnect(); pan.disconnect(); this.sources = this.sources.filter(s => s !== entry); };
    this.scheduled.push({ id: event.id, when: start, offset: elapsed, speed });
  }
  measure() {
    if (!this.analyser) return 0;
    this.analyser.getFloatTimeDomainData(this.wave);
    let peak = 0; for (const x of this.wave) peak = Math.max(peak, Math.abs(x));
    this.peak = Math.max(this.peak, peak); return peak;
  }
  async dispose() { this.stop(); if (this.context) await this.context.close(); }
}
