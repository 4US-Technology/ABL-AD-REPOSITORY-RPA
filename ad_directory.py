from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ldap3 import ALL, MODIFY_REPLACE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars


FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


@dataclass
class AdConfig:
    server: str
    port: int
    use_ssl: bool
    use_starttls: bool
    tls_validate: bool
    bind_user: str
    bind_password: str
    base_dn: str
    search_attr: str
    expiry_attr: str


@dataclass
class Decision:
    login: str
    dn: str
    current_expiry: datetime | None
    is_expired: bool
    should_renew: bool
    new_expiry: datetime | None
    reason: str


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_required(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"Variável de ambiente obrigatória ausente: {name}")
    return value


def env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() == "true"


def load_config() -> AdConfig:
    return AdConfig(
        server=env_required("AD_SERVER"),
        port=int(os.getenv("AD_PORT", "389")),
        use_ssl=env_bool("AD_USE_SSL"),
        use_starttls=env_bool("AD_USE_STARTTLS"),
        tls_validate=env_bool("AD_TLS_VALIDATE", "true"),
        bind_user=env_required("AD_BIND_USER"),
        bind_password=env_required("AD_BIND_PASSWORD"),
        base_dn=env_required("AD_BASE_DN"),
        search_attr=os.getenv("AD_SEARCH_ATTR", "sAMAccountName"),
        expiry_attr=os.getenv("AD_EXPIRY_ATTR", "accountExpires"),
    )


def connect_ad(config: AdConfig) -> Connection:
    validate = ssl.CERT_REQUIRED if config.tls_validate else ssl.CERT_NONE
    tls = Tls(validate=validate)
    server = Server(
        config.server,
        port=config.port,
        use_ssl=config.use_ssl,
        get_info=ALL,
        tls=tls,
        connect_timeout=int(os.getenv("AD_CONNECT_TIMEOUT", "10")),
    )
    conn = Connection(
        server,
        user=config.bind_user,
        password=config.bind_password,
        auto_bind=False,
        receive_timeout=int(os.getenv("AD_RECEIVE_TIMEOUT", "30")),
    )

    if conn.open() is False:
        raise RuntimeError(f"Falha ao abrir conexão AD: {conn.result}")
    if config.use_starttls and not conn.start_tls():
        raise RuntimeError(f"Falha ao iniciar StartTLS no AD: {conn.result}")
    if not conn.bind():
        raise RuntimeError(f"Falha ao autenticar no AD: {conn.result}")

    return conn


def filetime_to_datetime(value: Any) -> datetime | None:
    if value in (None, "", 0, "0", 9223372036854775807, "9223372036854775807"):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    filetime = int(value)
    return FILETIME_EPOCH + timedelta(microseconds=filetime / 10)


def datetime_to_filetime(value: datetime) -> int:
    utc_value = value.astimezone(timezone.utc)
    return int((utc_value - FILETIME_EPOCH).total_seconds() * 10_000_000)


def format_dt(value: datetime | None, tz_name: str) -> str:
    if value is None:
        return "Sem data definida"
    return value.astimezone(ZoneInfo(tz_name)).strftime("%d/%m/%Y %H:%M:%S %Z")


def add_months(value: datetime, months: int) -> datetime:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    month_days = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ]
    day = min(value.day, month_days[month - 1])
    return value.replace(year=year, month=month, day=day)


def find_user(conn: Connection, config: AdConfig, login: str):
    safe_login = escape_filter_chars(login)
    safe_attr = escape_filter_chars(config.search_attr)
    ok = conn.search(
        search_base=config.base_dn,
        search_filter=(
            f"(&(objectClass=user)(!(objectClass=computer))({safe_attr}={safe_login}))"
        ),
        search_scope=SUBTREE,
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
        raise RuntimeError(f"Usuário não encontrado no AD: {login}")
    if len(conn.entries) > 1:
        raise RuntimeError(f"Mais de um usuário encontrado no AD para: {login}")
    return conn.entries[0]


def build_decision(user, config: AdConfig, login: str, tz_name: str) -> Decision:
    current_expiry = filetime_to_datetime(getattr(user, config.expiry_attr).value)
    dn = str(user.entry_dn)
    if current_expiry is None:
        return Decision(
            login,
            dn,
            None,
            False,
            False,
            None,
            "Acesso configurado para nunca expirar.",
        )

    now = datetime.now(timezone.utc)
    is_expired = current_expiry <= now
    if not is_expired:
        return Decision(
            login,
            dn,
            current_expiry,
            False,
            False,
            None,
            "Acesso ainda não está expirado.",
        )

    new_expiry = add_months(now.astimezone(ZoneInfo(tz_name)), 3).replace(
        hour=23,
        minute=59,
        second=59,
        microsecond=0,
    )
    return Decision(
        login,
        dn,
        current_expiry,
        True,
        True,
        new_expiry,
        "Acesso expirado. Renovação permitida por regra.",
    )


def apply_renewal(conn: Connection, config: AdConfig, decision: Decision) -> None:
    if decision.new_expiry is None:
        raise RuntimeError("Não há nova expiração calculada.")

    ok = conn.modify(
        decision.dn,
        {
            config.expiry_attr: [
                (MODIFY_REPLACE, [datetime_to_filetime(decision.new_expiry)])
            ]
        },
    )
    if not ok:
        raise RuntimeError(f"Falha ao alterar {config.expiry_attr} no AD: {conn.result}")
