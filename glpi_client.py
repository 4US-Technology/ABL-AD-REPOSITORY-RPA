from __future__ import annotations

import base64
import json
import os
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass
class GlpiConfig:
    api_url: str
    app_token: str | None
    user_token: str | None
    login: str | None
    password: str | None
    verify_tls: bool


def env_required(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"Variável de ambiente obrigatória ausente: {name}")
    return value


def load_config() -> GlpiConfig:
    user_token = os.getenv("GLPI_USER_TOKEN") or None
    login = os.getenv("GLPI_LOGIN") or None
    password = os.getenv("GLPI_PASSWORD") or None
    if not user_token and not (login and password):
        raise RuntimeError("Configure GLPI_USER_TOKEN ou GLPI_LOGIN + GLPI_PASSWORD no .env")

    return GlpiConfig(
        api_url=env_required("GLPI_URL").rstrip("/"),
        app_token=os.getenv("GLPI_APP_TOKEN") or None,
        user_token=user_token,
        login=login,
        password=password,
        verify_tls=os.getenv("GLPI_VERIFY_TLS", "true").lower() == "true",
    )


class GlpiClient:
    def __init__(self, config: GlpiConfig, *, debug: bool = False) -> None:
        self.config = config
        self.debug = debug
        self.session_token: str | None = None
        self.ssl_context = None if config.verify_tls else ssl._create_unverified_context()

    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.config.app_token:
            headers["App-Token"] = self.config.app_token
        if self.session_token:
            headers["Session-Token"] = self.session_token
        elif self.config.user_token:
            headers["Authorization"] = f"user_token {self.config.user_token}"
        elif self.config.login and self.config.password:
            credentials = f"{self.config.login}:{self.config.password}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
        return headers

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.config.api_url}/{endpoint.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(url, data=data, headers=self.headers(), method=method)
        try:
            with urlopen(request, timeout=30, context=self.ssl_context) as response:
                raw = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "")
                if self.debug:
                    print(f"DEBUG {method} {url}", flush=True)
                    print(
                        f"DEBUG status={response.status} content_type={content_type}",
                        flush=True,
                    )
                if not raw:
                    return None
                return json.loads(raw)
        except HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} em {url}: {detail}") from e
        except URLError as e:
            raise RuntimeError(f"Falha de conexão em {url}: {e}") from e

    def init_session(self) -> None:
        data = self.request("GET", "initSession")
        token = data.get("session_token") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError(f"GLPI não retornou session_token: {data}")
        self.session_token = str(token)

    def kill_session(self) -> None:
        if self.session_token:
            self.request("GET", "killSession")
            self.session_token = None

    def get_item(self, item_type: str, item_id: int) -> dict[str, Any]:
        data = self.request("GET", f"{item_type}/{item_id}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Resposta inesperada do GLPI em {item_type}/{item_id}: {data}")
        return data

    def add_followup(self, ticket_id: int, content: str) -> Any:
        return self.request(
            "POST",
            "ITILFollowup",
            body={
                "input": {
                    "items_id": ticket_id,
                    "itemtype": "Ticket",
                    "content": content,
                }
            },
        )

    def add_solution(self, ticket_id: int, content: str) -> Any:
        return self.request(
            "POST",
            "ITILSolution",
            body={
                "input": {
                    "items_id": ticket_id,
                    "itemtype": "Ticket",
                    "solutiontypes_id": 0,
                    "content": content,
                }
            },
        )

    def update_ticket(self, ticket_id: int, fields: dict[str, Any]) -> Any:
        return self.request(
            "PUT",
            f"Ticket/{ticket_id}",
            body={"input": {"id": ticket_id, **fields}},
        )
