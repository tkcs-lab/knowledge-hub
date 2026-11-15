-- Every table of plan section 3.4. Policies, grants and triggers come in later migrations.

CREATE TABLE kh.documents (
    id            uuid PRIMARY KEY,
    repo          text NOT NULL,
    path          text NOT NULL,
    doc_key       text NOT NULL,
    tier          text NOT NULL CHECK (tier IN ('T0', 'T1')),
    domain        text NOT NULL,
    title         text NOT NULL,
    body          text NOT NULL,
    frontmatter   jsonb NOT NULL DEFAULT '{}',
    status        text NOT NULL CHECK (status IN ('current', 'superseded', 'disputed')),
    stale         boolean NOT NULL DEFAULT false,
    review_by     date,
    owner         text,
    entities      text[] NOT NULL DEFAULT '{}',
    supersedes    text[] NOT NULL DEFAULT '{}',
    superseded_by uuid,
    commit_sha    text NOT NULL,
    content_sha   text NOT NULL,
    approved_by   text[],
    approved_at   timestamptz,
    merge_pr      int,
    pr_url        text,
    indexed_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    required_audiences text[] NOT NULL,
    allowed_emails     text[],
    CONSTRAINT kh_no_open_rows CHECK (cardinality(required_audiences) > 0 OR allowed_emails IS NOT NULL),
    UNIQUE (repo, doc_key),
    UNIQUE (repo, path)
);
CREATE INDEX documents_repo_doc_key_idx ON kh.documents (repo, doc_key);

CREATE TABLE kh.document_versions (
    document_id uuid NOT NULL REFERENCES kh.documents (id) ON DELETE CASCADE,
    commit_sha  text NOT NULL,
    path        text NOT NULL,
    tier        text NOT NULL,
    status      text NOT NULL,
    approved_by text[],
    approved_at timestamptz,
    indexed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, commit_sha)
);

CREATE TABLE kh.raw_records (
    id               uuid PRIMARY KEY,
    source_system    text NOT NULL,
    source_ref       text NOT NULL,
    source_url       text,
    series           text,
    folder           text,
    participants     text[],
    title            text,
    blob_key         text,
    excerpt          text,
    content_type     text,
    occurred_at      timestamptz,
    ingested_by_sub  text NOT NULL,
    ingested_by_type text NOT NULL,
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    domain           text,
    entities         text[] NOT NULL DEFAULT '{}',
    generated_by     jsonb,
    required_audiences text[] NOT NULL,
    allowed_emails     text[],
    CONSTRAINT kh_no_open_rows CHECK (cardinality(required_audiences) > 0 OR allowed_emails IS NOT NULL),
    UNIQUE (ingested_by_sub, source_system, source_ref)
);

CREATE TABLE kh.raw_audience_map (
    kind      text NOT NULL,
    key       text NOT NULL,
    audiences text[] NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE TABLE kh.derived_records (
    id              uuid PRIMARY KEY,
    kind            text NOT NULL CHECK (kind IN ('summary', 'extracted-fact', 'decision-candidate', 'changelog')),
    title           text NOT NULL,
    body            text NOT NULL,
    claims          jsonb,
    domain          text,
    entities        text[] NOT NULL DEFAULT '{}',
    generated_by    jsonb,
    created_by_sub  text NOT NULL,
    created_by_type text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    required_audiences text[] NOT NULL,
    allowed_emails     text[],
    CONSTRAINT kh_no_open_rows CHECK (cardinality(required_audiences) > 0 OR allowed_emails IS NOT NULL)
);

CREATE TABLE kh.derived_sources (
    derived_id          uuid NOT NULL REFERENCES kh.derived_records (id) ON DELETE CASCADE,
    source_kind         text NOT NULL CHECK (source_kind IN ('doc', 'raw', 'derived')),
    source_id           text NOT NULL,
    version             text,
    src_required_audiences text[] NOT NULL,
    src_allowed_emails  text[],
    PRIMARY KEY (derived_id, source_kind, source_id)
);

CREATE TABLE kh.chunks (
    id               uuid PRIMARY KEY,
    document_id      uuid REFERENCES kh.documents (id) ON DELETE CASCADE,
    raw_id           uuid REFERENCES kh.raw_records (id) ON DELETE CASCADE,
    derived_id       uuid REFERENCES kh.derived_records (id) ON DELETE CASCADE,
    entities         text[] NOT NULL DEFAULT '{}',
    ordinal          int NOT NULL,
    heading_path     text,
    text             text NOT NULL,
    content_sha      text NOT NULL,
    embedding        vector(1024),
    embedding_model  text,
    tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(heading_path, '') || ' ' || text)) STORED,
    owner_key        text NOT NULL,
    owner_created_at timestamptz,
    tier             text NOT NULL CHECK (tier IN ('T0', 'T1', 'T2', 'T3')),
    domain           text,
    status           text,
    stale            boolean NOT NULL DEFAULT false,
    superseded       boolean NOT NULL DEFAULT false,
    required_audiences text[] NOT NULL,
    allowed_emails     text[],
    CONSTRAINT kh_no_open_rows CHECK (cardinality(required_audiences) > 0 OR allowed_emails IS NOT NULL),
    CONSTRAINT chunks_one_owner CHECK (num_nonnulls(document_id, raw_id, derived_id) = 1)
);
CREATE INDEX chunks_owner_key_idx ON kh.chunks (owner_key);
CREATE INDEX chunks_tsv_idx ON kh.chunks USING gin (tsv);

CREATE TABLE kh.entities_cache (
    id         text PRIMARY KEY,
    kind       text,
    name       text,
    owner_team text,
    status     text,
    attrs      jsonb,
    synced_at  timestamptz NOT NULL DEFAULT now(),
    required_audiences text[] NOT NULL,
    allowed_emails     text[],
    CONSTRAINT kh_no_open_rows CHECK (cardinality(required_audiences) > 0 OR allowed_emails IS NOT NULL)
);

CREATE TABLE kh.candidates (
    id             uuid PRIMARY KEY,
    derived_id     uuid REFERENCES kh.derived_records (id) ON DELETE SET NULL,
    target_doc_id  uuid REFERENCES kh.documents (id) ON DELETE SET NULL,
    state          text NOT NULL CHECK (state IN ('proposed', 'pr_open', 'promoted', 'rejected', 'failed', 'expired')),
    domain         text NOT NULL,
    target_repo    text NOT NULL,
    target_path    text NOT NULL,
    title          text NOT NULL,
    body_sha       text NOT NULL,
    dedupe_key     text NOT NULL,
    branch         text,
    pr_number      int,
    pr_url         text,
    created_by_sub text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    decided_at     timestamptz,
    error          text,
    required_audiences text[] NOT NULL,
    allowed_emails     text[],
    CONSTRAINT kh_no_open_rows CHECK (cardinality(required_audiences) > 0 OR allowed_emails IS NOT NULL)
);
CREATE UNIQUE INDEX candidates_dedupe_key_open_idx ON kh.candidates (dedupe_key)
    WHERE state IN ('proposed', 'pr_open');

CREATE TABLE kh.candidate_sources (
    candidate_id uuid NOT NULL REFERENCES kh.candidates (id) ON DELETE CASCADE,
    source_kind  text NOT NULL,
    source_id    text NOT NULL,
    src_required_audiences text[] NOT NULL,
    src_allowed_emails text[],
    PRIMARY KEY (candidate_id, source_kind, source_id)
);

CREATE TABLE kh.conflicts (
    id             uuid PRIMARY KEY,
    state          text NOT NULL CHECK (state IN ('open', 'closed')),
    domain         text,
    note           text,
    created_by_sub text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    closed_at      timestamptz,
    closed_by_pr   text,
    required_audiences text[] NOT NULL,
    allowed_emails     text[],
    CONSTRAINT kh_no_open_rows CHECK (cardinality(required_audiences) > 0 OR allowed_emails IS NOT NULL)
);

CREATE TABLE kh.conflict_items (
    conflict_id uuid NOT NULL REFERENCES kh.conflicts (id) ON DELETE CASCADE,
    item_kind   text NOT NULL,
    item_id     text NOT NULL,
    src_required_audiences text[] NOT NULL,
    src_allowed_emails text[],
    PRIMARY KEY (conflict_id, item_kind, item_id)
);

CREATE TABLE kh.index_jobs (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('git_push', 'embed_raw', 'embed_derived', 'pr_event')),
    repo       text,
    payload    jsonb NOT NULL DEFAULT '{}',
    state      text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed')),
    attempts   int NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    run_after  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE kh.repo_state (
    repo             text PRIMARY KEY,
    trust_anchor_sha text,
    last_indexed_sha text,
    trusted          boolean NOT NULL DEFAULT true,
    untrusted_reason text,
    config_hash      text
);

CREATE TABLE kh.index_alerts (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo       text,
    path       text,
    message    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
