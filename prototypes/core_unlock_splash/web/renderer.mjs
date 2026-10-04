import { clamp, smooth } from './timeline.mjs';

const TAU = Math.PI * 2;
const metal = ['#101a29', '#718197', '#d0d9e3', '#4f6076', '#192435', '#8c9eb4', '#202d40'];
function circle(c, r) { c.beginPath(); c.arc(0, 0, r, 0, TAU); }
function gradient(c, extent, colors = metal) {
  const g = c.createLinearGradient(-extent, -extent, extent, extent);
  colors.forEach((color, i) => g.addColorStop(i / (colors.length - 1), color)); return g;
}
function sprite(size, draw) {
  const el = document.createElement('canvas'); el.width = el.height = size * 2;
  const c = el.getContext('2d'); c.scale(2, 2); c.translate(size / 2, size / 2); draw(c);
  return el;
}
function blit(c, asset, size) { c.drawImage(asset, -size / 2, -size / 2, size, size); }
function plasmaSprite() {
  const el = document.createElement('canvas'); el.width = el.height = 256;
  const c = el.getContext('2d'), data = c.createImageData(256, 256);
  const hash = (x, y, z) => { const v = Math.sin(x * 127.1 + y * 311.7 + z * 74.7) * 43758.5453; return v - Math.floor(v); };
  const mix = (a, b, x) => a + (b - a) * x;
  const noise = (x, y, z) => {
    const ix = Math.floor(x), iy = Math.floor(y), iz = Math.floor(z);
    const u = smooth(x - ix), v = smooth(y - iy), w = smooth(z - iz);
    return mix(mix(mix(hash(ix, iy, iz), hash(ix + 1, iy, iz), u), mix(hash(ix, iy + 1, iz), hash(ix + 1, iy + 1, iz), u), v),
      mix(mix(hash(ix, iy, iz + 1), hash(ix + 1, iy, iz + 1), u), mix(hash(ix, iy + 1, iz + 1), hash(ix + 1, iy + 1, iz + 1), u), v), w);
  };
  for (let y = 0; y < 256; y++) for (let x = 0; x < 256; x++) {
    const nx = (x - 128) / 88, ny = (y - 128) / 88, r2 = nx * nx + ny * ny;
    if (r2 > 1) continue;
    const z = Math.sqrt(1 - r2);
    let n = 0, amplitude = .55, frequency = 3;
    for (let octave = 0; octave < 5; octave++) { n += amplitude * noise(nx * frequency + 11, ny * frequency + 7, z * frequency + 3); amplitude *= .5; frequency *= 2; }
    const vein = Math.exp(-Math.abs(Math.sin(nx * 5 + ny * 3 + n * 15)) * 24);
    const cloud = clamp((n - .34) * 2.7);
    const hot = Math.exp(-((nx + .1) ** 2 + (ny + .09) ** 2) * 13);
    const rim = Math.pow(1 - z, 5);
    const glow = hot * .7 + vein * .52 * (.4 + z * .6);
    const i = (y * 256 + x) * 4;
    data.data[i] = clamp(10 + cloud * 28 + glow * 240 + rim * 53, 0, 255);
    data.data[i + 1] = clamp(24 + cloud * 124 + glow * 220 + rim * 165, 0, 255);
    data.data[i + 2] = clamp(80 + cloud * 165 + glow * 150 + rim * 140, 0, 255);
    data.data[i + 3] = Math.round(clamp((1 - r2) * 180) * 255);
  }
  c.putImageData(data, 0, 0); return el;
}
function ringSprite(outer, inner, teeth, label) {
  return sprite(220, c => {
    c.beginPath(); c.arc(0, 0, outer, 0, TAU); c.arc(0, 0, inner, 0, TAU, true);
    c.fillStyle = gradient(c, outer); c.fill('evenodd');
    for (const [r, color, width] of [[outer, '#c1d4ea', .65], [outer - 1.4, '#0d1522', 1.5], [inner + 1.5, '#c4d3e899', .75], [inner, '#020810', 2]]) {
      circle(c, r); c.strokeStyle = color; c.lineWidth = width; c.stroke();
    }
    // Fine concentric tool marks; cached, never regenerated per frame.
    for (let r = inner + 3; r < outer - 3; r += .72) {
      circle(c, r); c.strokeStyle = r % 2 < 1 ? '#ffffff0e' : '#00000024'; c.lineWidth = .3; c.stroke();
    }
    for (let i = 0; i < teeth; i++) {
      c.save(); c.rotate(i / teeth * TAU); c.fillStyle = '#050a14'; c.fillRect(-.8, -outer + 2, 1.6, (outer - inner) * .37);
      c.fillStyle = '#d4e7ff50'; c.fillRect(.8, -outer + 2, .5, (outer - inner) * .37); c.restore();
    }
    c.font = '3.4px Segoe UI'; c.fillStyle = '#cce0f3aa'; c.textAlign = 'center';
    c.fillText(label, 0, -(outer + inner) / 2 + 1);
  });
}

export class Renderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.c = canvas.getContext('2d', { alpha: true });
    if (!this.c) throw new Error('Canvas unavailable');
    // Fixed render budget: backing buffer never grows with monitor DPI.
    canvas.width = 1024; canvas.height = 576;
    this.rings = [ringSprite(94, 83, 72, 'NEXUS / CONTAINMENT 01'), ringSprite(81, 73, 60, 'SECURE'), ringSprite(71, 63, 48, 'AUTHORIZED')];
    this.plate = sprite(220, c => {
      const p = new Path2D('M -9 -8 C 6 -25 25 -47 62 -56 L 91 -32 Q 101 3 76 55 L 47 48 C 35 22 13 4 -9 -8 Z');
      c.fillStyle = gradient(c, 72, ['#afbac9', '#596c83', '#192636', '#62758c', '#111d2b']); c.fill(p);
      c.strokeStyle = '#03080e'; c.lineWidth = 1.6; c.stroke(p);
      c.save(); c.clip(p);
      for (let y = -56; y < 58; y += 1.3) {
        c.strokeStyle = '#dfeaff0a'; c.lineWidth = .4; c.beginPath(); c.moveTo(-20, y); c.lineTo(100, y - 18); c.stroke();
      }
      c.restore();
      c.strokeStyle = '#b2c7dd88'; c.lineWidth = .55; c.beginPath(); c.moveTo(-5, -8); c.bezierCurveTo(12, -25, 32, -46, 62, -54); c.stroke();
      c.strokeStyle = '#030912'; c.lineWidth = 1; c.beginPath(); c.moveTo(39, -25); c.lineTo(67, -36); c.lineTo(78, -26); c.stroke();
      c.fillStyle = '#7389a2'; c.font = '3px Segoe UI'; c.fillText('NC // 06', 52, -26);
    });
    this.cylinder = sprite(80, c => {
      circle(c, 26); c.fillStyle = '#030713'; c.fill();
      circle(c, 24); c.fillStyle = gradient(c, 22); c.fill(); c.strokeStyle = '#9daec2'; c.lineWidth = .8; c.stroke();
      circle(c, 20); c.fillStyle = gradient(c, 20, ['#273343', '#a8b6c7', '#46546a', '#0f1927']); c.fill();
      circle(c, 18.5); c.strokeStyle = '#d6e7fa55'; c.lineWidth = .5; c.stroke();
      for (let i = 0; i < 32; i++) { c.save(); c.rotate(i / 32 * TAU); c.fillStyle = '#0c1627'; c.fillRect(-.3, -23, .6, 2); c.restore(); }
      const key = new Path2D('M -4 0 A 6.5 6.5 0 1 1 4 0 L 6 12 L -6 12 Z');
      c.fillStyle = '#02050a'; c.fill(key); c.strokeStyle = '#c5d8e088'; c.lineWidth = .7; c.stroke(key);
      c.fillStyle = '#acbdd0'; c.fillRect(-15, -1, 3, 1); c.fillRect(12, -1, 3, 1);
    });
    this.clamp = sprite(60, c => {
      c.fillStyle = '#040910'; c.fillRect(-9, -14, 18, 34);
      c.beginPath(); c.moveTo(-8, -16); c.lineTo(8, -16); c.lineTo(11, 14); c.lineTo(6, 20); c.lineTo(-6, 20); c.lineTo(-11, 14); c.closePath();
      c.fillStyle = gradient(c, 20); c.fill(); c.strokeStyle = '#a0b8d1'; c.lineWidth = .7; c.stroke();
      c.fillStyle = '#162132'; c.fillRect(-5, -11, 10, 16);
      c.fillStyle = gradient(c, 8); c.fillRect(-3, -9, 6, 21);
      for (const y of [-12, 15]) { c.beginPath(); c.arc(0, y, 2, 0, TAU); c.fillStyle = '#102030'; c.fill(); c.strokeStyle = '#c3d8eb'; c.lineWidth = .4; c.stroke(); c.fillStyle = '#899aaa'; c.fillRect(-1.2, y - .2, 2.4, .4); }
    });
    this.glow = sprite(256, c => {
      const g = c.createRadialGradient(0, 0, 0, 0, 0, 128);
      g.addColorStop(0, '#58eaffb0'); g.addColorStop(.2, '#1aafff65'); g.addColorStop(.48, '#185bfc2a'); g.addColorStop(1, '#053bef00');
      c.fillStyle = g; c.fillRect(-128, -128, 256, 256);
    });
    this.sphere = sprite(128, c => {
      const g = c.createRadialGradient(-8, -8, 1, 0, 0, 44);
      g.addColorStop(0, '#f0ffff'); g.addColorStop(.13, '#b3ffff'); g.addColorStop(.34, '#2bd8ff'); g.addColorStop(.62, '#1876e4'); g.addColorStop(.88, '#172b87'); g.addColorStop(1, '#65e6ff');
      circle(c, 44); c.fillStyle = g; c.fill(); c.lineWidth = .6; c.strokeStyle = '#b9fbff'; c.stroke();
    });
    this.plasma = plasmaSprite();
  }

  render(s) {
    const c = this.c; c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, 1024, 576);
    // Only light is composited over the immutable production chamber/lettering.
    if (s.reveal > 0) {
      c.save(); c.globalCompositeOperation = 'screen';
      c.globalAlpha = .34 * s.charge + .1 * s.pulse;
      c.translate(512, 444); c.scale(2.6 + s.pulse * .25, .24); blit(c, this.glow, 256); c.restore();
      c.save(); c.translate(512, 204); c.globalCompositeOperation = 'screen';
      c.globalAlpha = .16 * s.reveal + .42 * s.charge + .09 * s.pulse; blit(c, this.glow, 390); c.restore();
    }
    c.save(); c.translate(512, 204);
    // Permanent opaque reactor cavity masks the original painted core in every state.
    circle(c, 94); c.fillStyle = '#020813'; c.fill();
    c.save(); circle(c, 64); c.clip();
    this.drawCore(c, s);
    // Six rigid armor leaves with radial rails and a slight physical hinge rotation.
    for (let i = 5; i >= 0; i--) {
      c.save(); c.rotate(i * TAU / 6);
      if (s.reduced) c.globalAlpha = 1 - s.reveal;
      else { c.translate(s.iris[i] * 88, 0); c.rotate(s.iris[i] * .34); }
      blit(c, this.plate, 220); c.restore();
    }
    // Cylinder belongs to the top carriage; it withdraws through the same aperture.
    c.save();
    if (s.reduced) c.globalAlpha = 1 - s.reveal;
    else c.translate(0, -s.reveal * 105);
    c.rotate(s.cylinder); blit(c, this.cylinder, 80);
    c.globalAlpha *= .15 + .8 * s.authorization;
    c.fillStyle = '#5ddcff'; c.fillRect(-1, 4, 2, 5); c.restore();
    c.restore();
    for (let i = 0; i < 3; i++) {
      c.save(); c.rotate(s.rings[i]); blit(c, this.rings[i], 220);
      c.lineWidth = i === 1 ? 1.5 : 1;
      for (let j = 0; j < 6; j++) {
        const activated = clamp(s.security * 6 - j);
        c.strokeStyle = j % 3 === 2 ? `rgba(149,98,249,${.06 + .64 * activated})` : `rgba(69,205,255,${.07 + .75 * activated})`;
        c.beginPath(); c.arc(0, 0, [85, 75, 65][i], j * TAU / 6 + .13, j * TAU / 6 + .7); c.stroke();
      }
      c.restore();
    }
    if (s.security > 0 && s.authorization < 1 && !s.reduced) {
      c.strokeStyle = '#a0ebff'; c.lineWidth = 1; c.beginPath();
      c.arc(0, 0, 92, s.t * 2.6, s.t * 2.6 + .19); c.stroke();
    }
    // Four independent solenoid clamps: top, right, bottom, left.
    for (let i = 0; i < 4; i++) {
      c.save(); c.rotate(i * Math.PI / 2); c.translate(0, -80 - s.pins[i] * (s.reduced ? 3 : 13));
      blit(c, this.clamp, 60);
      c.fillStyle = s.pins[i] > .9 ? '#8bf4ed' : (s.security > i / 4 ? '#46acdc' : '#203950');
      c.fillRect(-2, -7, 4, 5); c.restore();
    }
    // Energy spills onto the metal housing, without concealing its stable geometry.
    if (s.reveal > 0) {
      c.save(); c.globalCompositeOperation = 'screen'; c.globalAlpha = s.reveal * (.06 + .23 * s.charge);
      blit(c, this.glow, 218); c.restore();
    }
    // Restrained activation halo, localized to the shield (never a screen flash).
    if (s.pulse > 0) {
      circle(c, 97 + 25 * smooth(s.pulsePhase));
      c.strokeStyle = `rgba(117,228,255,${s.pulse * .32})`; c.lineWidth = 1; c.stroke();
    }
    c.restore();
    this.drawParticles(c, s);
  }

  drawCore(c, s) {
    c.fillStyle = '#020a20'; c.fillRect(-64, -64, 128, 128);
    c.save(); c.globalAlpha = .35 + .5 * s.brightness; blit(c, this.glow, 186); c.restore();
    c.save(); c.scale(.96 + .04 * s.charge, .96 + .04 * s.charge);
    c.globalAlpha = .45 + .55 * s.brightness; blit(c, this.sphere, 128); c.restore();
    c.save(); c.rotate(s.orbit * .23); c.globalAlpha = .78; blit(c, this.plasma, 128); c.restore();
    c.save(); c.globalCompositeOperation = 'screen';
    c.globalAlpha = .12 + .32 * s.charge; blit(c, this.glow, 116); c.restore();
    // Internal current: deterministic ellipses, clipped to the sphere surface.
    c.save(); circle(c, 43); c.clip();
    for (let i = 0; i < 7; i++) {
      c.save(); c.rotate(i * .78 + s.orbit * (i % 2 ? 1 : -1));
      c.beginPath(); c.ellipse(0, 0, 18 + i * 4, 7 + i * 2, .3, 0, TAU);
      c.strokeStyle = i % 3 ? '#8bf8ff22' : '#b797ff33'; c.lineWidth = .55; c.stroke(); c.restore();
    }
    for (let i = 0; i < (s.reduced ? 8 : 32); i++) {
      const angle = i * 2.39996 + s.orbit * .7;
      const r = 7 + ((i * 17) % 35);
      c.fillStyle = `rgba(162,242,255,${.2 + .45 * Math.sin(i * 2.2 + s.orbit) ** 2})`;
      c.fillRect(Math.cos(angle) * r, Math.sin(angle) * r, .65, .65);
    }
    c.restore();
    for (let i = 0; i < 3; i++) {
      c.save(); c.rotate(i * Math.PI / 3 + s.orbit * (i % 2 ? -1 : 1));
      c.globalAlpha = s.charge;
      c.beginPath(); c.ellipse(0, 0, 59, 19, 0, 0, TAU);
      c.strokeStyle = i === 1 ? '#c4adff' : '#a0f7ff'; c.lineWidth = 1.45; c.stroke();
      const p = s.orbit * 2 + i * 2; c.beginPath(); c.arc(Math.cos(p) * 59, Math.sin(p) * 19, 1.4, 0, TAU); c.fillStyle = '#d9ffff'; c.fill();
      c.restore();
    }
    if (s.pulse > 0) { c.save(); c.globalAlpha = s.pulse * .23; blit(c, this.glow, 126); c.restore(); }
  }

  drawParticles(c, s) {
    for (let i = 0; i < s.particleCount; i++) {
      const a = i * 2.399963;
      const cycle = (s.ambientTime * (.025 + i % 3 * .006) + i * .618034) % 1;
      const r = 104 + cycle * 85;
      const alpha = Math.sin(cycle * Math.PI) * (.12 + s.charge * .25);
      c.fillStyle = `rgba(113,198,255,${alpha})`;
      c.fillRect(512 + Math.cos(a) * r, 208 + Math.sin(a) * r * .75, i % 6 ? .6 : 1.1, i % 6 ? .6 : 1.1);
    }
  }
}
