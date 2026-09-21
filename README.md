# AD-RPA

Serviço Python para avisar vencimentos por e-mail e renovar acessos VPN via AD e GLPI.

## Uso

```bash
ad-rpa --once                 # dry-run: e-mail, VPN
ad-rpa --apply --interval 60  # serviço contínuo
ad-rpa email --help
ad-rpa reset --help
ad-rpa db migrate --db-path relatorio.db
```

O comando instalado `ad-rpa` é a interface suportada. `python -m ad_rpa` oferece o mesmo comportamento.

## Configuração

Desenvolvimento lê `.env`. Em produção use Docker secrets: cada variável aceita o par `NOME_FILE`, contendo o caminho do arquivo do segredo. `VAR_FILE` também pode apontar para um arquivo `NOME=valor` ou diretório de secrets. O banco padrão do serviço é `/data/ad-rpa.db` (`AD_RPA_DB_PATH` altera isso).

Variáveis de integração: `AD_SERVER`, `AD_BIND_USER`, `AD_BIND_PASSWORD`, `AD_BASE_DN`, `GLPI_URL`, `GLPI_USER_TOKEN` (ou `GLPI_LOGIN` e `GLPI_PASSWORD`), e SMTP ou Graph conforme o provedor configurado.

## Docker

Crie os arquivos em `secrets/` exigidos pelo `docker-compose.yml`, execute `docker compose up --build -d` e acompanhe `docker compose logs -f main`. A porta de operação não é publicada; healthcheck usa `/livez` internamente. Os endpoints internos são `/livez`, `/readyz` e `/metrics` em `127.0.0.1:8080`.

## Banco

Antes do rollout: `ad-rpa db check --db-path relatorio.db`; depois faça `ad-rpa db backup relatorio.db backup.db`. Para restaurar: `ad-rpa db restore backup.db /data/ad-rpa.db`. Migrações são seguras para bancos existentes e o volume Docker preserva estado de VPN e idempotência de e-mails.

## Operação

Logs são JSON em stdout e não incluem credenciais ou identificadores pessoais estruturados. Falhas de AD, GLPI ou SMTP em um fluxo são registradas e não interrompem o outro; valide conectividade externa em dry-run e pelos comandos de teste já disponíveis nos fluxos de e-mail.

Quando a renovação VPN é concluída, o chamado é encerrado no GLPI com status `6`.
