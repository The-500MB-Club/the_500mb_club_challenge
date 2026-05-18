# Modelo de Segurança

## Premissa

Cada submissão entrega **o `docker-compose.yml` completo** mais a imagem Docker pública e o repositório open source. O organizador **não** controla o compose — ele é input não-confiável, escrito pelo participante, e roda num Raspberry Pi.

Isso muda tudo em relação a um cenário onde o organizador fornece o compose: aqui o compose é uma superfície de ataque de primeira classe. Um participante mal-intencionado (ou um PR comprometido) pode tentar usar o compose para ler/apagar arquivos do host, escapar do container ou derrubar o Pi.

A defesa tem três camadas:

1. **Hardening padrão injetado** (`scripts/harden_compose.py`) — após `docker compose config`, o gate injeta `read_only: true`, `security_opt: [no-new-privileges:true]` e `tmpfs` adequado por papel **somente onde o participante não declarou nada**, e adiciona `cap_drop: [ALL]` **apenas no perfil `api`**. Em `lb`/`redis`/`db` a injeção de `cap_drop` é deliberadamente omitida porque as imagens oficiais desses serviços iniciam como root e usam `setpriv`/`su-exec`/`chown` no boot para baixar para um usuário dedicado — dropar todas as capabilities ali quebra a inicialização (`setresuid: Operation not permitted`). A defesa nesses papéis fica em `no-new-privileges` + `read_only` (quando aplicável) + `USER` non-root vinda da própria imagem. Reduz a chance de submissão insegura por omissão sem mudar o que o participante escreveu de propósito.
2. **Gate automático no PR** (`.github/workflows/pr-security.yml`) — toda abertura ou sincronização de PR roda `scripts/validate_compose.py` + `scripts/audit_image.sh` sobre o resultado da injeção. Qualquer violação bloqueante **reprova o PR e impede o merge**. Defesa primária e obrigatória — vale tanto contra omissão quanto contra valor explícito inseguro (o injetor não sobrescreve nada explícito).
3. **Revisão humana** — o checklist do gate orienta o revisor, mas decisões de ressalva (`WARN`) e qualquer coisa que o validador marque para inspeção exigem olho humano antes do merge e antes de rodar no Pi.

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
| `user` explicitamente `root`/`0` em serviço de API (override no compose) | Anula a `USER` non-root da imagem. Unset não bloqueia — a auditoria da imagem confere o `Config.User` real. |
| `entrypoint:`/`command:` com shell + download | `sh -c "wget … \| sh"` = baixar segundo estágio. |
| `mem_limit` ausente em qualquer serviço | Serviço sem teto de memória. |
| CPU agregada > 2.0 ou memória agregada > 500 MiB | Viola a regra fundamental do desafio. |
| Composição < 3 APIs / sem LB | Não atende o desenho mínimo do desafio. |

Bind mounts são permitidos desde que (i) não toquem paths sensíveis do host listados acima, (ii) não contenham `..` (path traversal) e (iii) sejam declarados **read-only**. O caso canônico é o `nginx.conf` do load balancer montado read-only a partir do repositório. Qualquer bind mount que falhe em qualquer uma das três condições reprova.

Capabilities têm tratamento por papel: serviços de API não podem ter **nenhuma** capability adicionada e levam `cap_drop: [ALL]` injetado pelo gate. Em `lb`/`redis`/`db` o gate **não injeta** `cap_drop` — o entrypoint dessas imagens depende de capabilities do root (`setpriv`/`chown`) para baixar privilégio no boot. Se o participante quiser ainda assim dropar capabilities manualmente no LB (declarando `cap_drop` + `cap_add` explicitamente no compose), só o conjunto mínimo do nginx oficial é aceito (`CHOWN`, `SETUID`, `SETGID`, `NET_BIND_SERVICE`, `DAC_OVERRIDE`); qualquer cap fora disso reprova. O validador classifica o papel pela imagem, não pelo nome do serviço — renomear `api-1` para `web` não burla a regra.

### Storage suportado (allowlist)

O desafio aceita **somente quatro engines** como serviço de storage:

| Engine | Papel detectado | Hardening injetado | Por quê só esses |
| --- | --- | --- | --- |
| `redis` | `redis` | `read_only=true`, `no-new-privileges` | K/V em memória, ~30 MiB ocioso, ~80 MiB sob carga. Cabe folgado nos 500 MiB agregados. |
| `postgres` (e `postgresql`) | `db` | `no-new-privileges` | SQL maduro; com `shared_buffers=16MB` e `max_connections=20` roda em ~80–150 MiB. |
| `mariadb` | `db` | `no-new-privileges` | SQL maduro; com `innodb_buffer_pool_size=32M` roda em ~150–200 MiB. |
| `mysql` | `db` | `no-new-privileges` | Mesmo perfil do MariaDB, com mais agressividade no tuning. |

**Por que a allowlist é fechada.** O orçamento agregado de 500 MiB obriga a stack inteira (API ×3 + LB + storage) a caber em meio gigabyte. Subtraindo ~120 MiB×3 para as APIs e ~40 MiB para o LB, sobram ~100 MiB para o banco — orçamento que apenas estes quatro engines respeitam de forma realista. Bancos como **MongoDB, Cassandra, ScyllaDB, Elasticsearch, OpenSearch, ClickHouse, CockroachDB, InfluxDB 2.x, Neo4j** pedem 512 MiB–1 GiB **só de heap**: estouram o teto sozinhos, fazem OOM no Pi e desvirtuam a comparação entre runtimes.

**O perfil `db` não força `read_only=true`** porque Postgres/MariaDB/MySQL precisam escrever em `/var/lib/<engine>/data` (volume), em `/var/run/<engine>` (socket) e em `/tmp`. Forçar rootfs imutável quebraria o boot do engine. É responsabilidade do participante:

- Montar volume nomeado em `/var/lib/postgresql/data` (ou equivalente);
- Usar imagem oficial — o usuário não-root padrão (`postgres`/`mysql`) é suficiente, não precisa override de `user:`;
- Definir `mem_limit` cabível na fatia que sobra.

**Nem o perfil `redis`/`db` nem o `lb` recebem `cap_drop` injetado.** As imagens oficiais desses serviços iniciam como root e usam `setpriv`/`su-exec`/`chown` no entrypoint para baixar privilégio para um usuário dedicado (`redis`, `postgres`, `mysql`, `nginx`). Injetar `cap_drop: [ALL]` arranca as capabilities (`SETUID`, `SETGID`, `CHOWN`, `DAC_OVERRIDE`) que o entrypoint precisa, e o container falha logo no boot com `setresuid failed: Operation not permitted`. O sandboxing nesses papéis fica em `no-new-privileges` (bloqueia escalada via setuid), `read_only` (onde o engine permite) e a `USER` non-root da própria imagem.

**Como o validador trata uma imagem fora da allowlist.** Se o `image:` não contém nenhum dos tokens `redis`, `postgres`, `postgresql`, `mariadb`, `mysql` nem um LB conhecido, o serviço cai no papel `api` — e leva o pacote completo de hardening estrito (`read_only: true`, `cap_drop: [ALL]`, non-root, etc.). Como nenhum banco de verdade roda com rootfs imutável e UID arbitrário, isso reprova na prática. A topologia mínima também checa `≥1 storage` entre os quatro engines aceitos (`WARN` se ausente).

## 2. Ressalvas (não bloqueiam, exigem revisão humana)

Marcadas como `WARN` no checklist. O PR não é reprovado automaticamente, mas o revisor decide antes do merge:

- `extra_hosts:` definido — pode mascarar acesso a serviço interno; verificar o alvo.
- `memswap_limit` diferente de `mem_limit` — swap pode mascarar memory leak no benchmark.
- Token de rede (`wget`/`curl`) em entrypoint sem o padrão completo shell+download — pode ser legítimo (healthcheck), revisar.
- `cpus` ausente em algum serviço — impede somar o orçamento agregado com confiança.
- `user:` não declarado no compose de uma API — ressalva no validador; a auditoria da imagem (`audit_image.sh`) decide com base no `Config.User` real da imagem construída (essa, sim, é a regra bloqueante).
- Hardening de API injetado pelo gate (`read_only`, `cap_drop: [ALL]`, `no-new-privileges`) — uma ressalva por campo ausente. Não bloqueia, mas avisa que `docker compose up` localmente **não tem essa proteção**: o gate injeta só pra benchmark. Para emparelhar local e CI, declare explicitamente no seu compose.
- Build com download de rede nas camadas (visto em `docker history`) — conferir contra o repositório open source.
- Manifesto não declara `arm64` explicitamente — pode ser imagem **single-arch** empurrada sem índice OCI (arquitetura fica só no `config` blob). Não bloqueia o PR, mas o revisor deve confirmar no `docker inspect` que a imagem é de fato arm64 nativa antes do merge.

## 3. Auditoria da imagem

`scripts/audit_image.sh` roda no mesmo gate, sem executar a imagem:

- **arm64 nativo** declarado no manifesto (também é regra do desafio; emulação QEMU desclassifica). Ausência da declaração explícita é **ressalva**, não fail — imagens single-arch empurradas sem índice OCI ainda podem ser arm64 nativas; o revisor confirma manualmente.
- Imagem **pública e baixável**.
- Imagem **construída para non-root** — `Config.User` da imagem deve apontar para um UID não-zero. Esta é a *enforcement* real do non-root: forçar `user:` no compose quebra entrypoints que dropam privilégio sozinhos (nginx, postgres), então a regra mora no nível da imagem. Imagem com `Config.User` vazio ou `root`/`0` é **fail** — adicione `USER <uid>:<gid>` no Dockerfile.
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
