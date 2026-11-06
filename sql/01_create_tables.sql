-- Run this once in the Supabase dashboard: SQL Editor > New query > paste > Run.
-- It creates one table per embedding model, because each model has its own vector size.
--   hr_chunks         -> Supabase built-in gte-small       (384 numbers per vector)
--   hr_chunks_voyage  -> Voyage voyage-3.5-lite            (1024 numbers per vector)

create extension if not exists vector;

-- ------------------------------------------------------------------ gte-small table
create table if not exists hr_chunks (
    chunk_id        text primary key,          -- stable id from the chunker, e.g. HR-LEAVE-001#policy-at-a-glance#00
    doc_id          text not null,             -- e.g. HR-LEAVE-001
    status          text,                      -- Approved / Draft ... (used to hide non-approved documents)
    content         text not null,             -- the chunk text that was embedded
    content_hash    text not null,             -- hash from the chunker; tells us when the chunk changed
    embedding_model text not null,             -- which model made the vector
    metadata        jsonb not null,            -- all other chunk fields (title, section, version ...)
    embedding       vector(384) not null,
    -- Keyword search column, filled by Postgres automatically from "content".
    content_search  tsvector generated always as (to_tsvector('english', content)) stored
);

create index if not exists hr_chunks_embedding_index
    on hr_chunks using hnsw (embedding vector_cosine_ops);
create index if not exists hr_chunks_search_index
    on hr_chunks using gin (content_search);
create index if not exists hr_chunks_doc_id_index
    on hr_chunks (doc_id);

-- ------------------------------------------------------------------ Voyage table
create table if not exists hr_chunks_voyage (
    chunk_id        text primary key,
    doc_id          text not null,
    status          text,
    content         text not null,
    content_hash    text not null,
    embedding_model text not null,
    metadata        jsonb not null,
    embedding       vector(1024) not null,
    content_search  tsvector generated always as (to_tsvector('english', content)) stored
);

create index if not exists hr_chunks_voyage_embedding_index
    on hr_chunks_voyage using hnsw (embedding vector_cosine_ops);
create index if not exists hr_chunks_voyage_search_index
    on hr_chunks_voyage using gin (content_search);
create index if not exists hr_chunks_voyage_doc_id_index
    on hr_chunks_voyage (doc_id);

-- ------------------------------------------------------------------ security
-- Row Level Security ON with no policies = the public (anon) key can read nothing.
-- Our Python code uses the service role key, which bypasses these rules.
alter table hr_chunks         enable row level security;
alter table hr_chunks_voyage  enable row level security;
