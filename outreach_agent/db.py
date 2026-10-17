"""SQLite store. One connection, serialised by a lock so worker threads can share it."""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL UNIQUE,
    segment TEXT,
    country TEXT,
    why_fit TEXT,
    evidence TEXT,
    evidence_url TEXT,
    source TEXT,
    -- new | qualified | disqualified | no_email | drafted | contacted | replied | dead | blocked | bounced | error
    status TEXT NOT NULL DEFAULT 'new',
    status_reason TEXT,
    fit_score INTEGER,
    observation TEXT,
    pitch_angle TEXT,
    summary TEXT,
    decision_maker TEXT,
    dm_title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    name TEXT,
    title TEXT,
    email TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,          -- person | role
    source TEXT,                 -- website | hunter
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sequences (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    segment TEXT,
    -- pending (awaiting you) | active (approved, sending) | rejected | completed | stopped
    status TEXT NOT NULL DEFAULT 'pending',
    stop_reason TEXT,
    discord_message_id TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    sequence_id INTEGER NOT NULL REFERENCES sequences(id),
    step INTEGER NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    -- draft | approved | sent | cancelled | failed | bounced
    status TEXT NOT NULL DEFAULT 'draft',
    due_at TEXT,
    sent_at TEXT,
    message_id TEXT,
    error TEXT,
    UNIQUE (sequence_id, step)
);
CREATE TABLE IF NOT EXISTS inbound (
    id INTEGER PRIMARY KEY,
    uid INTEGER NOT NULL UNIQUE,
    message_id TEXT,
    in_reply_to TEXT,
    from_email TEXT,
    subject TEXT,
    body TEXT,
    received_at TEXT,
    sequence_id INTEGER,
    kind TEXT,                   -- reply | bounce | auto | unsubscribe
    classification TEXT,
    summary TEXT,
    handled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reply_drafts (
    id INTEGER PRIMARY KEY,
    inbound_id INTEGER NOT NULL REFERENCES inbound(id),
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | dismissed | failed
    discord_message_id TEXT,
    sent_message_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_apps (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL UNIQUE REFERENCES companies(id),
    note TEXT NOT NULL,          -- what to paste into their form
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | skipped
    discord_message_id TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS suppression (
    value TEXT PRIMARY KEY,      -- an email address or a bare domain
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS ix_messages_status ON messages(status);
CREATE INDEX IF NOT EXISTS ix_companies_status ON companies(status);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params=(), default=None):
        row = self.one(sql, params)
        return default if row is None or row[0] is None else row[0]

    def insert(self, sql: str, params=()) -> int:
        with self._lock:
            return self.conn.execute(sql, params).lastrowid

    # ── key/value ──

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.scalar("SELECT value FROM kv WHERE key = ?", (key,), default)

    def set(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, None if value is None else str(value)),
        )

    def incr(self, key: str, by: int = 1) -> int:
        with self._lock:
            current = int(self.get(key, "0") or 0) + by
            self.set(key, current)
            return current

    def log(self, kind: str, detail=None) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        self.execute("INSERT INTO events(ts, kind, detail) VALUES(?, ?, ?)", (iso(), kind, detail))

    # ── suppression ──

    def is_suppressed(self, email: str) -> bool:
        email = email.lower()
        domain = email.rsplit("@", 1)[-1]
        return self.one("SELECT 1 FROM suppression WHERE value IN (?, ?)", (email, domain)) is not None

    def suppress(self, value: str, reason: str) -> None:
        self.execute(
            "INSERT OR IGNORE INTO suppression(value, reason, created_at) VALUES(?, ?, ?)",
            (value.lower(), reason, iso()),
        )

    # ── sending stats ──

    def sent_since(self, since: datetime) -> int:
        return self.scalar(
            "SELECT COUNT(*) FROM messages WHERE status IN ('sent', 'bounced') AND sent_at >= ?",
            (iso(since),), 0,
        )

    def recent_bounce_rate(self, last_n: int = 50) -> tuple[float, int]:
        rows = self.all(
            "SELECT status FROM messages WHERE status IN ('sent', 'bounced') ORDER BY sent_at DESC LIMIT ?",
            (last_n,),
        )
        if not rows:
            return 0.0, 0
        bounced = sum(1 for r in rows if r["status"] == "bounced")
        return bounced / len(rows), len(rows)

    def start_of_utc_day(self, offset_days: int = 0) -> datetime:
        now = utcnow()
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset_days)
