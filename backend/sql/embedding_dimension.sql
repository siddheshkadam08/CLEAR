-- Vector width follows the configured embedding model.
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
--
-- This file is what remains of `cip_schema_fixes.sql`. The rest of that script
-- patched `cip_DocMaster` / `cip_DocContentMaster` / `cip_docMapping`, three
-- tables owned by another team that the platform no longer reads or writes.
--
-- Apply with:  python -m app.cli fix-embedding-dimension

DROP INDEX IF EXISTS clear.ix_embeddings_hnsw_document_summary;
DROP INDEX IF EXISTS clear.ix_embeddings_hnsw_clause;
DROP INDEX IF EXISTS clear.ix_embeddings_hnsw_chunk;

DELETE FROM clear.embeddings;
ALTER TABLE clear.embeddings ALTER COLUMN embedding TYPE halfvec(1536);
ALTER TABLE clear.embeddings ALTER COLUMN dim SET DEFAULT 1536;

-- m / ef_construction match HNSW_M and HNSW_EF_CONSTRUCTION in .env, and the
-- values migration 0002 used for this table.
CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw_document_summary
    ON clear.embeddings USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64) WHERE level = 'document_summary';
CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw_clause
    ON clear.embeddings USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64) WHERE level = 'clause';
CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw_chunk
    ON clear.embeddings USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64) WHERE level = 'chunk';
