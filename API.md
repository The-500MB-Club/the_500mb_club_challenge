# API

A sua API deve expor exatamente os endpoints abaixo. O tráfego chega sempre através do load balancer na porta **8080**, em round-robin entre as 3+ instâncias.

Toda resposta, em qualquer endpoint, **deve** incluir o header `X-Instance-Id` com o identificador da instância que respondeu. O verificador de distribuição do load balancer depende dele.

## `GET /healthz`

Liveness probe. A sua API deve responder com `HTTP 200` enquanto o processo estiver vivo. **Não** deve consultar o storage — este endpoint mede apenas se o processo está de pé.

## `GET /readyz`

Readiness probe. Responde `HTTP 200` quando o serviço está pronto para receber tráfego (conexão com o storage estabelecida, pool montado) e `HTTP 503` enquanto não estiver. O storage é um dos quatro engines aceitos pelo desafio: `redis`, `postgres`, `mariadb` ou `mysql` (motivo em [`SECURITY.md`](./SECURITY.md#storage-suportado-allowlist)). A primeira requisição contável do benchmark é a primeira depois de `readyz` retornar `200`.

## `GET /metrics`

Exposição de métricas no formato Prometheus (`text/plain; version=0.0.4`). Deve responder `HTTP 200`.

## `POST /devices/{id}/telemetry`

Ingere um único ponto de telemetria de um dispositivo. O `id` no path identifica o device e deve casar com o padrão `^[a-zA-Z0-9_-]{1,64}$`.

O formato do payload é como o seguinte exemplo:

```json
{
  "ts": 1715800000000,
  "lat": -23.5505,
  "lon": -46.6333,
  "battery": 0.82,
  "ax": 0.11,
  "ay": -0.04,
  "az": 9.81
}
```

### Campos da requisição

| Campo | Tipo | Descrição |
| --- | --- | --- |
| `ts` | integer (int64) | Timestamp do ponto em epoch millis. Deve ser positivo. |
| `lat` | number | Latitude, no intervalo `[-90, 90]`. |
| `lon` | number | Longitude, no intervalo `[-180, 180]`. |
| `battery` | number | Nível de bateria, no intervalo `[0, 1]`. |
| `ax` | number | Aceleração no eixo X. Deve ser finito. |
| `ay` | number | Aceleração no eixo Y. Deve ser finito. |
| `az` | number | Aceleração no eixo Z. Deve ser finito. |

Campos obrigatórios: `ts`, `lat`, `lon`, `ax`, `ay`, `az`. O campo `battery` é opcional.

### Resposta

A sua API deve responder com `HTTP 202` e corpo vazio em caso de sucesso. A persistência pode ser síncrona ou assíncrona — o contrato apenas promete o `202`.

| Status | Quando |
| --- | --- |
| `202` | Ponto aceito. |
| `400` | Payload inválido (JSON malformado, campo obrigatório ausente, valor fora do intervalo). |
| `413` | Payload maior que o limite. |
| `429` | Rate limited (opcional, a critério da submissão). |

## `POST /devices/{id}/telemetry/batch`

Ingere de 1 a 100 pontos de telemetria em uma única requisição. Mesmo padrão de `id` do endpoint single.

O formato do payload é como o seguinte exemplo:

```json
{
  "points": [
    {
      "ts": 1715800000000,
      "lat": -23.5505,
      "lon": -46.6333,
      "battery": 0.82,
      "ax": 0.11,
      "ay": -0.04,
      "az": 9.81
    },
    {
      "ts": 1715800000100,
      "lat": -23.5506,
      "lon": -46.6334,
      "battery": 0.81,
      "ax": 0.09,
      "ay": -0.02,
      "az": 9.79
    }
  ]
}
```

### Campos da requisição

| Campo | Tipo | Descrição |
| --- | --- | --- |
| `points` | array | Lista de pontos de telemetria. Mínimo 1, máximo 100 itens. |
| `points[].*` | — | Cada item segue exatamente o schema de `POST /devices/{id}/telemetry`. |

### Resposta

A sua API deve responder no formato deste exemplo:

```json
{
  "accepted": 2
}
```

| Status | Quando |
| --- | --- |
| `202` | Lote aceito. `accepted` traz a quantidade de pontos persistidos. |
| `400` | Lote inválido (vazio, JSON malformado, algum ponto inválido). |
| `413` | Lote excede 100 pontos. |

## `GET /devices/{id}/telemetry`

Consulta os pontos de um device dentro de uma janela temporal, com paginação por cursor.

### Parâmetros de query

| Parâmetro | Tipo | Descrição |
| --- | --- | --- |
| `from` | integer (int64) | **Obrigatório.** Início da janela, epoch millis (inclusivo). |
| `to` | integer (int64) | **Obrigatório.** Fim da janela, epoch millis (inclusivo). |
| `limit` | integer | Máximo de pontos por página. Default `100`, mínimo `1`, máximo `500`. |
| `cursor` | string | Cursor opaco para a próxima página. Ausente na primeira chamada. |

`from` deve ser menor ou igual a `to`.

### Resposta

A sua API deve responder no formato deste exemplo:

```json
{
  "points": [
    {
      "ts": 1715800000000,
      "lat": -23.5505,
      "lon": -46.6333,
      "battery": 0.82,
      "ax": 0.11,
      "ay": -0.04,
      "az": 9.81
    }
  ],
  "next_cursor": "1715800000000"
}
```

| Campo | Tipo | Descrição |
| --- | --- | --- |
| `points` | array | Pontos da janela, ordenados por `ts` ascendente. Nunca maior que `limit`. |
| `next_cursor` | string \| `null` | Cursor para a próxima página, ou `null` quando não há mais dados. |

| Status | Quando |
| --- | --- |
| `200` | Consulta bem-sucedida (mesmo que `points` esteja vazio). |
| `400` | Parâmetros inválidos (`from`/`to` ausentes, `from > to`, `limit` fora do intervalo, cursor inválido). |
| `404` | Device sem dados (a critério da submissão; `200` com lista vazia também é aceito). |

## `GET /devices/{id}/anomaly`

Calcula o z-score da magnitude da aceleração sobre os últimos **256** pontos do device.

A magnitude de cada ponto é `sqrt(ax² + ay² + az²)`. O z-score é o do ponto **mais recente** contra a média e o desvio padrão dessa janela de 256 pontos. **Cache não é permitido** — o cálculo deve ser refeito a cada chamada.

### Resposta

A sua API deve responder no formato deste exemplo:

```json
{
  "z_score": 4.21,
  "samples": 256,
  "anomalous": true,
  "mean": 9.78,
  "stddev": 0.34
}
```

| Campo | Tipo | Descrição |
| --- | --- | --- |
| `z_score` | number | Z-score da magnitude do ponto mais recente. |
| `samples` | integer | Quantidade de pontos usados no cálculo (`0`–`256`). |
| `anomalous` | boolean | `true` se `z_score > 3`. |
| `mean` | number | Média das magnitudes na janela. |
| `stddev` | number | Desvio padrão das magnitudes na janela. |

Campos obrigatórios na resposta: `z_score`, `samples`, `anomalous`.

| Status | Quando |
| --- | --- |
| `200` | Cálculo realizado. |
| `404` | Device com menos de 8 pontos (amostras insuficientes). |

---

## Regras gerais

- O `id` do device, em qualquer endpoint, deve casar com `^[a-zA-Z0-9_-]{1,64}$`. Fora do padrão → `HTTP 400`.
- Todo corpo de requisição é `application/json`.
- Toda resposta inclui o header `X-Instance-Id`.
- Graceful shutdown obrigatório: ao receber `SIGTERM`, a aplicação deve drenar requisições em andamento em até 10 segundos.
