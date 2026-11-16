-- 14_shared_documents.sql
-- Run once in the Supabase SQL editor. Safe to run again.
--
-- A shared-documents registry: everyone can see every file's NAME, but the file itself
-- is read only by its owner, until the owner approves someone else's request. An approved
-- request gives read-only access for a limited time (the service decides how long).
-- The actual bytes live in Supabase Storage (bucket "shared-documents"), under a path that
-- includes the owner's employee id, so a listing of the bucket alone never identifies anyone.

-- ---------------------------------------------------------------- 1. the document itself (one row per uploaded file)
create table if not exists public.shared_documents (
    id bigint generated always as identity primary key,
    owner_id bigint not null references public.employee (id) on delete cascade,
    original_filename text not null,
    storage_path text not null unique,
    content_type text not null,
    size_bytes bigint not null check (size_bytes > 0),
    created_at timestamptz not null default now()
);
create index if not exists shared_documents_created_idx on public.shared_documents (created_at desc);

-- ---------------------------------------------------------------- 2. who asked to see it, and the owner's answer
create table if not exists public.document_access_requests (
    id bigint generated always as identity primary key,
    document_id bigint not null references public.shared_documents (id) on delete cascade,
    requester_id bigint not null references public.employee (id) on delete cascade,
    status text not null default 'pending' check (status in ('pending', 'approved', 'denied')),
    requested_at timestamptz not null default now(),
    decided_at timestamptz null,
    expires_at timestamptz null   -- set when approved; the person can read the file only until this moment
);
create index if not exists document_access_requests_document_idx on public.document_access_requests (document_id);
create index if not exists document_access_requests_requester_idx on public.document_access_requests (requester_id);
-- Only one request waiting for a decision per person per document (a decided one does not block asking again).
create unique index if not exists document_access_requests_one_pending
    on public.document_access_requests (document_id, requester_id) where status = 'pending';

alter table public.shared_documents enable row level security;
alter table public.document_access_requests enable row level security;

notify pgrst, 'reload schema';
