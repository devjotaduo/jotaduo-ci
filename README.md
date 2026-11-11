# jotaduo-ci

Executa os testes de um repositório privado em runners hospedados pelo GitHub.

O workflow `Tests` roda de duas formas:

- por um push em `run/pr-<número>-<commit>-<execução>-<tentativa>`, feito pelo CI do repositório de origem com uma chave de deploy; testa o merge desse pull request e apaga a branch ao terminar. Com `run/affected-pr-...`, cada suíte roda só os testes que o diff do pull request alcança, pelo seletor `packages/twenty-utils/ci/run-affected-tests.sh` do repositório de origem; uma linha sem o seletor roda as suítes inteiras;
- manualmente, com a branch, tag ou commit a testar:

```bash
gh workflow run tests.yaml --repo devjotaduo/jotaduo-ci -f ref=<branch>
```

As suítes `typecheck` e `lint` (`nx run-many` em `twenty-server` e `twenty-front`) ficam fora do seletor e sempre olham os pacotes inteiros: os workflows `CI Server` e `CI Front` do repositório de origem não têm runner hospedado.

O código é lido com uma chave de leitura guardada no secret `SOURCE_DEPLOY_KEY`. Os logs das execuções são públicos.
