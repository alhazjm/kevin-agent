-- =========================================================================
-- Kevin — consolidated database schema
--
-- GENERATED FILE — do not edit by hand.
-- Regenerate with:  python supabase/build_schema.py
--
-- Every file in supabase/migrations/ concatenated in run order, so a fresh
-- install is one paste into the Supabase SQL editor instead of 8.
--
-- The numbered migrations remain the source of truth. A database that
-- already exists has run them one at a time, so a new schema change is
-- always a NEW numbered file — never an edit to this one, and never a
-- re-squash. Regenerate this file in the same commit.
--
-- Every statement is idempotent (create table if not exists, add column if
-- not exists, and the do $$ ... duplicate_object policy pattern), so
-- running this more than once is safe.
--
-- BEFORE YOU RUN IT: change the email address in is_owner() to the one you
-- will sign into the dashboard with. Every row-level-security policy calls
-- that function, so leaving it wrong means the dashboard signs in and shows
-- you nothing, with no error.
--
-- Source files, in order:
--   0001_init.sql
--   0002_budget_write_policies.sql
--   0003_cards_bonus_cap.sql
--   0004_transactions_update_policy.sql
--   0005_loans.sql
--   0006_sub_overrides.sql
--   0007_cards_base_mpd.sql
--   0008_category_meta.sql
-- =========================================================================

-- =========================================================================
-- 0001_init.sql
-- =========================================================================

-- ============================================================================
-- 0001_init.sql — Supabase schema for the Kevin expense tracker
--
-- Mirrors the 11-tab Google Sheet schema (supabase/migrations/0001_init.sql, v6)
-- plus the Time column added 2026-07-27 for the idempotency time-component
-- fix. Run once in the Supabase SQL Editor. Idempotent-ish: uses IF NOT
-- EXISTS where Postgres allows it; safe to re-run on a fresh project.
--
-- Design notes (docs in repo memory / PR body):
--   * transactions.idempotency_key UNIQUE → dedup becomes ATOMIC at insert
--     (ON CONFLICT DO NOTHING), killing the read-then-write race the Sheet
--     path has. NULLs never collide, so legacy rows without keys are fine.
--   * next_txn_id() preserves the txn_<YYYYMMDD>_<NNN> format exactly
--     (reply-to-edit fallback parses it out of bubble text — frozen format).
--   * Notes conventions ([bucket:X], "orig: CUR amt @ rate", "||PREV:")
--     remain plain text, parsed by the same regexes as today.
--   * source stays the closed enum email|manual|backfill; the receipt-
--     ingestion feature will ALTER the check when it lands.
--   * RLS: every table locked; SELECT-only for the authenticated owner
--     email (the PWA path). The agent writes with the service key, which
--     bypasses RLS. No INSERT/UPDATE/DELETE policies exist on purpose.
-- ============================================================================

-- --- core ledger ------------------------------------------------------------

create table if not exists transactions (
  id               bigint generated always as identity primary key,
  txn_id           text not null unique,          -- txn_<YYYYMMDD>_<NNN> / txn_legacy_NNN
  date             date not null,
  txn_time         text not null default '',      -- "HH:MM[:SS]" SGT; '' for legacy/manual
  merchant         text not null,
  amount           numeric(12,2) not null,
  currency         text not null default 'SGD',
  category         text not null,
  source           text not null default 'email'
                     check (source in ('email','manual','backfill')),
  payment_method   text not null default '',
  notes            text not null default '',
  telegram_message_id text,
  idempotency_key  text unique,                   -- null for legacy rows
  created_at       timestamptz not null default now()
);
create index if not exists transactions_date_idx on transactions (date);
create index if not exists transactions_cat_date_idx on transactions (category, date);

-- race-free txn_id generation, format-identical to the Sheet path
create table if not exists txn_id_counters (
  day text primary key,          -- 'YYYYMMDD'
  seq int not null
);
create or replace function next_txn_id(d date) returns text
language sql as $$
  insert into txn_id_counters values (to_char(d,'YYYYMMDD'), 1)
  on conflict (day) do update set seq = txn_id_counters.seq + 1
  returning 'txn_' || day || '_' || lpad(seq::text, 3, '0');
$$;

-- --- budgets ----------------------------------------------------------------
-- collapses the Sheet's per-month vs simple dual layout into one shape;
-- month is 'YYYY-MM'
create table if not exists budgets (
  category      text not null,
  month         text not null,
  limit_amount  numeric(12,2) not null default 0,
  primary key (category, month)
);

-- --- merchant map -----------------------------------------------------------
-- longest-substring-wins matching stays in Python over fetched rows
create table if not exists merchant_map (
  pattern     text primary key,
  category    text not null,
  created_at  date not null default current_date
);
create unique index if not exists merchant_map_lower_idx on merchant_map (lower(pattern));

-- --- insights / journal -----------------------------------------------------

create table if not exists insights (
  id        bigint generated always as identity primary key,
  date      date not null default current_date,
  category  text not null default '',
  month     text not null default '',
  insight   text not null
);

create table if not exists journal (
  id                   bigint generated always as identity primary key,
  date                 date not null default current_date,
  reply_text           text not null,
  txn_ids_referenced   text not null default '',
  tags                 text not null default ''
);

-- --- webhook audit log (written by Apps Script via PostgREST in PR 4) -------

create table if not exists webhook_log (
  id               bigint generated always as identity primary key,
  ts               timestamptz not null default now(),
  bank             text not null default '',
  type             text not null default '',
  amount           numeric(12,2),
  currency         text not null default '',
  merchant         text not null default '',
  txn_date         text not null default '',
  payment_method   text not null default '',
  idempotency_key  text not null default '',
  webhook_status   text not null default 'pending',
  matched          text not null default ''
);
create index if not exists webhook_log_idem_idx on webhook_log (idempotency_key);

-- --- card optimiser ---------------------------------------------------------

create table if not exists cards (
  card_id                 text primary key,
  display_name            text not null default '',
  payment_method_pattern  text not null default '',
  cycle_start_day         int  not null default 1 check (cycle_start_day between 1 and 31),
  min_spend_bonus         numeric(12,2) not null default 0,
  notes                   text not null default ''
);

create table if not exists card_strategy (
  category            text primary key,        -- includes the '_default' sentinel row
  primary_card_id     text not null default '',
  primary_cap         numeric(12,2) not null default 0,
  primary_earn_rate   numeric(8,3) not null default 0,
  fallback_card_id    text not null default '',
  fallback_earn_rate  numeric(8,3) not null default 0,
  promo_active_until  date,                    -- null = no promo override
  notes               text not null default '' -- carries the ||PREV: snapshot
);

create table if not exists card_nudge_log (
  id                 bigint generated always as identity primary key,
  ts                 timestamptz not null default now(),
  cycle_window       text not null,            -- 'YYYY-MM-DD_YYYY-MM-DD'
  card_id            text not null,
  category           text not null,
  threshold          int  not null,            -- 80 | 100
  triggering_txn_id  text not null default '',
  fallback_card_id   text not null default '',
  unique (cycle_window, card_id, category, threshold)  -- the dedup tuple, now DB-enforced
);

-- --- travel mode ------------------------------------------------------------

create table if not exists travel_mode (
  id             bigint generated always as identity primary key,
  start_date     date not null,
  end_date       date not null,
  label          text not null,
  trip_category  text not null,
  budget_map     text not null default '',     -- 'food=450; transport=300; ...'
  total_budget   numeric(12,2) not null default 0,
  notes          text not null default ''
);

create table if not exists trip_nudge_log (
  id                 bigint generated always as identity primary key,
  ts                 timestamptz not null default now(),
  trip_label         text not null,
  bucket             text not null,
  threshold          int  not null,            -- 80 | 100
  budget_at_nudge    numeric(12,2) not null,   -- reallocation re-arms the nudge
  triggering_txn_id  text not null default '',
  unique (trip_label, bucket, threshold, budget_at_nudge)
);

-- --- row-level security -----------------------------------------------------
-- One reader: the owner, authenticated via Supabase Auth magic link in the PWA.
-- The agent's service key bypasses RLS entirely. No write policies exist:
-- anon/authenticated clients cannot mutate anything.

create or replace function is_owner() returns boolean
language sql stable as $$
  select coalesce(auth.jwt() ->> 'email', '') = 'you@example.com'
$$;

do $$
declare t text;
begin
  foreach t in array array[
    'transactions','txn_id_counters','budgets','merchant_map','insights',
    'journal','webhook_log','cards','card_strategy','card_nudge_log',
    'travel_mode','trip_nudge_log'
  ] loop
    execute format('alter table %I enable row level security', t);
    execute format(
      'create policy owner_read on %I for select to authenticated using (is_owner())', t);
  end loop;
end $$;

-- =========================================================================
-- 0002_budget_write_policies.sql
-- =========================================================================

-- Budget write policies for the PWA editor (rename = UPDATE, row delete =
-- DELETE, cell save = INSERT/UPDATE upsert). 0001 created read-only
-- policies; the insert/update pair was applied by hand before this file existed
-- setup SQL and never landed in a migration — this makes all three
-- reproducible. Idempotent: an already-existing policy name is skipped, and
-- permissive policies OR together, so hand-applied variants coexist safely.
--
-- Run in the Supabase SQL editor. Without the DELETE policy, RLS makes
-- budget.html's delete "succeed" touching 0 rows (the page detects and
-- reports that case).

do $$ begin
  create policy owner_insert on budgets
    for insert to authenticated with check (is_owner());
exception when duplicate_object then null; end $$;

do $$ begin
  create policy owner_update on budgets
    for update to authenticated using (is_owner()) with check (is_owner());
exception when duplicate_object then null; end $$;

do $$ begin
  create policy owner_delete on budgets
    for delete to authenticated using (is_owner());
exception when duplicate_object then null; end $$;

-- =========================================================================
-- 0003_cards_bonus_cap.sql
-- =========================================================================

-- cards.bonus_cap — the calendar-month bonus spend cap in S$ (0 = none).
-- This column was added to the LIVE table by hand before migration 0003
-- (`alter table cards add column ... ; update ... uob-pref = 600`) and
-- later hsbc-revo = 1000 alongside the card_strategy seed rows. This makes
-- it reproducible for a fresh database. Idempotent — safe to run on the
-- live project (it will no-op the column and only assert the two values).

alter table cards add column if not exists bonus_cap numeric(12,2) not null default 0;

update cards set bonus_cap = 600  where card_id = 'uob-pref'  and bonus_cap = 0;
update cards set bonus_cap = 1000 where card_id = 'hsbc-revo' and bonus_cap = 0;

-- =========================================================================
-- 0004_transactions_update_policy.sql
-- =========================================================================

-- PWA quick-categorize (index.html): pending rows get an inline dropdown
-- that sets the category to an EXISTING Budget name — the same
-- snap-or-refuse guard the agent's write tools enforce, minus creation.
-- Needs UPDATE on transactions for the owner; 0001 created read-only
-- policies. Complex edits (splits, renames, MerchantMap learning) stay
-- conversational via Kevin on purpose — learning fires only on user
-- corrections, and a first categorisation is not a correction.
--
-- Run in the Supabase SQL editor. Without this policy, RLS makes the
-- PWA's update "succeed" touching 0 rows (the page detects and reports
-- that case). Idempotent, same pattern as 0002.

do $$ begin
  create policy owner_update on transactions
    for update to authenticated using (is_owner()) with check (is_owner());
exception when duplicate_object then null; end $$;

-- =========================================================================
-- 0005_loans.sql
-- =========================================================================

-- Loans (IOU) ledger: lending is NOT a budget to burn down — it is money
-- that should come back. One row per loan, created conversationally via
-- the agent ("that $50 PayLah to Sarah was a loan"); repayment flips
-- status to 'repaid' and the agent logs an offsetting NEGATIVE
-- transactions row (category "Lending") so monthly totals self-correct —
-- the owner chose the offset-txn design explicitly over excluding Lending from
-- reports. txn_id links the outflow ledger row ('' for cash loans with no
-- bank alert); repay_txn_id links the negative offset row once repaid.
--
-- Run in the Supabase SQL editor by hand (migrations are never
-- auto-applied). Idempotent: create table if not exists + the same
-- do $$ duplicate_object policy pattern as 0002/0004.
--
-- RLS: owner_read (SELECT) + owner_update (UPDATE) only. No INSERT/DELETE
-- policies on purpose: loans are created conversationally via the agent
-- (the service key bypasses RLS); the PWA only reads the list and flips
-- status to repaid.

create table if not exists loans (
  id bigint generated always as identity primary key,
  person text not null,
  amount numeric(12,2) not null,
  lent_date date not null,
  channel text not null default 'paylah',
  txn_id text not null default '',
  status text not null default 'open',
  repaid_date date,
  repay_txn_id text not null default '',
  notes text not null default ''
);

alter table loans enable row level security;

do $$ begin
  create policy owner_read on loans
    for select to authenticated using (is_owner());
exception when duplicate_object then null; end $$;

do $$ begin
  create policy owner_update on loans
    for update to authenticated using (is_owner()) with check (is_owner());
exception when duplicate_object then null; end $$;

-- =========================================================================
-- 0006_sub_overrides.sql
-- =========================================================================

-- PWA subscriptions section: the detector is deterministic and
-- category-blind, so some genuinely-recurring charges are things the owner
-- does not consider subscriptions — insurance premiums, Atome split
-- payments (finite instalments no algorithm can see the end of), a
-- monthly haircut with a steady price. A dismissal (the row's ✕) is a
-- durable human verdict: one tap, remembered across devices, applied
-- by both the PWA (calcSubs) and the agent's detect_subscription_creep.
-- merchant_key is the merchant trimmed + uppercased — the same grouping
-- key both detectors use.
--
-- Run in the Supabase SQL editor. Idempotent, same patterns as 0002/0004.

create table if not exists sub_overrides (
  merchant_key text primary key,
  verdict text not null default 'exclude',
  created_at timestamptz not null default now()
);

alter table sub_overrides enable row level security;

do $$ begin
  create policy owner_read on sub_overrides
    for select to authenticated using (is_owner());
exception when duplicate_object then null; end $$;

do $$ begin
  create policy owner_write on sub_overrides
    for insert to authenticated with check (is_owner());
exception when duplicate_object then null; end $$;

do $$ begin
  create policy owner_update on sub_overrides
    for update to authenticated using (is_owner()) with check (is_owner());
exception when duplicate_object then null; end $$;

-- =========================================================================
-- 0007_cards_base_mpd.sql
-- =========================================================================

-- 0007: cards.base_mpd — the card's flat "everything else" earn rate.
--
-- Why: review_card_efficiency scored any card outside a category's
-- primary/fallback strategy row at 0 mpd. DBS Vantage earns an uncapped
-- 1.5 mpd on general spend, so every Vantage transaction inflated
-- "miles left on the table" (Jul 2026 scorecard: Grab rides shown as
-- −317 when the true gap vs the modeled optimal was ~−79).
--
-- Run BY HAND in the Supabase SQL editor. Code degrades gracefully
-- pre-migration: an absent column reads as 0.0 — the exact old behavior.

alter table cards add column if not exists base_mpd numeric not null default 0;

-- Seed values for the current five cards (approximate mile-equivalents;
-- adjust to taste — the column is hand-maintained like the rest of the
-- cards table). yuu's 0.1 stands in for its 0.25% non-partner cashback.
update cards set base_mpd = 1.5 where card_id = 'dbs-vantage';
update cards set base_mpd = 0.4 where card_id = 'uob-pref';
update cards set base_mpd = 0.4 where card_id = 'hsbc-revo';
update cards set base_mpd = 0.1 where card_id = 'dbs-yuu';
update cards set base_mpd = 0   where card_id = 'cash-paylah';

-- =========================================================================
-- 0008_category_meta.sql
-- =========================================================================

-- 0008: category_meta — which budget categories are FIXED monthly bills.
--
-- Why: the 21:00 review and Friday summary listed every category over 80%,
-- so iCloud $4/$4, Spotify $12/$12, Insurance $61/$61 showed as 🔴 every
-- single day (the owner, 2026-08-17: "I don't need reminding that subscriptions
-- are at $6/$7"). A fixed bill at 100% is the expected state; the ONLY
-- interesting signal from a fixed category is when it comes in OVER its
-- budget (a price increase or a double charge). This table is the
-- deterministic source of that distinction — get_spending_summary reads it
-- and stamps `kind` on every row; the prompts render, never decide.
--
-- Run BY HAND in the Supabase SQL editor. Code degrades gracefully
-- pre-migration: absent table → every category reads as `variable`
-- (today's behavior).

create table if not exists category_meta (
  category    text primary key,
  kind        text not null check (kind in ('fixed', 'variable')),
  created_at  timestamptz not null default now()
);

alter table category_meta enable row level security;

do $$ begin
  create policy owner_read on category_meta
    for select to authenticated using (is_owner());
exception when duplicate_object then null; end $$;

do $$ begin
  create policy owner_write on category_meta
    for insert to authenticated with check (is_owner());
exception when duplicate_object then null; end $$;

do $$ begin
  create policy owner_update on category_meta
    for update to authenticated using (is_owner()) with check (is_owner());
exception when duplicate_object then null; end $$;

do $$ begin
  create policy owner_delete on category_meta
    for delete to authenticated using (is_owner());
exception when duplicate_object then null; end $$;

-- Seed: an EXAMPLE of fixed monthly bills. These are illustrative names —
-- replace them with your own fixed categories (the exact names you use in
-- the budgets table). Anything NOT listed here is `variable`. Match is
-- case-insensitive on the exact category name. Skipping this block is safe:
-- everything reads as variable, so the evening review lists your
-- subscriptions at 100%.
insert into category_meta (category, kind) values
  ('Spotify', 'fixed'),
  ('iCloud', 'fixed'),
  ('Insurance', 'fixed'),
  ('Phone Bill', 'fixed'),
  ('Bills & Utilities', 'fixed')
on conflict (category) do nothing;
