-- Load Pairing Tool -- Postgres schema.
--
-- Apply with:  psql "$LOAD_PAIRING_DB" -f schema/postgres.sql
-- The application creates these tables itself on first connect; this file is
-- here for DBAs who would rather migrate them by hand.

CREATE TABLE IF NOT EXISTS location (
    zip          TEXT PRIMARY KEY,
    city         TEXT,
    state        TEXT,
    store        TEXT,
    lat          DOUBLE PRECISION,
    lon          DOUBLE PRECISION,
    dwell_hours  NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    first_seen   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Databases created before the store number was recorded.
ALTER TABLE location ADD COLUMN IF NOT EXISTS store TEXT;

COMMENT ON COLUMN location.store IS
    'Every store number an uploaded sheet has delivered to this ZIP, comma '
    'separated. A ZIP nearly always serves one store; nothing in the sheet '
    'guarantees it, so a second is appended rather than replacing the first.';

COMMENT ON COLUMN location.dwell_hours IS
    'Hours on the dock at this location. Inserted at the 1.0 h default the '
    'first time the ZIP appears in an uploaded sheet; owned by the dispatcher '
    'from then on and never overwritten by a later upload.';

CREATE TABLE IF NOT EXISTS lane (
    from_zip    TEXT NOT NULL,
    to_zip      TEXT NOT NULL,
    miles       NUMERIC(7,1) NOT NULL,
    source      TEXT NOT NULL,          -- 'pcmiler' | 'google' | 'here' | 'estimated'
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_zip, to_zip)
);

COMMENT ON COLUMN lane.source IS
    'Where the mileage came from. ''estimated'' marks the offline fallback '
    '(great-circle x 1.20 circuity) and can be backfilled with real truck '
    'miles later without touching anything else.';

-- Lanes still waiting on real truck mileage.
CREATE INDEX IF NOT EXISTS lane_estimated_idx ON lane (source) WHERE source = 'estimated';
