#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import emailvencimentosenha
import relatorio
import reset_vpn


def should_run_daily_email(
    *,
    now: datetime,
    last_run_date: str | None,
    scheduled_time: dt_time,
) -> bool:
    today = now.date().isoformat()
    if now.time() < scheduled_time:
        return False
    return last_run_date != today


def run_general(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Fluxo geral do AD-RPA: lista vencimentos e chamados; com --apply executa as acoes."
    )
    parser.add_argument("--apply", action="store_true", help="Executa envio de e-mail e reset real.")
    parser.add_argument("--debug", action="store_true", help="Mostra debug do fluxo de reset.")
    parser.add_argument("--days", type=int, default=3, help="Janela de dias para aviso de expiracao.")
    parser.add_argument("--from-date", help="Data inicial YYYY-MM-DD para o fluxo de e-mail.")
    parser.add_argument("--tz", default="America/Sao_Paulo", help="Timezone de exibicao.")
    parser.add_argument("--login", help="Limita o fluxo de e-mail a um login.")
    parser.add_argument("--force-expired", action="store_true", help="Permite incluir contas ja vencidas no aviso.")
    parser.add_argument("--limit", type=int, default=20, help="Maximo de chamados GLPI.")
    parser.add_argument("--ticket-id", type=int, help="Processa apenas um chamado especifico.")
    parser.add_argument("--statuses", default="1,2", help="Status GLPI separados por virgula. Padrao: 1,2.")
    parser.add_argument("--once", action="store_true", help="Executa um unico ciclo e sai.")
    parser.add_argument("--interval", type=int, default=60, help="Intervalo do polling do reset.")
    parser.add_argument("--repeat-seen", action="store_true", help="Reprocessa chamados ja vistos no mesmo polling.")
    args = parser.parse_args(argv)

    email_args = ["--days", str(args.days), "--tz", args.tz]
    if args.apply:
        email_args.append("--apply")
    if args.from_date:
        email_args.extend(["--from-date", args.from_date])
    if args.login:
        email_args.extend(["--login", args.login])
    if args.force_expired:
        email_args.append("--force-expired")

    reset_args = [
        "--limit",
        str(args.limit),
        "--tz",
        args.tz,
        "--statuses",
        args.statuses,
    ]
    if args.apply:
        reset_args.append("--apply")
    if args.debug:
        reset_args.append("--debug")
    if args.ticket_id is not None:
        reset_args.extend(["--ticket-id", str(args.ticket_id)])
    if args.repeat_seen:
        reset_args.append("--repeat-seen")
    seen = False
    last_email_run_date: str | None = None
    scheduled_time = dt_time(hour=7, minute=0)
    tz = ZoneInfo(args.tz)

    while True:
        if seen:
            print()

        run_email_now = args.once
        now = datetime.now(tz)
        if not args.once:
            run_email_now = should_run_daily_email(
                now=now,
                last_run_date=last_email_run_date,
                scheduled_time=scheduled_time,
            )

        if run_email_now:
            print("=== EMAIL ===")
            email_code = emailvencimentosenha.main(email_args)
            if email_code != 0:
                return email_code
            last_email_run_date = now.date().isoformat()

        print("\n=== RESET ===")
        reset_code = reset_vpn.main(reset_args)
        if reset_code != 0:
            return reset_code

        if args.once:
            return 0

        seen = True
        time.sleep(args.interval)


def main(argv: list[str] | None = None) -> int:
    raw_args = sys.argv[1:] if argv is None else argv
    commands = {"reset", "email", "relatorio"}

    if not raw_args or raw_args[0] not in commands:
        return run_general(raw_args)

    parser = argparse.ArgumentParser(description="CLI principal do AD-RPA.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("reset", help="Executa o fluxo de reset via GLPI/AD.")
    subparsers.add_parser("email", help="Executa o fluxo de aviso por e-mail.")
    subparsers.add_parser("relatorio", help="Mostra painel de usuarios a vencer e chamados ativos.")

    args, remaining = parser.parse_known_args(raw_args)

    if args.command == "reset":
        return reset_vpn.main(remaining)
    if args.command == "email":
        return emailvencimentosenha.main(remaining)
    if args.command == "relatorio":
        return relatorio.main(remaining)
    parser.error(f"Comando desconhecido: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
