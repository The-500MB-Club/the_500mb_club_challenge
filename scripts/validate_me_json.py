#!/usr/bin/env python3
"""
The 500MB Club - validador do `me.json` da branch `implementation`.

Roda depois do clone, junto com `validate_compose.py`. Verifica que o
participante incluiu na raiz da branch `implementation` um `me.json`
parseável e com o schema mínimo exigido pelo desafio:

  {
    "collaborators": [
      {"name": "...", "social_links": ["...", "..."]}
    ],
    "stack": ["go", "redis", "nginx"]
  }

Saída:
  --md PATH     fragmento de checklist em Markdown
  exit 0 se tudo OK; 1 se houver FAIL; 2 erro de uso

Uso:
  validate_me_json.py --me-json /tmp/impl/me.json --md /tmp/me.md
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Finding:
    severity: str          # "FAIL"
    detail: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def fail(self, detail: str) -> None:
        self.findings.append(Finding("FAIL", detail))

    @property
    def has_fail(self) -> bool:
        return any(f.severity == "FAIL" for f in self.findings)


CHECKS = [
    ("present", "`me.json` presente na raiz da branch `implementation`"),
    ("valid_json", "`me.json` é JSON válido"),
    ("collaborators", "Campo `collaborators` é um array não-vazio de objetos"),
    ("collaborator_fields",
     "Cada colaborador tem `name` (string) e `social_links` (array de strings)"),
    ("stack", "Campo `stack` é um array não-vazio de strings"),
]


def _is_nonempty_str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _is_list_of_nonempty_str(v) -> bool:
    return (
        isinstance(v, list)
        and len(v) > 0
        and all(_is_nonempty_str(x) for x in v)
    )


def validate(path: Path) -> tuple[dict[str, str], Report]:
    """Roda os checks. Retorna status por check_id e o Report com detalhes."""
    rep = Report()
    status: dict[str, str] = {cid: "skip" for cid, _ in CHECKS}

    if not path.is_file():
        status["present"] = "fail"
        rep.fail(f"arquivo `me.json` não encontrado em '{path.name}' "
                 "na raiz da branch")
        return status, rep
    status["present"] = "pass"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        status["valid_json"] = "fail"
        rep.fail(f"falha ao parsear `me.json`: {e}")
        return status, rep
    status["valid_json"] = "pass"

    if not isinstance(data, dict):
        status["collaborators"] = "fail"
        status["stack"] = "fail"
        rep.fail(f"`me.json` deve ser um objeto, veio {type(data).__name__}")
        return status, rep

    collaborators = data.get("collaborators")
    if not isinstance(collaborators, list) or len(collaborators) == 0:
        status["collaborators"] = "fail"
        rep.fail("`collaborators` deve ser um array não-vazio")
    else:
        status["collaborators"] = "pass"
        bad = []
        for i, c in enumerate(collaborators):
            if not isinstance(c, dict):
                bad.append(f"#{i}: não é objeto")
                continue
            if not _is_nonempty_str(c.get("name")):
                bad.append(f"#{i}: `name` ausente ou vazio")
            if not _is_list_of_nonempty_str(c.get("social_links")):
                bad.append(f"#{i}: `social_links` deve ser array não-vazio de strings")
        if bad:
            status["collaborator_fields"] = "fail"
            rep.fail("colaboradores inválidos: " + "; ".join(bad))
        else:
            status["collaborator_fields"] = "pass"

    stack = data.get("stack")
    if not _is_list_of_nonempty_str(stack):
        status["stack"] = "fail"
        rep.fail("`stack` deve ser um array não-vazio de strings")
    else:
        status["stack"] = "pass"

    return status, rep


def render_md(status: dict[str, str], rep: Report) -> str:
    lines = ["### Identidade da submissão (`me.json`)", ""]
    for cid, title in CHECKS:
        st = status.get(cid, "skip")
        if st == "pass":
            lines.append(f"- [x] {title}")
        elif st == "fail":
            lines.append(f"- [ ] {title} — ❌")
        else:
            lines.append(f"- [ ] {title} — _não avaliado_")
    if rep.findings:
        lines.append("")
        for f in rep.findings:
            lines.append(f"  - ❌ {f.detail}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--me-json", required=True,
                    help="caminho para o me.json no clone (ex: /tmp/impl/me.json)")
    ap.add_argument("--md", required=True,
                    help="caminho para escrever o fragmento Markdown")
    args = ap.parse_args()

    status, rep = validate(Path(args.me_json))
    Path(args.md).write_text(render_md(status, rep), encoding="utf-8")
    return 1 if rep.has_fail else 0


if __name__ == "__main__":
    sys.exit(main())
