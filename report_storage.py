from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import emailvencimentosenha as email_jobs
import reset_vpn


@dataclass
class ReportSnapshot:
    id: int
    created_at: str
    days: int
    ticket_limit: int
    user_count: int
    ticket_count: int


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS report_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            days INTEGER NOT NULL,
            ticket_limit INTEGER NOT NULL,
            user_count INTEGER NOT NULL,
            ticket_count INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS report_users (
            snapshot_id INTEGER NOT NULL,
            login TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT,
            expiry_utc TEXT NOT NULL,
            dn TEXT NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES report_snapshots(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS report_tickets (
            snapshot_id INTEGER NOT NULL,
            ticket_id INTEGER NOT NULL,
            status INTEGER,
            login TEXT NOT NULL,
            name TEXT NOT NULL,
            requester_logins TEXT NOT NULL,
            content TEXT NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES report_snapshots(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS email_notifications (
            login TEXT NOT NULL,
            expiry_utc TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            email TEXT,
            PRIMARY KEY (login, expiry_utc)
        );

        CREATE TABLE IF NOT EXISTS ticket_actions (
            ticket_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            login TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            note TEXT,
            PRIMARY KEY (ticket_id)
        );
        """
    )
    conn.commit()


def was_ticket_processed(conn: sqlite3.Connection, ticket_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ticket_actions WHERE ticket_id = ? LIMIT 1",
        (ticket_id,),
    ).fetchone()
    return row is not None


def mark_ticket_processed(
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    action: str,
    login: str,
    note: str | None = None,
) -> None:
    processed_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    conn.execute(
        """
        INSERT OR REPLACE INTO ticket_actions (ticket_id, action, login, processed_at, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ticket_id, action, login, processed_at, note),
    )
    conn.commit()


def store_snapshot(
    conn: sqlite3.Connection,
    *,
    days: int,
    ticket_limit: int,
    users: list[email_jobs.ExpiringUser],
    tickets: list[reset_vpn.VpnResetTicket],
) -> ReportSnapshot:
    created_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    cur = conn.execute(
        """
        INSERT INTO report_snapshots (
            created_at,
            days,
            ticket_limit,
            user_count,
            ticket_count
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (created_at, days, ticket_limit, len(users), len(tickets)),
    )
    snapshot_id = int(cur.lastrowid)

    conn.executemany(
        """
        INSERT INTO report_users (
            snapshot_id,
            login,
            name,
            email,
            expiry_utc,
            dn
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_id,
                user.login,
                user.name,
                user.email,
                user.expiry.astimezone(timezone.utc).isoformat(),
                user.dn,
            )
            for user in users
        ],
    )

    conn.executemany(
        """
        INSERT INTO report_tickets (
            snapshot_id,
            ticket_id,
            status,
            login,
            name,
            requester_logins,
            content
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_id,
                ticket.id,
                ticket.status,
                ticket.login,
                ticket.name,
                ",".join(ticket.requester_logins),
                ticket.content,
            )
            for ticket in tickets
        ],
    )

    conn.commit()
    return ReportSnapshot(
        id=snapshot_id,
        created_at=created_at,
        days=days,
        ticket_limit=ticket_limit,
        user_count=len(users),
        ticket_count=len(tickets),
    )


def was_email_sent(
    conn: sqlite3.Connection,
    *,
    login: str,
    expiry_utc: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM email_notifications
        WHERE login = ? AND expiry_utc = ?
        LIMIT 1
        """,
        (login, expiry_utc),
    ).fetchone()
    return row is not None


def mark_email_sent(
    conn: sqlite3.Connection,
    *,
    login: str,
    expiry_utc: str,
    email: str | None,
    sent_at: str | None = None,
) -> None:
    if sent_at is None:
        sent_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    conn.execute(
        """
        INSERT OR REPLACE INTO email_notifications (
            login,
            expiry_utc,
            sent_at,
            email
        ) VALUES (?, ?, ?, ?)
        """,
        (login, expiry_utc, sent_at, email),
    )
    conn.commit()
