# CLAUDE.md

The operating manual for this repo is **[AGENTS.md](AGENTS.md)**. It is the
same file every other coding agent reads, kept in one place so there is
nothing to sync.

**If the person wants their own Kevin running** ("set this up", "walk me
through the deploy", "I just cloned this"): read the section of AGENTS.md
called "Walking someone through first-time setup", then
[docs/SETUP.md](docs/SETUP.md) in full, and start with the four questions
that section gives you. One step at a time, check each step before the next,
never ask for a secret in the chat, and make sure their copy of the repo is
private before the first push.

**If you are changing this repo**: read AGENTS.md before making changes.
Short version, if you only read this: run `pytest tests/ -q` before and
after (it takes a couple of seconds and needs no network), never loosen a
Dockerfile anchor or a patch anchor to make a build pass, and treat
`supabase/migrations/` as append-only. The rest is in AGENTS.md.
