// Helpers compartilhados entre os cenarios k6.
// Configuravel por env: BASE_URL, DEVICE_COUNT.

import { randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

export const BASE = __ENV.BASE_URL || 'http://localhost:8080';
const DEVICE_COUNT = parseInt(__ENV.DEVICE_COUNT || '50', 10);

export const JSON_HEADER = { 'Content-Type': 'application/json' };

export function deviceId() {
  return `dev-${randomIntBetween(1, DEVICE_COUNT)}`;
}

function randomPoint(tsOffset = 0) {
  return {
    ts: Date.now() - tsOffset,
    lat: -23.55 + (Math.random() - 0.5) * 0.1,
    lon: -46.63 + (Math.random() - 0.5) * 0.1,
    battery: Math.random(),
    ax: (Math.random() - 0.5) * 4,
    ay: (Math.random() - 0.5) * 4,
    az: 9.8 + (Math.random() - 0.5) * 2,
  };
}

export function telemetryPayload() {
  return JSON.stringify(randomPoint());
}

// Batch com tamanho variavel (default 10..100 pontos).
// Timestamps decrescentes em incrementos de 100ms para evitar empate no
// score do sorted set, que tornaria a ordem dentro do batch indeterminada.
export function telemetryBatchPayload(minN = 10, maxN = 100) {
  const n = randomIntBetween(minN, maxN);
  const points = new Array(n);
  for (let i = 0; i < n; i++) {
    points[i] = randomPoint((n - 1 - i) * 100);
  }
  return JSON.stringify({ points });
}

export function tagged(op) {
  return { headers: JSON_HEADER, tags: { op } };
}

// Mixes nomeados. Soma == 1.0. Lidos por pickOp.
export const MIX_STEADY    = { post: 0.60, batch: 0.10, range: 0.20, anomaly: 0.10 };

// pickOp recebe um valor uniforme em [0,1) e o objeto mix.
// Itera em ordem de declaracao (V8 preserva ordem para chaves string).
export function pickOp(rand, mix) {
  let acc = 0;
  for (const op of Object.keys(mix)) {
    acc += mix[op];
    if (rand < acc) return op;
  }
  return 'post'; // fallback defensivo se mix nao somar 1
}
