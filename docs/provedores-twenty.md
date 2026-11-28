# Configuração de provedores do Twenty

O workflow manual **Configurar provedores do Twenty** grava a configuração desejada dos provedores LiteLLM e CLIProxy de um ambiente no jotaduo-g. O serviço usa essa configuração quando o operador instala ou recria os contêineres pelos procedimentos do fork.

## GitHub

1. Em `jotaduo2-eng/jotaduo-ci`, proteja `main` para aceitar alterações por pull request revisado, com revisão dos arquivos `.github/workflows/configurar-provedores-twenty.yaml` e `scripts/twenty_ai_providers.py`. Restrinja bypass e force push. O job usa o commit do próprio espelho em `main`.
2. Crie os GitHub Environments `teste` e `homologacao`. Em cada um, selecione **Selected branches and tags**, permita somente a **branch** `main` e configure **Required reviewers** com **Prevent self-review**. Desative o bypass administrativo da proteção, quando disponível. Cadastre os valores na configuração do ambiente correspondente.
3. Para publicar ou rotacionar, rode **Actions → Configurar provedores do Twenty → Run workflow**, em `main`, e escolha o ambiente. O revisor confere o ambiente e o commit antes de liberar o acesso às chaves. Se a atualização falhar, corrija a configuração e dispare uma execução nova.

| Tipo | Nome | Valor |
| --- | --- | --- |
| Variable | `LITELLM_BASE_URL` | URL HTTPS, normalmente `https://litellm.jotaduo.com/v1` |
| Variable | `LITELLM_MODELS_JSON` | Lista JSON com IDs autorizados para esse ambiente, como `["id-do-modelo"]` |
| Variable | `CLIPROXY_BASE_URL` | URL HTTPS, normalmente `https://cliproxy.jotaduo.com/v1` |
| Variable | `CLIPROXY_MODELS_JSON` | Lista JSON com IDs autorizados para esse ambiente |
| Variable | `TWENTY_CONFIG_SSH_HOST` | Nome DNS do receptor no jotaduo-g, alcançável na porta 22 pelo runner hospedado |
| Variable | `TWENTY_CONFIG_SSH_KNOWN_HOSTS` | Linha `host ssh-ed25519 chave-pública` com o mesmo nome da variável anterior. Confirme a chave do host por um canal independente antes de publicar. Durante uma troca, é possível fixar até quatro linhas para o mesmo host. |
| Secret | `LITELLM_API_KEY` | Credencial de LiteLLM exclusiva do ambiente |
| Secret | `CLIPROXY_API_KEY` | Credencial de CLIProxy exclusiva do ambiente |
| Secret | `TWENTY_CONFIG_SSH_KEY` | Chave privada OpenSSH dedicada à identidade receptora desse ambiente |

URLs aceitam caminho e porta HTTPS, com IDs de modelo e chaves sem espaços. Antes do SSH, o workflow verifica os IDs no catálogo autenticado `/models` de cada proxy. Gere um par SSH dedicado por ambiente e instale apenas a chave pública no host. Os logs do workflow mostram somente o resultado e diagnósticos sanitizados. O payload segue no stdin do SSH e a chave de transporte fica em arquivo temporário `0600` privado do job.

## Receptor no jotaduo-g

Um operador do host instala uma versão revisada de `scripts/twenty_ai_providers.py` em `/usr/local/libexec/twenty-ai-providers.py`, como `root:root` e modo `0644`, em diretório administrado por root. Use Python 3.12 ou posterior. Crie duas contas de sistema dedicadas sem senha, com shell `/bin/sh` para o comando forçado, sem chaves de uso geral, sem grupos de serviço ou Docker. Os destinos são fixos.

| Ambiente | Conta SSH | Diretório `0700` ou `0750` | Arquivo `0640` |
| --- | --- | --- | --- |
| Teste | `twenty-ai-config-teste` | `/etc/twenty-test-image`, `root:root` | `/etc/twenty-test-image/providers.json`, `root:root` |
| Homologação | `twenty-ai-config-homologacao` | `/etc/twenty-teste`, `root:root` ou `root:twenty-teste` | `/etc/twenty-teste/providers.json`, `root:twenty-teste` |

Preserve o diretório de homologação existente em `root:root` e modo `0700`. O receptor associa o arquivo ao grupo `twenty-teste` independentemente do grupo do diretório. O GID desse grupo deve corresponder ao GID do serviço que usa o arquivo montado. Em teste, a instalação do fork com arquivo protegido exige operador root autorizado. Antes da rotação, corrija as permissões de um arquivo anterior se o receptor recusar sua propriedade, grupo ou modo.

Nos ambientes gerenciados, crie um arquivo vazio `providers.json.required`, pertencente ao root, ao lado de cada `providers.json`. Esse marcador faz o instalador do fork deter a troca quando a configuração esperada estiver ausente. Crie o marcador antes da primeira instalação gerenciada.

Crie `/etc/ssh/twenty-ai-config` como `root:root` e modo `0755`. Em cada arquivo `/etc/ssh/twenty-ai-config/<conta>`, mantenha somente a chave pública do ambiente, prefixada por `restrict`. Os arquivos devem pertencer a `root:root` e ter modo `0644` para leitura pelo sshd. Adicione a configuração a seguir ao `sshd_config` ou a um include ativo.

```sshconfig
Match User twenty-ai-config-teste
    AuthorizedKeysFile /etc/ssh/twenty-ai-config/%u
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    DisableForwarding yes
    PermitTTY no
    PermitUserRC no
    ForceCommand /usr/bin/sudo -n -- /usr/bin/python3 -I /usr/local/libexec/twenty-ai-providers.py receive teste

Match User twenty-ai-config-homologacao
    AuthorizedKeysFile /etc/ssh/twenty-ai-config/%u
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    DisableForwarding yes
    PermitTTY no
    PermitUserRC no
    ForceCommand /usr/bin/sudo -n -- /usr/bin/python3 -I /usr/local/libexec/twenty-ai-providers.py receive homologacao

Match all
```

Use `visudo` para cadastrar apenas os comandos fixos, com gravação de entrada e saída desabilitada para essa transferência.

```sudoers
twenty-ai-config-teste ALL=(root) NOPASSWD: NOLOG_INPUT: NOLOG_OUTPUT: /usr/bin/python3 -I /usr/local/libexec/twenty-ai-providers.py receive teste
twenty-ai-config-homologacao ALL=(root) NOPASSWD: NOLOG_INPUT: NOLOG_OUTPUT: /usr/bin/python3 -I /usr/local/libexec/twenty-ai-providers.py receive homologacao
```

Valide o sshd com `sshd -t` e confira a configuração efetiva para as duas contas com `sshd -T -C user=<conta>,host=<host>,addr=<endereço>`. Confirme que somente a chave dedicada autoriza cada conta, inclusive se o host usar uma autoridade de certificados SSH ou um `AuthorizedKeysCommand`. Verifique com `sudo -l -U <conta>` que só o comando previsto foi concedido. Recarregue o serviço pela rotina do host. Se o SSH do jotaduo-g não for alcançável pelo runner hospedado, a execução fica pendente até existir um ingresso aprovado para o receptor.

O receptor confere o formato e substitui atomicamente o arquivo desejado no diretório protegido. O bind mount do fork aponta para `/run/secrets/twenty-ai-providers.json` no contêiner. Na rotação, atualize os Secrets, dispare o workflow, confira o sucesso e reaplique a configuração do ambiente pelo procedimento do fork. Recrie os contêineres que mantêm bind mount no arquivo anterior, usando a mesma imagem. Faça uma chamada autenticada por provedor e só então revogue as chaves substituídas. A homologação usa no deploy a cópia ativa gerenciada pelo fork.

Teste o contrato localmente com `python3 -B -m unittest discover -s scripts -p 'test_*.py'`.

[GitHub Actions e proteção de ambientes](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments) · [Sintaxe de workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax) · [OpenSSH e opções de chaves autorizadas](https://man.openbsd.org/sshd.8) · [sudoers](https://www.sudo.ws/docs/man/sudoers.man/) · [Python e substituição atômica de arquivos](https://docs.python.org/3.12/library/os.html#os.replace)
