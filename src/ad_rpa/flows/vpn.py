from __future__ import annotations
import argparse
import html
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from ldap3.core.exceptions import LDAPException
from ..integrations import ad
from ..integrations.glpi import GlpiClient, TransientGlpiError, load_config as load_glpi_config
from ..storage import database
TARGET_TICKET_NAMES = (
    "Acesso Expirado",
    "Acesso Expirado Rede/VPN ou Internet",
    "Renovação de acesso de rede",
    "Renovação de acesso rede e Internet",
)
REQUEST_TYPE_LABEL = "Tipo de solicitação"
LOGIN_LABEL = "Login da Rede/VPN ou Internet"
VPN_LOGIN_LABELS = (
    LOGIN_LABEL,
    "Login da VPN / Internet",
    "Usuário(Login) da VPN / Internet",
)
FORM_URLS = (
    "https://suporte.ablprime.com.br/plugins/formcreator/front/formdisplay.php?id=46",
)
RESET_FORM_ID = 46
SOLVED_STATUS = 6
DEFAULT_TICKET_STATUSES = ("1", "2")
ACTIVE_TICKET_STATUSES = ("1", "2")
@dataclass
class VpnResetTicket:
    id: int
    name: str
    status: int | None
    request_type: str | None
    login: str
    content: str
    requester_logins: tuple[str, ...]
def normalize_login(value: str | None) -> str:
    login = (value or "").strip().lower()
    if "\\" in login:
        login = login.rsplit("\\", 1)[-1]
    if "@" in login:
        login = login.split("@", 1)[0]
    return login
def extract_id(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("id", "value"):
            found = extract_id(value.get(key))
            if found is not None:
                return found
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
def glpi_list_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("data", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []
def load_ticket_requester_logins(
    client: GlpiClient,
    ticket_id: int,
    ticket_data: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    requester_user_ids: list[int] = []
    try:
        ticket_users = glpi_list_items(client.request("GET", f"Ticket/{ticket_id}/Ticket_User"))
    except Exception:
        ticket_users = []
    for ticket_user in ticket_users:
        user_type = extract_id(ticket_user.get("type"))
        if user_type != 1:
            continue
        user_id = extract_id(ticket_user.get("users_id"))
        if user_id is not None:
            requester_user_ids.append(user_id)
    if not requester_user_ids and ticket_data:
        fallback_user_id = extract_id(ticket_data.get("users_id_recipient"))
        if fallback_user_id is not None:
            requester_user_ids.append(fallback_user_id)
    requester_logins: list[str] = []
    seen_user_ids: set[int] = set()
    for user_id in requester_user_ids:
        if user_id in seen_user_ids:
            continue
        seen_user_ids.add(user_id)
        user = client.get_item("User", user_id)
        for field in ("name", "user_name", "login"):
            login = str(user.get(field) or "").strip()
            if login:
                requester_logins.append(login)
                break
        try:
            user_emails = glpi_list_items(
                client.request("GET", f"User/{user_id}/UserEmail")
            )
            for ue in user_emails:
                email = str(ue.get("email") or "").strip()
                if email:
                    requester_logins.append(email)
        except Exception:
            pass
    return tuple(requester_logins)
def requester_matches_login(ticket: VpnResetTicket) -> bool:
    requested_login = normalize_login(ticket.login)
    requester_logins = {normalize_login(login) for login in ticket.requester_logins}
    requester_logins.discard("")
    return bool(requested_login and requested_login in requester_logins)
def build_requester_mismatch_note(ticket: VpnResetTicket) -> str:
    requesters = ", ".join(ticket.requester_logins) or "não identificado"
    return (
        "Não foi possível renovar automaticamente porque o chamado deve ser aberto "
        "pelo próprio dono do login de acesso.\n\n"
        f"Login informado no formulário: {ticket.login}\n"
        f"Requerente(s) do chamado no GLPI: {requesters}\n\n"
        "Abra um novo chamado usando o mesmo usuário dono do login que precisa ser renovado."
    )
def build_non_renewal_reason(decision: ad.Decision) -> str:
    if not decision.is_expired and decision.current_expiry is not None:
        return (
            "Não foi possível renovar automaticamente porque o acesso "
            f"não está expirado e o vencimento é superior a {ad.RENEWAL_WINDOW_DAYS} dias."
        )
    return f"Não foi possível renovar automaticamente. Motivo: {decision.reason}"
def build_non_renewal_options(decision: ad.Decision) -> str:
    if not decision.is_expired and decision.current_expiry is not None:
        return (
            "\n\nOpções:\n"
            "- Abrir chamado de configuração/manutenção de máquina.\n"
            "- Validar conexão, VPN, internet e perfil local do computador.\n"
            f"- Abrir novo chamado de renovação quando o acesso estiver expirado ou faltarem até {ad.RENEWAL_WINDOW_DAYS} dias para o vencimento."
        )
    return ""
def try_update_ticket_status(glpi: GlpiClient, ticket_id: int, status: int) -> None:
    try:
        glpi.update_ticket(ticket_id, {"status": status})
    except Exception as e:
        print(f"Aviso: não foi possível alterar status do chamado {ticket_id}: {e}")
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
def normalized_ticket_lines(content: str) -> list[str]:
    text = strip_html(content)
    return [line.strip() for line in text.splitlines() if line.strip()]
def extract_field_value(lines: list[str], label: str) -> str | None:
    for index, line in enumerate(lines):
        if line != label:
            continue
        for candidate in lines[index + 1 :]:
            if candidate and candidate not in {"Descrição", "Prints do erro de Rede / VPN ou Acesso de internet"}:
                return candidate
    return None
def extract_request_type_from_ticket_content(content: str) -> str | None:
    lines = normalized_ticket_lines(content)
    return extract_field_value(lines, REQUEST_TYPE_LABEL)
def extract_login_from_ticket_content(content: str) -> str | None:
    text = strip_html(content)
    lines = normalized_ticket_lines(content)
    preferred_labels = VPN_LOGIN_LABELS
    for label in preferred_labels:
        value = extract_field_value(lines, label)
        if value:
            return value
    match = re.search(
        r"(?:Login da Rede/VPN ou Internet|Login da VPN / Internet|Usuário\(Login\) da VPN / Internet)\s+([A-Za-z0-9._-]+)",
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
def search_vpn_reset_ticket_ids(
    client: GlpiClient,
    *,
    limit: int,
    statuses: tuple[str, ...] = DEFAULT_TICKET_STATUSES,
) -> list[int]:
    return client.search_formcreator_ticket_ids(
        form_id=RESET_FORM_ID,
        limit=limit,
        statuses=statuses,
    )
def load_vpn_reset_tickets(
    client: GlpiClient,
    *,
    limit: int,
    statuses: tuple[str, ...] = DEFAULT_TICKET_STATUSES,
) -> list[VpnResetTicket]:
    tickets: list[VpnResetTicket] = []
    for ticket_id in search_vpn_reset_ticket_ids(client, limit=limit, statuses=statuses):
        ticket = client.get_item("Ticket", ticket_id)
        name = str(ticket.get("name") or "")
        content = str(ticket.get("content") or "")
        login = extract_login_from_ticket_content(content)
        request_type = extract_request_type_from_ticket_content(content)
        if name not in TARGET_TICKET_NAMES:
            continue
        if statuses and str(ticket.get("status", "")) not in statuses:
            continue
        if not login:
            continue
        tickets.append(
            VpnResetTicket(
                id=ticket_id,
                name=name,
                status=ticket.get("status"),
                request_type=request_type,
                login=login,
                content=content,
                requester_logins=load_ticket_requester_logins(client, ticket_id, ticket),
            )
        )
    return tickets
def _expiry_value(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None
def _finish_ticket(ticket: VpnResetTicket, glpi: GlpiClient, db_conn, *, solution: bool, action: str) -> None:
    """Executa somente o trecho GLPI ainda pendente; seguro após queda ambígua."""
    state = database.get_vpn_state(db_conn, ticket.id)
    if state is None:
        raise RuntimeError("Estado VPN ausente ao finalizar chamado.")
    message = str(state["glpi_message"] or "")
    if state["stage"] == "message_pending":
        if not glpi.ticket_has_message(ticket.id, message, solution=solution):
            (glpi.add_solution if solution else glpi.add_followup)(ticket.id, message)
        database.save_vpn_state(db_conn, ticket.id, "close_pending", ticket.login,
                                      state["planned_expiry"], message)
    state = database.get_vpn_state(db_conn, ticket.id)
    if state and state["stage"] == "close_pending":
        glpi.update_ticket(ticket.id, {"status": SOLVED_STATUS})
        current = glpi.get_item("Ticket", ticket.id)
        if str(current.get("status")) != str(SOLVED_STATUS):
            raise RuntimeError(f"GLPI não confirmou o fechamento do chamado {ticket.id}.")
        database.save_vpn_state(db_conn, ticket.id, "completed", ticket.login,
                                      state["planned_expiry"], message)
        database.mark_ticket_processed(db_conn, ticket_id=ticket.id, action=action,
                                             login=ticket.login, note=message)
def _queue_message(ticket: VpnResetTicket, db_conn, message: str, *, planned: str | None = None) -> None:
    database.save_vpn_state(db_conn, ticket.id, "message_pending", ticket.login, planned, message)
def process_ticket(
    ticket: VpnResetTicket,
    *,
    glpi: GlpiClient,
    ad_conn,
    ad_config: ad.AdConfig,
    apply: bool,
    tz_name: str,
    db_conn=None,
) -> None:
    print(f"\n==== CHAMADO {ticket.id} ====")
    print(f"Título: {ticket.name}")
    print(f"Status GLPI: {ticket.status}")
    print(f"Tipo de solicitação: {ticket.request_type or 'não identificado'}")
    print(f"Login detectado: {ticket.login}")
    print(
        "Requerente(s) GLPI: "
        f"{', '.join(ticket.requester_logins) if ticket.requester_logins else 'não identificado'}"
    )
    state = database.get_vpn_state(db_conn, ticket.id) if db_conn and apply else None
    if state and state["stage"] == "completed":
        if str(ticket.status) in ACTIVE_TICKET_STATUSES:
            database.clear_vpn_state(db_conn, ticket.id)
            state = None
        else:
            return
    if state and state["stage"] == "manual_review":
        print("Pendente de revisão manual: expiração no AD divergiu da intenção registrada.")
        return
    if not requester_matches_login(ticket):
        note = build_requester_mismatch_note(ticket)
        print("Deve renovar?: NÃO")
        print("Motivo: chamado aberto por usuário diferente do login informado.")
        if not apply:
            print("DRY-RUN: nenhuma alteração foi feita no AD.")
            print("DRY-RUN: uma nota seria adicionada no GLPI explicando o motivo.")
            return
        _queue_message(ticket, db_conn, note)
        _finish_ticket(ticket, glpi, db_conn, solution=False, action="requester_mismatch")
        print("Nota adicionada no GLPI explicando o motivo da não renovação.")
        print("Chamado solucionado no GLPI.")
        print("Nada a aplicar no AD.")
        return
    ad_login = normalize_login(ticket.login)
    original_email = ticket.login if "@" in ticket.login else None
    user = ad.find_user(ad_conn, ad_config, ad_login, email=original_email)
    real_login = str(getattr(user, "sAMAccountName").value or ad_login)
    decision = ad.build_decision(user, ad_config, real_login, tz_name)
    if state and state["stage"] in {"ad_pending", "message_pending", "close_pending"} and state["planned_expiry"]:
        if _expiry_value(decision.current_expiry) != state["planned_expiry"]:
            database.save_vpn_state(db_conn, ticket.id, "manual_review", ticket.login,
                                          state["planned_expiry"], state["glpi_message"])
            print("Divergência de expiração detectada; encaminhado para revisão manual.")
            return
        if state["stage"] == "ad_pending":
            database.save_vpn_state(db_conn, ticket.id, "message_pending", ticket.login,
                                          state["planned_expiry"], state["glpi_message"])
            state = database.get_vpn_state(db_conn, ticket.id)
        if state["stage"] in {"message_pending", "close_pending"}:
            _finish_ticket(ticket, glpi, db_conn, solution=True, action="renewed")
            print("Retomada do GLPI concluída sem reaplicar a renovação no AD.")
            return
    print(f"DN: {decision.dn}")
    print(f"Expiração atual: {ad.format_dt(decision.current_expiry, tz_name)}")
    print(f"Está expirado?: {'SIM' if decision.is_expired else 'NÃO'}")
    print(f"Deve renovar?: {'SIM' if decision.should_renew else 'NÃO'}")
    if decision.should_renew:
        print(f"Motivo: {decision.reason}")
    else:
        print(f"Motivo: {build_non_renewal_reason(decision)}")
    if decision.new_expiry:
        print(f"Nova expiração calculada: {ad.format_dt(decision.new_expiry, tz_name)}")
        print(f"Novo FILETIME: {ad.datetime_to_filetime(decision.new_expiry)}")
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
            f"Expiração atual: {ad.format_dt(decision.current_expiry, tz_name)}"
        )
        _queue_message(ticket, db_conn, note)
        _finish_ticket(ticket, glpi, db_conn, solution=False, action="not_expired")
        print("Nota adicionada no GLPI explicando o motivo da não renovação.")
        print("Chamado encerrado no GLPI (acesso não expirado).")
        print("Nada a aplicar no AD.")
        return
    solution = (
        "Acessos renovados por mais 3 meses.\n\n"
        f"Expira em: {decision.new_expiry.astimezone(ad.ZoneInfo(tz_name)).strftime('%d/%m/%Y')}.\n\n"
        "Obs.: O acesso expira a cada 3 meses sendo de responsabilidade do próprio "
        "usuário(a) pedir a renovação do seu acesso. Não serão atendidos chamados "
        "de renovação de senha que não seja do próprio dono do login de acesso."
    )
    planned = _expiry_value(decision.new_expiry)
    database.save_vpn_state(db_conn, ticket.id, "ad_pending", ticket.login, planned, solution)
    ad.apply_renewal(ad_conn, ad_config, decision)
    print("ALTERAÇÃO APLICADA NO AD COM SUCESSO.")
    _queue_message(ticket, db_conn, solution, planned=planned)
    _finish_ticket(ticket, glpi, db_conn, solution=True, action="renewed")
    print("Solução adicionada e chamado encerrado no GLPI.")
def load_tickets_for_args(
    glpi: GlpiClient,
    *,
    ticket_id: int | None,
    limit: int,
    statuses: tuple[str, ...] = DEFAULT_TICKET_STATUSES,
) -> list[VpnResetTicket]:
    if ticket_id:
        ticket_data = glpi.get_item("Ticket", ticket_id)
        content = str(ticket_data.get("content") or "")
        request_type = extract_request_type_from_ticket_content(content)
        login = extract_login_from_ticket_content(content)
        if not login:
            raise RuntimeError(f"Não encontrei login no chamado {ticket_id}.")
        return [
            VpnResetTicket(
                id=ticket_id,
                name=str(ticket_data.get("name") or ""),
                status=ticket_data.get("status"),
                request_type=request_type,
                login=login,
                content=content,
                requester_logins=load_ticket_requester_logins(glpi, ticket_id, ticket_data),
            )
        ]
    return load_vpn_reset_tickets(glpi, limit=limit, statuses=statuses)
def run_cycle(
    *,
    glpi: GlpiClient,
    ad_conn,
    ad_config: ad.AdConfig,
    ticket_id: int | None,
    limit: int,
    statuses: tuple[str, ...],
    apply: bool,
    tz_name: str,
    db_conn=None,
) -> None:
    print(f"\n---- CICLO {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ----")
    tickets = load_tickets_for_args(glpi, ticket_id=ticket_id, limit=limit, statuses=statuses)
    print(f"Chamados elegíveis encontrados: {len(tickets)}")
    for ticket in tickets:
        try:
            process_ticket(
                ticket,
                glpi=glpi,
                ad_conn=ad_conn,
                ad_config=ad_config,
                apply=apply,
                tz_name=tz_name,
                db_conn=db_conn,
            )
        except (TransientGlpiError, LDAPException, OSError):
            raise
        except Exception as e:
            print(f"Erro ao processar chamado {ticket.id}: {e}", file=sys.stderr)
            if apply:
                try:
                    note = build_processing_error_note(ticket, e)
                    _queue_message(ticket, db_conn, note)
                    _finish_ticket(ticket, glpi, db_conn, solution=False, action="error")
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
def run_glpi_only(
    *,
    glpi: GlpiClient,
    ticket_id: int | None,
    limit: int,
    statuses: tuple[str, ...],
) -> None:
    tickets = load_tickets_for_args(glpi, ticket_id=ticket_id, limit=limit, statuses=statuses)
    print(f"Chamados elegíveis encontrados no GLPI: {len(tickets)}")
    for ticket in tickets:
        requesters = ", ".join(ticket.requester_logins) or "não identificado"
        print(f"\n==== CHAMADO {ticket.id} ====")
        print(f"Título: {ticket.name}")
        print(f"Status GLPI: {ticket.status}")
        print(f"Tipo de solicitação: {ticket.request_type or 'não identificado'}")
        print(f"Login detectado: {ticket.login}")
        print(f"Requerente(s) GLPI: {requesters}")
def parse_statuses(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())
def main(argv: list[str] | None = None) -> int:
    ad.load_env_file()
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
        "--glpi-only",
        action="store_true",
        help="Lista chamados elegíveis no GLPI sem conectar no AD.",
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
        help="Obsoleto: tickets VPN ativos já são sempre reavaliados.",
    )
    parser.add_argument(
        "--statuses",
        default=",".join(DEFAULT_TICKET_STATUSES),
        help="Status GLPI separados por vírgula usados na busca. Padrão: 1,2.",
    )
    parser.add_argument(
        "--db-path",
        default="relatorio.db",
        help="Arquivo SQLite para histórico de ações. Padrão: relatorio.db.",
    )
    args = parser.parse_args(argv)
    statuses = parse_statuses(args.statuses)
    if args.repeat_seen:
        print("Aviso: --repeat-seen foi descontinuado; tickets VPN ativos são sempre reavaliados.", file=sys.stderr)
    db_conn = database.connect(args.db_path)
    database.initialize(db_conn)
    glpi_config = load_glpi_config()
    glpi = GlpiClient(glpi_config, debug=args.debug)
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
        if args.debug:
            print("DEBUG carregando configuração AD", flush=True)
        ad_config = ad.load_config()
        if ad_config.expiry_attr != "accountExpires":
            raise RuntimeError(
                "Este fluxo só altera accountExpires. "
                f"AD_EXPIRY_ATTR atual: {ad_config.expiry_attr}."
            )
        if args.debug:
            print(
                f"DEBUG conectando AD {ad_config.server}:{ad_config.port}",
                flush=True,
            )
        ad_conn = ad.connect_ad(ad_config)
        if args.debug:
            print("DEBUG AD conectado", flush=True)
        while True:
            try:
                run_cycle(glpi=glpi, ad_conn=ad_conn, ad_config=ad_config,
                          ticket_id=args.ticket_id, limit=args.limit, statuses=statuses,
                          apply=args.apply, tz_name=args.tz, db_conn=db_conn)
            except (TransientGlpiError, LDAPException, OSError) as e:
                if not args.poll:
                    raise
                print(f"Falha transitória: {e}. Reconectando no próximo ciclo.", file=sys.stderr)
                try:
                    glpi.kill_session()
                except Exception:
                    pass
                glpi = GlpiClient(glpi_config, debug=args.debug)
                try:
                    glpi.init_session()
                    ad_conn = ad.connect_ad(ad_config)
                except (TransientGlpiError, LDAPException, OSError) as reconnect_error:
                    print(f"Reconexão ainda indisponível: {reconnect_error}", file=sys.stderr)
                    time.sleep(args.interval)
                    continue
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
        try:
            db_conn.close()
        except Exception:
            pass
if __name__ == "__main__":
    raise SystemExit(main())
