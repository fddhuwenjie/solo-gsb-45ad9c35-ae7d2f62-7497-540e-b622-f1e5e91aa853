"""SQLite 连接与 schema。"""
from __future__ import annotations

import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    application_number TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    application_number TEXT NOT NULL REFERENCES applications(application_number),
    claim_number       INTEGER NOT NULL,
    version            INTEGER NOT NULL,
    claim_type         TEXT NOT NULL CHECK (claim_type IN ('independent', 'dependent')),
    reference_mode     TEXT NOT NULL DEFAULT 'all' CHECK (reference_mode IN ('all', 'any')),
    refs               TEXT NOT NULL DEFAULT '[]',
    text               TEXT NOT NULL,
    effective_from     TEXT NOT NULL,
    effective_to       TEXT,
    request_id         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    UNIQUE (application_number, claim_number, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_revisions_version
    ON revisions (application_number, claim_number, version);

CREATE TABLE IF NOT EXISTS requests (
    request_id    TEXT PRIMARY KEY,
    endpoint      TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    status_code   INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_packages (
    evidence_id        TEXT PRIMARY KEY,
    application_number TEXT NOT NULL,
    target_date        TEXT NOT NULL,
    snapshot           TEXT NOT NULL,  -- 冻结的规范 UTF-8 JSON（摘要即对其字节计算）
    sha256             TEXT NOT NULL,
    created_at         TEXT NOT NULL   -- 仅入库元数据，不参与摘要、不出现在快照中
);

CREATE INDEX IF NOT EXISTS ix_evidence_application
    ON evidence_packages (application_number, target_date);
"""


class Database:
    """单连接 + 可重入锁；显式事务（autocommit 模式）。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None  # 手动 BEGIN/COMMIT/ROLLBACK
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
