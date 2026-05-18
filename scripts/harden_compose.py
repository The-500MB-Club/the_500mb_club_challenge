#!/usr/bin/env python3
"""
The 500MB Club - injeta hardening padrao no docker-compose do participante.

Roda APOS `docker compose config` (que ja resolveu anchors, defaults, etc.)
e ANTES de `validate_compose.py`. Para cada servico classificado, adiciona
os flags de hardening *somente se ausentes*. Valores explicitos do
participante sao preservados - se ele escreveu `read_only: false`, o
validador (que roda em seguida) reprova.

Politica de injecao:
  - api:    read_only=true, cap_drop=[ALL], security_opt+= no-new-privileges,
            tmpfs+= /tmp
  - lb:     read_only=true, cap_drop=[ALL], cap_add=allowlist (so se nenhum
            cap_add foi declarado), security_opt+= no-new-privileges,
            tmpfs+= /var/cache/nginx,/var/run,/tmp
  - redis:  read_only=true, cap_drop=[ALL], security_opt+= no-new-privileges
  - db:     cap_drop=[ALL], security_opt+= no-new-privileges
            (postgres/mariadb/mysql: NAO forcamos read_only porque o engine
            precisa escrever em /var/lib/<engine>/data, /var/run/<engine> e
            /tmp; participante e responsavel por configurar volume e tmpfs.)

Storage suportado (allowlist): redis, postgres, mariadb, mysql. Qualquer
outro banco (mongo, cassandra, elastic, etc.) nao cabe no orcamento de
500 MiB nem foi modelado pelo desafio - cai no perfil `api`, com hardening
estrito, e tende a falhar a validacao.

Nao tocamos em: mem_limit, cpus, user, image, command, networks, volumes
(decisoes do participante - validate_compose.py audita).

Uso:
  harden_compose.py --in resolved.yml --out expanded.yml
                    [--md report.md] [--injected-out injected.json] [--quiet]

Saida:
  expanded.yml com o compose endurecido (pronto pra validar/rodar)
  report.md (opcional) lista o que foi injetado por servico.
  injected.json (opcional) lista os campos injetados em formato estavel;
    o validate_compose.py consome esse JSON pra emitir WARNs nos checks
    de hardening correspondentes, alertando o participante de que o
    compose dele dependia da injecao do gate.
  exit 0 sempre que o YAML foi parseavel; 2 erro de uso/parse.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERRO: PyYAML nao instalado (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

LB_CAP_ALLOWLIST = ["CHOWN", "SETUID", "SETGID", "NET_BIND_SERVICE", "DAC_OVERRIDE"]
NNP = "no-new-privileges:true"

# Bancos SQL aceitos como storage. Casamos pelo prefixo da tag da imagem
# (ex.: `postgres:16-alpine`, `mariadb:11`, `mysql:8.4`) e tambem por
# variantes oficiais comuns (`bitnami/postgresql`, `mysql/mysql-server`).
DB_IMAGE_TOKENS = ("postgres", "postgresql", "mariadb", "mysql")


def classify_role(svc: dict) -> str:
    image = str(svc.get("image", "")).lower()
    if "redis" in image:
        return "redis"
    if any(p in image for p in DB_IMAGE_TOKENS):
        return "db"
    if any(p in image for p in ("nginx", "haproxy", "caddy", "traefik", "envoy")):
        return "lb"
    if svc.get("build") or image:
        return "api"
    return "unknown"


def ensure_list(svc: dict, key: str) -> list:
    val = svc.get(key)
    if val is None:
        svc[key] = []
    elif not isinstance(val, list):
        svc[key] = [val]
    return svc[key]


def has_nnp(security_opt: list) -> bool:
    return any("no-new-privileges" in str(x) and "true" in str(x).lower()
               for x in security_opt)


def inject(svc: dict, role: str) -> list[tuple[str, str]]:
    """Injeta hardening adequado ao papel; retorna [(field_id, display)].

    `field_id` e estavel e serve para o validate_compose.py emitir WARNs
    sob os checks de hardening correspondentes. `display` e o texto
    legivel usado no relatorio Markdown e no log.
    """
    touched: list[tuple[str, str]] = []

    if role not in ("api", "lb", "redis", "db"):
        return touched

    # read_only: SQL DBs precisam escrever em /var/lib/<engine>/data, runtime
    # sockets em /var/run/<engine> e /tmp; nao injetamos read_only=true pra
    # eles. Para api/lb/redis injetamos quando ausente (se vier explicitamente
    # false, mantemos e o validate_compose.py reprova).
    if role != "db" and "read_only" not in svc:
        svc["read_only"] = True
        touched.append(("read_only", "read_only=true"))

    # cap_drop: adiciona ALL apenas se nada foi declarado. Se o
    # participante declarou cap_drop sem ALL, o validador reprova.
    if "cap_drop" not in svc:
        svc["cap_drop"] = ["ALL"]
        touched.append(("cap_drop", "cap_drop=[ALL]"))

    # security_opt: garante no-new-privileges. Lista e aditiva.
    sec = ensure_list(svc, "security_opt")
    if not has_nnp(sec):
        sec.append(NNP)
        touched.append(("no-new-privileges", f"security_opt+={NNP}"))

    if role == "api":
        tmpfs = ensure_list(svc, "tmpfs")
        if "/tmp" not in tmpfs:
            tmpfs.append("/tmp")
            touched.append(("tmpfs", "tmpfs+=/tmp"))

    elif role == "lb":
        # cap_add somente se o participante nao mexeu - nao "completamos"
        # uma lista parcial pra evitar surpresa.
        if "cap_add" not in svc:
            svc["cap_add"] = list(LB_CAP_ALLOWLIST)
            touched.append(("cap_add", f"cap_add={LB_CAP_ALLOWLIST}"))
        tmpfs = ensure_list(svc, "tmpfs")
        for path in ("/var/cache/nginx", "/var/run", "/tmp"):
            if path not in tmpfs:
                tmpfs.append(path)
                touched.append(("tmpfs", f"tmpfs+={path}"))

    return touched


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True,
                    help="compose ja resolvido (saida de `docker compose config`)")
    ap.add_argument("--out", dest="dst", required=True,
                    help="compose endurecido a escrever")
    ap.add_argument("--md", help="caminho opcional para relatorio Markdown")
    ap.add_argument("--injected-out", dest="injected_out",
                    help="caminho opcional para JSON com campos injetados "
                         "por servico (consumido pelo validate_compose.py)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        data = yaml.safe_load(Path(args.src).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        print(f"ERRO: parse de {args.src}: {e}", file=sys.stderr)
        return 2

    if not isinstance(data, dict) or "services" not in data:
        print(f"ERRO: {args.src} nao parece um docker-compose valido",
              file=sys.stderr)
        return 2

    services = data.get("services") or {}
    report: dict[str, tuple[str, list[tuple[str, str]]]] = {}

    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        role = classify_role(svc)
        touched = inject(svc, role)
        report[name] = (role, touched)

    Path(args.dst).write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    if not args.quiet:
        for name, (role, touched) in report.items():
            tag = ", ".join(d for _, d in touched) if touched else "nada a injetar"
            print(f"[{role:>7}] {name}: {tag}")

    if args.md:
        lines = [
            "### Hardening injetado automaticamente",
            "",
            "Estes flags foram adicionados pelo gate quando ausentes no compose. "
            "Valores explicitos seus foram preservados (e validados em seguida).",
            "",
        ]
        any_touched = False
        for name, (role, touched) in report.items():
            if not touched:
                continue
            any_touched = True
            display = ", ".join(d for _, d in touched)
            lines.append(f"- `{name}` ({role}): {display}")
        if not any_touched:
            lines.append("- Nada injetado (compose ja vinha com tudo).")
        lines.append("")
        Path(args.md).write_text("\n".join(lines), encoding="utf-8")

    if args.injected_out:
        payload = {
            name: {"role": role, "fields": [fid for fid, _ in touched]}
            for name, (role, touched) in report.items()
            if touched
        }
        Path(args.injected_out).write_text(
            json.dumps(payload, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
