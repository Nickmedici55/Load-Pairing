"""Persistence for locations and lane mileage.

Postgres is the target; SQLite is supported so the tool runs on a laptop with
nothing installed. Both back ends carry the same two tables:

* ``location`` -- one row per ZIP, holding the dispatcher-owned dwell override.
  A ZIP is inserted at the 1.0 h default the first time it appears in any
  uploaded sheet, and from then on the tool reads whatever has been set.
* ``lane`` -- mileage between two ZIPs, cached permanently on first fetch.
  ``source = 'estimated'`` marks a row that came from the offline fallback
  rather than a routing API, so those can be backfilled later without touching
  anything else.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Optional

from .costing import DEFAULT_DWELL_HOURS

POSTGRES_DDL = """
CREATE TABLE IF NOT EXISTS location (
    zip          TEXT PRIMARY KEY,
    city         TEXT,
    state        TEXT,
    lat          DOUBLE PRECISION,
    lon          DOUBLE PRECISION,
    dwell_hours  NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    first_seen   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lane (
    from_zip    TEXT NOT NULL,
    to_zip      TEXT NOT NULL,
    miles       NUMERIC(7,1) NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_zip, to_zip)
);
"""

SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS location (
    zip          TEXT PRIMARY KEY,
    city         TEXT,
    state        TEXT,
    lat          REAL,
    lon          REAL,
    dwell_hours  REAL NOT NULL DEFAULT 1.0,
    first_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lane (
    from_zip    TEXT NOT NULL,
    to_zip      TEXT NOT NULL,
    miles       REAL NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (from_zip, to_zip)
);
"""


@dataclass(frozen=True)
class Location:
    zip: str
    city: str = ""
    state: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    dwell_hours: float = DEFAULT_DWELL_HOURS


@dataclass(frozen=True)
class Lane:
    from_zip: str
    to_zip: str
    miles: float
    source: str


class Store:
    """Shared query layer over a DB-API connection.

    Subclasses supply the parameter placeholder and the upsert dialect.
    """

    placeholder = "?"
    ddl = SQLITE_DDL

    def __init__(self, connection):
        self.connection = connection

    # -- lifecycle ---------------------------------------------------------

    def create_schema(self) -> None:
        cursor = self.connection.cursor()
        for statement in self.ddl.strip().split(";"):
            if statement.strip():
                cursor.execute(statement)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def _sql(self, sql: str) -> str:
        return sql if self.placeholder == "?" else sql.replace("?", self.placeholder)

    def _execute(self, sql: str, params: tuple = ()):
        cursor = self.connection.cursor()
        cursor.execute(self._sql(sql), params)
        return cursor

    # -- locations ---------------------------------------------------------

    def ensure_locations(self, locations: Iterable[Location]) -> int:
        """Insert any ZIP not seen before at the default dwell.

        Existing rows are left alone: the dwell there belongs to the
        dispatcher, and city/state already recorded are not overwritten by a
        later sheet.
        """
        inserted = 0
        for location in locations:
            cursor = self._execute(
                "INSERT INTO location (zip, city, state, lat, lon, dwell_hours) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (zip) DO NOTHING",
                (
                    location.zip,
                    location.city,
                    location.state,
                    location.lat,
                    location.lon,
                    float(location.dwell_hours),
                ),
            )
            inserted += max(0, cursor.rowcount)
        self.connection.commit()
        return inserted

    def locations(self) -> list[Location]:
        cursor = self._execute(
            "SELECT zip, city, state, lat, lon, dwell_hours FROM location ORDER BY zip"
        )
        return [_as_location(row) for row in cursor.fetchall()]

    def location(self, zip_code: str) -> Optional[Location]:
        cursor = self._execute(
            "SELECT zip, city, state, lat, lon, dwell_hours FROM location WHERE zip = ?",
            (zip_code,),
        )
        row = cursor.fetchone()
        return _as_location(row) if row else None

    def dwell_hours(self) -> dict[str, float]:
        cursor = self._execute("SELECT zip, dwell_hours FROM location")
        return {row[0]: float(row[1]) for row in cursor.fetchall()}

    def set_dwell(self, zip_code: str, hours: float) -> bool:
        cursor = self._execute(
            "UPDATE location SET dwell_hours = ? WHERE zip = ?", (float(hours), zip_code)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def set_coordinates(self, zip_code: str, lat: float, lon: float) -> bool:
        cursor = self._execute(
            "UPDATE location SET lat = ?, lon = ? WHERE zip = ?", (lat, lon, zip_code)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    # -- lanes -------------------------------------------------------------

    def lane(self, from_zip: str, to_zip: str) -> Optional[Lane]:
        cursor = self._execute(
            "SELECT from_zip, to_zip, miles, source FROM lane WHERE from_zip = ? AND to_zip = ?",
            (from_zip, to_zip),
        )
        row = cursor.fetchone()
        return Lane(row[0], row[1], float(row[2]), row[3]) if row else None

    def lanes(self) -> list[Lane]:
        cursor = self._execute("SELECT from_zip, to_zip, miles, source FROM lane")
        return [Lane(r[0], r[1], float(r[2]), r[3]) for r in cursor.fetchall()]

    def save_lane(self, lane: Lane) -> None:
        self._execute(
            "INSERT INTO lane (from_zip, to_zip, miles, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (from_zip, to_zip) DO UPDATE SET miles = EXCLUDED.miles, "
            "source = EXCLUDED.source",
            (lane.from_zip, lane.to_zip, float(lane.miles), lane.source),
        )
        self.connection.commit()

    def estimated_lanes(self) -> list[Lane]:
        """Lanes still carrying offline mileage, ready to be backfilled."""
        cursor = self._execute(
            "SELECT from_zip, to_zip, miles, source FROM lane WHERE source = 'estimated'"
        )
        return [Lane(r[0], r[1], float(r[2]), r[3]) for r in cursor.fetchall()]


class SqliteStore(Store):
    placeholder = "?"
    ddl = SQLITE_DDL


class PostgresStore(Store):
    placeholder = "%s"
    ddl = POSTGRES_DDL


def _as_location(row) -> Location:
    return Location(
        zip=row[0],
        city=row[1] or "",
        state=row[2] or "",
        lat=float(row[3]) if row[3] is not None else None,
        lon=float(row[4]) if row[4] is not None else None,
        dwell_hours=float(row[5]),
    )


def connect(url: str | None = None) -> Store:
    """Open a store.

    ``url`` is a ``postgresql://`` URL or a path to a SQLite file. It defaults
    to ``$LOAD_PAIRING_DB``, then to ``load_pairing.sqlite3`` in the working
    directory.
    """
    url = url or os.environ.get("LOAD_PAIRING_DB") or "load_pairing.sqlite3"

    if url.startswith(("postgres://", "postgresql://")):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on the machine
            raise RuntimeError(
                "a postgresql:// URL needs psycopg installed (pip install 'psycopg[binary]')"
            ) from exc
        store = PostgresStore(psycopg.connect(url))
    else:
        path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url
        store = SqliteStore(sqlite3.connect(path))

    store.create_schema()
    return store
