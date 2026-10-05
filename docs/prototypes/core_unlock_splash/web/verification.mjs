const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const sealed = s => s.iris.every(x => x === 0) && s.pins.every(x => x === 0) && s.rings.every(x => x === 0) && s.cylinder === 0;

// Native development verification: uses the actual DOM, Canvas and Web Audio.
export async function verifyFailurePlayback({ clock, audio, manifest, changed, triggerFault, paint, host, setReduced, progress }) {
  const checks = {}, points = [0, 1.9, 2.8, 5.15, 7.8, 11.79];
  await audio.initialize(); setReduced(false);
  for (const at of points) {
    clock.resetNormal(); for (const g of manifest.gates) clock.setGate(g.id, true);
    clock.pause(); clock.seek(at); const before = clock.sample();
    triggerFault({ message: 'Core initialization could not complete.', detail: `Verification injection at ${at} seconds.` });
    clock.pause(); const after = clock.sample();
    const same = ['iris', 'pins'].every(key => before[key].every((v, i) => Math.abs(v - after[key][i]) < .001));
    clock.seek(manifest.failure.duration); changed();
    checks[`continuousAndContainedAt${at}`] = same && sealed(clock.sample());
  }
  clock.resetNormal(); clock.setGate('ready', false); clock.seek(7.7); clock.play(); changed();
  await delay(120); audio.peak = 0;
  triggerFault({ detail: 'Power-up interrupted while readiness remains closed.' });
  const frozen = progress();
  await delay(120); const firstTime = clock.time;
  const duplicate = triggerFault({ detail: 'Second diagnostic: same active fault.' });
  checks.noRepeatedFaultLoop = !duplicate && clock.time >= firstTime && clock.diagnostics.count >= 2;
  await delay(160); const unstable = clock.sample();
  checks.visibleInstability = unstable.orbitOffsets.some(x => Math.abs(x) > .02) && unstable.warning > .1;
  checks.failureAudioOnly = audio.disabled || (audio.scheduled.some(e => e.id === 'power_drop_start') && !audio.scheduled.some(e => e.id === 'online_pulse'));
  await delay(manifest.failure.duration * 1000 + 150 - 280);
  checks.contained = sealed(clock.sample());
  checks.recoveryPanel = !document.getElementById('recovery').hidden && document.getElementById('recovery').style.opacity === '1';
  checks.progressFrozen = progress() === frozen;
  checks.failureAudioOutput = audio.disabled ? audio.context === null : audio.peak > .001 && audio.peak < .86;
  checks.emergencyHum = audio.disabled || audio.measure() > .0001;
  audio.setMuted(true); await delay(120); checks.mutedFailure = audio.measure() < .00001; audio.setMuted(false);
  clock.setRecoveryState('REPAIR_ATTEMPT', { attempt: 2, total: 3 }); changed();
  checks.hostAttemptCounter = document.getElementById('recovery-attempt').textContent === 'RECOVERY ATTEMPT 2 OF 3';
  clock.setRecoveryState('ROLLBACK'); changed(); const rollback = clock.sample();
  checks.rollback = sealed(rollback) && rollback.diagnosticActive && rollback.diagnosticAngle < 0;
  clock.setRecoveryState('RESTARTING'); changed(); checks.restartingLocked = sealed(clock.sample());
  clock.setRecoveryState('HUMAN_INTERVENTION_REQUIRED'); changed(); checks.humanIntervention = !clock.sample().diagnosticActive;
  clock.repairSuccess(); changed(.16); await delay(650);
  checks.reauthorization = clock.mode === 'normal' && clock.time >= 1.6 && clock.time < 2.4;
  clock.setSpeed(2); changed(); await delay(5300);
  checks.recoveryRespectsReadiness = clock.held && !clock.sample().online;
  clock.setGate('ready', true); changed(); await delay(800);
  checks.recoveryReachedOnline = clock.sample().online;
  clock.resetNormal(); clock.setSpeed(1); clock.seek(7.8);
  triggerFault(); clock.setRecoveryState('SAFE_MODE'); changed(.16); await delay(200);
  checks.safeModeImmediate = sealed(clock.sample()) && clock.sample().panel === 1 && !clock.playing && audio.sources.length === 0;
  clock.resetNormal(); clock.seek(5.15); setReduced(true); triggerFault();
  clock.pause(); clock.seek(1); changed(); const minimal = clock.sample(true);
  checks.reducedFault = minimal.particleCount === 0 && minimal.orbitOffsets.every(x => x === 0) && minimal.pulse === 0;
  checks.allFailureStemsDecoded = audio.disabled || audio.buffers.size === Object.keys(manifest.sounds).length;
  checks.noAudioWarnings = audio.warnings.length === 0;
  clock.pause(); audio.stop();
  const report = { checks, passed: Object.values(checks).every(Boolean), decodedStems: audio.buffers.size,
    audioPeak: audio.peak, audioState: audio.context?.state ?? 'disabled', warnings: audio.warnings,
    testedNormalTimes: points, containmentSeconds: manifest.failure.duration, userAgent: navigator.userAgent };
  host({ type: 'verification-result', report }); return report;
}
