# Internal Docs RAG

A question-answering assistant over internal HR documents, plus the HR services
(leave, payroll, people) that the assistant calls as tools.

Word documents in `data/raw/` are parsed into labelled blocks, chunked along their
own headings, embedded, and stored in Supabase with pgvector. A question is embedded
the same way and answered from a hybrid keyword + vector search over those chunks.

## Layout

```
app/
  main.py                FastAPI app: routes only
  core/
    paths.py             PROJECT_ROOT and the data paths, in one place
    config.py            .env loading and get_required_setting()
    supabase.py          the generic PostgREST client every store shares
  rag/
    ingest/
      parsing.py         .docx -> ordered, labelled blocks + document metadata
      chunking.py        blocks -> chunks, split along headings, tables kept whole
      embeddings.py      chunk text -> 384-dim vectors (Supabase gte-small)
      index.py           upsert vectors into Supabase, skipping unchanged chunks
    answer/
      retriever.py       question -> the chunks most likely to answer it
      llm.py             chunks + question -> written answer
      assistant.py       chat entry point: picks between RAG and the HR tools
    eval/
      evaluation.py      scores retrieval against eval/questions.json
  hr/
    leave/               service.py (rules) + store.py (database)
    payroll/             service.py (who may see what) + store.py
    people/              service.py (visibility, profile text) + store.py
    employees/           store.py for the employee and employee_auth tables
  identity/auth.py       logins, passwords, JWTs, roles
  chat/store.py          chat history
  files/storage.py       private files in Supabase Storage

data/raw        the source .docx files
data/parsed     chunks.jsonl, the output of the chunking step
sql/            schema and the hybrid_search function, run in order
scripts/        one-off admin commands
tests/          306 tests, no network calls
```

Each layer only talks downwards: `main.py` -> services -> stores -> `core/supabase.py`.
A `store.py` never contains a rule; a `service.py` never builds a URL.

## Running it

Everything runs as a module from the project root, so `app` is importable:

```bash
# 1. parse and chunk (writes data/parsed/chunks.jsonl)
python -m app.rag.ingest.parsing          # parse only, prints block counts and warnings
python -m app.rag.ingest.chunking         # parse + chunk + validate

# 2. embed and store (needs SUPABASE_URL and SUPABASE_SECRET_KEY in .env)
python -m app.rag.ingest.embeddings --dry-run    # shows the plan, changes nothing
python -m app.rag.ingest.embeddings

# 3. ask
python -m app.rag.answer.retriever "How many casual leaves do I get?"
python -m app.rag.answer.llm "Can I use ChatGPT for work?"

# 4. score retrieval
python -m app.rag.eval.evaluation
python -m app.rag.eval.evaluation --sweep

# the API
uvicorn app.main:app --reload

# tests
pytest tests/ -q
```

Before the first embed: run the files in `sql/` in order in the Supabase SQL editor,
then deploy the embedding function with `supabase functions deploy embed --no-verify-jwt`.

## Dependencies

Only what the code imports. Supabase is reached over its REST API with `requests`,
so there is no Supabase client package, and no LangChain or vector-store SDK.

| Package | Used for |
| --- | --- |
| python-docx | reading the .docx files: paragraphs, headings, tables |
| requests | every call to Supabase, the embedding function, Groq |
| fastapi + uvicorn | the API |
| pydantic | request and response models |
| python-dotenv | loading .env |
| bcrypt | password hashing |
| PyJWT | login tokens |
| pytest | the test suite |
