-- Precomputed parcel -> H3 res-10 cell mapping for candidate parcels.
-- Built by ingest/parcel_cells.py (h3ronpy, vectorized) — computing this
-- in SQL via h3_polygon_to_cells LATERAL was abandoned after a 6-hour
-- runaway; the Rust path builds the same mapping in minutes.
-- Rebuild after refresh-candidates.

CREATE TABLE IF NOT EXISTS parcel_cells (
    parcel_id BIGINT NOT NULL REFERENCES parcels(id) ON DELETE CASCADE,
    cell      h3index NOT NULL,
    PRIMARY KEY (parcel_id, cell)
);

CREATE INDEX IF NOT EXISTS parcel_cells_cell_idx ON parcel_cells (cell);
