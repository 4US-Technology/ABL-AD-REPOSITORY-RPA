"""Long-running coordinator. A failed integration never stops other flows."""
from __future__ import annotations
import time
from datetime import datetime, time as clock
from zoneinfo import ZoneInfo
from .config import Settings
from .flows import email, vpn
from .health import ServiceState, start_server
from .observability.logging import get_logger
from .storage.database import connect, migrate
def run(*, apply: bool, interval: int, tz_name: str, once: bool = False, days: int = 3, limit: int = 20) -> int:
    settings = Settings.load()
    state = ServiceState()
    conn = connect(settings.db_path)
    try:
        migrate(conn); state.db_ok = True
    finally:
        conn.close()
    server = start_server(state, settings.metrics_host, settings.metrics_port)
    last_email_date: str | None = None
    tz = ZoneInfo(tz_name)
    try:
        while True:
            now = datetime.now(tz)
            flows: list[tuple[str, object, list[str]]] = []
            if once or (now.time() >= clock(7) and last_email_date != now.date().isoformat()):
                flows.append(("email", email.main, ["--days", str(days), "--tz", tz_name, "--db-path", settings.db_path]))
            flows.extend([
                ("vpn", vpn.main, ["--limit", str(limit), "--tz", tz_name, "--db-path", settings.db_path]),
            ])
            for name, handler, arguments in flows:
                logger = get_logger(name)
                if apply: arguments.append("--apply")
                try:
                    code = handler(arguments)  
                    state.cycles[name] += 1
                    if code:
                        state.failures[name] += 1
                        logger.error("flow_finished_with_error", extra={"event": "flow_failed"})
                    else:
                        logger.info("flow_completed", extra={"event": "flow_completed"})
                    if name == "email" and not code:
                        last_email_date = now.date().isoformat()
                except Exception:
                    state.failures[name] += 1
                    logger.exception("flow_exception", extra={"event": "flow_exception"})
            if once: return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 130
    finally:
        state.active = False
        server.shutdown()
