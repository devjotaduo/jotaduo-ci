# jotaduo-ci

Executa os testes de um repositório privado em runners hospedados pelo GitHub.

O workflow `Tests` roda de duas formas:

- por um push em `run/pr-<número>-<commit>`, feito pelo CI do repositório de origem com uma chave de deploy; testa o merge desse pull request e apaga a branch ao terminar;
- manualmente, com a branch, tag ou commit a testar:

```bash
gh workflow run tests.yaml --repo devjotaduo/jotaduo-ci -f ref=<branch>
```

O código é lido com uma chave de leitura guardada no secret `SOURCE_DEPLOY_KEY`. Os logs das execuções são públicos.
