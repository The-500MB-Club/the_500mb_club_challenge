#!/usr/bin/env bash
# Pi-Bench - coleta as metricas de footprint de uma submissao:
#   image_mb.txt     tamanho da imagem da API (camadas comprimidas, arm64)
#   cold_start_s.txt  tempo de compose up -> /readyz=200 (boot do runtime)
#
# Pensado pra ser chamado pelo bench-runner logo apos `docker compose up -d`
# (passando -s com o epoch do `up`), mas tambem roda manual. NAO sobe nem
# derruba a stack - so consulta a imagem e faz poll do /readyz.
#
# Uso:
#   measure_footprint.sh -u <LB_URL> -o <OUT_DIR> (-p <PROJETO> | -i "<IMG> ...") \
#                        [-s <START_EPOCH>] [-t <TIMEOUT_S>]
#
#   -u  URL base do load balancer (ex.: http://localhost:8080)
#   -o  diretorio de saida (onde caem image_mb.txt e cold_start_s.txt)
#   -p  nome do projeto compose: descobre a imagem da API automaticamente
#       (a mais replicada e nao-infra; mesma logica de descoberta por label do
#       capture-stats.sh). Use isto OU -i.
#   -i  imagem(ns) da API explicitas (override). Varias => soma. Entre aspas.
#   -s  epoch (date +%s.%N) do momento do `compose up`. Sem ele, o relogio do
#       cold start comeca AGORA (assume chamada logo apos o up).
#   -t  timeout do cold start em segundos (default 120)
#
# Exit: 0 sempre que conseguiu escrever pelo menos uma metrica; 2 em erro de uso.

set -uo pipefail

LB_URL=""
IMAGES=""
PROJECT=""
OUT_DIR=""
START_EPOCH=""
TIMEOUT=120

# Imagens de infra compartilhada (LB + storage da allowlist). Nao sao o
# artefato do participante; nunca contam no footprint.
INFRA_RE='^(.*/)?(redis|postgres|mariadb|mysql|nginx|haproxy|traefik|envoy|caddy)(:|$|@)'

usage() {
  cat >&2 <<EOF
Uso: $0 -u LB_URL -o OUT_DIR (-p PROJETO | -i "IMG ...") [-s START_EPOCH] [-t TIMEOUT]
EOF
  exit 2
}

while getopts ":u:i:p:o:s:t:h" opt; do
  case "$opt" in
    u) LB_URL="$OPTARG" ;;
    i) IMAGES="$OPTARG" ;;
    p) PROJECT="$OPTARG" ;;
    o) OUT_DIR="$OPTARG" ;;
    s) START_EPOCH="$OPTARG" ;;
    t) TIMEOUT="$OPTARG" ;;
    h|*) usage ;;
  esac
done

[ -n "$LB_URL" ] && [ -n "$OUT_DIR" ] || usage
mkdir -p "$OUT_DIR"

# Descoberta automatica: imagens nao-infra dos containers do projeto compose
# (a API tem >=3 replicas com a mesma imagem; LB/storage sao infra). uniq
# colapsa as replicas numa imagem so.
if [ -z "$IMAGES" ] && [ -n "$PROJECT" ]; then
  IMAGES="$(docker ps --filter "label=com.docker.compose.project=$PROJECT" \
              --format '{{.Image}}' 2>/dev/null \
            | grep -Ev "$INFRA_RE" | sort -u | tr '\n' ' ')"
  echo "footprint: imagem(ns) da API descoberta(s) no projeto '$PROJECT': ${IMAGES:-<nenhuma>}" >&2
fi

now() { date +%s.%N; }

# ---------------------------------------------------------------------------
# image_mb: soma das camadas COMPRIMIDAS do manifesto arm64 (semantica de
# "download na borda"). Requer jq; sem ele (ou se o manifesto nao for legivel)
# cai pro tamanho descomprimido local `docker inspect {{.Size}}`.
# ---------------------------------------------------------------------------
arm64_compressed_bytes() {
  local img="$1" raw arch_digest repo layers config
  command -v jq >/dev/null 2>&1 || return 1
  raw="$(docker manifest inspect "$img" 2>/dev/null)" || return 1
  [ -n "$raw" ] || return 1

  if echo "$raw" | jq -e '.manifests' >/dev/null 2>&1; then
    # Manifest list: acha o digest da plataforma arm64 e reinspeciona.
    arch_digest="$(echo "$raw" | jq -r \
      '.manifests[] | select(.platform.architecture=="arm64") | .digest' \
      | head -1)"
    [ -n "$arch_digest" ] && [ "$arch_digest" != "null" ] || return 1
    repo="${img%%@*}"; repo="${repo%:*}"   # tira tag/digest, mantem repo
    raw="$(docker manifest inspect "${repo}@${arch_digest}" 2>/dev/null)" || return 1
  fi
  # Manifesto unico (schema 2): config.size + sum(layers[].size), em bytes.
  layers="$(echo "$raw" | jq '[.layers[]?.size] | add // 0')" || return 1
  config="$(echo "$raw" | jq '.config.size // 0')" || return 1
  echo $(( layers + config ))
}

uncompressed_bytes() {
  docker inspect --format '{{.Size}}' "$1" 2>/dev/null || echo 0
}

total_bytes=0
method="comprimido(arm64)"
got_any=0
for img in $IMAGES; do
  b="$(arm64_compressed_bytes "$img")"
  if [ -z "$b" ] || [ "$b" = "0" ]; then
    b="$(uncompressed_bytes "$img")"
    method="descomprimido(local)"
  fi
  if [ -n "$b" ] && [ "$b" != "0" ]; then
    total_bytes=$(( total_bytes + b ))
    got_any=1
  fi
done

if [ "$got_any" = "1" ]; then
  # bytes -> MB (base 2, alinhado com o docker stats do capture-stats.sh).
  image_mb="$(awk -v b="$total_bytes" 'BEGIN{printf "%.2f", b/1048576}')"
  printf '%s\n' "$image_mb" > "$OUT_DIR/image_mb.txt"
  echo "footprint: image_mb=${image_mb} (${method}) -> $OUT_DIR/image_mb.txt" >&2
else
  echo "footprint: nao consegui medir a imagem ($IMAGES); image_mb.txt nao escrito" >&2
fi

# ---------------------------------------------------------------------------
# cold_start_s: tempo ate /readyz responder 200 de forma estavel (3x seguidas,
# pra nao pegar uma replica fria sob round-robin). Isola boot do runtime +
# init + conexao ao storage + readiness.
# ---------------------------------------------------------------------------
start="${START_EPOCH:-$(now)}"
deadline="$(awk -v s="$start" -v t="$TIMEOUT" 'BEGIN{printf "%.3f", s+t}')"
url="${LB_URL%/}/readyz"
consec=0
ready_at=""
while :; do
  cur="$(now)"
  if awk -v c="$cur" -v d="$deadline" 'BEGIN{exit !(c>d)}'; then
    break  # estourou o timeout
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$url" 2>/dev/null || echo 000)"
  if [ "$code" = "200" ]; then
    consec=$(( consec + 1 ))
    if [ "$consec" -ge 3 ]; then
      ready_at="$(now)"
      break
    fi
  else
    consec=0
  fi
  sleep 0.2
done

if [ -n "$ready_at" ]; then
  cold="$(awk -v a="$ready_at" -v s="$start" 'BEGIN{printf "%.2f", a-s}')"
  printf '%s\n' "$cold" > "$OUT_DIR/cold_start_s.txt"
  echo "footprint: cold_start_s=${cold} -> $OUT_DIR/cold_start_s.txt" >&2
else
  echo "footprint: /readyz nao ficou 200 em ${TIMEOUT}s; cold_start_s.txt nao escrito" >&2
fi

exit 0
