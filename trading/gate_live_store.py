"""Durable Gate-only state. Schema installation is an explicit deployment step.

No imports of the Bitvavo bot, no connection on import, no credential logging.
Use a dedicated connection: the session advisory lock covers the entire run.
"""
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4


SCHEMA = """
CREATE TABLE IF NOT EXISTS gate_v1_accounts (
    account text PRIMARY KEY, paused boolean NOT NULL DEFAULT false,
    pause_reason text, started_at timestamptz, succeeded_at timestamptz,
    failed_at timestamptz, last_error text
);
CREATE TABLE IF NOT EXISTS gate_v1_positions (
    id text PRIMARY KEY, account text NOT NULL REFERENCES gate_v1_accounts(account),
    pair text NOT NULL, signal_day date NOT NULL, state text NOT NULL,
    data jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(account,pair,signal_day)
);
CREATE TABLE IF NOT EXISTS gate_v1_events (
    id bigserial PRIMARY KEY, account text NOT NULL, position_id text,
    kind text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS gate_v1_cache (
    account text NOT NULL, key text NOT NULL, data jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(account,key)
);
"""


def encode(value):
    return json.dumps(value, default=lambda x: str(x) if isinstance(x, Decimal)
                      else x.isoformat() if isinstance(x, datetime) else str(x), allow_nan=False)


class Store:
    def __init__(self, conn, account):
        self.conn, self.account = conn, account
        self.conn.autocommit = True
        self.locked = False

    def install(self):
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute("INSERT INTO gate_v1_accounts(account) VALUES(%s) ON CONFLICT DO NOTHING", (self.account,))

    @contextmanager
    def run_lock(self):
        key = int.from_bytes(hashlib.sha256(self.account.encode()).digest()[:8], "big", signed=True)
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = cur.fetchone()[0]
        if not acquired:
            yield False
            return
        self.locked = True
        try:
            yield True
        finally:
            self.locked = False
            if not self.conn.closed:
                with self.conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))

    def require_lock(self):
        if not self.locked or self.conn.closed:
            raise RuntimeError("Gate executor requires an active database lock")
        # A disconnected session cannot continue trading on a stale local flag.
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1")

    def account_state(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT paused,pause_reason FROM gate_v1_accounts WHERE account=%s", (self.account,))
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("Run init-db before starting Gate executor")
        return {"paused": row[0], "reason": row[1]}

    def pause(self, reason, position_id=None):
        self.require_lock()
        with self.conn.cursor() as cur:
            cur.execute("UPDATE gate_v1_accounts SET paused=true,pause_reason=%s WHERE account=%s", (reason, self.account))
        self.event("PAUSED", {"reason": reason}, position_id)

    def heartbeat(self, phase, error=None):
        field = {"start": "started_at", "success": "succeeded_at", "failure": "failed_at"}[phase]
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE gate_v1_accounts SET {field}=now(),last_error=%s WHERE account=%s",
                        (error, self.account))

    def event(self, kind, data, position_id=None):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO gate_v1_events(account,position_id,kind,data) VALUES(%s,%s,%s,%s::jsonb)",
                        (self.account, position_id, kind, encode(data)))

    def create(self, pair, signal_day, data):
        self.require_lock()
        ident = uuid4().hex
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO gate_v1_positions(id,account,pair,signal_day,state,data)
                           VALUES(%s,%s,%s,%s,'INTENT',%s::jsonb)
                           ON CONFLICT(account,pair,signal_day) DO NOTHING RETURNING id""",
                        (ident, self.account, pair, signal_day, encode(data)))
            row = cur.fetchone()
        return self.get(row[0]) if row else None

    def get(self, ident):
        with self.conn.cursor() as cur:
            cur.execute("SELECT id,pair,signal_day,state,data FROM gate_v1_positions WHERE id=%s AND account=%s",
                        (ident, self.account))
            row = cur.fetchone()
        return dict(zip(("id", "pair", "day", "state", "data"), row)) if row else None

    def positions(self, active=True):
        clause = "AND state NOT IN ('CLOSED','REJECTED','PAPER_CLOSED')" if active else ""
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT id,pair,signal_day,state,data FROM gate_v1_positions WHERE account=%s {clause} ORDER BY created_at",
                        (self.account,))
            return [dict(zip(("id", "pair", "day", "state", "data"), r)) for r in cur.fetchall()]

    def update(self, position, state, **data):
        self.require_lock()
        with self.conn.cursor() as cur:
            cur.execute("""UPDATE gate_v1_positions SET state=%s,data=data || %s::jsonb,updated_at=now()
                           WHERE id=%s AND account=%s RETURNING id""",
                        (state, encode(data), position["id"], self.account))
            if not cur.fetchone():
                raise RuntimeError("Gate position disappeared")
        position["state"] = state
        position["data"].update(json.loads(encode(data)))

    def cache_get(self, key):
        with self.conn.cursor() as cur:
            cur.execute("SELECT data FROM gate_v1_cache WHERE account=%s AND key=%s", (self.account,key))
            row = cur.fetchone()
        return row[0] if row else None

    def cache_set(self, key, data):
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO gate_v1_cache(account,key,data) VALUES(%s,%s,%s::jsonb)
                           ON CONFLICT(account,key) DO UPDATE SET data=EXCLUDED.data,updated_at=now()""",
                        (self.account,key,encode(data)))

    def daily_pnl(self, now):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT COALESCE(sum((data->>'pnl_usdt')::numeric),0)
                           FROM gate_v1_positions WHERE account=%s AND state IN ('CLOSED','PAPER_CLOSED')
                           AND (data->>'closed_at')::timestamptz >= %s""",
                        (self.account, now.astimezone(timezone.utc).replace(hour=0, minute=0,second=0,microsecond=0)))
            return Decimal(cur.fetchone()[0])
