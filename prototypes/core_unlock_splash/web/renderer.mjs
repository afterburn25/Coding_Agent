import { clamp, smooth } from './timeline.mjs';

const TAU = Math.PI * 2;
const metal = ['#07101c', '#31445e', '#a2b8d0', '#e1edf9', '#34465d', '#091321', '#637c9c', '#142136'];
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
function lightSprite(stops) {
  return sprite(256, c => {
    const g = c.createRadialGradient(0, 0, 0, 0, 0, 128);
    for (const [at, color] of stops) g.addColorStop(at, color);
    c.fillStyle = g; c.fillRect(-128, -128, 256, 256);
  });
}
function nebulaSprite() {
  // A fixed procedural density field, cached once. Runtime moves light layers,
  // never the shield, wordmark, or mechanical geometry.
  const el = document.createElement('canvas'); el.width = el.height = 384;
  const c = el.getContext('2d'), data = c.createImageData(384, 384);
  const hash = (x, y) => { const v = Math.sin(x * 127.1 + y * 311.7) * 43758.5453; return v - Math.floor(v); };
  const mix = (a, b, t) => a + (b - a) * t;
  const noise = (x, y) => {
    const ix = Math.floor(x), iy = Math.floor(y), u = smooth(x - ix), v = smooth(y - iy);
    return mix(mix(hash(ix, iy), hash(ix + 1, iy), u), mix(hash(ix, iy + 1), hash(ix + 1, iy + 1), u), v);
  };
  for (let y = 0; y < 384; y++) for (let x = 0; x < 384; x++) {
    const nx = (x - 192) / 192, ny = (y - 192) / 192, r = Math.hypot(nx, ny);
    if (r >= 1) continue;
    let n = 0, frequency = 4, weight = .55;
    for (let i = 0; i < 5; i++) { n += noise(nx * frequency + 13, ny * frequency + 19) * weight; frequency *= 2; weight *= .5; }
    const cloud = clamp((n - .27) * 2.6);
    const rim = Math.exp(-(((r - .56) / .29) ** 2)) * clamp((1 - r) * 6);
    const filament = Math.exp(-Math.abs(Math.sin(n * 23 + nx * 2 - ny * 3)) * 17);
    const i = (y * 384 + x) * 4;
    data.data[i] = 17 + 76 * filament;
    data.data[i + 1] = 81 + 118 * cloud + 36 * filament;
    data.data[i + 2] = 240;
    data.data[i + 3] = 160 * rim * (cloud * cloud + filament * .25);
  }
  c.putImageData(data, 0, 0); return el;
}
function orbitSprite(color) {
  return sprite(184, c => {
    c.beginPath(); c.ellipse(0, 0, 72, 25, 0, 0, TAU);
    c.strokeStyle = color; c.shadowColor = color; c.shadowBlur = 9;
    c.lineWidth = 2.1; c.stroke(); c.shadowBlur = 3; c.stroke();
    c.shadowBlur = 0; c.strokeStyle = '#e6ffff'; c.lineWidth = .65; c.stroke();
  });
}
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
    for (const [r, color, width] of [[outer, '#d0e9ff', .8], [outer - 1.7, '#050b15', 2.6], [inner + 1.8, '#d4eaff99', .75], [inner, '#01050d', 3]]) {
      circle(c, r); c.strokeStyle = color; c.lineWidth = width; c.stroke();
    }
    // Recessed groove shadows and a machined bevel separate adjacent rings.
    c.beginPath(); c.arc(0, 0, outer - 2.5, .2, 2.85); c.strokeStyle = '#00050baa'; c.lineWidth = 2.2; c.stroke();
    c.beginPath(); c.arc(0, 0, outer - .55, -2.9, -.35); c.strokeStyle = '#e5f4ffbb'; c.lineWidth = .7; c.stroke();
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
      c.strokeStyle = '#010309'; c.lineWidth = 2.8; c.stroke(p);
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
    this.nebula = nebulaSprite();
    this.bloom = lightSprite([[0, '#dbffffed'], [.08, '#8cffffd4'], [.2, '#38d5ffad'], [.4, '#1387ff70'], [.7, '#1255fa28'], [1, '#0938ef00']]);
    this.coreLight = lightSprite([[0, '#d8ffffdd'], [.2, '#8cffffcc'], [.6, '#1ecfffaa'], [.82, '#168aff80'], [1, '#093bff00']]);
    this.hotspot = lightSprite([[0, '#ffffffff'], [.09, '#f5fffff5'], [.22, '#baffffdb'], [.42, '#42cfff7a'], [.7, '#126eff20'], [1, '#0654ff00']]);
    this.instabilityLight = lightSprite([[0, '#e3c3ffbb'], [.25, '#b276faaa'], [.6, '#6544da44'], [1, '#452cc000']]);
    this.housingShadow = lightSprite([[0, '#00020aff'], [.64, '#00020afa'], [.77, '#00020ab0'], [.9, '#00020a28'], [1, '#00020a00']]);
    this.orbits = [orbitSprite('#55dbff'), orbitSprite('#a284ff'), orbitSprite('#5eeeff')];
    this.ringEmission = sprite(240, c => {
      c.shadowBlur = 5;
      for (const r of [85, 75, 65]) for (let j = 0; j < 6; j++) {
        c.beginPath(); c.arc(0, 0, r, j * TAU / 6 + .13, j * TAU / 6 + .7);
        c.strokeStyle = c.shadowColor = j === 2 ? '#9a6dff' : '#17bfff'; c.lineWidth = 2.2; c.stroke();
        c.shadowBlur = 0; c.strokeStyle = '#b9f5ff'; c.lineWidth = .6; c.stroke(); c.shadowBlur = 5;
      }
    });
    this.platform = sprite(640, c => {
      for (const [width, alpha, blur] of [[7, .2, 16], [3, .65, 7], [1.2, 1, 2]]) {
        c.beginPath(); c.ellipse(0, 0, 213, 11, 0, 0, TAU);
        c.strokeStyle = `rgba(157,238,255,${alpha})`; c.lineWidth = width;
        c.shadowColor = '#098fff'; c.shadowBlur = blur; c.stroke();
      }
      c.shadowBlur = 0;
      const g = c.createRadialGradient(0, 0, 1, 0, 0, 82);
      g.addColorStop(0, '#e4ffffd0'); g.addColorStop(.2, '#64dcffa0'); g.addColorStop(1, '#167bff00');
      c.save(); c.scale(2.8, .26); c.fillStyle = g; c.fillRect(-82, -82, 164, 164); c.restore();
    });
  }

  render(s) {
    const c = this.c; c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, 1024, 576);
    this.drawEnvironment(c, s);
    c.save(); c.translate(512, 204);
    c.save(); c.translate(2, 5); c.globalAlpha = .8; blit(c, this.housingShadow, 243); c.restore();
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
        c.strokeStyle = s.fault && s.warning > .2 && j === 0
          ? `rgba(255,${s.critical > .7 ? 104 : 185},74,${.15 + .6 * s.warning})`
          : j % 3 === 2 ? `rgba(149,98,249,${.06 + .64 * activated})` : `rgba(69,205,255,${.07 + .75 * activated})`;
        c.beginPath(); c.arc(0, 0, [85, 75, 65][i], j * TAU / 6 + .13, j * TAU / 6 + .7); c.stroke();
      }
      c.restore();
    }
    c.save(); c.globalCompositeOperation = 'screen'; c.globalAlpha = (.18 * s.security + .82 * s.charge) * (s.energyScale ?? 1);
    blit(c, this.ringEmission, 240); c.restore();
    if (s.security > 0 && s.authorization < 1 && !s.reduced) {
      c.strokeStyle = '#a0ebff'; c.lineWidth = 1; c.beginPath();
      c.arc(0, 0, 92, s.t * 2.6, s.t * 2.6 + .19); c.stroke();
    }
    // Four independent solenoid clamps: top, right, bottom, left.
    for (let i = 0; i < 4; i++) {
      c.save(); c.rotate(i * Math.PI / 2); c.translate(0, -80 - s.pins[i] * (s.reduced ? 3 : 13));
      blit(c, this.clamp, 60);
      c.fillStyle = s.pins[i] > .9 ? '#8bf4ed' : (s.security > i / 4 ? '#46acdc' : '#203950');
      if (s.fault && s.warning > .2 && (i === 0 || s.contained || s.pinFlash?.[i] > 0))
        c.fillStyle = s.pinFlash?.[i] > .1 ? '#ffde91' : i === 0 && s.critical > .7 ? '#ec7359' : '#c79550';
      c.fillRect(-2, -7, 4, 5); c.restore();
      if (s.charge > 0) {
        c.save(); c.rotate(i * Math.PI / 2); c.translate(0, -87 - s.pins[i] * (s.reduced ? 3 : 13));
        c.globalCompositeOperation = 'screen'; c.globalAlpha = s.charge * .85; blit(c, this.hotspot, 26); c.restore();
      }
    }
    // Energy spills onto the metal housing, without concealing its stable geometry.
    if (s.reveal > 0) {
      c.save(); c.globalCompositeOperation = 'screen'; c.globalAlpha = s.reveal * (.05 + .5 * s.charge);
      blit(c, this.bloom, 235); c.restore();
    }
    this.drawOrbitals(c, s);
    if (s.fault) this.drawFaultIndicators(c, s);
    // Restrained activation halo, localized to the shield (never a screen flash).
    if (s.pulse > 0) {
      circle(c, 97 + 25 * smooth(s.pulsePhase));
      c.strokeStyle = `rgba(117,228,255,${s.pulse * .32})`; c.lineWidth = 1; c.stroke();
    }
    c.restore();
    this.drawParticles(c, s);
  }

  drawEnvironment(c, s) {
    if (s.reveal <= 0) return;
    const energy = s.reveal * (.12 + .88 * s.charge) * (s.energyScale ?? 1);
    c.save(); c.globalCompositeOperation = 'screen';
    c.save(); c.translate(512, 204); c.globalAlpha = energy * .88;
    blit(c, this.bloom, 435 + s.pulse * 25);
    c.globalAlpha = energy * .82; c.rotate(s.orbit * .025); blit(c, this.nebula, 385); c.restore();
    // Lit chamber haze on either side of the shield; leaves the lettering legible.
    for (const [x, y, scaleX, scaleY, alpha] of [[386, 348, 1.45, .58, .3], [644, 348, 1.45, .58, .3], [512, 430, 1.5, .34, .55]]) {
      c.save(); c.translate(x, y); c.scale(scaleX, scaleY); c.globalAlpha = energy * alpha; blit(c, this.bloom, 230); c.restore();
    }
    // A narrow volumetric column and floor reflection connect reactor and platform.
    c.save(); c.translate(512, 366); c.scale(.19, 1.7); c.globalAlpha = energy * .45; blit(c, this.bloom, 150); c.restore();
    c.save(); c.translate(512, 492); c.scale(.48, 1.2); c.globalAlpha = energy * .48; blit(c, this.bloom, 178); c.restore();
    c.save(); c.translate(512, 442); c.globalAlpha = energy * (.83 + .12 * s.pulse); blit(c, this.platform, 640);
    c.globalAlpha = energy * .75; c.scale(1.3, .34); blit(c, this.hotspot, 124); c.restore();
    c.restore();
  }

  drawCore(c, s) {
    c.fillStyle = '#020a20'; c.fillRect(-64, -64, 128, 128);
    // Only emitted light expands with charge; the sphere and its surface stay fixed.
    const glowExpansion = clamp(s.charge);
    c.save(); c.globalAlpha = .35 + .5 * s.brightness; blit(c, this.glow, 186); c.restore();
    c.save(); c.scale(1.12, 1.12);
    c.globalAlpha = .45 + .55 * s.brightness; blit(c, this.sphere, 128); c.restore();
    c.save(); c.rotate(s.orbit * .23); c.globalAlpha = .68; blit(c, this.plasma, 144); c.restore();
    c.save(); c.globalCompositeOperation = 'screen';
    c.globalAlpha = .18 + .65 * s.charge; blit(c, this.bloom, 132 + 112 * glowExpansion);
    c.globalAlpha = .2 + .74 * s.charge; blit(c, this.hotspot, 67 + 40 * glowExpansion); c.restore();
    if (s.instability > 0) {
      c.save(); c.globalCompositeOperation = 'screen'; c.globalAlpha = s.instability * .5;
      c.translate(Math.sin(s.t * 17) * 12, Math.cos(s.t * 13) * 8); blit(c, this.instabilityLight, 122);
      c.strokeStyle = '#c0a3ff'; c.lineWidth = .65;
      c.rotate(s.t * .8); c.beginPath(); c.ellipse(0, 0, 46, 19, .4, .2, 2.6); c.stroke(); c.restore();
    }
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
    // Preserve the fixed cavity edge while the glow spreads across its interior.
    const occlusion = c.createRadialGradient(0, 0, 48, 0, 0, 65);
    occlusion.addColorStop(0, '#00030a00'); occlusion.addColorStop(1, '#00030abb');
    circle(c, 65); c.fillStyle = occlusion; c.fill();
    // Diffuse emission fills the aperture without enlarging the sphere or plasma.
    c.save(); c.globalCompositeOperation = 'screen'; c.globalAlpha = .85 * glowExpansion;
    blit(c, this.coreLight, 100 + 100 * glowExpansion); c.restore();
    if (s.pulse > 0) { c.save(); c.globalAlpha = s.pulse * .23; blit(c, this.bloom, 126); c.restore(); }
  }

  drawOrbitals(c, s) {
    // Foreground orbital light can cross the housing, as in the supplied reference.
    // Cached bloom creates a luminous tube instead of a thin diagram-like line.
    for (let i = 0; i < 3; i++) {
      c.save(); c.rotate(i * Math.PI / 3 + s.orbit * (i % 2 ? -1 : 1) + (s.orbitOffsets?.[i] ?? 0));
      c.globalCompositeOperation = 'screen'; c.globalAlpha = s.charge * .88;
      blit(c, this.orbits[i], 184);
      const p = s.orbit * 2 + i * 2;
      c.translate(Math.cos(p) * 72, Math.sin(p) * 25); blit(c, this.hotspot, 14);
      c.restore();
    }
  }

  drawFaultIndicators(c, s) {
    c.save(); c.globalAlpha = s.warning;
    // Local warning only: retain the blue shield/chamber and original lettering.
    c.strokeStyle = s.critical > .7 ? '#dc7652' : '#d1a05b'; c.lineWidth = .85;
    c.beginPath(); c.arc(0, 0, 62, -.75, .1); c.stroke();
    c.globalAlpha = s.warning * .52;
    c.beginPath(); c.arc(0, 0, 84, 1.12, 1.9); c.stroke();
    c.globalAlpha = s.warning;
    c.translate(115, -71);
    c.beginPath(); c.moveTo(0, -6); c.lineTo(5, 4); c.lineTo(-5, 4); c.closePath(); c.stroke();
    c.fillStyle = '#e2bb7d'; c.fillRect(-.55, -2.5, 1.1, 3); c.fillRect(-.55, 1.7, 1.1, 1);
    c.font = '5px Segoe UI'; c.fillText(s.contained ? 'SECURED' : 'SYNC', 9, 2);
    c.restore();
    if (s.diagnosticActive) {
      c.save(); c.rotate(s.diagnosticAngle); c.strokeStyle = '#d9ac6366'; c.lineWidth = 1;
      for (let i = 0; i < 3; i++) {
        c.beginPath(); c.arc(0, 0, 106, i * TAU / 3, i * TAU / 3 + .38); c.stroke();
        for (let j = 0; j < 4; j++) { c.rotate(.025); c.fillStyle = '#c9a46a55'; c.fillRect(109, -1, 2, 2); }
      }
      c.restore();
    }
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
