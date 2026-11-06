-- Run this once in the Supabase SQL Editor (after 01_create_tables.sql and after the ingestion).
--
-- hybrid_search_hr_chunks combines two searches and merges them:
--   1. keyword search : finds chunks that contain the words of the question (good for "FNF", "POSH", codes)
--   2. vector search  : finds chunks with a similar meaning (good for "time off" vs "leave")
-- The two result lists are merged with Reciprocal Rank Fusion (RRF): a chunk that ranks high in
-- BOTH lists wins. Each list gives a chunk the score  1 / (rrf_k + rank).

create or replace function hybrid_search_hr_chunks(
    query_text          text,
    query_embedding     vector(384),
    match_count         int   default 5,
    keyword_weight      float default 1.0,
    semantic_weight     float default 1.0,
    rrf_k               int   default 50
)
returns table (
    chunk_id        text,
    doc_id          text,
    content         text,
    metadata        jsonb,
    score           float,
    keyword_rank    bigint,     -- empty when the keyword search did not find this chunk
    semantic_rank   bigint      -- empty when the vector search did not find this chunk
)
language sql
stable
as $$
    -- Turn "how many casual leaves" into the keyword query  many | casual | leav
    -- (plainto_tsquery joins words with AND; we change it to OR so one missing word does not kill the search)
    with keyword_query as (
        select replace(plainto_tsquery('english', query_text)::text, '&', '|')::tsquery as query
    ),

    keyword_results as (
        select
            chunk.chunk_id,
            row_number() over (order by ts_rank_cd(chunk.content_search, keyword_query.query) desc) as rank_position
        from hr_chunks as chunk, keyword_query
        where chunk.content_search @@ keyword_query.query
          and (chunk.status = 'Approved' or chunk.metadata->>'doc_type' = 'reference')
        order by rank_position
        limit match_count * 4
    ),

    semantic_results as (
        select
            chunk.chunk_id,
            row_number() over (order by chunk.embedding <=> query_embedding) as rank_position
        from hr_chunks as chunk
        where (chunk.status = 'Approved' or chunk.metadata->>'doc_type' = 'reference')
        order by rank_position
        limit match_count * 4
    )

    select
        chunk.chunk_id,
        chunk.doc_id,
        chunk.content,
        chunk.metadata,
        (coalesce(1.0 / (rrf_k + keyword_results.rank_position), 0.0) * keyword_weight
         + coalesce(1.0 / (rrf_k + semantic_results.rank_position), 0.0) * semantic_weight)::float as score,
        keyword_results.rank_position  as keyword_rank,
        semantic_results.rank_position as semantic_rank
    from keyword_results
    full outer join semantic_results on keyword_results.chunk_id = semantic_results.chunk_id
    join hr_chunks as chunk on chunk.chunk_id = coalesce(keyword_results.chunk_id, semantic_results.chunk_id)
    order by score desc
    limit match_count;
$$;

-- Quick check that the ingestion is complete (should say 176):
-- select count(*) from hr_chunks;
