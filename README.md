# jotaduo-ci

Espelho sem código, só para rodar os testes do repositório privado `devjotaduo/jotaduo-v0.01` em runners do GitHub, que são livres em repositório público. Segue o modelo do `devjotaduo/kit-agentes-universal-ci`.

O workflow `Tests` roda:

- por um push em `run/pr-<número>-<commit>-<execução>-<tentativa>`, feito pelo job `hosted-tests` do repositório privado com a chave de deploy `CI_TRIGGER_KEY`; testa o merge do pull request e apaga a branch ao terminar. `run/affected-merge-pr-<número>-<sha>-...` testa o próprio commit informado;
- manualmente, com o commit a testar:

```bash
gh workflow run tests.yaml --repo devjotaduo/jotaduo-ci -f sha=<40 caracteres>
```

## Como o código chega e o resultado volta

- `start` abre o check run `ci-publico` em andamento no commit do pull request.
- `fetch` gera um token de curta duração do GitHub App `kit-agentes-ci` (id 5895369, só `contents: read`), baixa o commit sem `docs/`, `deploy/`, `.github/`, instruções de agentes e `twenty-website` (dele só o `package.json`; o `twenty-docs` entra porque um teste do servidor lê os guias de UI) e envia o resto como artefato. Ele não executa nada do que baixa.
- `prepare` instala as dependências e compila o `twenty-shared` uma vez e guarda tudo (`node_modules`, `packages/*/node_modules` e o que o build do `twenty-shared` gera) no cache, com a chave do `.nvmrc`, do `yarn.lock` e do conteúdo do `twenty-shared`. Sem a chave exata, parte do que a main tiver salvo para os mesmos `.nvmrc` e `yarn.lock`, e recompila o `twenty-shared`.
- Os jobs de teste (`shared`, `frontend`, `server`, `ui`, `cli`, `typecheck-*`, `lint-*`) recebem o artefato, restauram o cache do `prepare` e não referenciam segredo nenhum. Num `run/affected-...`, o `fetch` lista o que o pull request mudou contra a base (primeiro pai do commit testado) e os jobs montam uma base com esses caminhos para o `packages/twenty-utils/ci/run-affected-tests.sh` da origem escolher os testes; ele roda a suíte inteira quando muda configuração, dependência ou o `twenty-shared`. `run/pr-...` e o disparo manual rodam tudo. Typecheck e lint rodam sempre inteiros.
- O agendamento a cada seis horas roda só `fetch` e `prepare` na main do privado: cache salvo numa branch `run/**` só serve àquela execução, e o salvo na main serve a todas.
- `report` conclui o `ci-publico` com success, failure ou cancelled. O `hosted-tests` do privado espera esse check.

A chave do App fica no secret `CI_APP_PRIVATE_KEY` do environment `ci`, usado só por `start`, `fetch` e `report`.

O artefato e os logs são públicos: o artefato com o código de `packages/` fica baixável por um dia, e uma falha de typecheck ou lint imprime trechos do código. A suíte `overlays` precisa do histórico Git e roda no repositório privado.
