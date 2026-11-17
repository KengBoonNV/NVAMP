-- NVAMP document store (OceanBase / MySQL dialect). All DDL lives here, never in code.

-- One row per document. `path` is the full slash path (e.g. "parts/P-001" or
-- "audit/<user>/entries/<id>"); `coll` is its collection (the path minus the last segment).
CREATE TABLE docs (
    path        VARCHAR(400)  NOT NULL,
    coll        VARCHAR(400)  NOT NULL,
    doc_id      VARCHAR(200)  NOT NULL,
    body        LONGTEXT      NOT NULL,
    sort_t      VARCHAR(64)   NULL,
    updated_at  BIGINT        NOT NULL,
    updated_by  VARCHAR(320)  NULL,
    PRIMARY KEY (path)
) DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_docs_coll_t ON docs (coll, sort_t);

-- A per-collection change counter. Every write bumps its collection's counter, so a
-- browser can poll one small table to learn which collections it needs to re-read.
CREATE TABLE coll_sig (
    coll  VARCHAR(400) NOT NULL,
    sig   BIGINT       NOT NULL,
    PRIMARY KEY (coll)
) DEFAULT CHARSET=utf8mb4;

-- Uploaded files (photos, signatures, PDFs). Bytes live in the object-storage bucket;
-- this table is the index.
CREATE TABLE assets (
    id            VARCHAR(64)   NOT NULL,
    content_type  VARCHAR(100)  NOT NULL,
    size_bytes    BIGINT        NOT NULL,
    filename      VARCHAR(255)  NULL,
    created_by    VARCHAR(320)  NULL,
    created_at    BIGINT        NOT NULL,
    PRIMARY KEY (id)
) DEFAULT CHARSET=utf8mb4;
