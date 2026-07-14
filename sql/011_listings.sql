-- Phase 5: listings — properties that are (or may become) purchasable.
-- Multi-source by design; each loader upserts on (source, source_listing_id).
-- listing_kind: 'tax_sale' | 'fsbo' | 'mls' | 'auction' | 'surplus'

CREATE TABLE IF NOT EXISTS listings (
    id                 BIGSERIAL PRIMARY KEY,
    source             TEXT NOT NULL,
    source_listing_id  TEXT NOT NULL,
    listing_kind       TEXT NOT NULL,
    status             TEXT,
    price              NUMERIC(12,2),
    acres              NUMERIC(12,3),
    title              TEXT,
    url                TEXT,
    address            TEXT,
    listed_at          DATE,
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    parcel_id          BIGINT REFERENCES parcels(id) ON DELETE SET NULL,
    geom               GEOMETRY(POINT, 4326),
    source_attrs       JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source, source_listing_id)
);

CREATE INDEX IF NOT EXISTS listings_parcel_idx ON listings (parcel_id);
CREATE INDEX IF NOT EXISTS listings_geom_idx ON listings USING GIST (geom);
CREATE INDEX IF NOT EXISTS listings_status_idx ON listings (source, status);
