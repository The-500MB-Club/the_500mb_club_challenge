#!/usr/bin/env bash
# Captura `docker stats` de TODOS os containers de um projeto Compose
# em formato CSV, sem precisar conhecer os nomes internos da submissao.
#
# Uso:
#   ./capture-stats.sh -p <projeto> [-d <duracao_s>] [-o saida.csv]
#                      [-i intervalo_s]
#
#   -d e opcional: se omitido, a captura roda ate ser encerrada
#   manualmente (Ctrl+C / SIGTERM); se informado, encerra no timeout.
#
# Exemplo:
#   PROJECT="bench-$(date +%s)"
#   docker compose -p "$PROJECT" up -d
#   ./capture-stats.sh -p "$PROJECT" -d 660 -o results/steady-stats.csv &
#   k6 run k6/steady.js
#   wait
#
# Variaveis de ambiente reconhecidas:
#   SAMPLE_INTERVAL  default 2  (segundos entre amostras)
#
# Como funciona:
#   1. Descobre os containers do projeto via label do Compose
#      `com.docker.compose.project=<projeto>`. Funciona com qualquer
#      submissao - nomes hardcoded sao um anti-padrao.
#   2. Re-descobre a CADA amostra. Se um container reinicia ou e
#      recriado durante o teste, o novo ID e captado automaticamente.
#   3. Saida CSV com nome humano do container (nao o ID) para o
#      score.py poder distinguir api-* de lb e redis se precisar.

set -euo pipefail

# ---------- args ----------
PROJECT=""
DURATION=""
OUT="stats-$(date +%s).csv"
INTERVAL="${SAMPLE_INTERVAL:-2}"

usage() {
  cat >&2 <<EOF
Uso: $0 -p PROJETO [-d DURACAO] [-o ARQUIVO] [-i INTERVALO]

  -p PROJETO     nome do projeto Compose (obrigatorio)
  -d DURACAO     duracao da captura em segundos (opcional; sem ela a
                 captura roda ate ser encerrada manualmente)
  -o ARQUIVO     saida CSV (default: stats-<epoch>.csv)
  -i INTERVALO   segundos entre amostras (default: 2)
EOF
  exit 2
}

while getopts ":p:d:o:i:h" opt; do
  case "$opt" in
    p) PROJECT="$OPTARG" ;;
    d) DURATION="$OPTARG" ;;
    o) OUT="$OPTARG" ;;
    i) INTERVAL="$OPTARG" ;;
    h|*) usage ;;
  esac
done

[ -n "$PROJECT" ] || { echo "ERRO: -p obrigatorio" >&2; usage; }
if [ -n "$DURATION" ] && ! [[ "$DURATION" =~ ^[0-9]+$ ]]; then
  echo "ERRO: -d deve ser um numero inteiro de segundos" >&2; usage
fi

if ! command -v docker >/dev/null; then
  echo "ERRO: docker nao encontrado no PATH" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUT")"
echo "ts,name,cpu_pct,mem_usage,mem_pct,net_io,block_io,pids" > "$OUT"

# Confirma que o projeto existe ANTES de comecar a janela de captura.
# Se nao existe, falhar cedo e claro evita um CSV vazio que confunde
# o score downstream.
initial=$(docker ps --filter "label=com.docker.compose.project=$PROJECT" \
                    --format '{{.ID}}' | wc -l | tr -d ' ')
if [ "$initial" = "0" ]; then
  echo "ERRO: nenhum container do projeto '$PROJECT' esta rodando." >&2
  echo "      Verifique: docker ps --filter label=com.docker.compose.project=$PROJECT" >&2
  exit 1
fi
if [ -n "$DURATION" ]; then
  END=$(( $(date +%s) + DURATION ))
  dur_desc="${DURATION}s"
else
  END=""
  dur_desc="ate encerramento manual"
fi
echo "capture-stats: projeto='$PROJECT', $initial container(s), " \
     "duracao=${dur_desc}, intervalo=${INTERVAL}s, saida=$OUT" >&2

# Encerramento manual (Ctrl+C / SIGTERM): para o loop de forma limpa
# para que o resumo final ainda seja gravado.
running=1
trap 'running=0' INT TERM

samples=0
while [ "$running" = 1 ] && { [ -z "$END" ] || [ "$(date +%s)" -lt "$END" ]; }; do
  ts=$(date -u +%FT%TZ)

  # Re-descobre a cada amostra: container que reinicia ganha ID novo.
  mapfile -t IDS < <(
    docker ps --filter "label=com.docker.compose.project=$PROJECT" \
              --format '{{.ID}}'
  )

  if [ "${#IDS[@]}" -gt 0 ]; then
    # --no-stream: 1 snapshot e sai. --no-trunc: nomes completos.
    docker stats --no-stream --no-trunc --format \
      "${ts},{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.NetIO}},{{.BlockIO}},{{.PIDs}}" \
      "${IDS[@]}" >> "$OUT" 2>/dev/null || true
  else
    # Stack toda caiu? Registra um marker para o score.py saber.
    echo "${ts},__no_containers__,0%,0B / 0B,0%,0B / 0B,0B / 0B,0" >> "$OUT"
  fi

  samples=$((samples + 1))
  # `|| true`: um sinal durante o sleep nao deve abortar via set -e;
  # o trap ja sinalizou running=0 e o loop encerra na proxima checagem.
  sleep "$INTERVAL" || true
done

echo "capture-stats: ${samples} amostras gravadas em $OUT" >&2
