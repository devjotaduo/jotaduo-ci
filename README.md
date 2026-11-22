# jotaduo-ci

Executa os testes de um repositório privado em runners dedicados auto-hospedados.

O workflow `Tests` roda de duas formas:

- por um push em `run/pr-<número>-<commit>-<execução>-<tentativa>`, feito pelo CI do repositório de origem com uma chave de deploy; testa o merge desse pull request e apaga a branch ao terminar. Com `run/affected-pr-...`, cada suíte roda só os testes que o diff do pull request alcança, pelo seletor `packages/twenty-utils/ci/run-affected-tests.sh` do repositório de origem; uma linha sem o seletor roda as suítes inteiras;
- manualmente, com a branch, tag ou commit a testar:

```bash
gh workflow run tests.yaml --repo devjotaduo/jotaduo-ci -f ref=<branch>
```

A suíte `cli` repete os passos do `ci-cli.yaml` do upstream para o `twenty-cli`: build do SDK e da CLI, lint, typecheck (`nx typecheck twenty-cli`: o `tsgo` só existe nos binários da raiz, que o `yarn` não enxerga) e Vitest, sem o registro local e o app de ponta a ponta. Ela usa o seletor só quando o `run-affected-tests.sh` da origem conhece a suíte (`local-ci` v25 em diante); antes disso, roda inteira.

As suítes `typecheck` e `lint` (`nx run-many` em `twenty-server` e `twenty-front`) ficam fora do seletor e sempre olham os pacotes inteiros: os workflows `CI Server` e `CI Front` do repositório de origem não têm runner hospedado. O `lint` roda o oxlint sem as dependências de tarefa: a checagem `twenty-front:lint:unlocalized-strings` acusa 33 strings sem marcação de tradução que já estão na `main` da origem (11/11/2026) e, como dependência, impediria o oxlint de rodar.

O código é lido com uma chave de leitura guardada no secret `SOURCE_DEPLOY_KEY`. Os logs das execuções são públicos: uma falha de typecheck ou de lint imprime trechos do código do repositório de origem.

O disparo `run/affected-merge-pr-<número>-<SHA do merge>-<execução>-<tentativa>` fixa o checkout das suítes no commit de merge informado e usa o primeiro pai como base. Se esse commit ficar indisponível, o checkout falha. Os prefixos anteriores continuam resolvendo a referência de merge do PR como antes.

## Cache privado da preparação

As suítes no mesmo runner podem reutilizar a instalação do Yarn e o build de `twenty-shared` realizados por outra suíte da mesma tentativa. A primeira suíte executa `yarn install --immutable --check-cache` e o build completo. O workflow só grava o resultado quando o perfil do Nx confirma que o build terminou com sucesso. Cada suíte executa seus próprios testes e análises.

A chave inclui o SHA efetivamente conferido da origem, o workflow, a execução e tentativa, um identificador aleatório do plano, o runner, o caminho do checkout, a arquitetura e a versão do Node. A troca de revisão ou execução invalida o arquivo antes de executar scripts da origem. A cópia local inclui `node_modules` da raiz e dos workspaces, estado de instalação do Yarn e os arquivos gerados ou alterados em `twenty-shared`, inclusive índices, manifests e declarações. Um layout de preparação desconhecido segue pelo caminho completo. Os tempos de instalação e compilação aparecem nos respectivos passos do Actions. O passo de restauração registra o tempo total do acerto ou da falta.

O arquivo fica em um diretório de modo 0700 em `/tmp`, separado por usuário de serviço. O workflow confere o SHA-256 antes de restaurá-lo. A cota é de 1,5 GiB por runner e a reserva de espaço é de 12 GiB durante a compactação e a extração. Um bloqueio serializa as mudanças e a publicação do arquivo é atômica. Uma tentativa nova limpa a cópia anterior. O prazo de reuso é de seis horas; a próxima operação remove o arquivo vencido. Sem novos jobs, a política local do host limpa arquivos inativos de `/tmp` após 30 dias, na varredura agendada.

O cache permanece no disco do executor. Os jobs com acesso ao mesmo usuário do sistema pertencem à mesma fronteira de confiança e conseguem ler os arquivos desse usuário. Use um usuário dedicado por runner e execute nele apenas workloads de confiança equivalente. Para separar workloads mutuamente não confiáveis é necessário isolamento do executor.
