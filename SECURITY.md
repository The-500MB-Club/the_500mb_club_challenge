# Modelo de Segurança

## Premissa

Cada submissão entrega **o `docker-compose.yml` completo** mais a imagem Docker pública e o repositório open source. O organizador **não** controla o compose — ele é input não-confiável, escrito pelo participante, e roda num Raspberry Pi.

Isso muda tudo em relação a um cenário onde o organizador fornece o compose: aqui o compose é uma superfície de ataque de primeira classe. Um participante mal-intencionado (ou um PR comprometido) pode tentar usar o compose para ler/apagar arquivos do host, escapar do container ou derrubar o Pi.

A defesa tem duas camadas:

1. **Gate automático no PR** (`.github/workflows/pr-security.yml`) — toda abertura ou sincronização de PR roda `scripts/validate_compose.py` + `scripts/audit_image.sh`. O resultado é postado como um comentário único com checklist, e qualquer violação bloqueante **reprova o PR e impede o merge**. Esta é a defesa primária e é obrigatória.
2. **Revisão humana** — o checklist do gate orienta o revisor, mas decisões de ressalva (`WARN`) e qualquer coisa que o validador marque para inspeção exigem olho humano antes do merge e antes de rodar no Pi.

Nenhuma das duas camadas executa o código da submissão durante a validação. O gate só faz parsing de YAML e `docker pull/inspect/history` — que não rodam a imagem.

---

## 1. Tabela de rejeição automática

O validador (`scripts/validate_compose.py`) reprova o PR se qualquer serviço do compose contiver:

| Construção | Por que é fatal |
| --- | --- |
| `volumes:` bind mount em path sensível do host (`/`, `/etc`, `/var`, `/root`, `/home`, `/proc`, `/sys`, `/dev`, `/boot`, `/usr`, `/bin`, `/lib`) | Monta filesystem crítico do host no container. `/:/host` = acesso total ao Pi. |
| `volumes:` bind mount não read-only | Bind mount gravável permite o container alterar arquivos do host. |
| `/var/run/docker.sock` (qualquer forma) | Acesso ao daemon Docker = criar container privilegiado e escapar. Game over. |
| bind mount com `..` (path traversal) | `../../etc/shadow` contorna checagem ingênua de prefixo. |
| `privileged: true` | Desliga quase todo o isolamento de uma vez. |
| `cap_add:` em serviço de API | Reintroduz capabilities (`SYS_ADMIN` permite `mount`). |
| `security_opt:` com `seccomp:unconfined` / `apparmor:unconfined` | Remove o filtro de syscalls / MAC. |
| `pid: host` | Container vê e mata processos do host. |
| `network: host` / `network_mode: host` | Remove isolamento de rede; fala direto na stack do host. |
| `userns_mode: host` | Anula o user namespace remap. |
| `ipc: host` | Memória compartilhada com o host. |
| `cgroup_parent:` / `cgroupns_mode: host` | Manipulação de cgroup do host. |
| `devices:` | Expõe dispositivos do host (`/dev/...`). |
| `read_only` ausente/`false` em serviço de API | Rootfs gravável: persistência de payload. |
| `cap_drop` sem `ALL` em serviço de API | Mantém capabilities desnecessárias. |
| `no-new-privileges` ausente em serviço de API | Permite escalada via binário setuid. |
| `user` root/vazio em serviço de API | Código não-confiável rodando como UID 0. |
| `entrypoint:`/`command:` com shell + download | `sh -c "wget … \| sh"` = baixar segundo estágio. |
| `mem_limit` ausente em qualquer serviço | Serviço sem teto de memória. |
| CPU agregada > 2.0 ou memória agregada > 500 MiB | Viola a regra fundamental do desafio. |
| Composição < 3 APIs / sem LB | Não atende o desenho mínimo do desafio. |

Bind mounts são permitidos desde que (i) não toquem paths sensíveis do host listados acima, (ii) não contenham `..` (path traversal) e (iii) sejam declarados **read-only**. O caso canônico é o `nginx.conf` do load balancer montado read-only a partir do repositório. Qualquer bind mount que falhe em qualquer uma das três condições reprova.

Capabilities têm tratamento por papel: serviços de API não podem ter **nenhuma** capability adicionada; o load balancer pode ter apenas o conjunto mínimo que o nginx exige no boot (`CHOWN`, `SETUID`, `SETGID`, `NET_BIND_SERVICE`, `DAC_OVERRIDE`). O validador classifica o papel pela imagem, não pelo nome do serviço — renomear `api-1` para `web` não burla a regra.

## 2. Ressalvas (não bloqueiam, exigem revisão humana)

Marcadas como `WARN` no checklist. O PR não é reprovado automaticamente, mas o revisor decide antes do merge:

- `extra_hosts:` definido — pode mascarar acesso a serviço interno; verificar o alvo.
- `memswap_limit` diferente de `mem_limit` — swap pode mascarar memory leak no benchmark.
- Token de rede (`wget`/`curl`) em entrypoint sem o padrão completo shell+download — pode ser legítimo (healthcheck), revisar.
- `cpus` ausente em algum serviço — impede somar o orçamento agregado com confiança.
- Imagem desenhada para rodar como root — o compose endurecido força non-root, mas é sinal amarelo.
- Build com download de rede nas camadas (visto em `docker history`) — conferir contra o repositório open source.
- Manifesto não declara `arm64` explicitamente — pode ser imagem **single-arch** empurrada sem índice OCI (arquitetura fica só no `config` blob). Não bloqueia o PR, mas o revisor deve confirmar no `docker inspect` que a imagem é de fato arm64 nativa antes do merge.

## 3. Auditoria da imagem

`scripts/audit_image.sh` roda no mesmo gate, sem executar a imagem:

- **arm64 nativo** declarado no manifesto (também é regra do desafio; emulação QEMU desclassifica). Ausência da declaração explícita é **ressalva**, não fail — imagens single-arch empurradas sem índice OCI ainda podem ser arm64 nativas; o revisor confirma manualmente.
- Imagem **pública e baixável**.
- Imagem **não desenhada para root** (ressalva se for).
- **ENTRYPOINT/CMD sem shell+download**.
- **Histórico de camadas sem download de rede** (ressalva, conferir contra o repo).
- **Tamanho razoável** — imagem gigante pode esconder payload (ressalva acima de 250 MB).

O repositório open source deve **bater** com a imagem publicada. Se o `Dockerfile` do repo não produz o que está no registry, trate como não-confiável independentemente do que o gate disser.

## 4. Isolamento do ambiente de execução

O gate reduz o risco no merge. Estas medidas reduzem o impacto **se algo passar mesmo assim** (namespace do Linux não é uma fronteira de segurança perfeita; escapes de runtime existem):

- **Pi dedicado e descartável.** Nada de dado pessoal ou de produção na mesma máquina. Reflashar o cartão entre temporadas zera qualquer persistência.
- **`userns-remap` ativo** em `/etc/docker/daemon.json`:
  ```json
  { "userns-remap": "default" }
  ```
  Root dentro do container vira UID sem privilégio no host. Custo de performance ~zero, mitiga muito do impacto de um escape. Recomendado fortemente.
- **Sem credenciais no host.** Nenhuma chave SSH, token de cloud, `.aws/`, `.kube/` na máquina que roda o benchmark.
- **Rede do Pi segmentada.** VLAN isolada, sem rota para o resto da sua rede. O `k6` (gerador de carga) só precisa alcançar a porta 8080.
- **Rodar via compose endurecido quando possível.** Mesmo com o compose vindo do participante, aplicar `docker-compose.hardened.yml` como override de base — ou exigir que a submissão parta dele — reduz a superfície. O gate valida que o resultado final respeita as regras independentemente da origem.

## 5. Durante e depois da execução

```bash
# Apos a corrida, derrube e remova tudo, incluindo volumes anonimos:
docker compose down -v --remove-orphans

# Confirme que nada sobrou:
docker ps -a --filter "name=pi-bench"
```

Se a imagem é distroless (sem shell), tentar `docker exec` nela falha — isso é **bom sinal**, não problema. Use `docker stats` e logs para observabilidade, não shell no container.

---

## Modelo de ameaça

| Ameaça | Mitigação primária | Mitigação secundária |
| --- | --- | --- |
| Compose malicioso lê/apaga arquivo do host | Gate reprova bind mounts em paths sensíveis, path traversal e binds graváveis | `userns-remap`; Pi descartável |
| Escapar via Docker socket | Gate reprova `docker.sock` em qualquer forma | Docker rootless |
| Escalar privilégio | Gate exige `cap_drop: ALL`, `no-new-privileges`, non-root | seccomp default do Docker |
| Exfiltrar / baixar 2º estágio | Gate reprova shell+download; rede `backend` `internal` | VLAN isolada; `/tmp` com `noexec` |
| Derrubar o Pi (OOM) | Gate exige `mem_limit` sob teto | Pi dedicado |
| Imagem ≠ código publicado | Auditoria `docker history` + conferência com o repo | Regra de desclassificação |
| PR altera as regras para se auto-aprovar | Gate roda o validador a partir do **base ref**, não do PR | Revisão humana obrigatória |

A defesa mais forte é a combinação: **gate automático bloqueante** + **revisão humana** + **Pi descartável e isolado**. Nenhuma camada sozinha é suficiente quando o compose vem de fora.
