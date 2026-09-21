from __future__ import annotations
import argparse
import sys
from .config import Settings
from .flows import email, report, vpn
from .service import run as run_service
from .storage.database import backup, integrity_check, migrate, connect, restore
def _database(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Administração do banco SQLite AD-RPA.")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("migrate", "check"):
        p = sub.add_parser(action); p.add_argument("--db-path", default=Settings.load().db_path)
    for action in ("backup", "restore"):
        p = sub.add_parser(action); p.add_argument("source"); p.add_argument("destination")
    args = parser.parse_args(argv)
    if args.action == "migrate":
        conn = connect(args.db_path)
        try: print(f"Migração concluída: versão {migrate(conn)}")
        finally: conn.close()
    elif args.action == "check": print(integrity_check(args.db_path))
    elif args.action == "backup": backup(args.source, args.destination)
    else: restore(args.source, args.destination)
    return 0
def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    Settings.load()
    if raw and raw[0] == "db": return _database(raw[1:])
    commands = {"reset": vpn.main, "email": email.main, "relatorio": report.main}
    if raw and raw[0] in commands: return commands[raw[0]](raw[1:])
    parser = argparse.ArgumentParser(description="Serviço AD-RPA: e-mail e renovação VPN.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=Settings.load().poll_interval)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--tz", default=Settings.load().timezone)
    args = parser.parse_args(raw)
    return run_service(apply=args.apply, interval=args.interval, tz_name=args.tz, once=args.once, days=args.days, limit=args.limit)
