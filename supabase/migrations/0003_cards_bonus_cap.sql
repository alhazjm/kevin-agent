-- cards.bonus_cap — the calendar-month bonus spend cap in S$ (0 = none).
-- This column was added to the LIVE table by hand before migration 0003
-- (`alter table cards add column ... ; update ... uob-pref = 600`) and
-- later hsbc-revo = 1000 alongside the card_strategy seed rows. This makes
-- it reproducible for a fresh database. Idempotent — safe to run on the
-- live project (it will no-op the column and only assert the two values).

alter table cards add column if not exists bonus_cap numeric(12,2) not null default 0;

update cards set bonus_cap = 600  where card_id = 'uob-pref'  and bonus_cap = 0;
update cards set bonus_cap = 1000 where card_id = 'hsbc-revo' and bonus_cap = 0;
