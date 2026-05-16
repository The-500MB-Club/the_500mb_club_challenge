// Smoke test: valida que a implementacao faz o basico funcionar.
// Roda em ~5s, baixa carga, asserts agressivos. Se isso passar, vale rodar
// steady/spike/endurance. Se nao passar, nem comece os benchmarks pesados.
//
// Cobre:
//   - healthchecks (healthz, readyz, metrics)
//   - ingest single (202)
//   - ingest batch (202 + accepted == N)
//   - query range (200, retorna o que foi escrito)
//   - cursor de paginacao (next_cursor presente quando ha mais pagina)
//   - anomaly (200 com payload completo)
//   - anomaly com amostras insuficientes (<8) -> 404
//   - validacao de payload (400 em ts ausente / lat fora do range / batch vazio / from > to / limit fora de 1..500)
//   - batch acima do limite (>100 itens) -> 413
//   - header X-Instance-Id presente em toda resposta do upstream (healthz e local no nginx, excecao)
//   - load balancer distribui entre as 3 instancias
//
// Rodar: k6 run --env BASE_URL=http://<pi>:8080 k6/smoke.js

import http from 'k6/http';
import { check, group, fail } from 'k6';
import { BASE, telemetryPayload, telemetryBatchPayload, JSON_HEADER } from './lib/helpers.js';

export const options = {
  vus: 1,
  iterations: 1,
  thresholds: {
    // Em smoke nenhuma checagem pode falhar e nenhuma request pode quebrar.
    checks: ['rate==1.0'],
    http_req_failed: ['rate==0'],
  },
};

// Device unico por execucao para nao colidir com runs anteriores.
const DEVICE = `smoke-${Date.now()}`;

export default function () {
  group('1. health endpoints', () => {
    const hz = http.get(`${BASE}/healthz`);
    check(hz, {
      'healthz 200':              (r) => r.status === 200,
      'healthz body contains ok': (r) => String(r.body).includes('ok'),
    });

    const rz = http.get(`${BASE}/readyz`);
    check(rz, {
      'readyz 200':              (r) => r.status === 200,
      'readyz traz X-Instance-Id': (r) => !!r.headers['X-Instance-Id'],
    });
  });

  group('2. ingest single', () => {
    const res = http.post(
      `${BASE}/devices/${DEVICE}/telemetry`,
      telemetryPayload(),
      { headers: JSON_HEADER }
    );
    check(res, {
      'POST single 202':          (r) => r.status === 202,
      'X-Instance-Id presente':   (r) => !!r.headers['X-Instance-Id'],
    });
  });

  group('3. ingest batch', () => {
    // Tamanho fixo de 50 para podermos asseverar exato.
    const payload = telemetryBatchPayload(50, 50);
    const res = http.post(
      `${BASE}/devices/${DEVICE}/telemetry/batch`,
      payload,
      { headers: JSON_HEADER }
    );
    const body = res.status === 202 ? safeJson(res.body) : null;
    check(res, {
      'POST batch 202':          (r) => r.status === 202,
      'batch accepted == 50':    () => body?.accepted === 50,
      'X-Instance-Id presente':  hasInstanceId,
    });
  });

  // Pequeno gap para dar margem caso ingest seja async em alguma submissao.
  // (Nossa referencia Go e sincrona, mas o contrato so promete 202.)
  http.get(`${BASE}/healthz`); // burn-in trivial

  group('4. query range', () => {
    const now = Date.now();
    const res = http.get(
      `${BASE}/devices/${DEVICE}/telemetry?from=${now - 600000}&to=${now}&limit=10`
    );
    const body = res.status === 200 ? safeJson(res.body) : null;
    check(res, {
      'GET range 200':                   (r) => r.status === 200,
      'response tem points[]':            () => Array.isArray(body?.points),
      'points <= limit':                  () => (body?.points?.length ?? 999) <= 10,
      'points nao vazio (ingest visivel)': () => (body?.points?.length ?? 0) > 0,
      'X-Instance-Id presente':           hasInstanceId,
    });

    // Cursor: pedimos limit=10 e ingerimos 51, deve haver next_cursor.
    check(res, {
      'next_cursor presente quando ha mais': () =>
        body?.next_cursor !== null && body?.next_cursor !== undefined,
    });

    // Segunda pagina via cursor.
    if (body?.next_cursor) {
      const pg2 = http.get(
        `${BASE}/devices/${DEVICE}/telemetry?from=${now - 600000}&to=${now}&limit=10&cursor=${body.next_cursor}`
      );
      const pg2body = pg2.status === 200 ? safeJson(pg2.body) : null;
      check(pg2, {
        'pagina 2 status 200':          (r) => r.status === 200,
        'pagina 2 tem pontos':          () => (pg2body?.points?.length ?? 0) > 0,
        'pagina 2 nao reentrega cursor': () =>
          pg2body?.points?.[0]?.ts !== body.points[body.points.length - 1]?.ts,
        'X-Instance-Id presente':        hasInstanceId,
      });
    }
  });

  group('5. anomaly', () => {
    const res = http.get(`${BASE}/devices/${DEVICE}/anomaly`);
    const body = res.status === 200 ? safeJson(res.body) : null;
    check(res, {
      'anomaly 200':                     (r) => r.status === 200,
      'campo z_score numerico':          () => typeof body?.z_score === 'number',
      'campo samples > 0':                () => (body?.samples ?? 0) > 0,
      'campo anomalous boolean':          () => typeof body?.anomalous === 'boolean',
      'X-Instance-Id presente':           hasInstanceId,
    });
  });

  group('6. validacao de erro (4xx esperados)', () => {
    // ts ausente -> 400. Marcamos como NAO falha para nao poluir http_req_failed,
    // que mede falhas inesperadas. Validacao explicita via check.
    const badPayload = JSON.stringify({ lat: 0, lon: 0, ax: 0, ay: 0, az: 0 });
    const r1 = http.post(
      `${BASE}/devices/${DEVICE}/telemetry`,
      badPayload,
      { headers: JSON_HEADER, responseCallback: http.expectedStatuses(400) }
    );
    check(r1, {
      'ts ausente -> 400':       (r) => r.status === 400,
      'X-Instance-Id presente':  hasInstanceId,
    });

    // lat fora do range
    const oob = JSON.stringify({ ts: Date.now(), lat: 200, lon: 0, ax: 0, ay: 0, az: 0, battery: 0.5 });
    const r2 = http.post(
      `${BASE}/devices/${DEVICE}/telemetry`,
      oob,
      { headers: JSON_HEADER, responseCallback: http.expectedStatuses(400) }
    );
    check(r2, {
      'lat fora do range -> 400': (r) => r.status === 400,
      'X-Instance-Id presente':   hasInstanceId,
    });

    // batch vazio
    const empty = JSON.stringify({ points: [] });
    const r3 = http.post(
      `${BASE}/devices/${DEVICE}/telemetry/batch`,
      empty,
      { headers: JSON_HEADER, responseCallback: http.expectedStatuses(400) }
    );
    check(r3, {
      'batch vazio -> 400':      (r) => r.status === 400,
      'X-Instance-Id presente':  hasInstanceId,
    });

    // batch acima do limite (>100 itens) -> 413
    const oversizedPoints = new Array(101);
    for (let i = 0; i < 101; i++) {
      oversizedPoints[i] = {
        ts: Date.now() - (100 - i) * 100,
        lat: 0, lon: 0, ax: 0, ay: 0, az: 0, battery: 0.5,
      };
    }
    const oversized = JSON.stringify({ points: oversizedPoints });
    const r5 = http.post(
      `${BASE}/devices/${DEVICE}/telemetry/batch`,
      oversized,
      { headers: JSON_HEADER, responseCallback: http.expectedStatuses(413) }
    );
    check(r5, {
      'batch > 100 itens -> 413': (r) => r.status === 413,
      'X-Instance-Id presente':   hasInstanceId,
    });

    // from > to
    const r4 = http.get(
      `${BASE}/devices/${DEVICE}/telemetry?from=2000&to=1000&limit=10`,
      { responseCallback: http.expectedStatuses(400) }
    );
    check(r4, {
      'from > to -> 400':        (r) => r.status === 400,
      'X-Instance-Id presente':  hasInstanceId,
    });

    // limit < 1 -> 400 (faixa valida e 1..500)
    const nowLim = Date.now();
    const r6 = http.get(
      `${BASE}/devices/${DEVICE}/telemetry?from=${nowLim - 600000}&to=${nowLim}&limit=0`,
      { responseCallback: http.expectedStatuses(400) }
    );
    check(r6, {
      'limit < 1 -> 400':        (r) => r.status === 400,
      'X-Instance-Id presente':  hasInstanceId,
    });

    // limit > 500 -> 400
    const r7 = http.get(
      `${BASE}/devices/${DEVICE}/telemetry?from=${nowLim - 600000}&to=${nowLim}&limit=501`,
      { responseCallback: http.expectedStatuses(400) }
    );
    check(r7, {
      'limit > 500 -> 400':      (r) => r.status === 400,
      'X-Instance-Id presente':  hasInstanceId,
    });

    // anomaly em device com amostras insuficientes (<8) -> 404.
    // Usa um device novo, sem ingest, para garantir 0 pontos.
    const emptyDevice = `smoke-empty-${Date.now()}`;
    const r8 = http.get(
      `${BASE}/devices/${emptyDevice}/anomaly`,
      { responseCallback: http.expectedStatuses(404) }
    );
    check(r8, {
      'anomaly samples < 8 -> 404': (r) => r.status === 404,
      'X-Instance-Id presente':     hasInstanceId,
    });
  });

  // /metrics so emite series http_request_* depois que alguma rota instrumentada
  // foi observada. Healthchecks ficam fora do middleware de propósito, entao
  // esse check vive aqui no final, depois dos grupos 2-5 ja terem gerado labels.
  group('7. metrics endpoint', () => {
    const mz = http.get(`${BASE}/metrics`);
    check(mz, {
      'metrics 200':                    (r) => r.status === 200,
      'metrics em formato prometheus':  (r) => String(r.body).includes('http_request'),
      'X-Instance-Id presente':         hasInstanceId,
    });
  });

  group('8. round-robin distribui entre as 3 instancias', () => {
    // 30 requests a /readyz (que vai ao upstream e carrega X-Instance-Id).
    // /healthz no nginx responde local, nao serve pra esse sanity.
    const seen = {};
    for (let i = 0; i < 30; i++) {
      const r = http.get(`${BASE}/readyz`);
      const id = r.headers['X-Instance-Id'];
      if (id) seen[id] = (seen[id] || 0) + 1;
    }
    const instances = Object.keys(seen);
    console.log(`distribuicao do LB: ${JSON.stringify(seen)}`);
    check(null, {
      '3 instancias distintas responderam': () => instances.length === 3,
      'cada instancia recebeu >= 3 hits (10% do total)': () =>
        instances.every((i) => seen[i] >= 3),
    });
  });
}

function safeJson(body) {
  try { return JSON.parse(body); } catch { return null; }
}

// Toda resposta servida pelo upstream deve carregar X-Instance-Id.
// /healthz e excecao porque o nginx responde local (nao chega no app).
function hasInstanceId(r) {
  return !!r.headers['X-Instance-Id'];
}
