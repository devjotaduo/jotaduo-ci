# jotaduo-ci

Executa os testes de um repositório privado em runners hospedados pelo GitHub.

O workflow `Tests` é disparado manualmente com a branch, tag ou commit a testar:

```bash
gh workflow run tests.yaml --repo devjotaduo/jotaduo-ci -f ref=<branch>
```

O código é lido com uma chave de leitura guardada no secret `SOURCE_DEPLOY_KEY`. Os logs das execuções são públicos.
