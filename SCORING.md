# Como a pontuação funciona

Este documento explica, em detalhe, como cada submissão é pontuada na execução do teste final — quando o benchmark de verdade roda contra a stack no Raspberry Pi.

## TL;DR

- A nota é **relativa a um perfil-alvo absoluto** (SLOs de latência + orçamento de
  2 CPU / 500 MB), **não** à nenhuma implementação específica. **`100` = você atende o alvo**; acima
  de 100 = você o supera.
- O **score global** (média ponderada de 6 dimensões × 100) é o **decisor único** do
  ranking. Além dele, cada dimensão dá uma **medalha** 🥇 ao líder — para que
  linguagens diferentes brilhem em eixos diferentes.
- Antes de pontuar, há um **gate**: sustentar a carga base sem erro e caber no
  orçamento. Falhou o gate → sai do pódio (mas não é desqualificada em silêncio).

## Por que alvos absolutos (e não "corrida contra uma implementação")

Uma alternativa seria normalizar tudo contra uma implementação de referência
(`100 = igual a ela`). O efeito colateral: implementações nativas (compiladas, sem GC)
são excelentes nas métricas de baixo nível, então qualquer runtime gerenciado começaria
atrás só por **não ser essa referência** — uma latência p99 de 67 ms (perfeitamente
adequada para ingestão de telemetria) cairia no piso só por ser "mais lenta que 2 ms".

Por isso ancoramos cada métrica a um **alvo absoluto**: um SLO de latência generoso,
o orçamento de memória/CPU, um RPS de referência. Quem cumpre os SLOs e cabe no
orçamento pontua bem (≥ 100), independente da linguagem; a **eficiência** e a
**capacidade** — o coração do "500 MB Club" — é que separam o topo.

## Os cenários de carga

O benchmark roda quatro cenários k6 contra a stack (o gerador roda **fora** do Pi).
Cada dimensão é alimentada por um cenário específico:

| Cenário | Carga | Alimenta |
|---|---|---|
| **steady** | taxa fixa de 200 RPS, 10 min, mix realista (60% POST, 10% batch, 20% range, 10% anomaly) | eficiência, latência p99 + o **gate** |
| **capacity** | rampa por degraus (200 → 5000 RPS) até estourar o SLO | capacidade (RPS máx sustentado) |
| **spike** | rampa 50 → 800 RPS, pico sustentado, recuo | resiliência |
| **endurance** | carga prolongada (~45 min) | estabilidade (deriva) |
| _footprint_ | medido fora dos cenários k6 (manifesto da imagem + `compose up`→`/readyz`) | footprint |

Os cenários `smoke` e `test` rodam **antes** como verificação de corretude (contrato
da API): se o `smoke` falha, nada mais roda. Eles **não** entram na nota.

## O gate (pré-condição)

Antes da média ponderada, a submissão precisa, no `steady`:

- **Sustentar a carga oferecida** (~200 RPS) com `http_req_failed` < **0,5%**.
- **Caber no orçamento em runtime**: RSS p95 agregado < **500 MB** e CPU < **200%** (2 cores).

Quem falha recebe a flag **`gated`**: cai para fora do pódio no leaderboard, mas a nota
ainda é calculada e mostrada — nada de desqualificação opaca.

## As 6 dimensões

Cada dimensão é a **média** das razões (após _clip_) das suas métricas. Para uma
métrica com alvo `T` e valor observado `V`:

- "**maior é melhor**" (`up`): razão = `V / T`
- "**menor é melhor**" (`down`): razão = `T / V`

`razão = 1.0` significa exatamente no alvo; `> 1` supera, `< 1` fica abaixo. A razão é
travada (_clipped_) no intervalo da dimensão antes de entrar na média.

| Dimensão | Peso | Métrica(s) → alvo | Clip | Papel |
|---|---|---|---|---|
| **efficiency** | **0,30** | `rss_p95` → 250 MB · `cpu_avg` → 40% (no steady) | 0,25–4,0 | o tema; clip largo = alta resolução |
| **capacity** | **0,25** | `max_sustained_rps` → 1500 RPS | 0,25–4,0 | trabalho dentro do orçamento (manchete) |
| **tail_latency** | **0,18** | p99 post/batch/range/anomaly → 75/150/120/150 ms | 0,25–1,5 | "cumpre o SLO" |
| **resilience** | **0,12** | `spike_p99` → 120 ms · `spike_error` → 1% | 0,25–2,0 | aguenta o pico |
| **footprint** | **0,08** | `image_mb` → 80 MB · `cold_start_s` → 12 s | 0,25–2,0 | enxuto para a borda |
| **stability** | **0,07** | `latency_drift` → 1,10 · `rss_drift` → 1,10 | 0,25–1,5 | sem vazar/degradar |

Por que esses clips: **efficiency** e **capacity** (clip largo até 4,0) carregam a
separação real — quem usa 1/4 do orçamento marca ~3×. As demais saturam em "está bom"
(teto 1,5–2,0) para que diferenças irrelevantes (2 ms vs 5 ms de p99) não dominem a
média. O **par de eficiência é metade do orçamento** (250 MB / 40%): preserva resolução
no topo sem estourar o teto do clip.

### efficiency (30%)

`RSS p95` e `CPU médio` agregados (soma de todos os containers: APIs + LB + storage),
medidos no `steady@200`, contra a metade do orçamento. É onde a frugalidade aparece:
usar 90 MB pontua ~2,8×; usar 350 MB, ~0,7×.

### capacity (25%)

O **RPS máximo sustentado** dentro do orçamento — o "joelho" da curva de carga. Mede
quanto trabalho real você entrega com 2 CPU / 500 MB. Detalhe do cálculo abaixo. (O RPS
de referência é **provisório** e será calibrado a partir da primeira rodada real no Pi.)

### tail_latency (18%)

p99 das quatro operações no `steady`, contra SLOs generosos. Cumprir o SLO já dá nota
cheia (o clip satura em 1,5); o objetivo é premiar quem atende o SLO sem exigir uma
micro-latência irrelevante para o caso de uso.

### resilience (12%)

Como o serviço se comporta **durante** o pico (spike): o p99 sob pico (sinal robusto)
e a taxa de erro no pico.

### footprint (8%)

Tamanho da imagem da API (camadas comprimidas do manifesto arm64) e o cold start
(`compose up` → `/readyz=200`). Premia imagens enxutas e boot rápido — relevante para
borda. Alvos generosos para que JVM/.NET (imagens e warm-up maiores) não caiam no piso.

### stability (7%)

Deriva ao longo do `endurance`: p99 dos últimos 5 min ÷ primeiros 5 min, e RSS final ÷
inicial. É uma **garantia** ("não vazou memória, não degradou"), não um diferenciador —
por isso o peso baixo.

## A fórmula do score global

```
para cada dimensão d presente:
    dim[d] = média( clip(razão de cada métrica, clip_min[d], clip_max[d]) )

score = 100 × Σ ( peso[d] × dim[d] )  ÷  Σ peso[d]      (só sobre as dimensões presentes)
```

A renormalização (`÷ Σ peso[d]` só das presentes) é o que faz uma dimensão **ausente**
não derrubar a nota — ver "métrica ausente" abaixo.

### Exemplo trabalhado (dados reais do Pi)

| Dimensão | Cálculo | dim |
|---|---|---|
| efficiency | média(`250/107`=2,34 ; `40/18.5`=2,16) | **2,25** |
| tail_latency | p99 2/5,4/3,5/4,6 ms → todas ≫ alvo → clip | **1,50** |
| resilience | média(`120/14`→clip 2,0 ; erro 0 → 2,0) | **2,00** |
| stability | média(`1.10/1.00` ; `1.10/1.04`) | **1,11** |
| capacity / footprint | sem dado → **excluídas** | — |

Pesos presentes: 0,30 + 0,18 + 0,12 + 0,07 = **0,67**.

```
score = 100 × (0,30·2,25 + 0,18·1,50 + 0,12·2,00 + 0,07·1,11) ÷ 0,67
      = 100 × 1,2627 ÷ 0,67 ≈ 188,5
```

## Como o "joelho" da capacidade é medido

O cenário de capacidade sobe a carga em **degraus sustentados** (platô de ~45 s +
rampa de ~10 s), de 200 a 5000 RPS. O joelho **não** é um threshold do k6 — é
calculado a partir da série temporal por request (um evento por requisição):

1. Cada request é atribuído ao seu degrau pelo tempo decorrido desde o início do teste.
2. No **platô** de cada degrau (descartando os ~10 s iniciais de acomodação) mede-se:
   p99 da latência, taxa de erro, RPS efetivamente entregue e se houve
   `dropped_iterations` (sinal de que o serviço não acompanha a taxa oferecida).
3. Um degrau **conta como sustentado** se: `p99 < 150 ms` **E** `erro < 0,5%` **E**
   `entregue ≥ 95% do oferecido` **E** sem `dropped_iterations`.
4. **`max_sustained_rps` = o maior degrau contíguo** (a partir do primeiro) que se
   sustentou.

Por que o critério é por **SLO**, não por crash: a 800 RPS, nos dados reais, *nenhuma*
linguagem deu erro — mas o p99 do Python já estava em 230 ms (vs 10–67 ms das outras).
Um serviço "no ar, mas a 230 ms" já quebrou para o caso de uso (o mapa que trava). O
joelho por SLO captura isso; o joelho por crash não.

## Como o footprint é medido

- **`image_mb`** — soma das camadas **comprimidas** do manifesto **arm64** da imagem da
  API (o seu artefato; redis/postgres/nginx oficiais não contam). Fallback para o
  tamanho descomprimido local se o manifesto não for legível.
- **`cold_start_s`** — tempo entre o `docker compose up` e o `/readyz` responder `200`
  de forma estável (3× seguidas). As imagens são baixadas **antes** e não contam — isola
  o boot do runtime + init + conexão ao storage + readiness (onde JVM/.NET pagam warm-up).

## Política de métrica ausente

- **Cenário rodou e falhou** (crash) → a dimensão afetada recebe o piso (`0,25`).
- **O harness não coletou** a métrica (ex.: capacity/footprint ainda não disponíveis) →
  a dimensão é **excluída e os pesos renormalizam** entre as presentes. Você nunca é
  punido por uma lacuna do harness — só pelo que de fato foi medido.

## Reconhecimento

- **Score global** = decisor único do ranking e do vencedor.
- **Medalhas por dimensão** 🥇 vão para o líder **único** de cada eixo (mais frugal em
  RAM, mais frugal em CPU, maior RPS sustentado, menor p99, melhor resiliência, menor
  imagem, mais estável). Eixos em que todos saturam em "excelente" (latência,
  resiliência) não dão medalha — ela vai para os diferenciadores reais.

## Onde ver o detalhe

Cada submissão recebe um detalhamento métrica-a-métrica — a razão de cada métrica
contra o alvo, as seis dimensões, as flags de gate e o score final —, e o **leaderboard**
mostra o ranking pelo score global com as medalhas. Todos os pesos, alvos e clips usados
no cálculo estão documentados neste arquivo.
