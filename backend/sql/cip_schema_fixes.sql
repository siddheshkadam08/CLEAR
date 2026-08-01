-- Schema fixes for the externally-owned cip_* tables (schema `clear`).
--
-- These three tables are created and owned outside this repository, so they are
-- deliberately not under Alembic. Applying them here keeps the change reviewable
-- in a diff instead of being pasted into psql once and forgotten.
--
-- Every statement is idempotent: re-running is a no-op. All three tables were
-- empty when these were written, so nothing is destroyed.
--
-- Apply with:  python -m app.cli fix-cip-schema

-- 1. embeddings: unconstrained `vector` -> halfvec(2048).
--
-- nvidia/nemotron-3-embed-1b emits 2048 dimensions. pgvector's HNSW index refuses
-- `vector` above 2000 dimensions, so vectors would insert happily and then never
-- be indexable - every search silently degrades to a sequential scan. `halfvec`
-- indexes to 4000. This is the same finding that shaped the main `embeddings`
-- table (see EMBEDDING_AUDIT.md and migration 0002).
ALTER TABLE clear."cip_DocContentMaster"
    ALTER COLUMN embeddings TYPE halfvec(2048);

-- m / ef_construction match HNSW_M and HNSW_EF_CONSTRUCTION in .env, and the
-- values migration 0002 used for the main embeddings table.
CREATE INDEX IF NOT EXISTS ix_cip_doccontentmaster_embeddings_hnsw
    ON clear."cip_DocContentMaster"
    USING hnsw (embeddings halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- 2. polygon: bigint[] -> double precision[].
--
-- Azure prebuilt-layout emits polygons as 8 floats in inches
-- (1.7371, 1.0047, 7.4946, ...). A bigint array truncates every coordinate to
-- 1, 1, 7, 0 - the boxes survive as numbers and are useless as geometry.
ALTER TABLE clear."cip_DocContentMaster"
    ALTER COLUMN polygon TYPE double precision[]
    USING polygon::double precision[];

-- 3. cip_DocMaster's primary key column is literally named "id " - with a
-- trailing space. Left alone, every query against it must carry that space
-- forever, and the first person to write "id" gets a column-not-found error
-- they will not guess the cause of.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'clear' AND table_name = 'cip_DocMaster' AND column_name = 'id '
    ) THEN
        EXECUTE 'ALTER TABLE clear."cip_DocMaster" RENAME COLUMN "id " TO id';
    END IF;
END $$;

-- 4. Vector width follows the configured embedding model.
--
-- Azure's text-embedding-3-small returns 1536; the previous model returned 2048.
-- pgvector enforces the declared width on insert - "expected 2048 dimensions,
-- not 1536" - so the column has to move or nothing can be stored at all.
--
-- Existing vectors are discarded rather than converted. A 2048-dim vector from
-- one model and a 1536-dim vector from another do not share a space; cosine
-- distance between them is not a worse number, it is not a defined one.
-- Re-running the pipeline regenerates them.
--
-- halfvec is kept even though 1536 is under pgvector's 2000-dimension HNSW limit
-- for `vector`: both index at this width, halfvec is half the bytes, and staying
-- on one type means a future model above 2000 needs no second column rewrite.
--
-- Applied here rather than as an Alembic revision, by decision: `alembic_version`
-- therefore does not describe this column's width. A fresh deployment is
-- unaffected, because the baseline builds the column from EMBEDDING_DIM.
DROP INDEX IF EXISTS clear.ix_cip_doccontentmaster_embeddings_hnsw;
DROP INDEX IF EXISTS clear.ix_embeddings_hnsw_document_summary;
DROP INDEX IF EXISTS clear.ix_embeddings_hnsw_clause;
DROP INDEX IF EXISTS clear.ix_embeddings_hnsw_chunk;

UPDATE clear."cip_DocContentMaster" SET embeddings = NULL WHERE embeddings IS NOT NULL;
ALTER TABLE clear."cip_DocContentMaster" ALTER COLUMN embeddings TYPE halfvec(1536);

DELETE FROM clear.embeddings;
ALTER TABLE clear.embeddings ALTER COLUMN embedding TYPE halfvec(1536);
ALTER TABLE clear.embeddings ALTER COLUMN dim SET DEFAULT 1536;

CREATE INDEX IF NOT EXISTS ix_cip_doccontentmaster_embeddings_hnsw
    ON clear."cip_DocContentMaster"
    USING hnsw (embeddings halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw_document_summary
    ON clear.embeddings USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64) WHERE level = 'document_summary';
CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw_clause
    ON clear.embeddings USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64) WHERE level = 'clause';
CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw_chunk
    ON clear.embeddings USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64) WHERE level = 'chunk';

-- 5. Lookup indexes. cip_docMapping is read once per document by docType;
-- cip_DocContentMaster is read back by docid.
CREATE INDEX IF NOT EXISTS ix_cip_docmapping_doctype
    ON clear."cip_docMapping" ("docType");

CREATE INDEX IF NOT EXISTS ix_cip_doccontentmaster_docid
    ON clear."cip_DocContentMaster" (docid);
