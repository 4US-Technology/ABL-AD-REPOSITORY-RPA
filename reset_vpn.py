#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ldap3.core.exceptions import LDAPException

import auto
from glpi import GlpiClient, load_config as load_glpi_config


TARGET_TICKET_NAME = "Acesso Expirado Rede/VPN ou Internet"
LOGIN_LABEL = "Login da Rede/VPN ou Internet"
SOLVED_STATUS = 5


@dataclass
class VpnResetTicket:
    id: int
    name: str
    status: int | None
    login: str
    content: str


def build_non_renewal_reason(decision: auto.Decision) -> str:
    if not decision.is_expired and decision.current_expiry is not None:
        return (
            "Não foi possível renovar automaticamente porque o acesso "
            "não estava expirado no AD."
        )

    return f"Não foi possível renovar automaticamente. Motivo: {decision.reason}"


def build_non_renewal_options(decision: auto.Decision) -> str:
    if not decision.is_expired and decision.current_expiry is not None:
        return (
            "\n\nOpções:\n"
            "- Abrir chamado de configuração/manutenção de máquina.\n"
            "- Validar conexão, VPN, internet e perfil local do computador.\n"
            "- Abrir novo chamado de renovação somente quando o acesso estiver expirado."
        )

    return ""


def build_processing_error_note(ticket: VpnResetTicket, error: Exception) -> str:
    return (
        "Não foi possível processar a renovação automática deste acesso.\n\n"
        f"Motivo: {error}\n\n"
        f"Login informado no chamado: {ticket.login}\n"
        "Verifique se o login foi preenchido corretamente e se o usuário existe no AD."
    )


def strip_html(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def extract_login_from_ticket_content(content: str) -> str | None:
    text = strip_html(content)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    for index, line in enumerate(lines):
        if line == LOGIN_LABEL:
            for candidate in lines[index + 1 :]:
                if candidate and candidate not in {"Descrição", "Prints do erro de Rede / VPN ou Acesso de internet"}:
                    return candidate

    match = re.search(
        r"Login da Rede/VPN ou Internet\s+([A-Za-z0-9._-]+)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1)

    return None


def ticket_id_from_search_row(row: dict[str, Any]) -> int | None:
    for key in ("2", "id", "ID"):
        value = row.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def search_vpn_reset_ticket_ids(client: GlpiClient, *, limit: int) -> list[int]:
    params = {
        "criteria[0][field]": 1,
        "criteria[0][searchtype]": "contains",
        "criteria[0][value]": TARGET_TICKET_NAME,
        "criteria[1][link]": "AND",
        "criteria[1][field]": 12,
        "criteria[1][searchtype]": "equals",
        "criteria[1][value]": "2",
        "forcedisplay[0]": 2,
        "forcedisplay[1]": 1,
        "forcedisplay[2]": 12,
        "range": f"0-{limit - 1}",
        "sort": 2,
        "order": "DESC",
    }
    data = client.request("GET", "search/Ticket", params=params)
    rows = data.get("data", []) if isinstance(data, dict) else []

    ids: list[int] = []
    for row in rows:
        if isinstance(row, dict):
            ticket_id = ticket_id_from_search_row(row)
            if ticket_id is not None:
                ids.append(ticket_id)
    return ids


def load_vpn_reset_tickets(client: GlpiClient, *, limit: int) -> list[VpnResetTicket]:
    tickets: list[VpnResetTicket] = []
    for ticket_id in search_vpn_reset_ticket_ids(client, limit=limit):
        ticket = client.get_item("Ticket", ticket_id)
        name = str(ticket.get("name") or "")
        content = str(ticket.get("content") or "")
        login = extract_login_from_ticket_content(content)

        if name != TARGET_TICKET_NAME or not login:
            continue

        tickets.append(
            VpnResetTicket(
                id=ticket_id,
                name=name,
                status=ticket.get("status"),
                login=login,
                content=content,
            )
        )
    return tickets


def process_ticket(
    ticket: VpnResetTicket,
    *,
    glpi: GlpiClient,
    ad_conn,
    ad_config: auto.AdConfig,
    apply: bool,
    tz_name: str,
) -> None:
    print(f"\n==== CHAMADO {ticket.id} ====")
    print(f"Título: {ticket.name}")
    print(f"Status GLPI: {ticket.status}")
    print(f"Login detectado: {ticket.login}")

    user = auto.find_user(ad_conn, ad_config, ticket.login)
    decision = auto.build_decision(user, ad_config, ticket.login, tz_name)

    print(f"DN: {decision.dn}")
    print(f"Expiração atual: {auto.format_dt(decision.current_expiry, tz_name)}")
    print(f"Está expirado?: {'SIM' if decision.is_expired else 'NÃO'}")
    print(f"Deve renovar?: {'SIM' if decision.should_renew else 'NÃO'}")
    if decision.should_renew:
        print(f"Motivo: {decision.reason}")
    else:
        print(f"Motivo: {build_non_renewal_reason(decision)}")

    if decision.new_expiry:
        print(f"Nova expiração calculada: {auto.format_dt(decision.new_expiry, tz_name)}")
        print(f"Novo FILETIME: {auto.datetime_to_filetime(decision.new_expiry)}")

    if not apply:
        print("DRY-RUN: nenhuma alteração foi feita no AD.")
        if decision.should_renew and decision.new_expiry:
            print("DRY-RUN: o chamado seria solucionado e encerrado no GLPI após renovar.")
        else:
            print("DRY-RUN: uma nota seria adicionada no GLPI explicando o motivo.")
        return

    if not decision.should_renew:
        note = (
            f"{build_non_renewal_reason(decision)}"
            f"{build_non_renewal_options(decision)}\n\n"
            f"Login analisado: {decision.login}\n"
            f"Expiração atual: {auto.format_dt(decision.current_expiry, tz_name)}"
        )
        glpi.add_followup(ticket.id, note)
        print("Nota adicionada no GLPI explicando o motivo da não renovação.")
        print("Nada a aplicar no AD.")
        return

    auto.apply_renewal(ad_conn, ad_config, decision)
    print("ALTERAÇÃO APLICADA NO AD COM SUCESSO.")

    solution = (
        "Acessos renovados por mais 3 meses.\n\n"
        f"Expira em: {decision.new_expiry.astimezone(auto.ZoneInfo(tz_name)).strftime('%d/%m/%Y')}.\n\n"
        "Obs.: O acesso expira a cada 3 meses sendo de responsabilidade do próprio "
        "usuário(a) pedir a renovação do seu acesso. Não serão atendidos chamados "
        "de renovação de senha que não seja do próprio dono do login de acesso."
    )
    glpi.add_solution(ticket.id, solution)
    glpi.update_ticket(ticket.id, {"status": SOLVED_STATUS})
    print("Solução adicionada e chamado encerrado no GLPI.")


def load_tickets_for_args(
    glpi: GlpiClient,
    *,
    ticket_id: int | None,
    limit: int,
) -> list[VpnResetTicket]:
    if ticket_id:
        ticket_data = glpi.get_item("Ticket", ticket_id)
        login = extract_login_from_ticket_content(str(ticket_data.get("content") or ""))
        if not login:
            raise RuntimeError(f"Não encontrei login no chamado {ticket_id}.")
        return [
            VpnResetTicket(
                id=ticket_id,
                name=str(ticket_data.get("name") or ""),
                status=ticket_data.get("status"),
                login=login,
                content=str(ticket_data.get("content") or ""),
            )
        ]

    return load_vpn_reset_tickets(glpi, limit=limit)


def run_cycle(
    *,
    glpi: GlpiClient,
    ad_conn,
    ad_config: auto.AdConfig,
    ticket_id: int | None,
    limit: int,
    apply: bool,
    tz_name: str,
    seen_ticket_ids: set[int],
    repeat_seen: bool,
) -> None:
    print(f"\n---- CICLO {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ----")
    tickets = load_tickets_for_args(glpi, ticket_id=ticket_id, limit=limit)

    if not repeat_seen:
        tickets = [ticket for ticket in tickets if ticket.id not in seen_ticket_ids]

    print(f"Chamados elegíveis encontrados: {len(tickets)}")

    for ticket in tickets:
        seen_ticket_ids.add(ticket.id)
        try:
            process_ticket(
                ticket,
                glpi=glpi,
                ad_conn=ad_conn,
                    ad_config=ad_config,
                    apply=apply,
                tz_name=tz_name,
            )
        except Exception as e:
            print(f"Erro ao processar chamado {ticket.id}: {e}", file=sys.stderr)
            if apply:
                try:
                    glpi.add_followup(ticket.id, build_processing_error_note(ticket, e))
                    print("Nota adicionada no GLPI informando falha no processamento.")
                except Exception as followup_error:
                    print(
                        f"Erro ao adicionar nota no chamado {ticket.id}: {followup_error}",
                        file=sys.stderr,
                    )
            else:
                print("DRY-RUN: uma nota seria adicionada no GLPI informando falha no processamento.")

    sys.stdout.flush()
    sys.stderr.flush()


def main() -> int:
    auto.load_env_file()

    parser = argparse.ArgumentParser(
        description=(
            "Busca chamados GLPI de Acesso Expirado Rede/VPN ou Internet "
            "e renova accountExpires no AD pela regra de 3 meses."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Quantidade máxima de chamados recentes para avaliar. Padrão: 20.",
    )
    parser.add_argument(
        "--ticket-id",
        type=int,
        help="Processa um chamado específico em vez de pesquisar a fila.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica a renovação no AD. Sem isso, roda em dry-run.",
    )
    parser.add_argument(
        "--tz",
        default="America/Sao_Paulo",
        help="Timezone usado para regra de data. Padrão: America/Sao_Paulo.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Mostra detalhes HTTP das chamadas ao GLPI.",
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help="Roda continuamente, consultando a fila em intervalos.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Intervalo em segundos entre ciclos no modo --poll. Padrão: 60.",
    )
    parser.add_argument(
        "--repeat-seen",
        action="store_true",
        help="No modo --poll, reprocessa chamados já vistos na mesma execução.",
    )

    args = parser.parse_args()

    glpi = GlpiClient(load_glpi_config(), debug=args.debug)

    try:
        glpi.init_session()

        ad_config = auto.load_config()
        if ad_config.expiry_attr != "accountExpires":
            raise RuntimeError(
                "Este fluxo só altera accountExpires. "
                f"AD_EXPIRY_ATTR atual: {ad_config.expiry_attr}."
            )

        ad_conn = auto.connect_ad(ad_config)

        seen_ticket_ids: set[int] = set()
        while True:
            run_cycle(
                glpi=glpi,
                ad_conn=ad_conn,
                ad_config=ad_config,
                ticket_id=args.ticket_id,
                limit=args.limit,
                apply=args.apply,
                tz_name=args.tz,
                seen_ticket_ids=seen_ticket_ids,
                repeat_seen=args.repeat_seen or not args.poll,
            )

            if not args.poll:
                break

            time.sleep(args.interval)

        return 0
    except LDAPException as e:
        print(f"Erro LDAP/AD: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            glpi.kill_session()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
