"""The hosted compute service: the same retrieval, evidence, verification and response code as the local engine, reading its knowledge from Supabase (PostgreSQL) instead of a package folder.
It runs as a long-lived process (a container on any small host), not as a Vercel function: it loads the embedding model once and keeps each business's index in memory.

Needs the optional extras: pip install "faqengine[hosted]"  (psycopg, cryptography)."""
