#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime, timezone

import ad_directory as ad
import emailvencimentosenha as email_jobs
import report_storage
import reset_vpn
from glpi_client import GlpiClient, load_config as load_glpi_config


def clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value.ljust(width)
    if width == 1:
        return value[:1]
    return value[: width - 1] + "…"


def render_table(headers: list[str], rows: list[list[str]], widths: list[int]) -> list[str]:
    header = " | ".join(clip(text, width) for text, width in zip(headers, widths))
    separator = "-+-".join("-" * width for width in widths)
    lines = [header, separator]
    for row in rows:
        lines.append(" | ".join(clip(text, width) for text, width in zip(row, widths)))
    return lines


def build_user_rows(users: list[email_jobs.ExpiringUser], tz_name: str) -> list[list[str]]:
    rows: list[list[str]] = []
    now_utc = datetime.now(timezone.utc)
    for user in users:
        expires_text = ad.format_dt(user.expiry, tz_name)
        if user.expiry <= now_utc:
            status = "vencido"
        elif not user.email:
            status = "sem email"
        else:
            status = "ok"
        rows.append([user.login, user.name, expires_text, user.email or "-", status])
    return rows


def build_ticket_rows(tickets: list[reset_vpn.VpnResetTicket]) -> list[list[str]]:
    rows: list[list[str]] = []
    for ticket in tickets:
        requesters = ", ".join(ticket.requester_logins) or "-"
        owner = "sim" if reset_vpn.requester_matches_login(ticket) else "nao"
        rows.append([str(ticket.id), str(ticket.status or "-"), ticket.login, requesters, owner])
    return rows


def render_screen(
    *,
    users: list[email_jobs.ExpiringUser],
    tickets: list[reset_vpn.VpnResetTicket],
    tz_name: str,
    days: int,
    interval: int,
    snapshot: report_storage.ReportSnapshot,
    db_path: str,
) -> str:
    width = shutil.get_terminal_size((140, 40)).columns
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    user_lines = render_table(
        ["login", "nome", "expira", "email", "status"],
        build_user_rows(users, tz_name),
        [18, 28, 22, 34, 10],
    )
    if len(user_lines) == 2:
        user_lines.append("(nenhum usuario na janela)")

    ticket_lines = render_table(
        ["ticket", "status", "login", "requerente", "mesmo dono"],
        build_ticket_rows(tickets),
        [10, 8, 20, 36, 10],
    )
    if len(ticket_lines) == 2:
        ticket_lines.append("(nenhum chamado ativo)")

    lines = [
        f"Relatorio AD/GLPI  {timestamp}",
        f"Usuarios expiram em ate {days} dia(s); chamados ativos status 1,2; refresh {interval}s",
        f"SQLite: {db_path}  snapshot_id={snapshot.id}  usuarios={snapshot.user_count}  chamados={snapshot.ticket_count}",
        "=" * min(width, 120),
        "",
        "Usuarios a vencer",
        *user_lines,
        "",
        "Chamados GLPI ativos",
        *ticket_lines,
    ]
    return "\x1b[2J\x1b[H" + "\n".join(lines)


def collect_users(days: int) -> list[email_jobs.ExpiringUser]:
    ad_config = ad.load_config()
    conn = ad.connect_ad(ad_config)
    return email_jobs.find_expiring_users(conn, ad_config, days=days, from_date=None)


def collect_tickets(limit: int) -> list[reset_vpn.VpnResetTicket]:
    glpi = GlpiClient(load_glpi_config(), debug=False)
    glpi.init_session()
    try:
        return reset_vpn.load_vpn_reset_tickets(
            glpi,
            limit=limit,
            statuses=reset_vpn.ACTIVE_TICKET_STATUSES,
        )
    finally:
        glpi.kill_session()


def main(argv: list[str] | None = None) -> int:
    ad.load_env_file()

    parser = argparse.ArgumentParser(
        description="Mostra usuarios a vencer e chamados GLPI ativos em uma tela de relatorio."
    )
    parser.add_argument("--days", type=int, default=3, help="Janela de dias para expiracao. Padrão: 3.")
    parser.add_argument("--limit", type=int, default=20, help="Maximo de chamados GLPI. Padrão: 20.")
    parser.add_argument("--interval", type=int, default=15, help="Intervalo de refresh em segundos. Padrão: 15.")
    parser.add_argument("--tz", default="America/Sao_Paulo", help="Timezone de exibicao. Padrão: America/Sao_Paulo.")
    parser.add_argument("--db-path", default="relatorio.db", help="Arquivo SQLite para armazenar snapshots. Padrão: relatorio.db.")
    parser.add_argument("--once", action="store_true", help="Renderiza uma vez e sai.")
    args = parser.parse_args(argv)

    conn = report_storage.connect(args.db_path)
    try:
        report_storage.initialize(conn)
        while True:
            users = collect_users(args.days)
            tickets = collect_tickets(args.limit)
            snapshot = report_storage.store_snapshot(
                conn,
                days=args.days,
                ticket_limit=args.limit,
                users=users,
                tickets=tickets,
            )
            sys.stdout.write(
                render_screen(
                    users=users,
                    tickets=tickets,
                    tz_name=args.tz,
                    days=args.days,
                    interval=args.interval,
                    snapshot=snapshot,
                    db_path=args.db_path,
                )
            )
            sys.stdout.flush()

            if args.once:
                break

            time.sleep(args.interval)
        return 0
    except KeyboardInterrupt:
        return 130
    except ad.LDAPException as e:
        print(f"Erro LDAP/AD: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
