#!/usr/bin/env bash
# Pi-Bench - auditoria da imagem Docker da submissao.
# NAO executa a imagem. Apenas: manifest inspect (sem pull), pull, inspect,
# history. Tudo read-only do ponto de vista de execucao.
#
# Saida em formato checklist (- [x] / - [ ]) para compor a mensagem unica do PR.
#
# Uso: ./audit_image.sh <IMAGE_REF> [MD_OUT]
# Exit: 0 ok / 1 violacao bloqueante / 2 erro de uso

set -uo pipefail

IMAGE="${1:?uso: audit_image.sh <IMAGE_REF> [MD_OUT]}"
MD_OUT="${2:-/dev/null}"

fails=0
warns=0
md=""

# check <status: pass|warn|fail> <titulo> [detalhe]
check() {
  local st="$1" title="$2" detail="${3:-}"
  case "$st" in
    pass) md+="- [x] ${title}"$'\n'; echo "PASS ${title}" ;;
    warn) md+="- [x] ${title} — ⚠️ ressalva"$'\n'
          [ -n "$detail" ] && md+="  - ⚠️ ${detail}"$'\n'
          echo "WARN ${title}"; warns=$((warns+1)) ;;
    fail) md+="- [ ] ${title} — ❌"$'\n'
          [ -n "$detail" ] && md+="  - ❌ ${detail}"$'\n'
          echo "FAIL ${title}"; fails=$((fails+1)) ;;
  esac
}

md+="### Auditoria da imagem"$'\n'$'\n'
md+="Imagem: \`${IMAGE}\`"$'\n'$'\n'

# 1. Arquitetura: precisa ter arm64 nativo (regra do desafio).
arch_json="$(docker manifest inspect "$IMAGE" 2>/dev/null || true)"
if [ -z "$arch_json" ]; then
  check warn "Manifesto legível" "manifest inspect falhou (imagem privada/inexistente?)"
elif echo "$arch_json" | grep -q '"architecture": *"arm64"'; then
  check pass "Imagem contém arm64 nativo"
else
  check fail "Imagem contém arm64 nativo" "manifesto não declara arm64"
fi

# A partir daqui precisamos da imagem localmente. pull NAO executa nada.
if ! docker pull --quiet "$IMAGE" >/dev/null 2>&1; then
  check fail "Imagem é pública e baixável" "docker pull falhou"
  printf '%s' "$md" > "$MD_OUT"
  echo; echo "${fails} FAIL, ${warns} WARN"
  [ "$fails" -gt 0 ] && exit 1 || exit 0
fi
check pass "Imagem é pública e baixável"

# 2. Usuario non-root configurado na imagem.
img_user="$(docker inspect --format '{{.Config.User}}' "$IMAGE" 2>/dev/null)"
if [ -z "$img_user" ] || [ "$img_user" = "0" ] || [ "$img_user" = "root" ] \
   || [ "$img_user" = "0:0" ]; then
  check warn "Imagem desenhada para non-root" \
        "imagem declara usuário '${img_user:-<vazio>}'; o compose força non-root mas é sinal amarelo"
else
  check pass "Imagem desenhada para non-root (${img_user})"
fi

# 3. ENTRYPOINT/CMD sem shell+download.
ep="$(docker inspect --format '{{.Config.Entrypoint}} {{.Config.Cmd}}' "$IMAGE" 2>/dev/null | tr 'A-Z' 'a-z')"
if echo "$ep" | grep -Eq '(sh|bash) .*-c' && \
   echo "$ep" | grep -Eq 'wget|curl|eval|nc |ncat'; then
  check fail "ENTRYPOINT/CMD sem shell+download" "padrão detectado: ${ep}"
else
  check pass "ENTRYPOINT/CMD sem shell+download"
fi

# 4. Historico de build sem download de rede nas camadas.
hist="$(docker history --no-trunc --format '{{.CreatedBy}}' "$IMAGE" 2>/dev/null | tr 'A-Z' 'a-z')"
if echo "$hist" | grep -Eq '(wget|curl) +https?://'; then
  hit="$(echo "$hist" | grep -E '(wget|curl) +https?://' | head -1 | cut -c1-90)"
  check warn "Build sem download de rede nas camadas" \
        "camada com download: ${hit}… (revisar contra o repo)"
else
  check pass "Build sem download de rede nas camadas"
fi

# 5. Tamanho - imagem gigante e vetor de payload escondido.
size_bytes="$(docker inspect --format '{{.Size}}' "$IMAGE" 2>/dev/null || echo 0)"
size_mb=$(( size_bytes / 1024 / 1024 ))
if [ "$size_mb" -gt 250 ]; then
  check warn "Tamanho da imagem razoável" \
        "${size_mb} MB (esperado < 100 MB para um serviço enxuto)"
else
  check pass "Tamanho da imagem razoável (${size_mb} MB)"
fi

printf '%s' "$md" > "$MD_OUT"
echo
echo "${fails} FAIL, ${warns} WARN"
[ "$fails" -gt 0 ] && exit 1 || exit 0
