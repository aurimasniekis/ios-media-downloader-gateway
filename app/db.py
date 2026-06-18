"""SQLite audit log of every media request."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Device sub-fields stored as individual ``device_<field>`` columns.
DEVICE_FIELDS = (
    "name",
    "type",
    "os",
    "system_version",
    "system_build_number",
    "model",
    "hostname",
    "details",
    "is_locked",
    "current_volume",
    "current_brightness",
    "current_appearance",
    "screen_width",
    "screen_height",
)

_BASE_COLUMNS = (
    "ts",
    "api_key_name",
    "ip",
    "site",
    "endpoint",
    "url",
    "item_count",
    "format_ids",
    "title",
    "status",
    "error",
)

_DEVICE_COLUMNS = tuple(f"device_{field}" for field in DEVICE_FIELDS)
_ALL_COLUMNS = _BASE_COLUMNS + _DEVICE_COLUMNS

_COLUMN_DEFS = (
    "id            INTEGER PRIMARY KEY AUTOINCREMENT",
    "ts            TEXT NOT NULL",
    "api_key_name  TEXT",
    "ip            TEXT",
    "site          TEXT",
    "endpoint      TEXT",
    "url           TEXT",
    "item_count    INTEGER",
    "format_ids    TEXT",
    "title         TEXT",
    "status        TEXT",
    "error         TEXT",
    *(f"{col:<13} TEXT" for col in _DEVICE_COLUMNS),
)

_SCHEMA = "CREATE TABLE IF NOT EXISTS downloads (\n    " + ",\n    ".join(_COLUMN_DEFS) + "\n)"

# Fingerprint source fields stored per device so rejections can show what changed.
FINGERPRINT_FIELDS = (
    "name",
    "hostname",
    "model",
    "type",
    "screen_width",
    "screen_height",
)
_FP_COLUMNS = tuple(f"fp_{field}" for field in FINGERPRINT_FIELDS)

_DEVICE_COLUMN_DEFS = (
    "id            INTEGER PRIMARY KEY AUTOINCREMENT",
    "api_key_name  TEXT NOT NULL",
    "device_id     TEXT NOT NULL",
    "label         TEXT",
    "first_seen    TEXT",
    "last_seen     TEXT",
    *(f"{col:<13} TEXT" for col in _FP_COLUMNS),
    "UNIQUE(api_key_name, device_id)",
)

_DEVICES_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS devices (\n    "
    + ",\n    ".join(_DEVICE_COLUMN_DEFS)
    + "\n)"
)


class AuditDB:
    """Thread-safe single-connection audit log under ``./data/``."""

    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.executescript(_DEVICES_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(downloads)")}
        for col in _DEVICE_COLUMNS:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE downloads ADD COLUMN {col} TEXT")
        dev_existing = {row[1] for row in self._conn.execute("PRAGMA table_info(devices)")}
        for col in _FP_COLUMNS:
            if col not in dev_existing:
                self._conn.execute(f"ALTER TABLE devices ADD COLUMN {col} TEXT")

    def record(
        self,
        *,
        api_key_name: str | None,
        ip: str | None,
        site: str | None,
        endpoint: str,
        url: str,
        item_count: int,
        format_ids: str | None,
        title: str | None,
        status: str,
        error: str | None,
        device: dict | None = None,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        dev = device or {}
        values = [
            ts,
            api_key_name,
            ip,
            site,
            endpoint,
            url,
            item_count,
            format_ids,
            title,
            status,
            error,
        ]
        for field in DEVICE_FIELDS:
            value = dev.get(field)
            values.append(str(value) if value is not None else None)

        placeholders = ", ".join("?" * len(_ALL_COLUMNS))
        columns = ", ".join(_ALL_COLUMNS)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO downloads ({columns}) VALUES ({placeholders})",
                values,
            )
            self._conn.commit()

    # --- device registration / limiting --------------------------------------

    def check_and_register_device(
        self,
        api_key_name: str,
        device_id: str,
        label: str | None,
        fields: dict | None,
        max_devices: int,
    ) -> bool:
        """Register a device for an API key, enforcing ``max_devices``.

        Returns True if the device is already known or fits within the limit
        (registering it), False if a new device would exceed the limit.
        """
        now = datetime.now(timezone.utc).isoformat()
        fields = fields or {}
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM devices WHERE api_key_name = ? AND device_id = ?",
                (api_key_name, device_id),
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE devices SET last_seen = ?, label = ? WHERE id = ?",
                    (now, label, row[0]),
                )
                self._conn.commit()
                return True

            count = self._conn.execute(
                "SELECT COUNT(*) FROM devices WHERE api_key_name = ?", (api_key_name,)
            ).fetchone()[0]
            if count >= max_devices:
                return False

            columns = ["api_key_name", "device_id", "label", "first_seen", "last_seen", *_FP_COLUMNS]
            values = [
                api_key_name,
                device_id,
                label,
                now,
                now,
                *(
                    str(fields.get(f)) if fields.get(f) is not None else None
                    for f in FINGERPRINT_FIELDS
                ),
            ]
            placeholders = ", ".join("?" * len(columns))
            self._conn.execute(
                f"INSERT INTO devices ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
            self._conn.commit()
            return True

    def list_registered(self, api_key_name: str) -> list[dict]:
        """Registered devices for a key, each with its fingerprint ``fields``."""
        columns = ["device_id", "label", "first_seen", "last_seen", *_FP_COLUMNS]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(columns)} FROM devices "
                "WHERE api_key_name = ? ORDER BY first_seen",
                (api_key_name,),
            ).fetchall()
        result = []
        for row in rows:
            data = dict(zip(columns, row))
            result.append(
                {
                    "device_id": data["device_id"],
                    "label": data["label"],
                    "first_seen": data["first_seen"],
                    "last_seen": data["last_seen"],
                    "fields": {f: data.get(f"fp_{f}") for f in FINGERPRINT_FIELDS},
                }
            )
        return result

    def list_devices(self, api_key_name: str | None = None) -> list[tuple]:
        sql = (
            "SELECT api_key_name, device_id, label, first_seen, last_seen FROM devices"
        )
        params: list = []
        if api_key_name:
            sql += " WHERE api_key_name = ?"
            params.append(api_key_name)
        sql += " ORDER BY api_key_name, first_seen"
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def remove_device(
        self,
        api_key_name: str,
        *,
        device_id: str | None = None,
        label: str | None = None,
        remove_all: bool = False,
    ) -> int:
        with self._lock:
            if remove_all:
                cur = self._conn.execute(
                    "DELETE FROM devices WHERE api_key_name = ?", (api_key_name,)
                )
            elif device_id:
                cur = self._conn.execute(
                    "DELETE FROM devices WHERE api_key_name = ? AND device_id = ?",
                    (api_key_name, device_id),
                )
            elif label:
                cur = self._conn.execute(
                    "DELETE FROM devices WHERE api_key_name = ? AND label = ?",
                    (api_key_name, label),
                )
            else:
                return 0
            self._conn.commit()
            return cur.rowcount

    # --- audit log query ------------------------------------------------------

    def list_audit(
        self, days: int, api_key_name: str | None = None, limit: int = 100
    ) -> list[tuple]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        sql = (
            "SELECT ts, api_key_name, ip, site, endpoint, item_count, status, "
            "device_name, url FROM downloads WHERE ts >= ?"
        )
        params: list = [cutoff]
        if api_key_name:
            sql += " AND api_key_name = ?"
            params.append(api_key_name)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
