"""SQLite persistence for the canonical dataform.

One table per canonical entity: a handful of indexed columns for filtering
plus a `data` column holding the full Pydantic JSON blob. Mirrors the
`corpus/store.py` SQLite-index pattern described in the Amicus README.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional, Type, TypeVar

from pydantic import BaseModel

from dataform.models import ALL_MODELS

T = TypeVar("T", bound=BaseModel)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "dataform.db"


def _table_name(model: Type[BaseModel]) -> str:
    return model.__name__.lower()


_SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS {table} (
    id TEXT PRIMARY KEY,
    source_system TEXT,
    source_id TEXT,
    doc_type TEXT,
    date TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_{table}_source ON {table}(source_system, source_id);
CREATE INDEX IF NOT EXISTS idx_{table}_doc_type ON {table}(doc_type);
CREATE INDEX IF NOT EXISTS idx_{table}_date ON {table}(date);
"""


class Store:
    """Generic upsert-by-id SQLite store for any canonical entity model."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: each worker thread in parallel_load.py owns its own
        # Store instance (own connection) -- this only relaxes sqlite3's same-thread
        # assertion for the rare case a Store outlives the thread that created it.
        self.conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL allows one writer + many readers concurrently instead of exclusive-locking
        # the whole file; busy_timeout makes a writer that *does* collide with another
        # writer retry for up to 30s instead of raising "database is locked" immediately.
        # Required for parallel_load.py's multiple worker threads/connections to be safe.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        for model in ALL_MODELS:
            self.conn.executescript(_SCHEMA_TEMPLATE.format(table=_table_name(model)))
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                key TEXT PRIMARY KEY,
                value TEXT,
                records_done INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            );
            """
        )
        self.conn.commit()

    def get_checkpoint(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM checkpoints WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def get_checkpoint_records_done(self, key: str) -> int:
        row = self.conn.execute("SELECT records_done FROM checkpoints WHERE key = ?", (key,)).fetchone()
        return row["records_done"] if row else 0

    def set_checkpoint(self, key: str, value: Optional[str], records_done_delta: int = 0) -> None:
        import datetime

        self.conn.execute(
            """INSERT INTO checkpoints (key, value, records_done, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value,
                   records_done=records_done + excluded.records_done,
                   updated_at=excluded.updated_at""",
            (key, value, records_done_delta, datetime.datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def all_checkpoints(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM checkpoints ORDER BY key").fetchall()

    def clear_all(self) -> None:
        """Delete every record AND every checkpoint -- used for a full fresh start."""
        for model in ALL_MODELS:
            self.conn.execute(f"DELETE FROM {_table_name(model)}")
        self.conn.execute("DELETE FROM checkpoints")
        self.conn.commit()

    def _date_value(self, record: BaseModel) -> Optional[str]:
        for attr in ("date_issued", "date_filed", "date", "effective_date"):
            envelope_value = getattr(record, attr, None)
            if envelope_value:
                return str(envelope_value)
        envelope = getattr(record, "envelope", None)
        if envelope is not None and envelope.effective_date:
            return str(envelope.effective_date)
        return None

    def save(self, record: BaseModel, commit: bool = True) -> None:
        """Upsert one record. Assumes `record.envelope` follows RecordEnvelope.

        `commit=False` skips the per-call commit -- for bulk loads (hundreds of
        thousands of rows) committing every single insert is the dominant cost;
        the caller should batch-commit periodically (see save_many) instead."""
        table = _table_name(type(record))
        envelope = record.envelope  # type: ignore[attr-defined]
        doc_type = getattr(record, "doc_type", None)
        doc_type_value = doc_type.value if hasattr(doc_type, "value") else doc_type
        self.conn.execute(
            f"""INSERT INTO {table} (id, source_system, source_id, doc_type, date, data)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_system=excluded.source_system,
                    source_id=excluded.source_id,
                    doc_type=excluded.doc_type,
                    date=excluded.date,
                    data=excluded.data""",
            (
                envelope.id,
                envelope.source_system.value,
                envelope.source_id,
                doc_type_value,
                self._date_value(record),
                record.model_dump_json(),
            ),
        )
        if commit:
            self.conn.commit()

    def save_all(self, records: Iterable[BaseModel]) -> int:
        count = 0
        for record in records:
            self.save(record)
            count += 1
        return count

    def save_many(self, records: Iterable[BaseModel], batch_size: int = 2000) -> int:
        """Bulk-load path: commits every `batch_size` rows instead of every row.
        Orders of magnitude faster than save_all() for large streams (bulk CSV
        imports) -- the per-row fsync-equivalent commit is the dominant cost
        at that scale, not the insert itself."""
        count = 0
        for record in records:
            count += 1
            self.save(record, commit=(count % batch_size == 0))
        self.conn.commit()  # flush the final partial batch
        return count

    def get(self, model: Type[T], record_id: str) -> Optional[T]:
        table = _table_name(model)
        row = self.conn.execute(
            f"SELECT data FROM {table} WHERE id = ?", (record_id,)
        ).fetchone()
        return model.model_validate_json(row["data"]) if row else None

    def list(
        self,
        model: Type[T],
        source_system: Optional[str] = None,
        limit: int = 100,
    ) -> List[T]:
        table = _table_name(model)
        if source_system:
            rows = self.conn.execute(
                f"SELECT data FROM {table} WHERE source_system = ? LIMIT ?",
                (source_system, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT data FROM {table} LIMIT ?", (limit,)
            ).fetchall()
        return [model.model_validate_json(row["data"]) for row in rows]

    def count(self, model: Type[BaseModel]) -> int:
        table = _table_name(model)
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def close(self) -> None:
        self.conn.close()


if __name__ == "__main__":
    store = Store()
    for model in ALL_MODELS:
        print(f"{model.__name__}: {store.count(model)} rows")
    store.close()
