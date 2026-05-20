#!/usr/bin/env python3
"""
Pi-Bench - resolve e valida a submissao apontada por um PR.

O PR NAO contem o codigo. Ele adiciona exatamente um arquivo
`submissions/<username>.json` que aponta para um ou mais repositorios
externos (cada um com um `id` proprio).

Este script faz as validacoes 1 e 2 (puramente sobre o conteudo do PR e
do JSON, SEM rede). A validacao 3 (branch `implementation` existe) e o
clone ficam no workflow, porque envolvem git/rede.

Validacao 1: o PR altera EXATAMENTE um arquivo, e ele casa
             `submissions/<username>.json` (username = regra do GitHub).
Validacao 2: para cada item em `submissions[]`:
             - `id` casa a regex (1-50 chars, [a-z0-9._-], sem comecar/
               terminar com separador)
             - `id` e unico dentro do arquivo
             - `repo_url` e https://github.com/<user>/<repo> ou
               git@github.com:<user>/<repo> e o `<user>` e o dono do
               arquivo (`<username>.json`)

Tambem valida estritamente o formato de `repo_url` ANTES de qualquer
clone - isso barra SSRF (file://, IP interno, host != github.com).

DIFF INCREMENTAL:
  O formato do arquivo inteiro e SEMPRE validado (schema, ids unicos,
  ownership - tudo local e instantaneo). Mas a lista emitida em
  `--submissions-out` (que o workflow clona/expande/audita - a parte
  cara e barulhenta) contem APENAS as submissoes novas ou alteradas em
  relacao ao `--base-dir`. Submissoes inalteradas (mesmo `id` + mesmo
  `repo_url` canonico do base) ja foram validadas no PR que as
  introduziu; reprocessa-las so polui o comentario. Sem `--base-dir`
  (ou base ausente/malformado), todas as submissoes sao validadas.

Saida:
  --md PATH              : fragmento de checklist em Markdown
  --meta-out P           : arquivo KEY=VALUE com USERNAME / RESOLVE_OK /
                           SUBMISSION_COUNT (so as novas/alteradas)
  --submissions-out P    : JSON com a lista de submissoes a (re)validar
                           ([{id, repo_url}, ...]); presente apenas
                           quando RESOLVE_OK=1
  exit 0 se validacoes 1 e 2 OK; 1 caso contrario; 2 erro de uso

Uso:
  resolve_submission.py --changed-files lista.txt --pr-dir pr/ \
      --base-dir base/ --md frag.md --meta-out meta.env \
      --submissions-out subs.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SUBMISSIONS_DIR = "submissions"

# Regras de username do GitHub: 1-39 chars, alfanumerico ou hifen,
# nao comeca nem termina com hifen, sem hifen duplo.
USERNAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$"
)

# Regras do `id` de submissao: 1-50 chars, minusculas/digitos/`.-_`,
# nao comeca nem termina com separador. Single-char permitido ([a-z0-9]).
SUBMISSION_ID_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]{0,48}[a-z0-9])?$"
)

# repo_url aceito: somente github.com, https ou ssh-scp. Nada de file://,
# http:// puro, IP, localhost, portas, userinfo, query, fragmento.
REPO_HTTPS_RE = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$"
)
REPO_SSH_RE = re.compile(
    r"^git@github\.com:"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?$"
)


class Result:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failed = False

    def check(self, ok: bool, title: str, detail: str = "") -> bool:
        if ok:
            self.lines.append(f"- [x] {title}")
        else:
            self.lines.append(f"- [ ] {title} — ❌")
            if detail:
                self.lines.append(f"  - ❌ {detail}")
            self.failed = True
        return ok


def normalize_repo(url: str):
    """Retorna (owner, repo, canonical_https) ou (None, None, None)."""
    url = url.strip()
    m = REPO_HTTPS_RE.match(url) or REPO_SSH_RE.match(url)
    if not m:
        return None, None, None
    owner, repo = m.group("owner"), m.group("repo")
    canonical = f"https://github.com/{owner}/{repo}.git"
    return owner, repo, canonical


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--changed-files", required=True,
                    help="arquivo texto com a lista de arquivos do diff do PR")
    ap.add_argument("--pr-dir", required=True,
                    help="diretorio com o checkout do head do PR")
    ap.add_argument("--base-dir", default=None,
                    help="diretorio com o checkout do base ref. Usado so "
                         "para o diff incremental: submissoes identicas ao "
                         "base sao puladas. Ausente => valida todas.")
    ap.add_argument("--md", required=True)
    ap.add_argument("--meta-out", required=True)
    ap.add_argument("--submissions-out", required=True,
                    help="JSON com a lista resolvida de submissoes")
    args = ap.parse_args()

    res = Result()
    res.lines.append("### Submissão")
    res.lines.append("")

    meta = {"USERNAME": "", "RESOLVE_OK": "0", "SUBMISSION_COUNT": "0"}
    resolved: list[dict] = []

    # --- Validacao 1: exatamente um arquivo, submissions/<username>.json ---
    try:
        raw = Path(args.changed_files).read_text(encoding="utf-8")
    except OSError as e:
        res.check(False, "Lista de arquivos alterados legível", str(e))
        _finish(res, meta, resolved, args)
        return 1

    changed = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    ok_count = res.check(
        len(changed) == 1,
        "PR altera exatamente um arquivo",
        f"o PR altera {len(changed)} arquivo(s): {changed}" if len(changed) != 1 else "",
    )

    username = None
    if ok_count:
        path = changed[0]
        m = re.fullmatch(
            rf"{re.escape(SUBMISSIONS_DIR)}/([^/]+)\.json", path
        )
        if not m:
            res.check(False,
                      f"Arquivo é `{SUBMISSIONS_DIR}/<username>.json`",
                      f"caminho inesperado: '{path}'")
        else:
            candidate = m.group(1)
            if not USERNAME_RE.match(candidate):
                res.check(False,
                          "Nome do arquivo é um username GitHub válido",
                          f"'{candidate}' não casa as regras de username")
            else:
                username = candidate
                res.check(True,
                          f"Arquivo é `{SUBMISSIONS_DIR}/{username}.json`")

    if username is None:
        _finish(res, meta, resolved, args)
        return 1
    meta["USERNAME"] = username

    # --- Le o JSON da submissao ---
    sub_path = Path(args.pr_dir) / SUBMISSIONS_DIR / f"{username}.json"
    try:
        data = json.loads(sub_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        res.check(False, "JSON da submissão é válido", str(e))
        _finish(res, meta, resolved, args)
        return 1
    res.check(True, "JSON da submissão é válido")

    # --- Schema: objeto com chave `submissions` (array nao-vazio) ---
    if not isinstance(data, dict):
        res.check(False, "Schema do JSON",
                  "raiz deve ser um objeto JSON com a chave `submissions`")
        _finish(res, meta, resolved, args)
        return 1

    subs = data.get("submissions")
    if not isinstance(subs, list) or not subs:
        res.check(False, "Campo `submissions` é um array não-vazio",
                  "ausente, não é array ou está vazio")
        _finish(res, meta, resolved, args)
        return 1
    res.check(True, "Campo `submissions` é um array não-vazio")

    # --- Valida cada submissao + unicidade de id ---
    seen_ids: set[str] = set()
    duplicate_ids: list[str] = []
    invalid = False

    for idx, item in enumerate(subs):
        prefix = f"submissions[{idx}]"
        if not isinstance(item, dict):
            res.check(False, f"`{prefix}` é um objeto",
                      f"esperado objeto, veio {type(item).__name__}")
            invalid = True
            continue

        sub_id = item.get("id")
        if not isinstance(sub_id, str) or not sub_id.strip():
            res.check(False, f"`{prefix}.id` presente",
                      "ausente ou não é string não-vazia")
            invalid = True
            continue
        sub_id = sub_id.strip()

        if not SUBMISSION_ID_RE.match(sub_id):
            res.check(False, f"`{prefix}.id` é válido",
                      f"'{sub_id}' não casa "
                      "`^[a-z0-9](?:[a-z0-9._-]{0,48}[a-z0-9])?$` "
                      "(1-50 chars, minúsculas/dígitos/`.-_`, "
                      "não começa/termina com separador)")
            invalid = True
            continue

        if sub_id in seen_ids:
            duplicate_ids.append(sub_id)
            res.check(False, f"`{prefix}.id` é único no arquivo",
                      f"id '{sub_id}' aparece mais de uma vez")
            invalid = True
            # continue checando o resto pra reportar tudo de uma vez

        seen_ids.add(sub_id)

        repo_url = item.get("repo_url")
        if not isinstance(repo_url, str) or not repo_url.strip():
            res.check(False, f"`{prefix}.repo_url` presente",
                      "ausente ou não é string")
            invalid = True
            continue

        owner, _repo, canonical = normalize_repo(repo_url)
        if owner is None:
            res.check(False,
                      f"`{prefix}.repo_url` é um repositório github.com válido",
                      f"formato recusado: '{repo_url}' "
                      "(só https://github.com/owner/repo "
                      "ou git@github.com:owner/repo)")
            invalid = True
            continue

        if owner.lower() != username.lower():
            res.check(False,
                      f"`{prefix}` pertence ao dono do arquivo",
                      f"arquivo é de '{username}' mas "
                      f"`{prefix}.repo_url` pertence a '{owner}'")
            invalid = True
            continue

        resolved.append({"id": sub_id, "repo_url": canonical})

    if invalid:
        # ao menos uma falhou; tudo ja foi reportado no loop
        _finish(res, meta, resolved, args)
        return 1

    # --- Diff incremental: emite so as submissoes novas ou alteradas ---
    # O arquivo inteiro ja foi validado acima (formato/unicidade/owner).
    # Aqui filtramos o que o workflow vai realmente clonar e auditar.
    base_map = _load_base_submissions(args.base_dir, username)
    to_validate: list[dict] = []
    unchanged: list[str] = []
    for s in resolved:
        if base_map.get(s["id"]) == s["repo_url"]:
            unchanged.append(s["id"])
        else:
            to_validate.append(s)

    if base_map and unchanged:
        res.lines.append(
            f"- [x] {len(unchanged)} submissão(ões) inalterada(s), "
            f"pulada(s): {', '.join(sorted(unchanged))}"
        )

    if to_validate:
        label = ("nova(s)/alterada(s) para validar"
                 if base_map else "válida(s)")
        res.check(True,
                  f"{len(to_validate)} submissão(ões) {label}: "
                  f"{', '.join(sorted(s['id'] for s in to_validate))}")
    else:
        res.check(True, "Nenhuma submissão nova ou alterada neste PR")

    resolved = to_validate
    meta["RESOLVE_OK"] = "1"
    meta["SUBMISSION_COUNT"] = str(len(resolved))
    _finish(res, meta, resolved, args)
    return 0


def _load_base_submissions(base_dir, username: str) -> dict[str, str]:
    """Mapa {id: repo_url_canonico} do `<username>.json` no base ref.

    Usado apenas para o diff incremental. Qualquer problema (sem
    --base-dir, arquivo ausente, JSON malformado, item invalido) e
    tratado como "sem base" => o PR valida tudo. NUNCA falha o PR: o
    base ja foi validado quando entrou no master.
    """
    if not base_dir:
        return {}
    path = Path(base_dir) / SUBMISSIONS_DIR / f"{username}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    subs = data.get("submissions")
    if not isinstance(subs, list):
        return {}
    out: dict[str, str] = {}
    for item in subs:
        if not isinstance(item, dict):
            continue
        sid, url = item.get("id"), item.get("repo_url")
        if not isinstance(sid, str) or not isinstance(url, str):
            continue
        _owner, _repo, canonical = normalize_repo(url)
        if canonical is not None:
            out[sid.strip()] = canonical
    return out


def _finish(res: Result, meta: dict, resolved: list[dict], args) -> None:
    Path(args.md).write_text("\n".join(res.lines) + "\n", encoding="utf-8")
    Path(args.meta_out).write_text(
        "".join(f"{k}={v}\n" for k, v in meta.items()), encoding="utf-8"
    )
    Path(args.submissions_out).write_text(
        json.dumps(resolved, indent=2) + "\n", encoding="utf-8"
    )
    print("\n".join(res.lines))


if __name__ == "__main__":
    sys.exit(main())
