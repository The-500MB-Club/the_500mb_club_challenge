# Testando no hardware real

Quando o PR da sua submissão é mergeado no default branch, um **benchmark inicial é enfileirado automaticamente**: o workflow [`submission-merged-benchmark.yml`](../../.github/workflows/submission-merged-benchmark.yml) abre uma issue `test/<owner>/<id>` (já com o label `benchmark-request`) para cada id **novo** no seu `submissions/<owner>.json`, e o daemon Pi-Bench a executa na próxima passagem de polling — sem nenhum passo manual.

Este documento trata do **caminho manual**: como pedir uma re-execução no daemon Pi-Bench abrindo você mesmo uma issue no GitHub.

## Quando usar

- Você mergeou uma submissão e quer rodar de novo contra a **mesma imagem** já listada em `submissions/<seu-login>.json` (por exemplo, depois de publicar uma nova tag, ajustar o compose dentro das regras, ou como sanity check antes de uma semana com patrocinador).
- Esse caminho **não** serve para enviar código novo. Mudança de código sempre passa por PR (veja [submitting.md](./submitting.md)) — este fluxo de issue só re-dispara uma medição contra o que já está mergeado.

## Pré-requisitos

- `submissions/<seu-login>.json` existe no default branch.
- O `<id>` que você quer re-rodar é um dos `submissions[].id` desse arquivo. O formato é o mesmo validado pelo gate do PR: 1-50 chars, `[a-z0-9._-]`, sem começar ou terminar com separador (veja [submitting.md](./submitting.md#o-que-o-gate-valida)).

## Como abrir o pedido

Abra uma nova issue com:

- **Título**: exatamente `test/<id>` — por exemplo `test/go`, `test/rust`, `test/zig`. O gate valida o título contra a regex `^test/[a-z0-9](([a-z0-9._-]{0,48}[a-z0-9])?)$`.
- **Corpo**: texto livre; uma nota curta sobre o motivo da re-execução ajuda, mas não é obrigatória.

Há um issue form pronto em **New issue → Benchmark request** (`.github/ISSUE_TEMPLATE/benchmark-request.yml`).

### Mantenedores: benchmarkando a submissão de outro usuário

Mantenedores (e o `github-actions[bot]` usado pelo disparo automático) podem mirar uma submissão que **não** é sua usando o título estendido `test/<owner>/<id>` — por exemplo `test/gandarez/go`. O `<owner>` só é honrado quando o autor da issue está na lista de confiáveis (configurada em [`issue-benchmark-gate.yml`](../../.github/workflows/issue-benchmark-gate.yml) como `TRUSTED_AUTHORS` e no daemon como `BR_TRUSTED_AUTHORS`). Para todos os demais, só `test/<id>` (sua própria submissão) é aceito; um `test/<owner>/<id>` vindo de autor não-confiável é rejeitado.

## Ciclo de labels

O workflow do gate ([`.github/workflows/issue-benchmark-gate.yml`](../../.github/workflows/issue-benchmark-gate.yml)) é a **única** origem confiável do label `benchmark-request`. O daemon Pi-Bench faz polling desse label, então é ele que controla a execução:

| Label | Cor | Significado |
|---|---|---|
| `benchmark-request` | verde | pedido validado, na fila do daemon |
| `benchmark-running` | amarelo | o daemon Pi-Bench está executando no Pi |
| `benchmark-done` | roxo | execução concluída com sucesso; resultados postados na issue |
| `benchmark-failed` | vermelho | execução começou mas falhou no Pi |
| `benchmark-rejected` | vermelho-escuro | o gate rejeitou o pedido; a issue é fechada |

O gate roda em `opened`, `edited` e `reopened` — então, se você corrigir o título via edição, a validação roda de novo.

## Por que um pedido pode ser rejeitado

O gate rejeita com um comentário automático e fecha a issue (reason `not planned`) nestes casos:

1. **Você usou `test/<owner>/<id>` mas não é um mantenedor confiável.** Só autores confiáveis podem mirar a submissão de outro usuário; use `test/<id>` para a sua própria.
2. **O owner-alvo não passa no formato de username do GitHub** (`^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`). Extremamente raro; só ocorre em edge cases legados.
3. **`submissions/<owner>.json` não está no default branch.** Ou nenhum PR de submissão foi aberto ainda, ou ele ainda não foi mergeado. Abra um PR primeiro e aguarde o merge.
4. **O `<id>` no título não existe em `submissions[].id`.** O comentário de rejeição lista os ids que realmente existem no arquivo — copie um deles e abra uma nova issue.

## Modelo de segurança

O gate roda só a partir do default branch e só faz parsing de JSON via `gh api`; ele nunca executa código da sua submissão. O daemon Pi-Bench é o único componente que executa a stack, e o faz em ambiente isolado e dedicado.

## Limitações conhecidas

- Não há rate limit documentado para pedidos; o daemon pega via polling, então pedidos seguidos no mesmo `<id>` são serializados.
- Os resultados são postados como comentário na issue de origem e o label transita para `benchmark-done` / `benchmark-failed`. O formato exato do comentário de resultado é responsabilidade do repositório do [daemon Pi-Bench](../../README.md) e está documentado lá.
