// Test state: 100 RPS, 1 min
// Mix: 60% POST single | 10% POST batch | 20% GET range | 10% GET anomaly
//
// Rodar: k6 run --env BASE_URL=http://<url/ip>:8080 test.js

import http from 'k6/http';
import {
  BASE, deviceId, telemetryPayload, telemetryBatchPayload,
  tagged, pickOp, MIX_STEADY,
} from './lib/helpers.js';

export const options = {
  discardResponseBodies: true,
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: 100,
      timeUnit: '1s',
      duration: '1m',
      preAllocatedVUs: 100,
      maxVUs: 400,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.001'],
    'http_req_duration{op:post}':    ['p(99)<60'],
    'http_req_duration{op:batch}':   ['p(99)<80'],
    'http_req_duration{op:range}':   ['p(99)<60'],
    'http_req_duration{op:anomaly}': ['p(99)<50'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'p(99.9)', 'max'],
};

export default function () {
  const id = deviceId();
  const op = pickOp(Math.random(), MIX_STEADY);

  switch (op) {
    case 'post':
      http.post(`${BASE}/devices/${id}/telemetry`, telemetryPayload(), tagged('post'));
      break;
    case 'batch':
      http.post(`${BASE}/devices/${id}/telemetry/batch`, telemetryBatchPayload(), tagged('batch'));
      break;
    case 'range': {
      const now = Date.now();
      http.get(`${BASE}/devices/${id}/telemetry?from=${now - 60000}&to=${now}&limit=100`, tagged('range'));
      break;
    }
    case 'anomaly':
      http.get(`${BASE}/devices/${id}/anomaly`, tagged('anomaly'));
      break;
  }
}
