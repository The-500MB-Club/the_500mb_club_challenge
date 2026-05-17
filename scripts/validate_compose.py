#!/usr/bin/env python3
"""
The 500MB Club - validador de seguranca de docker-compose submetido por participante.

Implementa as regras do docs/SECURITY.md. Roda preferencialmente sobre a saida
de `docker compose config` (normalizada e com anchors resolvidos), mas tambem
funciona sobre o arquivo bruto - o parsing e defensivo para ambos os formatos.

Severidades:
  FAIL  -> bloqueia o merge. Violacao da tabela de rejeicao automatica.
  WARN  -> nao bloqueia, mas exige revisao humana antes de rodar.

Saida:
  - stdout: relatorio legivel
  - --md PATH: relatorio em Markdown (para o comentario do PR / step summary)
  - exit code: 0 se nenhum FAIL, 1 se houver ao menos um FAIL, 2 erro de uso

Uso:
  validate_compose.py --compose expanded.yml [--raw docker-compose.yml] [--md report.md]

Limites do desafio (teto agregado da stack):
  CPU total <= 2.0
  Memoria total <= 500 MiB
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERRO: PyYAML nao instalado (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

CPU_CAP = 2.0
MEM_CAP_BYTES = 500 * 1024 * 1024          # 500 MiB

# Capabilities que o nginx oficial precisa no boot (e so o LB pode ter).
# Tudo fora disso, mesmo no LB, e bloqueado.
LB_CAP_ALLOWLIST = {"CHOWN", "SETUID", "SETGID", "NET_BIND_SERVICE", "DAC_OVERRIDE"}

SHELL_TOKENS = ("sh", "bash", "ash", "/bin/sh", "/bin/bash")
DOWNLOAD_TOKENS = ("wget", "curl", "ftp", "tftp", "nc", "ncat", "eval")


@dataclass
class Finding:
    severity: str          # "FAIL" | "WARN"
    service: str           # nome do servico, ou "(global)"
    rule: str              # id curto da regra
    detail: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def fail(self, service: str, rule: str, detail: str) -> None:
        self.findings.append(Finding("FAIL", service, rule, detail))

    def warn(self, service: str, rule: str, detail: str) -> None:
        self.findings.append(Finding("WARN", service, rule, detail))

    @property
    def has_fail(self) -> bool:
        return any(f.severity == "FAIL" for f in self.findings)

    def fails(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "FAIL"]

    def warns(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "WARN"]


# ----------------------------- parsing helpers -----------------------------

def parse_mem_to_bytes(value) -> int | None:
    """Aceita int (bytes, formato do `compose config`) ou string com sufixo."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgtb]?b?)?", s)
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "").rstrip("b")
    mult = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}.get(unit)
    if mult is None:
        return None
    return int(num * mult)


def parse_cpus(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        # formato millicpu "500m" (raro em cpus:, comum em deploy.resources)
        s = str(value).strip().lower()
        if s.endswith("m"):
            try:
                return float(s[:-1]) / 1000.0
            except ValueError:
                return None
    return None


def get_limits(svc: dict) -> tuple[float | None, int | None]:
    """Retorna (cpus, mem_bytes) considerando top-level e deploy.resources."""
    cpus = parse_cpus(svc.get("cpus"))
    mem = parse_mem_to_bytes(svc.get("mem_limit"))

    deploy = svc.get("deploy") or {}
    res = (deploy.get("resources") or {}).get("limits") or {}
    if cpus is None and res.get("cpus") is not None:
        cpus = parse_cpus(res.get("cpus"))
    if mem is None and res.get("memory") is not None:
        mem = parse_mem_to_bytes(res.get("memory"))

    return cpus, mem


def normalize_volumes(svc: dict) -> list[dict]:
    """Normaliza volumes para [{type, source, target, read_only}] cobrindo
    short string ('a:b:ro') e long syntax (dict)."""
    out = []
    for v in svc.get("volumes") or []:
        if isinstance(v, str):
            parts = v.split(":")
            if len(parts) == 1:
                # volume anonimo, sem source de host
                out.append({"type": "volume", "source": None,
                            "target": parts[0], "read_only": False})
            else:
                source, target = parts[0], parts[1]
                ro = len(parts) >= 3 and "ro" in parts[2].split(",")
                is_bind = source.startswith(("/", ".", "~")) or source == ""
                out.append({
                    "type": "bind" if is_bind else "volume",
                    "source": source, "target": target, "read_only": ro,
                })
        elif isinstance(v, dict):
            out.append({
                "type": v.get("type", "volume"),
                "source": v.get("source"),
                "target": v.get("target"),
                "read_only": bool(v.get("read_only", False)),
            })
    return out


def classify_role(name: str, svc: dict) -> str:
    """api (codigo nao-confiavel, hardening estrito) | lb | redis | unknown."""
    image = str(svc.get("image", "")).lower()
    if "redis" in image:
        return "redis"
    if any(p in image for p in ("nginx", "haproxy", "caddy", "traefik", "envoy")):
        if svc.get("ports"):
            return "lb"
        return "lb"
    if svc.get("build") or image:
        return "api"
    return "unknown"


def flatten_cmd(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(x) for x in value)
    return str(value)


# ----------------------------- the rules -----------------------------

def check_service(name: str, svc: dict, role: str, rep: Report) -> None:
    svc = svc or {}

    # --- tabela de rejeicao automatica (vale para QUALQUER servico) ---

    if svc.get("privileged") is True:
        rep.fail(name, "privileged", "privileged: true desliga o isolamento")

    # cap_add: PROIBIDO em servico de API (codigo nao-confiavel).
    # No LB, o nginx oficial exige um conjunto minimo conhecido para o boot
    # (chown/setuid/setgid dos workers + bind). Allowlist estrita so para o LB.
    cap_add = [str(c).upper() for c in (svc.get("cap_add") or [])]
    if cap_add:
        if role == "lb":
            extra = set(cap_add) - LB_CAP_ALLOWLIST
            if extra:
                rep.fail(name, "cap_add-lb-extra",
                         f"LB so pode ter {sorted(LB_CAP_ALLOWLIST)}; "
                         f"extras nao permitidos: {sorted(extra)}")
        else:
            rep.fail(name, "cap_add",
                     f"cap_add proibido em servico de API: {cap_add}")

    sec = [str(x) for x in (svc.get("security_opt") or [])]
    if any("unconfined" in x for x in sec):
        rep.fail(name, "security_opt-unconfined",
                 "seccomp/apparmor unconfined remove o filtro de syscalls")

    for key in ("pid", "ipc", "userns_mode", "network_mode", "cgroup"):
        val = str(svc.get(key, "")).lower()
        if val == "host" or val.startswith("host"):
            rep.fail(name, f"{key}-host", f"{key}: host fura o isolamento")

    if svc.get("cgroup_parent"):
        rep.fail(name, "cgroup_parent",
                 "cgroup_parent permite manipular cgroup do host")

    if svc.get("cgroupns_mode") == "host":
        rep.fail(name, "cgroupns-host", "cgroupns_mode: host")

    if svc.get("devices"):
        rep.fail(name, "devices",
                 f"devices expoe hardware do host: {svc.get('devices')}")

    # network: host na forma de lista de redes nomeada 'host'
    nets = svc.get("networks")
    if isinstance(nets, dict) and "host" in nets:
        rep.fail(name, "network-host", "rede 'host' referenciada")
    if isinstance(nets, list) and "host" in nets:
        rep.fail(name, "network-host", "rede 'host' referenciada")

    # entrypoint/command com shell + download
    combined = (flatten_cmd(svc.get("entrypoint")) + " " +
                flatten_cmd(svc.get("command"))).lower()
    if combined.strip():
        has_shell = any(t in combined.split() or t in combined for t in SHELL_TOKENS)
        has_dl = any(t in combined for t in DOWNLOAD_TOKENS)
        if has_shell and has_dl:
            rep.fail(name, "shell-download",
                     f"entrypoint/command invoca shell + download: '{combined.strip()[:120]}'")
        elif has_dl:
            rep.warn(name, "download-token",
                     f"entrypoint/command contem token de rede: '{combined.strip()[:120]}'")

    if svc.get("extra_hosts"):
        rep.warn(name, "extra_hosts",
                 f"extra_hosts requer revisao humana: {svc.get('extra_hosts')}")

    # --- volumes / bind mounts ---
    for vol in normalize_volumes(svc):
        src = vol.get("source") or ""
        tgt = vol.get("target") or ""
        if "docker.sock" in src or "docker.sock" in tgt:
            rep.fail(name, "docker-sock",
                     "monta o socket do Docker = escape total para o host")
            continue
        if vol["type"] != "bind":
            continue  # named/anon volume nao toca host FS arbitrario
        # bind mount: bloqueia paths sensiveis/traversal e exige read-only
        if "/" == src.strip() or src.strip().startswith(("/etc", "/var", "/root",
                                                         "/home", "/proc", "/sys",
                                                         "/dev", "/boot", "/usr",
                                                         "/bin", "/lib")):
            rep.fail(name, "bind-host-path",
                     f"bind mount de path sensivel do host: '{src}' -> '{tgt}'")
        elif ".." in src:
            rep.fail(name, "bind-traversal",
                     f"bind mount com path traversal: '{src}'")
        elif not vol["read_only"]:
            rep.fail(name, "bind-not-ro",
                     f"bind mount nao read-only: '{src}'")

    # --- hardening obrigatorio (somente servicos 'api', codigo nao-confiavel) ---
    if role == "api":
        if svc.get("read_only") is not True:
            rep.fail(name, "no-read-only",
                     "servico de API deve ter read_only: true (rootfs imutavel)")

        cap_drop = [str(x).upper() for x in (svc.get("cap_drop") or [])]
        if "ALL" not in cap_drop:
            rep.fail(name, "no-cap-drop-all",
                     f"servico de API deve ter cap_drop: [ALL]; veio {cap_drop or '[]'}")

        if not any("no-new-privileges:true" in x for x in sec):
            rep.fail(name, "no-nnp",
                     "servico de API deve ter security_opt: [no-new-privileges:true]")

        user = str(svc.get("user", "")).strip()
        if user in ("", "0", "0:0", "root", "root:root"):
            rep.fail(name, "runs-as-root",
                     f"servico de API nao pode rodar como root (user='{user or 'unset'}')")

    cpus, mem = get_limits(svc)

    # memswap deve == mem (sem swap mascarando leak); divergencia = WARN
    msw = parse_mem_to_bytes(svc.get("memswap_limit"))
    if msw is not None and mem is not None and msw != mem:
        rep.warn(name, "memswap-mismatch",
                 f"memswap_limit ({msw}) != mem_limit ({mem}); swap pode mascarar leak")

    if mem is None:
        rep.fail(name, "no-mem-limit",
                 "mem_limit ausente: servico sem teto de memoria")
    if cpus is None:
        rep.warn(name, "no-cpu-limit",
                 "cpus ausente: nao da pra somar no orcamento agregado")


def check_aggregate(services: dict, rep: Report) -> None:
    total_cpu = 0.0
    total_mem = 0
    cpu_known = True
    for name, svc in services.items():
        cpus, mem = get_limits(svc or {})
        if cpus is None:
            cpu_known = False
        else:
            total_cpu += cpus
        if mem is not None:
            total_mem += mem

    if cpu_known and total_cpu > CPU_CAP + 1e-9:
        rep.fail("(global)", "cpu-budget",
                 f"CPU agregada {total_cpu:.2f} > teto {CPU_CAP}")
    if total_mem > MEM_CAP_BYTES:
        rep.fail("(global)", "mem-budget",
                 f"memoria agregada {total_mem/1024/1024:.0f} MiB > teto "
                 f"{MEM_CAP_BYTES//1024//1024} MiB")

    # composicao minima exigida pelo desafio
    roles = [classify_role(n, s or {}) for n, s in services.items()]
    if roles.count("api") < 3:
        rep.fail("(global)", "min-api-replicas",
                 f"desafio exige >=3 instancias de API; encontradas {roles.count('api')}")
    if roles.count("lb") < 1:
        rep.fail("(global)", "no-lb", "nenhum load balancer identificado")
    if roles.count("redis") < 1:
        rep.warn("(global)", "no-redis",
                 "nenhum servico Redis identificado (esperado pelo contrato)")

    return total_cpu if cpu_known else None, total_mem


# ----------------------------- reporting -----------------------------

# Catalogo canonico de validacoes. Cada item vira um checkbox no PR.
# (check_id, titulo, {rules que pertencem a este check})
# A ordem aqui e a ordem de exibicao.
CHECKS: list[tuple[str, str, set[str]]] = [
    ("no_privileged", "Nenhum container privilegiado",
     {"privileged"}),
    ("no_cap_add", "Sem capabilities indevidas (cap_add)",
     {"cap_add", "cap_add-lb-extra"}),
    ("no_unconfined", "Sem seccomp/apparmor unconfined",
     {"security_opt-unconfined"}),
    ("no_host_ns", "Sem namespaces do host (pid/ipc/network/userns/cgroup)",
     {"pid-host", "ipc-host", "userns_mode-host", "network_mode-host",
      "cgroup-host", "network-host", "cgroupns-host"}),
    ("no_cgroup_parent", "Sem manipulação de cgroup do host",
     {"cgroup_parent"}),
    ("no_devices", "Sem exposição de devices do host",
     {"devices"}),
    ("no_docker_sock", "Docker socket não montado",
     {"docker-sock", "docker-sock-raw"}),
    ("bind_allowlist", "Bind mounts seguros (sem path sensível, read-only)",
     {"bind-host-path", "bind-not-ro"}),
    ("no_traversal", "Sem path traversal em mounts",
     {"bind-traversal"}),
    ("no_shell_dl", "Entrypoint/command sem shell + download",
     {"shell-download"}),
    ("hardening_ro", "APIs com read_only rootfs",
     {"no-read-only"}),
    ("hardening_capdrop", "APIs com cap_drop: [ALL]",
     {"no-cap-drop-all"}),
    ("hardening_nnp", "APIs com no-new-privileges",
     {"no-nnp"}),
    ("hardening_nonroot", "APIs rodando como non-root",
     {"runs-as-root"}),
    ("mem_limit", "mem_limit definido em todos os serviços",
     {"no-mem-limit"}),
    ("cpu_budget", f"CPU agregada ≤ {CPU_CAP}",
     {"cpu-budget"}),
    ("mem_budget", f"Memória agregada ≤ {MEM_CAP_BYTES//1024//1024} MiB",
     {"mem-budget"}),
    ("min_topology", "Composição mínima (≥3 APIs, 1 LB, 1 Redis)",
     {"min-api-replicas", "no-lb", "no-redis"}),
    ("compose_parse", "docker-compose parseável e bem-formado",
     {"parse"}),
]

# Regras que sao apenas WARN (nao bloqueiam, mas aparecem como ressalva).
WARN_ONLY_RULES = {
    "download-token", "extra_hosts", "memswap-mismatch", "no-cpu-limit",
}


def compute_checks(rep: Report) -> list[dict]:
    """Para cada check canonico decide: pass / fail / warn, e coleta detalhes."""
    by_rule: dict[str, list[Finding]] = {}
    for f in rep.findings:
        by_rule.setdefault(f.rule, []).append(f)

    results = []
    for check_id, title, rules in CHECKS:
        related = [f for r in rules for f in by_rule.get(r, [])]
        has_fail = any(f.severity == "FAIL" for f in related)
        has_warn = any(f.severity == "WARN" for f in related)
        results.append({
            "id": check_id,
            "title": title,
            "status": "fail" if has_fail else ("warn" if has_warn else "pass"),
            "findings": related,
        })
    return results


def render_md(rep: Report, agg) -> str:
    total_cpu, total_mem = agg
    checks = compute_checks(rep)
    n_fail = sum(1 for c in checks if c["status"] == "fail")
    n_warn = sum(1 for c in checks if c["status"] == "warn")
    n_pass = sum(1 for c in checks if c["status"] == "pass")

    if n_fail:
        status = "❌ REPROVADO"
    elif n_warn:
        status = "⚠️ APROVADO COM RESSALVAS"
    else:
        status = "✅ APROVADO"

    cpu_str = ("%.2f" % total_cpu) if total_cpu is not None else "?"
    lines = [
        "## The 500MB Club — validação de segurança do compose",
        "",
        f"**Resultado: {status}** &nbsp;·&nbsp; "
        f"{n_pass}/{len(checks)} validações OK &nbsp;·&nbsp; "
        f"{n_fail} bloqueio(s) &nbsp;·&nbsp; {n_warn} ressalva(s)",
        "",
        f"CPU agregada `{cpu_str} / {CPU_CAP}` &nbsp;·&nbsp; "
        f"Memória `{total_mem/1024/1024:.0f} / {MEM_CAP_BYTES//1024//1024} MiB`",
        "",
        "### Checklist",
        "",
    ]

    for c in checks:
        if c["status"] == "pass":
            box, suffix = "[x]", ""
        elif c["status"] == "warn":
            box, suffix = "[x]", " — ⚠️ ressalva"
        else:
            box, suffix = "[ ]", " — ❌"
        lines.append(f"- {box} {c['title']}{suffix}")
        # Sob o item que falhou, lista quais serviços e por quê.
        if c["status"] != "pass":
            for f in c["findings"]:
                tag = "❌" if f.severity == "FAIL" else "⚠️"
                lines.append(f"  - {tag} `{f.service}`: {f.detail}")

    lines.append("")

    # Ressalvas que nao pertencem a nenhum check canonico (so informativas).
    extra_warns = [f for f in rep.warns()
                   if f.rule in WARN_ONLY_RULES]
    if extra_warns:
        lines += ["### ⚠️ Ressalvas adicionais (revisão humana)", ""]
        for f in extra_warns:
            lines.append(f"- ⚠️ `{f.service}` ({f.rule}): {f.detail}")
        lines.append("")

    lines += [
        "---",
        "_Gerado automaticamente pelo PR Security Gate. "
        "Regras completas em [`SECURITY.md`](SECURITY.md)._",
    ]
    return "\n".join(lines)


def render_text(rep: Report, agg) -> str:
    total_cpu, total_mem = agg
    checks = compute_checks(rep)
    out = []
    for c in checks:
        mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[c["status"]]
        out.append(f"[{mark}] {c['title']}")
        if c["status"] != "pass":
            for f in c["findings"]:
                out.append(f"       {f.severity} {f.service}: {f.detail}")
    out.append("")
    out.append(f"CPU agregada: "
               f"{'%.2f' % total_cpu if total_cpu is not None else '?'}/{CPU_CAP}  "
               f"Mem: {total_mem/1024/1024:.0f}/{MEM_CAP_BYTES//1024//1024} MiB")
    n_fail = sum(1 for c in checks if c["status"] == "fail")
    n_warn = sum(1 for c in checks if c["status"] == "warn")
    out.append(f"{n_fail} check(s) com FAIL, {n_warn} com WARN")
    return "\n".join(out)


def load_compose(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "services" not in data:
        raise ValueError(f"{path}: nao parece um docker-compose valido")
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compose", required=True,
                    help="compose a validar (idealmente saida de `compose config`)")
    ap.add_argument("--raw", help="arquivo bruto, para checagens textuais extras")
    ap.add_argument("--md", help="caminho para escrever o relatorio Markdown")
    args = ap.parse_args()

    try:
        data = load_compose(args.compose)
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"FAIL [(global)] parse: {e}")
        if args.md:
            Path(args.md).write_text(
                f"## The 500MB Club — validação de segurança\n\n"
                f"**❌ REPROVADO** — não foi possível parsear o compose:\n\n`{e}`\n",
                encoding="utf-8")
        return 1

    services = data.get("services") or {}
    rep = Report()

    for name, svc in services.items():
        role = classify_role(name, svc or {})
        check_service(name, svc or {}, role, rep)

    agg = check_aggregate(services, rep)

    # checagem textual extra no arquivo bruto: docker.sock pode estar
    # escondido em formas que o parser normaliza.
    if args.raw:
        try:
            raw_text = Path(args.raw).read_text(encoding="utf-8")
            if "docker.sock" in raw_text and not any(
                    f.rule == "docker-sock" for f in rep.findings):
                rep.fail("(global)", "docker-sock-raw",
                         "string 'docker.sock' presente no arquivo bruto")
        except OSError:
            pass

    print(render_text(rep, agg))
    if args.md:
        Path(args.md).write_text(render_md(rep, agg), encoding="utf-8")

    return 1 if rep.has_fail else 0


if __name__ == "__main__":
    sys.exit(main())
