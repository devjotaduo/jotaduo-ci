# jotaduo-ci

Executa os testes de um repositório privado em runners hospedados pelo GitHub.

O workflow `Tests` é disparado manualmente com a branch, tag ou commit a testar:

```bash
gh workflow run tests.yaml --repo devjotaduo/jotaduo-ci -f ref=<branch>
```

Com `-f status_sha=<commit>`, cada suíte devolve um status `hosted / <suíte>` nesse commit do repositório de origem, usando o secret `SOURCE_STATUS_TOKEN`.

O código é lido com uma chave de leitura guardada no secret `SOURCE_DEPLOY_KEY`. Os logs das execuções são públicos.
