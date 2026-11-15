"""SQLite 连接与建表。

资料模型要点：
- 工艺路线按 (编号, 修订号) 整体登记，前置组保存在同一张表内；
- 工单固定 route_revision，路线后续修订不影响旧工单；
- 结果（证据）只增不改，“当前结果”在查询时按最大尝试号计算；
- 设备资料按修订号整体保存，旧修订永久可查；
- 放行单冻结当时快照 JSON，并通过 release_status 支持自动撤销。
"""

import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS route (
    route_id          TEXT NOT NULL,
    revision          INTEGER NOT NULL,
    operations_json   TEXT NOT NULL,
    delivery_json     TEXT NOT NULL,
    prereq_groups_json TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (route_id, revision)
);

CREATE TABLE IF NOT EXISTS work_order (
    wo_id          TEXT PRIMARY KEY,
    route_id       TEXT NOT NULL,
    route_revision INTEGER NOT NULL,
    parts_json     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equipment_revision (
    equipment_id TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    periods_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (equipment_id, revision)
);

CREATE TABLE IF NOT EXISTS result (
    evidence_id TEXT PRIMARY KEY,
    wo_id       TEXT NOT NULL,
    part        TEXT NOT NULL,
    operation   TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    executed_at TEXT NOT NULL,
    passed      INTEGER NOT NULL,
    equipment_json TEXT NOT NULL,
    refs_json   TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    batch_id    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_result_wo_part_op
    ON result (wo_id, part, operation, attempt);

CREATE TABLE IF NOT EXISTS import_batch (
    batch_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL,
    item_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS release_request (
    request_id  TEXT PRIMARY KEY,
    wo_id       TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status      TEXT NOT NULL,          -- granted / denied
    release_id  TEXT,
    reason_json TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS release_order (
    release_id   TEXT PRIMARY KEY,
    request_id   TEXT NOT NULL,
    wo_id        TEXT NOT NULL,
    status       TEXT NOT NULL,         -- active / revoked
    snapshot_json TEXT NOT NULL,
    revoke_reason_json TEXT,
    created_at   TEXT NOT NULL,
    revoked_at   TEXT
);
"""


def connect(path):
    """打开（必要时创建）本地数据库并确保表结构存在。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
