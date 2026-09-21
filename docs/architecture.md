# Arquitetura

`ad-rpa` executa e-mail diariamente após 07:00 e VPN a cada ciclo. Cada fluxo usa o mesmo SQLite persistido em `/data`; e-mail é idempotente por login e vencimento, enquanto VPN registra a etapa AD/GLPI antes de executá-la. O coordenador isola erros por fluxo. `/livez` indica processo ativo; `/readyz` exige banco e pelo menos um ciclo de cada fluxo; `/metrics` expõe contadores sem labels pessoais.
