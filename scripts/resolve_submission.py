#!/usr/bin/env python3
"""
Pi-Bench - resolve e valida a submissao apontada por um PR.

O PR NAO contem o codigo. Ele adiciona exatamente um arquivo
`submissions/<username>.json` que aponta para um repositorio externo.

Este script faz as validacoes 1 e 2 (puramente sobre o conteudo do PR e
do JSON, SEM rede). A validacao 3 (branch `implementation` existe) e o
clone ficam no workflow, porque envolvem git/rede.

Validacao 1: o PR altera EXATAMENTE um arquivo, e ele casa
             `submissions/<username>.json` (username = regra do GitHub).
Validacao 2: o `<username>` (nome do arquivo) e o dono do repositorio
             em `repo_url` (comparacao case-insensitive).

Tambem valida estritamente o formato de `repo_url` ANTES de qualquer
clone - isso barra SSRF (file://, IP interno, host != github.com).

Saida:
  --md PATH     : fragmento de checklist em Markdown
  --meta-out P  : arquivo KEY=VALUE com USERNAME / REPO_URL / IMAGE / CLONE_OK
  exit 0 se validacoes 1 e 2 OK; 1 caso contrario; 2 erro de uso

Uso:
  resolve_submission.py --changed-files lista.txt --pr-dir pr/ \
      --md frag.md --meta-out meta.env
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
    ap.add_argument("--md", required=True)
    ap.add_argument("--meta-out", required=True)
    args = ap.parse_args()

    res = Result()
    res.lines.append("### Submissão")
    res.lines.append("")

    meta = {"USERNAME": "", "REPO_URL": "", "IMAGE": "", "RESOLVE_OK": "0"}

    # --- Validacao 1: exatamente um arquivo, submissions/<username>.json ---
    try:
        raw = Path(args.changed_files).read_text(encoding="utf-8")
    except OSError as e:
        res.check(False, "Lista de arquivos alterados legível", str(e))
        _finish(res, meta, args)
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
        _finish(res, meta, args)
        return 1
    meta["USERNAME"] = username

    # --- Le o JSON da submissao ---
    sub_path = Path(args.pr_dir) / SUBMISSIONS_DIR / f"{username}.json"
    try:
        data = json.loads(sub_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        res.check(False, "JSON da submissão é válido", str(e))
        _finish(res, meta, args)
        return 1
    res.check(True, "JSON da submissão é válido")

    repo_url = data.get("repo_url")
    if not isinstance(repo_url, str) or not repo_url.strip():
        res.check(False, "Campo `repo_url` presente",
                  "ausente ou não é string")
        _finish(res, meta, args)
        return 1
    res.check(True, "Campo `repo_url` presente")

    # imagem e opcional; se vier, e usada na auditoria
    image = data.get("image")
    if isinstance(image, str) and image.strip():
        meta["IMAGE"] = image.strip()

    # --- Formato estrito do repo_url (barra SSRF antes de clonar) ---
    owner, repo, canonical = normalize_repo(repo_url)
    if owner is None:
        res.check(False,
                  "`repo_url` é um repositório github.com válido",
                  f"formato recusado: '{repo_url}' "
                  f"(só https://github.com/owner/repo ou git@github.com:owner/repo)")
        _finish(res, meta, args)
        return 1
    res.check(True, "`repo_url` é um repositório github.com válido")
    meta["REPO_URL"] = canonical

    # --- Validacao 2: username (arquivo) == dono do repo ---
    if owner.lower() != username.lower():
        res.check(False,
                  "`<username>` corresponde ao dono do repositório",
                  f"arquivo é de '{username}' mas o repo pertence a '{owner}'")
        _finish(res, meta, args)
        return 1
    res.check(True, "`<username>` corresponde ao dono do repositório")

    meta["RESOLVE_OK"] = "1"
    _finish(res, meta, args)
    return 0


def _finish(res: Result, meta: dict, args) -> None:
    Path(args.md).write_text("\n".join(res.lines) + "\n", encoding="utf-8")
    Path(args.meta_out).write_text(
        "".join(f"{k}={v}\n" for k, v in meta.items()), encoding="utf-8"
    )
    print("\n".join(res.lines))


if __name__ == "__main__":
    sys.exit(main())
