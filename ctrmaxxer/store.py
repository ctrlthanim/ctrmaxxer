"""SQLite memory: every creative ever made, its lineage, and its results."""
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS creatives (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL,
    prompt      TEXT,
    hypothesis  TEXT,
    parent_id   INTEGER,
    generation  INTEGER DEFAULT 0,
    asset_id    TEXT,
    campaign_id TEXT,
    -- generated | live | paused | rejected | failed
    status      TEXT DEFAULT 'generated',
    impressions INTEGER DEFAULT 0,
    clicks      INTEGER DEFAULT 0,
    spend_usd   REAL DEFAULT 0,
    note        TEXT,
    created_at  TEXT,
    launched_at TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS insights (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT,
    created_at TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add_creative(self, path, prompt, hypothesis=None, parent_id=None, generation=0):
        cur = self.db.execute(
            "INSERT INTO creatives (path, prompt, hypothesis, parent_id, generation, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (str(path), prompt, hypothesis, parent_id, generation, now(), now()))
        self.db.commit()
        return cur.lastrowid

    def update(self, cid, **fields):
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE creatives SET {cols} WHERE id=?", (*fields.values(), cid))
        self.db.commit()

    def get(self, cid):
        return self.db.execute("SELECT * FROM creatives WHERE id=?", (cid,)).fetchone()

    def by_status(self, *statuses):
        q = ",".join("?" * len(statuses))
        return self.db.execute(f"SELECT * FROM creatives WHERE status IN ({q}) ORDER BY id", statuses).fetchall()

    def all(self):
        return self.db.execute("SELECT * FROM creatives ORDER BY id").fetchall()

    def total_spend(self):
        return self.db.execute("SELECT COALESCE(SUM(spend_usd),0) FROM creatives").fetchone()[0]

    def add_insight(self, text):
        self.db.execute("INSERT INTO insights (text, created_at) VALUES (?,?)", (text, now()))
        self.db.commit()

    def recent_insights(self, n=5):
        rows = self.db.execute("SELECT text FROM insights ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [r["text"] for r in reversed(rows)]
