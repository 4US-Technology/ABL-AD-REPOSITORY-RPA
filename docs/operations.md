# Operação

1. Rode `ad-rpa db check --db-path relatorio.db` e `ad-rpa db backup relatorio.db backup.db`.
2. Configure secrets Docker e suba o Compose.
3. Execute primeiro `ad-rpa --once` sem `--apply`; então use `--apply`.

Em indisponibilidade de AD, GLPI ou SMTP, corrija conectividade/credenciais e deixe o próximo ciclo retomar. Para recuperar banco, pare o serviço, execute `ad-rpa db restore backup.db /data/ad-rpa.db`, rode `ad-rpa db check --db-path /data/ad-rpa.db` e reinicie-o.
