# CLAUDE.md

The operating manual for this repo is **[AGENTS.md](AGENTS.md)** — read it
before making changes. It is the same file every other coding agent reads,
kept in one place so there is nothing to sync.

Short version, if you only read this: run `pytest tests/ -q` before and
after (it takes a second and needs no network), never loosen a Dockerfile
anchor or a patch anchor to make a build pass, and treat
`supabase/migrations/` as append-only. The rest is in AGENTS.md.
