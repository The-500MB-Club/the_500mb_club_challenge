# Como submeter

Sua submissão **não** vai neste repositório. Você mantém o código no **seu próprio repositório** e abre um Pull Request aqui contendo apenas um arquivo que aponta para ele. Um pipeline automático valida tudo e comenta o resultado no PR.

## Passo 1 — Prepare seu repositório

No seu repositório (público, no GitHub, com licença OSI-aprovada):

- A branch `main` deve conter a implementação da API.
- Crie uma branch chamada exatamente **`implementation`**. É nela que o validador vai procurar o código.
- A branch `implementation` deve conter, na raiz, o seu **`docker-compose.yml`** atendendo o orçamento de **2 CPUs / 500 MB** agregados.
  - Também deve conter o arquivo `me.json` com as informações da equipe (veja [README.md](./README.md)).
- A imagem Docker da sua API deve ser **pública**, com manifesto **`arm64` nativo** (sem emulação QEMU).
- O `docker-compose.yml` deve subir a stack mínima: ≥3 réplicas da API, 1 load balancer round-robin, 1 storage.
  - **Storage permitido (allowlist)**: `redis`, `postgres`, `mariadb` ou `mysql`. São os únicos engines que cabem de forma realista nos 500 MiB agregados — outros bancos (Mongo, Cassandra, Elastic, ClickHouse, etc.) pedem 512 MiB–1 GiB só de heap e estouram o teto sozinhos. Detalhes do perfil de hardening por engine em [`SECURITY.md`](./SECURITY.md#storage-suportado-allowlist).

> **O gate injeta hardening padrão pra você.** Não precisa escrever
> `read_only: true`, `security_opt: [no-new-privileges:true]` nem o `tmpfs`
> certo por papel — o gate detecta o papel pela imagem e adiciona o que
> estiver ausente. `cap_drop: [ALL]` é injetado **só nas APIs**: em LB e
> storage (redis/postgres/mariadb/mysql) o entrypoint oficial usa
> `setpriv`/`chown` para baixar privilégio e quebra com `cap_drop=[ALL]`,
> então o gate deixa por sua conta — se quiser dropar, declare explícito.
> Você fica só com o que importa: imagem, comando, rede, `mem_limit`/`cpus`,
> `user` non-root nas APIs e bind mounts (se houver). Valores explicitos
> seus prevalecem; se forem inseguros, o validador reprova. Veja
> [`examples/docker-compose.minimal.yml`](./examples/docker-compose.minimal.yml).

### Arquivo `me.json` na branch `implementation`

Cada submissão deve incluir um arquivo `me.json` com as seguintes informações:

```json
{
  "collaborators": [
    {
      "name": "Carlos Gandarez",
      "social_links": ["https://github.com/gandarez", "https://www.linkedin.com/in/gandarez"]
    },
    {
      "name": "Rapha Rossi",
      "social_links": ["https://www.linkedin.com/in/rapha-rossi"]
    }
  ],
  "stack": ["go", "redis", "nginx"]
}
```

## Passo 2 — Abra o PR

Faça um fork **deste** repositório e adicione **um único arquivo**:

```text
submissions/<username>.json
```

O nome do arquivo deve ser exatamente o seu nome de usuário do GitHub. O arquivo pode listar **uma ou mais submissões** (linguagens/variantes diferentes do mesmo participante), cada uma com um `id` próprio. Exemplo:

```json
{
  "submissions": [
    {
      "id": "go",
      "repo_url": "https://github.com/<username>/<repository-go>"
    },
    {
      "id": "python",
      "repo_url": "https://github.com/<username>/<repository-python>"
    }
  ]
}
```

| Campo | Obrigatório | Descrição |
| --- | --- | --- |
| `submissions` | sim | Array não-vazio de submissões. |
| `submissions[].id` | sim | Identificador da submissão. 1-50 chars; minúsculas/dígitos/`.`/`-`/`_`; não começa nem termina com separador. **Único por arquivo** (o mesmo `id` pode aparecer em arquivos de outros participantes — a unicidade é por usuário). Você escolhe o nome (ex: `go`, `gandarez-go`, `python`). |
| `submissions[].repo_url` | sim | URL do seu repositório. Só `https://github.com/owner/repo` ou `git@github.com:owner/repo`. O `owner` **deve** ser igual ao seu usuário (o nome do arquivo). |

O PR **só pode alterar esse um arquivo**. Qualquer outra mudança reprova automaticamente.

## Passo 3 — O pipeline valida

Ao abrir ou atualizar o PR, o gate roda automaticamente e posta **um comentário único** com um checklist. A ordem das validações:

1. **PR altera exatamente um arquivo** e ele é `submissions/<username>.json`.
2. **Schema do JSON**: objeto com `submissions` (array não-vazio); cada item tem `id` (formato válido, único no arquivo) e `repo_url` cujo `owner` é o `<username>`.
3. **Para cada submissão**, em sequência:
   1. A branch `implementation` existe no `repo_url`.
   2. O validador **clona apenas a branch `implementation`** (raso, sem executar nada), expande o compose via `docker compose config`, **injeta o hardening padrão por papel** (`scripts/harden_compose.py`) e então roda `scripts/validate_compose.py` sobre o resultado:
      - todas as regras de segurança de [`SECURITY.md`](./SECURITY.md);
      - o orçamento agregado de 2 CPUs / 500 MB;
      - a composição mínima (≥3 APIs, LB, 1 Storage entre `redis`/`postgres`/`mariadb`/`mysql`).
      - O comentário do PR lista, por serviço, o que foi auto-injetado.
   3. **`me.json` na raiz**: presente, JSON válido e com `collaborators` (array não-vazio de `{name, social_links}`) e `stack` (array não-vazio de strings).
   4. **Auditoria da imagem**: pública, `arm64` nativo, sem `ENTRYPOINT` shell+download, sem download de rede nas camadas de build.

Cada item vira `- [x]` (passou) ou `- [ ]` (falhou, com o motivo logo abaixo). **Qualquer item falho em qualquer submissão bloqueia o merge.** Corrija no(s) seu(s) repositório(s), atualize o PR (qualquer push reexecuta o gate) e o mesmo comentário é atualizado.

## O que o pipeline NÃO faz

Por segurança, o gate **nunca executa o código da sua submissão** durante a validação. Ele só faz parsing de YAML/JSON, `git clone` raso e `docker pull/inspect/history`. O benchmark de verdade (k6 contra a stack rodando no Raspberry Pi) acontece **depois** do merge, num ambiente isolado e dedicado.

## Erros comuns

- **"PR altera N arquivos"** — você commitou algo além do `submissions/<username>.json`. Reabra o PR só com esse arquivo.
- **"submissions[].id é único no arquivo"** — você repetiu o mesmo `id` em duas submissões. O `id` precisa ser único por usuário (pode repetir entre usuários diferentes).
- **"submissions[].id é válido"** — o `id` saiu do formato (1-50 chars, `[a-z0-9._-]`, sem começar/terminar com separador).
- **"submissions[].repo_url pertence ao dono do arquivo"** — o `owner` no `repo_url` difere do nome do arquivo. Você não pode listar repo de outra pessoa em `submissions[]`.
- **"Branch implementation ausente"** — você não criou a branch num dos repositórios, ou ela está só no seu fork local. Faça `git push origin implementation`.
- **"repo_url formato recusado"** — use a URL canônica do GitHub. Nada de URL encurtada, IP, ou outro host.
- **"sem arm64 no manifesto"** — uma das imagens foi buildada só para amd64. Use `docker buildx` num runner arm64 nativo.

Regras completas e o modelo de ameaça por trás dessas validações: [`SECURITY.md`](./SECURITY.md).
