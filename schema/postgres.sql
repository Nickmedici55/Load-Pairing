-- Load Pairing Tool -- Postgres schema.
--
-- Apply with:  psql "$LOAD_PAIRING_DB" -f schema/postgres.sql
-- The application creates these tables itself on first connect; this file is
-- here for DBAs who would rather migrate them by hand.

-- A pickup location. Every load is a round trip out of one of these, and a
-- dispatch sheet is uploaded against the service center it runs from.
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

-- Databases created before the store number was recorded.
ALTER TABLE location ADD COLUMN IF NOT EXISTS store TEXT;

-- Databases created before locations were scoped to a service center. Every
-- location already on file was uploaded against one service center -- there
-- was no way to say otherwise -- so it all goes to the default.
INSERT INTO service_center (zip, name, city, state)
     VALUES ('01020', 'New England SC', 'Chicopee', 'MA')
ON CONFLICT (zip) DO NOTHING;

ALTER TABLE location ADD COLUMN IF NOT EXISTS service_center TEXT;
UPDATE location SET service_center = '01020' WHERE service_center IS NULL;

DO $$
DECLARE pkey TEXT;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'location' AND column_name = 'service_center'
                  AND is_nullable = 'YES') THEN
        ALTER TABLE location ALTER COLUMN service_center SET NOT NULL;
        SELECT constraint_name INTO pkey FROM information_schema.table_constraints
         WHERE table_name = 'location' AND constraint_type = 'PRIMARY KEY';
        IF pkey IS NOT NULL THEN
            EXECUTE format('ALTER TABLE location DROP CONSTRAINT %I', pkey);
        END IF;
        ALTER TABLE location ADD PRIMARY KEY (service_center, zip);
    END IF;
END $$;

COMMENT ON COLUMN location.service_center IS
    'Which pickup location this delivery ZIP belongs to. Two service centers '
    'delivering to the same ZIP keep separate rows, so one''s dwell never '
    'leaks into the other''s plan.';

COMMENT ON COLUMN location.store IS
    'Every store number a sheet uploaded for this service center has delivered '
    'to this ZIP, comma separated. A ZIP nearly always serves one store; '
    'nothing in the sheet guarantees it, so a second is appended rather than '
    'replacing the first.';

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

COMMENT ON TABLE lane IS
    'Mileage between two ZIPs, shared by every service center: miles are miles.';

COMMENT ON COLUMN lane.source IS
    'Where the mileage came from. ''estimated'' marks the offline fallback '
    '(great-circle x 1.20 circuity) and can be backfilled with real truck '
    'miles later without touching anything else.';

-- Lanes still waiting on real truck mileage.
CREATE INDEX IF NOT EXISTS lane_estimated_idx ON lane (source) WHERE source = 'estimated';
