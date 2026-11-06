Package	                            Purpose
python-docx	                        Read the .docx files: paragraphs, headings and tables
langchain-text-splitters	        Split text into chunks by heading and size (MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter). It is the only LangChain piece you need.
pinecone	                        Pinecone client: create indexes, upsert and query vectors. It also gives you hosted embeddings and reranking.
pinecone-text	                    Builds BM25 sparse vectors for the keyword half of hybrid search
groq	                            Calls the Groq LLM to write the final answer
fastapi	                            The /query API
uvicorn	                            Runs the FastAPI app
python-dotenv	                    Loads API keys from .env
pydantic-settings	                Typed config in app/config.py
ipykernel	                        Lets the notebooks use your ragVenv
docling                             Docling turns PDFs, Word files, slides and scans into clean structured text (with tables and headings intact) so your RAG gets good chunks to search.

>> pip install pinecone pinecone-text groq docling langchain-text-splitters fastapi uvicorn boto3 ragas

