#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import ad_directory as ad
import report_storage
from glpi_client import GlpiClient, load_config as load_glpi_config
from reset_vpn import (
    SKYONE_LOGIN_LABEL,
    extract_field_value,
    load_ticket_requester_logins,
    normalize_login,
    normalized_ticket_lines,
)


SKYONE_FORM_URL = "https://suporte.ablprime.com.br/plugins/formcreator/front/formdisplay.php?id=44"
SKYONE_FORM_ID = 44
SKYONE_LOGIN_PREFIX = "abl."
SOLVED_STATUS = 5
DEFAULT_TICKET_STATUSES = ("2",)
ACTIVE_TICKET_STATUSES = ("1", "2")
DEFAULT_ATTACHMENT_PATH = Path(__file__).resolve().parent / "files" / "Reset de senha da Skyone.pdf"

SKYONE_SOLUTION = """Segue em anexo o passo a passo para realizar a redefinição do seu acesso à plataforma da Skyone.

🔄 PROCEDIMENTO DE ATUALIZAÇÃO:

Arquivo em Anexo: Tutorial_Reset_Senha_Skyone.pdf (ou o formato que você enviar)

🛑 INFORMAÇÕES IMPORTANTES:

Siga o Tutorial: Por favor, abra o arquivo anexo e siga as instruções corretamente e na ordem indicada para que a renovação do seu acesso seja concluída com sucesso.

Evite Bloqueios: Realizar o procedimento fora do padrão do tutorial pode bloquear temporariamente o seu usuário no sistema.

Precisa de Ajuda? Caso encontre qualquer erro durante o processo, responda diretamente a este chamado com o print da tela."""


@dataclass
class SkyoneTicket:
    id: int
    name: str
    status: int | None
    login: str
    content: str
    requester_logins: tuple[str, ...]


def extract_skyone_login_from_ticket_content(content: str) -> str | None:
    lines = normalized_ticket_lines(content)
    return extract_field_value(lines, SKYONE_LOGIN_LABEL)


def is_skyone_ticket(ticket: dict[str, Any], content: str) -> bool:
    name = str(ticket.get("name") or "")
    text = "\n".join(normalized_ticket_lines(content))
    return "skyone" in name.lower() or SKYONE_LOGIN_LABEL.lower() in text.lower()


def search_skyone_ticket_ids(
    client: GlpiClient,
    *,
    limit: int,
    statuses: tuple[str, ...] = DEFAULT_TICKET_STATUSES,
) -> list[int]:
    return client.search_formcreator_ticket_ids(
        form_id=SKYONE_FORM_ID,
        limit=limit,
        statuses=statuses,
    )


def build_skyone_ticket(
    glpi: GlpiClient,
    ticket_id: int,
    *,
    statuses: tuple[str, ...] = (),
) -> SkyoneTicket | None:
    ticket_data = glpi.get_item("Ticket", ticket_id)
    content = str(ticket_data.get("content") or "")
    if statuses and str(ticket_data.get("status", "")) not in statuses:
        return None
    if not is_skyone_ticket(ticket_data, content):
        return None

    login = extract_skyone_login_from_ticket_content(content)
    if not login:
        return None

    return SkyoneTicket(
        id=ticket_id,
        name=str(ticket_data.get("name") or ""),
        status=ticket_data.get("status"),
        login=login,
        content=content,
        requester_logins=load_ticket_requester_logins(glpi, ticket_id, ticket_data),
    )


def load_skyone_tickets(
    glpi: GlpiClient,
    *,
    limit: int,
    statuses: tuple[str, ...] = DEFAULT_TICKET_STATUSES,
) -> list[SkyoneTicket]:
    tickets: list[SkyoneTicket] = []
    for ticket_id in search_skyone_ticket_ids(glpi, limit=limit, statuses=statuses):
        ticket = build_skyone_ticket(glpi, ticket_id, statuses=statuses)
        if ticket is not None:
            tickets.append(ticket)
    return tickets


def load_tickets_for_args(
    glpi: GlpiClient,
    *,
    ticket_id: int | None,
    limit: int,
    statuses: tuple[str, ...] = DEFAULT_TICKET_STATUSES,
) -> list[SkyoneTicket]:
    if ticket_id:
        ticket = build_skyone_ticket(glpi, ticket_id, statuses=statuses)
        if ticket is None:
            raise RuntimeError(f"Chamado {ticket_id} não é um chamado Skyone elegível ou não possui login.")
        return [ticket]

    return load_skyone_tickets(glpi, limit=limit, statuses=statuses)


def build_processing_error_note(ticket: SkyoneTicket, error: Exception) -> str:
    return (
        "Não foi possível processar automaticamente este chamado Skyone.\n\n"
        f"Motivo: {error}\n\n"
        f"Login informado no chamado: {ticket.login}\n"
        "Verifique se o login foi preenchido corretamente e se o usuário existe no GLPI."
    )


def normalize_skyone_login(value: str | None) -> str:
    return (value or "").strip().lower()


def expected_skyone_logins(ticket: SkyoneTicket) -> set[str]:
    expected: set[str] = set()
    for requester_login in ticket.requester_logins:
        normalized = normalize_login(requester_login)
        if not normalized:
            continue
        expected.add(f"{SKYONE_LOGIN_PREFIX}{normalized}")
        if normalized.startswith(SKYONE_LOGIN_PREFIX):
            expected.add(normalized)
    return expected


def requester_matches_skyone_login(ticket: SkyoneTicket) -> bool:
    return normalize_skyone_login(ticket.login) in expected_skyone_logins(ticket)


def build_skyone_login_mismatch_note(ticket: SkyoneTicket) -> str:
    requesters = ", ".join(ticket.requester_logins) or "não identificado"
    expected = ", ".join(sorted(expected_skyone_logins(ticket))) or "não identificado"
    return (
        "Não foi possível processar automaticamente este chamado Skyone porque "
        "o login informado não corresponde ao usuário que abriu o chamado.\n\n"
        f"Login da Skyone informado: {ticket.login}\n"
        f"Requerente(s) do chamado no GLPI: {requesters}\n"
        f"Login Skyone esperado: {expected}\n\n"
        "Abra um novo chamado usando o próprio usuário dono do acesso Skyone."
    )


def process_ticket(
    ticket: SkyoneTicket,
    *,
    glpi: GlpiClient,
    apply: bool,
    attachment_path: Path,
    db_conn=None,
) -> None:
    print(f"\n==== CHAMADO {ticket.id} ====")
    print(f"Título: {ticket.name}")
    print(f"Status GLPI: {ticket.status}")
    print(f"Login detectado: {ticket.login}")
    print(
        "Requerente(s) GLPI: "
        f"{', '.join(ticket.requester_logins) if ticket.requester_logins else 'não identificado'}"
    )

    expected = ", ".join(sorted(expected_skyone_logins(ticket))) or "não identificado"
    print(f"Login Skyone esperado: {expected}")

    if not requester_matches_skyone_login(ticket):
        note = build_skyone_login_mismatch_note(ticket)
        print("Deve responder?: NÃO")
        print("Motivo: Login da Skyone não corresponde ao requerente do chamado.")
        if not apply:
            print("DRY-RUN: uma nota seria adicionada no GLPI explicando o motivo.")
            print("DRY-RUN: o chamado seria solucionado no GLPI.")
            return

        glpi.add_followup(ticket.id, note)
        glpi.update_ticket(ticket.id, {"status": SOLVED_STATUS})
        if db_conn and apply:
            report_storage.mark_ticket_processed(
                db_conn,
                ticket_id=ticket.id,
                action="skyone_login_mismatch",
                login=ticket.login,
                note=note,
            )
        print("Nota adicionada no GLPI explicando o motivo.")
        print("Chamado solucionado no GLPI.")
        return

    if not apply:
        print("DRY-RUN: a resposta Skyone seria adicionada.")
        print(f"DRY-RUN: o anexo seria enviado: {attachment_path}")
        print("DRY-RUN: o chamado seria solucionado no GLPI.")
        return

    glpi.add_document_to_ticket(ticket.id, attachment_path, name="Tutorial_Reset_Senha_Skyone.pdf")
    glpi.add_followup(ticket.id, SKYONE_SOLUTION)
    glpi.update_ticket(ticket.id, {"status": SOLVED_STATUS})
    if db_conn and apply:
        report_storage.mark_ticket_processed(
            db_conn,
            ticket_id=ticket.id,
            action="skyone_answered",
            login=ticket.login,
            note=SKYONE_SOLUTION,
        )
    print("Anexo enviado e resposta adicionada no GLPI.")
    print("Chamado solucionado no GLPI.")


def run_cycle(
    *,
    glpi: GlpiClient,
    ticket_id: int | None,
    limit: int,
    statuses: tuple[str, ...],
    apply: bool,
    attachment_path: Path,
    seen_ticket_ids: set[int],
    repeat_seen: bool,
    db_conn=None,
) -> None:
    print(f"\n---- CICLO {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ----")
    tickets = load_tickets_for_args(glpi, ticket_id=ticket_id, limit=limit, statuses=statuses)

    if not repeat_seen:
        tickets = [ticket for ticket in tickets if ticket.id not in seen_ticket_ids]

    if db_conn and apply:
        tickets = [t for t in tickets if not report_storage.was_ticket_processed(db_conn, t.id)]

    print(f"Chamados Skyone elegíveis encontrados: {len(tickets)}")

    for ticket in tickets:
        seen_ticket_ids.add(ticket.id)
        try:
            process_ticket(
                ticket,
                glpi=glpi,
                apply=apply,
                attachment_path=attachment_path,
                db_conn=db_conn,
            )
        except Exception as e:
            print(f"Erro ao processar chamado {ticket.id}: {e}", file=sys.stderr)
            if apply:
                try:
                    glpi.add_followup(ticket.id, build_processing_error_note(ticket, e))
                    glpi.update_ticket(ticket.id, {"status": SOLVED_STATUS})
                    print("Nota adicionada no GLPI informando falha no processamento.")
                    print("Chamado solucionado no GLPI.")
                except Exception as followup_error:
                    print(
                        f"Erro ao adicionar nota no chamado {ticket.id}: {followup_error}",
                        file=sys.stderr,
                    )
            else:
                print("DRY-RUN: uma nota seria adicionada no GLPI informando falha no processamento.")

    sys.stdout.flush()
    sys.stderr.flush()


def run_glpi_only(
    *,
    glpi: GlpiClient,
    ticket_id: int | None,
    limit: int,
    statuses: tuple[str, ...],
) -> None:
    tickets = load_tickets_for_args(glpi, ticket_id=ticket_id, limit=limit, statuses=statuses)
    print(f"Chamados Skyone elegíveis encontrados no GLPI: {len(tickets)}")
    for ticket in tickets:
        requesters = ", ".join(ticket.requester_logins) or "não identificado"
        print(f"\n==== CHAMADO {ticket.id} ====")
        print(f"Título: {ticket.name}")
        print(f"Status GLPI: {ticket.status}")
        print(f"Login detectado: {ticket.login}")
        print(f"Requerente(s) GLPI: {requesters}")


def parse_statuses(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def main(argv: list[str] | None = None) -> int:
    ad.load_env_file()

    parser = argparse.ArgumentParser(
        description="Busca chamados Skyone no GLPI, envia tutorial de reset e soluciona o chamado."
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
        help="Aplica a resposta/anexo no GLPI. Sem isso, roda em dry-run.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Mostra detalhes HTTP das chamadas ao GLPI.",
    )
    parser.add_argument(
        "--glpi-only",
        action="store_true",
        help="Lista chamados Skyone elegíveis no GLPI sem responder.",
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
    parser.add_argument(
        "--statuses",
        default=",".join(DEFAULT_TICKET_STATUSES),
        help="Status GLPI separados por vírgula usados na busca. Padrão: 2.",
    )
    parser.add_argument(
        "--attachment",
        default=str(DEFAULT_ATTACHMENT_PATH),
        help="PDF que será anexado ao chamado.",
    )
    parser.add_argument(
        "--db-path",
        default="relatorio.db",
        help="Arquivo SQLite para histórico de ações. Padrão: relatorio.db.",
    )

    args = parser.parse_args(argv)
    statuses = parse_statuses(args.statuses)
    attachment_path = Path(args.attachment)

    db_conn = report_storage.connect(args.db_path)
    report_storage.initialize(db_conn)

    glpi = GlpiClient(load_glpi_config(), debug=args.debug)

    try:
        if args.debug:
            print("DEBUG iniciando sessão GLPI", flush=True)
        glpi.init_session()
        if args.debug:
            print("DEBUG sessão GLPI iniciada", flush=True)

        if args.glpi_only:
            run_glpi_only(
                glpi=glpi,
                ticket_id=args.ticket_id,
                limit=args.limit,
                statuses=statuses,
            )
            return 0

        seen_ticket_ids: set[int] = set()
        while True:
            run_cycle(
                glpi=glpi,
                ticket_id=args.ticket_id,
                limit=args.limit,
                statuses=statuses,
                apply=args.apply,
                attachment_path=attachment_path,
                seen_ticket_ids=seen_ticket_ids,
                repeat_seen=args.repeat_seen or not args.poll,
                db_conn=db_conn,
            )

            if not args.poll:
                break

            time.sleep(args.interval)

        return 0
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            glpi.kill_session()
        except Exception:
            pass
        try:
            db_conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
