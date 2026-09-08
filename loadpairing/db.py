"""Persistence for service centers, locations and lane mileage.

Postgres is the target; SQLite is supported so the tool runs on a laptop with
nothing installed. Both back ends carry the same three tables:

* ``service_center`` -- a pickup location. Every load is a round trip out of
  one of these, and a dispatch sheet is uploaded against the service center it
  runs from.
* ``location`` -- one row per delivery ZIP **within one service center**,
  holding the dispatcher-owned dwell override and the store numbers delivered
  there. A ZIP is inserted at the 1.0 h default the first time it appears in a
  sheet uploaded for that service center, and from then on the tool reads
  whatever has been set. Two service centers delivering to the same ZIP keep
  separate rows, so one's dwell never leaks into the other's plan.
* ``lane`` -- mileage between two ZIPs, cached permanently on first fetch and
  shared by every service center: miles are miles.
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
CREATE TABLE IF NOT EXISTS service_center (
    zip         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    city        TEXT,
    state       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS location (
    service_center TEXT NOT NULL,
    zip          TEXT NOT NULL,
    city         TEXT,
    state        TEXT,
    store        TEXT,
    lat          DOUBLE PRECISION,
    lon          DOUBLE PRECISION,
    dwell_hours  NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    first_seen   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (service_center, zip)
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
CREATE TABLE IF NOT EXISTS service_center (
    zip         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    city        TEXT,
    state       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS location (
    service_center TEXT NOT NULL,
    zip          TEXT NOT NULL,
    city         TEXT,
    state        TEXT,
    store        TEXT,
    lat          REAL,
    lon          REAL,
    dwell_hours  REAL NOT NULL DEFAULT 1.0,
    first_seen   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (service_center, zip)
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


#: Separator between the store numbers recorded against one ZIP. A ZIP nearly
#: always serves a single store, but nothing in the sheet guarantees it.
STORE_SEPARATOR = ", "


@dataclass(frozen=True)
class ServiceCenter:
    """A pickup location: where a driver loads and returns to."""

    zip: str
    name: str = ""
    city: str = ""
    state: str = ""

    @property
    def where(self) -> str:
        return ", ".join(part for part in (self.city, self.state) if part)

    @property
    def label(self) -> str:
        """How the service center reads in a dropdown."""
        name = self.name or self.where or "Service center"
        return f"{name} - {self.where} {self.zip}".strip() if self.where else f"{name} - {self.zip}"


#: Seeded into an empty database so the tool works out of the box, and used to
#: adopt the locations of a database that predates service centers.
DEFAULT_SERVICE_CENTER = ServiceCenter(
    zip="01020", name="New England SC", city="Chicopee", state="MA"
)


@dataclass(frozen=True)
class Location:
    zip: str
    city: str = ""
    state: str = ""
    store: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    dwell_hours: float = DEFAULT_DWELL_HOURS
    service_center: str = DEFAULT_SERVICE_CENTER.zip


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

    #: Columns added after the first release, applied to databases that predate
    #: them as ``(table, column, definition)``.
    added_columns: tuple[tuple[str, str, str], ...] = ()

    def __init__(self, connection):
        self.connection = connection

    # -- lifecycle ---------------------------------------------------------

    def create_schema(self) -> None:
        cursor = self.connection.cursor()
        for statement in self.ddl.strip().split(";"):
            if statement.strip():
                cursor.execute(statement)
        self._migrate(cursor)
        self.connection.commit()

    def _migrate(self, cursor) -> None:
        """Bring a database created before the current schema up to date."""
        for table, column, definition in self.added_columns:
            if column not in self._columns(table):
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        if "service_center" not in self._columns("location"):
            self._scope_locations_to_service_centers(cursor)
        self._seed_service_center(cursor)

    def _columns(self, table: str) -> set[str]:
        raise NotImplementedError

    def _scope_locations_to_service_centers(self, cursor) -> None:
        """Adopt the locations of a database that predates service centers.

        Everything already on file was uploaded against one service center --
        there was no way to say otherwise -- so it all goes to the default.
        """
        raise NotImplementedError

    def _seed_service_center(self, cursor) -> None:
        """Give an empty database the service center the sample sheet runs from."""
        cursor.execute("SELECT COUNT(*) FROM service_center")
        if int(cursor.fetchone()[0]):
            return
        default = DEFAULT_SERVICE_CENTER
        cursor.execute(
            self._sql("INSERT INTO service_center (zip, name, city, state) VALUES (?, ?, ?, ?)"),
            (default.zip, default.name, default.city, default.state),
        )

    # -- service centers ---------------------------------------------------

    def service_centers(self) -> list[ServiceCenter]:
        cursor = self._execute("SELECT zip, name, city, state FROM service_center ORDER BY name, zip")
        return [ServiceCenter(*row) for row in cursor.fetchall()]

    def service_center(self, zip_code: str) -> Optional[ServiceCenter]:
        cursor = self._execute(
            "SELECT zip, name, city, state FROM service_center WHERE zip = ?", (zip_code,)
        )
        row = cursor.fetchone()
        return ServiceCenter(*row) if row else None

    def save_service_center(self, center: ServiceCenter) -> bool:
        """Add a pickup location, or rename one already on file.

        Returns True when it is new. The ZIP identifies a service center, so
        re-saving one keeps every location already recorded against it.
        """
        is_new = self.service_center(center.zip) is None
        self._execute(
            "INSERT INTO service_center (zip, name, city, state) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (zip) DO UPDATE SET name = EXCLUDED.name, city = EXCLUDED.city, "
            "state = EXCLUDED.state",
            (center.zip, center.name, center.city, center.state),
        )
        self.connection.commit()
        return is_new

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
        """Insert any ZIP this service center has not seen at the default dwell.

        Existing rows keep their dwell -- that belongs to the dispatcher -- and
        the city/state already recorded. A store number the sheet delivers to a
        ZIP already on file is added to that row, so a second store behind one
        ZIP shows up rather than being lost.

        Rows are scoped to their service center: the same ZIP delivered from
        two service centers is two rows, each with its own dwell.

        Returns the number of rows inserted.
        """
        inserted = 0
        for location in _merged_by_zip(locations):
            cursor = self._execute(
                "INSERT INTO location (service_center, zip, city, state, store, lat, lon, dwell_hours) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (service_center, zip) DO NOTHING",
                (
                    location.service_center,
                    location.zip,
                    location.city,
                    location.state,
                    location.store,
                    location.lat,
                    location.lon,
                    float(location.dwell_hours),
                ),
            )
            if cursor.rowcount > 0:
                inserted += cursor.rowcount
            elif location.store:
                self._add_stores(location.service_center, location.zip, location.store)
        self.connection.commit()
        return inserted

    def _add_stores(self, service_center: str, zip_code: str, stores: str) -> None:
        """Record store numbers not yet on an existing row."""
        existing = self.location(zip_code, service_center)
        merged = _join_stores(
            _split_stores(existing.store if existing else "") + _split_stores(stores)
        )
        if existing is not None and merged != existing.store:
            self._execute(
                "UPDATE location SET store = ? WHERE service_center = ? AND zip = ?",
                (merged, service_center, zip_code),
            )

    def locations(self, service_center: Optional[str] = None) -> list[Location]:
        """Every location, or only those of one service center."""
        sql = (
            "SELECT service_center, zip, city, state, store, lat, lon, dwell_hours FROM location"
        )
        if service_center is None:
            cursor = self._execute(sql + " ORDER BY service_center, zip")
        else:
            cursor = self._execute(
                sql + " WHERE service_center = ? ORDER BY zip", (service_center,)
            )
        return [_as_location(row) for row in cursor.fetchall()]

    def location(self, zip_code: str, service_center: str = DEFAULT_SERVICE_CENTER.zip) -> Optional[Location]:
        cursor = self._execute(
            "SELECT service_center, zip, city, state, store, lat, lon, dwell_hours "
            "FROM location WHERE service_center = ? AND zip = ?",
            (service_center, zip_code),
        )
        row = cursor.fetchone()
        return _as_location(row) if row else None

    def dwell_hours(self, service_center: str) -> dict[str, float]:
        """The dwell each ZIP carries for one service center."""
        cursor = self._execute(
            "SELECT zip, dwell_hours FROM location WHERE service_center = ?", (service_center,)
        )
        return {row[0]: float(row[1]) for row in cursor.fetchall()}

    def set_dwell(
        self, zip_code: str, hours: float, service_center: str = DEFAULT_SERVICE_CENTER.zip
    ) -> bool:
        cursor = self._execute(
            "UPDATE location SET dwell_hours = ? WHERE service_center = ? AND zip = ?",
            (float(hours), service_center, zip_code),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def set_coordinates(self, zip_code: str, lat: float, lon: float) -> bool:
        """Coordinates belong to the ZIP, so every service center gets them."""
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


#: Copied across when an older ``location`` table is rebuilt with its service
#: center in the primary key.
_LOCATION_COLUMNS = "zip, city, state, store, lat, lon, dwell_hours, first_seen"


class SqliteStore(Store):
    placeholder = "?"
    ddl = SQLITE_DDL
    added_columns = (("location", "store", "TEXT"),)

    def _columns(self, table: str) -> set[str]:
        cursor = self.connection.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}

    def _scope_locations_to_service_centers(self, cursor) -> None:
        # SQLite cannot alter a primary key, so the table is rebuilt around it.
        cursor.execute("ALTER TABLE location RENAME TO location_unscoped")
        for statement in SQLITE_DDL.split(";"):
            if "CREATE TABLE IF NOT EXISTS location (" in statement:
                cursor.execute(statement)
        cursor.execute(
            f"INSERT INTO location (service_center, {_LOCATION_COLUMNS}) "
            f"SELECT ?, {_LOCATION_COLUMNS} FROM location_unscoped",
            (DEFAULT_SERVICE_CENTER.zip,),
        )
        cursor.execute("DROP TABLE location_unscoped")


class PostgresStore(Store):
    placeholder = "%s"
    ddl = POSTGRES_DDL
    added_columns = (("location", "store", "TEXT"),)

    def _columns(self, table: str) -> set[str]:
        cursor = self.connection.cursor()
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        return {row[0] for row in cursor.fetchall()}

    def _scope_locations_to_service_centers(self, cursor) -> None:
        cursor.execute("ALTER TABLE location ADD COLUMN service_center TEXT")
        cursor.execute(
            "UPDATE location SET service_center = %s WHERE service_center IS NULL",
            (DEFAULT_SERVICE_CENTER.zip,),
        )
        cursor.execute("ALTER TABLE location ALTER COLUMN service_center SET NOT NULL")
        cursor.execute(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_name = 'location' AND constraint_type = 'PRIMARY KEY'"
        )
        for (name,) in cursor.fetchall():
            cursor.execute(f'ALTER TABLE location DROP CONSTRAINT "{name}"')
        cursor.execute("ALTER TABLE location ADD PRIMARY KEY (service_center, zip)")


def _as_location(row) -> Location:
    return Location(
        service_center=row[0],
        zip=row[1],
        city=row[2] or "",
        state=row[3] or "",
        store=row[4] or "",
        lat=float(row[5]) if row[5] is not None else None,
        lon=float(row[6]) if row[6] is not None else None,
        dwell_hours=float(row[7]),
    )


def _split_stores(stores: str) -> list[str]:
    return [part.strip() for part in stores.split(",") if part.strip()]


def _join_stores(stores: Iterable[str]) -> str:
    """One entry per store number, in the order first seen."""
    seen: dict[str, None] = {}
    for store in stores:
        seen.setdefault(store, None)
    return STORE_SEPARATOR.join(seen)


def _merged_by_zip(locations: Iterable[Location]) -> list[Location]:
    """Collapse one sheet's stops to a row per service center and ZIP."""
    merged: dict[tuple[str, str], Location] = {}
    for location in locations:
        key = (location.service_center, location.zip)
        seen = merged.get(key)
        if seen is None:
            merged[key] = Location(
                service_center=location.service_center,
                zip=location.zip,
                city=location.city,
                state=location.state,
                store=_join_stores(_split_stores(location.store)),
                lat=location.lat,
                lon=location.lon,
                dwell_hours=location.dwell_hours,
            )
            continue
        merged[key] = Location(
            service_center=seen.service_center,
            zip=seen.zip,
            city=seen.city or location.city,
            state=seen.state or location.state,
            store=_join_stores(_split_stores(seen.store) + _split_stores(location.store)),
            lat=seen.lat if seen.lat is not None else location.lat,
            lon=seen.lon if seen.lon is not None else location.lon,
            dwell_hours=seen.dwell_hours,
        )
    return list(merged.values())


def connect(url: str | None = None) -> Store:
    """Open a store.

    ``url`` is a ``postgresql://`` URL or a path to a SQLite file. It defaults
    to ``$LOAD_PAIRING_DB``, then to ``$DATABASE_URL`` (what a hosted Postgres
    add-on sets), then to ``load_pairing.sqlite3`` in the working directory.
    """
    url = (
        url
        or os.environ.get("LOAD_PAIRING_DB")
        or os.environ.get("DATABASE_URL")
        or "load_pairing.sqlite3"
    )

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
