const { addNoise, clamp, decay, hillResponse } = require('../utils/math');
const config = require('../utils/config');

function getEffectiveBaseline(neuromodConfig, circadianMod, relationshipBonus, retrospectionMod) {
  return clamp(
    neuromodConfig.baseline + (circadianMod || 0) + (relationshipBonus || 0) + (retrospectionMod || 0),
    5,
    95
  );
}

function applyStimulus(state, neuromodName, rawDelta) {
  const nmCfg = config.neuromodulatorConfigs[neuromodName];
  if (!nmCfg || !state.neuromodulators[neuromodName]) return;

  const delta = hillResponse(rawDelta, nmCfg.EC50, nmCfg.hillN, nmCfg.Emax);

  if (neuromodName === 'cortisol') {
    // Fix #1: 30% 즉시 반영, 70% HPA 지연
    const immediateFraction = 0.3;
    const immediateDelta = delta * immediateFraction;
    const delayedDelta = delta * (1 - immediateFraction);

    // 즉시 반영분
    const noisyImmediate = addNoise(immediateDelta, config.noiseBaseFraction);
    state.neuromodulators.cortisol.value = clamp(
      state.neuromodulators.cortisol.value + noisyImmediate,
      0,
      100
    );

    // 지연분 (15-30분 후) — Fix #2: 상한 50개 제한
    if (Math.abs(delayedDelta) > 0.1) {
      const pending = state.neuromodulators.cortisol.pending;
      if (pending.length < 50) {
        const delayMs = (15 + Math.random() * 15) * 60 * 1000;
        pending.push({
          delta: delayedDelta,
          activateAt: Date.now() + delayMs,
          source: 'hpa_axis',
        });
      }
    }
    return;
  }

  // Fix #3: 극값 근처에서 반대 방향 delta에 1.5x 부스트
  let adjustedDelta = delta;
  const currentVal = state.neuromodulators[neuromodName].value;
  if (currentVal > 85 && delta < 0) {
    // 포화 상태에서 하락 → 1.5x 가중
    adjustedDelta = delta * 1.5;
  } else if (currentVal < 15 && delta > 0) {
    // 바닥 상태에서 상승 → 1.5x 가중
    adjustedDelta = delta * 1.5;
  }

  const noisyDelta = addNoise(adjustedDelta, config.noiseBaseFraction);
  state.neuromodulators[neuromodName].value = clamp(
    state.neuromodulators[neuromodName].value + noisyDelta,
    0,
    100
  );
}

function processPendingCortisol(state) {
  const now = Date.now();
  const pending = state.neuromodulators.cortisol.pending || [];
  const ready = pending.filter((p) => p.activateAt <= now);
  const notReady = pending.filter((p) => p.activateAt > now);

  for (const p of ready) {
    state.neuromodulators.cortisol.value = clamp(state.neuromodulators.cortisol.value + p.delta, 0, 100);
  }

  state.neuromodulators.cortisol.pending = notReady;
}

function decayAll(state, elapsedMin, effectiveBaselines) {
  for (const [name, nmState] of Object.entries(state.neuromodulators)) {
    const cfg = config.neuromodulatorConfigs[name];
    const baseline = typeof effectiveBaselines[name] === 'number' ? effectiveBaselines[name] : cfg.baseline;
    nmState.value = clamp(decay(nmState.value, baseline, cfg.tau, elapsedMin), 0, 100);
  }
}

module.exports = {
  applyStimulus,
  processPendingCortisol,
  decayAll,
  getEffectiveBaseline,
};
