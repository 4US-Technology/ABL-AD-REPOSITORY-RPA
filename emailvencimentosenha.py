#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import ad_directory as ad
import report_storage


FORM_URL = "https://suporte.ablprime.com.br/plugins/formcreator/front/formdisplay.php?id=15"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str | None
    password: str | None
    use_tls: bool
    use_ssl: bool


@dataclass
class GraphConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    sender: str
    save_to_sent_items: bool


@dataclass
class ExpiringUser:
    login: str
    name: str
    email: str | None
    expiry: datetime
    dn: str


def load_smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host=ad.env_required("SMTP_HOST"),
        port=int(ad.env_required("SMTP_PORT")),
        user=(ad.env_required("SMTP_USER") if ad.os.getenv("SMTP_USER") else None),
        password=(
            ad.env_required("SMTP_PASSWORD") if ad.os.getenv("SMTP_PASSWORD") else None
        ),
        use_tls=ad.os.getenv("SMTP_USE_TLS", "true").lower() == "true",
        use_ssl=ad.os.getenv("SMTP_USE_SSL", "false").lower() == "true",
    )


def load_graph_config() -> GraphConfig:
    return GraphConfig(
        tenant_id=ad.env_required("GRAPH_TENANT_ID"),
        client_id=ad.env_required("GRAPH_CLIENT_ID"),
        client_secret=ad.env_required("GRAPH_CLIENT_SECRET"),
        sender=ad.env_required("GRAPH_SENDER"),
        save_to_sent_items=ad.os.getenv("GRAPH_SAVE_TO_SENT_ITEMS", "false").lower()
        == "true",
    )


def find_expiring_users(
    conn,
    config: ad.AdConfig,
    *,
    days: int,
    from_date: datetime | None,
) -> list[ExpiringUser]:
    now_utc = datetime.now(timezone.utc)
    start_utc = from_date.astimezone(timezone.utc) if from_date else now_utc
    deadline_utc = now_utc + timedelta(days=days)

    ok = conn.search(
        search_base=config.base_dn,
        search_filter="(&(objectClass=user)(!(objectClass=computer)))",
        search_scope=ad.SUBTREE,
        attributes=[
            "cn",
            "displayName",
            "mail",
            "sAMAccountName",
            "userPrincipalName",
            config.expiry_attr,
        ],
    )
    if not ok:
        raise RuntimeError(f"Falha ao consultar usuários no AD: {conn.result}")

    users: list[ExpiringUser] = []
    for entry in conn.entries:
        login = getattr(entry, "sAMAccountName").value
        if not login:
            continue

        expiry = ad.filetime_to_datetime(getattr(entry, config.expiry_attr).value)
        if expiry is None:
            continue

        if expiry < start_utc or expiry > deadline_utc:
            continue

        name = (
            getattr(entry, "displayName").value
            or getattr(entry, "cn").value
            or login
        )
        email = getattr(entry, "mail").value

        users.append(
            ExpiringUser(
                login=str(login),
                name=str(name),
                email=str(email) if email else None,
                expiry=expiry,
                dn=str(entry.entry_dn),
            )
        )

    users.sort(key=lambda user: user.expiry)
    return users


def load_user_by_login(
    conn,
    config: ad.AdConfig,
    *,
    login: str,
) -> ExpiringUser:
    entry = ad.find_user(conn, config, login)
    expiry = ad.filetime_to_datetime(getattr(entry, config.expiry_attr).value)
    if expiry is None:
        raise RuntimeError(f"O usuário {login} não possui accountExpires com data definida.")

    name = (
        getattr(entry, "displayName").value
        or getattr(entry, "cn").value
        or login
    )
    email = getattr(entry, "mail").value

    return ExpiringUser(
        login=str(getattr(entry, "sAMAccountName").value or login),
        name=str(name),
        email=str(email) if email else None,
        expiry=expiry,
        dn=str(entry.entry_dn),
    )


def build_message(
    smtp_config: SmtpConfig,
    user: ExpiringUser,
    *,
    tz_name: str,
) -> EmailMessage:
    expiry_text = ad.format_dt(user.expiry, tz_name)

    text = (
        f"Olá, {user.name}.\n\n"
        "Seu acesso de Rede/VPN ou Internet está próximo do vencimento.\n"
        f"Expiração atual: {expiry_text}\n\n"
        "Para solicitar a renovação, abra o formulário abaixo:\n"
        f"{FORM_URL}\n\n"
        "Preencha os campos:\n"
        f"- Nome Completo do usuário\n"
        f"- Login da VPN / Internet\n\n"
        "Login sugerido para o formulário:\n"
        f"{user.login}\n"
    )

    html = (
        f"<p>Olá, {user.name}.</p>"
        "<p>Seu acesso de Rede/VPN ou Internet está próximo do vencimento.</p>"
        f"<p><strong>Expiração atual:</strong> {expiry_text}</p>"
        "<p>Para solicitar a renovação, abra o formulário abaixo:</p>"
        f'<p><a href="{FORM_URL}">{FORM_URL}</a></p>'
        "<p>Preencha os campos:</p>"
        "<ul>"
        "<li>Nome Completo do usuário</li>"
        "<li>Login da VPN / Internet</li>"
        "</ul>"
        f"<p><strong>Login sugerido para o formulário:</strong><br>{user.login}</p>"
    )

    msg = EmailMessage()
    sender_email = smtp_config.user or ad.env_required("SMTP_USER")
    msg["From"] = sender_email
    msg["To"] = user.email
    msg["Subject"] = "Aviso de vencimento do acesso de Rede/VPN ou Internet"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def build_graph_message_payload(
    user: ExpiringUser,
    *,
    tz_name: str,
    save_to_sent_items: bool,
) -> dict[str, Any]:
    expiry_text = ad.format_dt(user.expiry, tz_name)
    html = (
        f"<p>Olá, {user.name}.</p>"
        "<p>Seu acesso de Rede/VPN ou Internet está próximo do vencimento.</p>"
        f"<p><strong>Expiração atual:</strong> {expiry_text}</p>"
        "<p>Para solicitar a renovação, abra o formulário abaixo:</p>"
        f'<p><a href="{FORM_URL}">{FORM_URL}</a></p>'
        "<p>Preencha os campos:</p>"
        "<ul>"
        "<li>Nome Completo do usuário</li>"
        "<li>Login da VPN / Internet</li>"
        "</ul>"
        f"<p><strong>Login sugerido para o formulário:</strong><br>{user.login}</p>"
    )
    return {
        "message": {
            "subject": "Aviso de vencimento do acesso de Rede/VPN ou Internet",
            "body": {
                "contentType": "HTML",
                "content": html,
            },
            "toRecipients": [
                {
                    "emailAddress": {
                        "address": user.email,
                    }
                }
            ],
        },
        "saveToSentItems": save_to_sent_items,
    }


def graph_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    data = None
    request_headers = headers or {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers = {
            "Content-Type": "application/json",
            **request_headers,
        }

    req = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return None
            return json.loads(raw)
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} em {url}: {detail}") from e
    except URLError as e:
        raise RuntimeError(f"Falha de conexão em {url}: {e}") from e


def get_graph_token(config: GraphConfig) -> str:
    token_url = (
        f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/token"
    )
    data = urlencode(
        {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "scope": GRAPH_SCOPE,
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")
    req = Request(
        token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Falha ao obter token Graph: HTTP {e.code}: {detail}") from e
    except URLError as e:
        raise RuntimeError(f"Falha ao obter token Graph: {e}") from e

    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Graph não retornou access_token: {payload}")
    return str(token)


def test_graph(config: GraphConfig) -> None:
    get_graph_token(config)
    print(f"Graph OK: token obtido para sender={config.sender}")


def send_graph_email(config: GraphConfig, user: ExpiringUser, *, tz_name: str) -> None:
    token = get_graph_token(config)
    url = f"https://graph.microsoft.com/v1.0/users/{config.sender}/sendMail"
    payload = build_graph_message_payload(
        user,
        tz_name=tz_name,
        save_to_sent_items=config.save_to_sent_items,
    )
    graph_request(
        "POST",
        url,
        headers={"Authorization": f"Bearer {token}"},
        body=payload,
    )


def open_smtp(smtp_config: SmtpConfig):
    if smtp_config.use_ssl:
        context = ssl.create_default_context()
        server = smtplib.SMTP_SSL(smtp_config.host, smtp_config.port, context=context)
    else:
        server = smtplib.SMTP(smtp_config.host, smtp_config.port)
        if smtp_config.use_tls:
            context = ssl.create_default_context()
            server.starttls(context=context)

    if smtp_config.user and smtp_config.password:
        server.login(smtp_config.user, smtp_config.password)

    return server


def test_smtp(smtp_config: SmtpConfig) -> None:
    with open_smtp(smtp_config) as server:
        noop_code, noop_message = server.noop()
        print(f"SMTP OK: host={smtp_config.host} port={smtp_config.port} noop={noop_code} {noop_message!r}")


def send_email(smtp_config: SmtpConfig, message: EmailMessage) -> None:
    with open_smtp(smtp_config) as server:
        server.send_message(message)


def user_expiry_key(user: ExpiringUser) -> str:
    return user.expiry.astimezone(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    ad.load_env_file()

    parser = argparse.ArgumentParser(
        description=(
            "Envia aviso por e-mail para usuários cujo acesso de Rede/VPN ou "
            "Internet vence em até N dias."
        )
    )
    parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="Quantidade de dias antes do vencimento. Padrão: 3.",
    )
    parser.add_argument(
        "--login",
        help="Processa apenas um login específico.",
    )
    parser.add_argument(
        "--force-expired",
        action="store_true",
        help="Permite enviar teste mesmo se accountExpires já estiver vencido.",
    )
    parser.add_argument(
        "--from-date",
        help="Data inicial no formato YYYY-MM-DD para incluir vencimentos passados desde essa data.",
    )
    parser.add_argument(
        "--tz",
        default="America/Sao_Paulo",
        help="Timezone para exibição de datas. Padrão: America/Sao_Paulo.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Envia os e-mails. Sem isso, roda em dry-run.",
    )
    parser.add_argument(
        "--smtp-test",
        action="store_true",
        help="Testa apenas conexão e autenticação SMTP.",
    )
    parser.add_argument(
        "--graph-test",
        action="store_true",
        help="Testa apenas autenticação Microsoft Graph.",
    )
    parser.add_argument(
        "--db-path",
        default="relatorio.db",
        help="SQLite usado para controlar avisos já enviados. Padrão: relatorio.db.",
    )
    parser.add_argument(
        "--ignore-sent",
        action="store_true",
        help="Ignora o controle de avisos já enviados no SQLite.",
    )

    args = parser.parse_args(argv)

    try:
        if args.smtp_test:
            test_smtp(load_smtp_config())
            return 0
        if args.graph_test:
            test_graph(load_graph_config())
            return 0

        storage_conn = report_storage.connect(args.db_path)
        report_storage.initialize(storage_conn)

        ad_config = ad.load_config()
        conn = ad.connect_ad(ad_config)
        from_date = None
        if args.from_date:
            tz = ZoneInfo(args.tz)
            from_date = datetime.strptime(args.from_date, "%Y-%m-%d").replace(tzinfo=tz)

        if args.login:
            users = [load_user_by_login(conn, ad_config, login=args.login)]
        else:
            users = find_expiring_users(
                conn,
                ad_config,
                days=args.days,
                from_date=from_date,
            )

        print(f"Usuários encontrados para aviso: {len(users)}")
        if not users:
            return 0

        email_provider = ad.os.getenv("EMAIL_PROVIDER", "smtp").lower()
        smtp_config = None
        graph_config = None
        if args.apply:
            if email_provider == "graph":
                graph_config = load_graph_config()
            else:
                smtp_config = load_smtp_config()

        for user in users:
            print(f"\n==== USUÁRIO {user.login} ====")
            print(f"Nome: {user.name}")
            print(f"DN: {user.dn}")
            print(f"E-mail: {user.email or 'Sem e-mail no AD'}")
            print(f"Expiração atual: {ad.format_dt(user.expiry, args.tz)}")
            print(f"Formulário GLPI: {FORM_URL}")

            if user.expiry <= datetime.now(timezone.utc) and not args.force_expired:
                print("SKIP: conta já vencida; aviso preventivo não será enviado.")
                continue

            if not user.email:
                print("SKIP: usuário sem atributo mail no AD.")
                continue

            # Evita reenvio para o mesmo login enquanto a mesma expiração estiver vigente.
            expiry_key = user_expiry_key(user)
            if not args.ignore_sent and report_storage.was_email_sent(
                storage_conn,
                login=user.login,
                expiry_utc=expiry_key,
            ):
                print("SKIP: aviso já enviado para esta expiração.")
                continue

            if not args.apply:
                print("DRY-RUN: o e-mail seria enviado.")
                continue

            if email_provider == "graph":
                send_graph_email(graph_config, user, tz_name=args.tz)
            else:
                message = build_message(smtp_config, user, tz_name=args.tz)
                send_email(smtp_config, message)
            report_storage.mark_email_sent(
                storage_conn,
                login=user.login,
                expiry_utc=expiry_key,
                email=user.email,
            )
            print("E-mail enviado com sucesso.")

        return 0
    except ad.LDAPException as e:
        print(f"Erro LDAP/AD: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
