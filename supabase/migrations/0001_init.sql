-- ============================================================================
-- 0001_init.sql — Supabase schema for the Kevin expense tracker
--
-- The core schema: twelve tables, the txn-id minting function, and
-- row-level security. Run once in the Supabase SQL Editor — or paste
-- supabase/schema.sql instead, which is this file plus every later
-- migration in one go. Idempotent-ish: uses IF NOT EXISTS where Postgres
-- allows it, so it is safe to re-run.
--
-- Design notes:
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
-- One reader: Hadi, authenticated via Supabase Auth magic link in the PWA.
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
