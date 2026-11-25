# jotaduo-ci

Espelho sem código, só para rodar os testes do repositório privado `jotaduo2-eng/jotaduo-v0.01` em runners do GitHub, que são livres em repositório público. Segue o modelo do `devjotaduo/kit-agentes-universal-ci`.

O repositório privado só consegue criar branches aqui, com a chave de deploy `CI_TRIGGER_KEY`. O job `hosted-tests` de lá empurra uma branch `run/...`; o workflow `Relay` a apaga e dispara o `Tests` a partir da `main`, para as execuções dividirem o mesmo cache do Actions.

- `run/pr-<número>-<commit>-<execução>-<tentativa>` testa o merge do pull request com as suítes inteiras.
- `run/affected-merge-pr-<número>-<sha>-<execução>-<tentativa>` testa o commit de merge informado, só no que o diff alcança.
- Para um teste manual de um commit:

```bash
gh workflow run tests.yaml --repo jotaduo2-eng/jotaduo-ci -f sha=<40 caracteres>
```

## Como o código chega e o resultado volta

- `start` abre o check run `ci-publico` em andamento no head do pull request; no disparo `affected-merge`, o head é o segundo pai do commit de merge informado.
- `fetch` gera um token de curta duração do GitHub App `jotaduo-fork-bot` (id 5912992, com `contents: read` e `checks: write` no fork), baixa o commit sem `docs/`, `deploy/`, `.github/`, instruções de agentes e `twenty-website` (dele só o `package.json`; o `twenty-docs` entra porque um teste do servidor lê os guias de UI) e envia o resto como artefato. Ele não executa nada do que baixa.
- No modo `affected`, o `fetch` também lê da API a lista de arquivos do diff (nomes e estados) e a põe no artefato.
- `fetch` calcula a chave do preparo a partir dos fontes. `prepare` consulta o cache com ela e, só sem acerto, baixa o artefato, instala as dependências, compila o `twenty-shared` e grava o resultado. Sem acerto no job de teste, ele instala e compila por conta própria.
- A suíte `server` roda em três partes (`--shard`), porque o `test:ci` do servidor usa um worker só. No modo `affected`, os testes relacionados rodam só na primeira parte.
- Os jobs de teste (`shared`, `frontend`, `server`, `ui`, `cli`, `typecheck-*`, `lint-*`) recebem o artefato e não referenciam segredo nenhum. As suítes rodam pelo `packages/twenty-utils/ci/run-affected-tests.sh` da origem. No modo `affected`, cada job cria um commit base local que difere do testado exatamente nos caminhos da lista, e o seletor escolhe entre nada, testes relacionados e a suíte inteira. Um arquivo renomeado conta como apagado, o que leva à suíte inteira do pacote. Typecheck e lint rodam sempre inteiros.
- `report` conclui o `ci-publico` com success, failure ou cancelled. O `hosted-tests` do privado espera esse check.

A chave do App fica no secret `CI_APP_PRIVATE_KEY` do environment `ci`, usado só por `start`, `fetch` e `report`.

O artefato e os logs são públicos: o artefato com o código de `packages/` fica baixável por um dia, e uma falha de typecheck ou lint imprime trechos do código. A suíte `overlays` precisa do histórico Git e roda no repositório privado.

O cache do Actions é gravado só pelo `prepare`, mas qualquer job tem as credenciais de execução para gravar nele: os testes só devem rodar código de pull requests do próprio repositório privado, que é o que a chave de deploy garante.
