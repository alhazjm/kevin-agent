-- Loans (IOU) ledger: lending is NOT a budget to burn down — it is money
-- that should come back. One row per loan, created conversationally via
-- the agent ("that $50 PayLah to Sarah was a loan"); repayment flips
-- status to 'repaid' and the agent logs an offsetting NEGATIVE
-- transactions row (category "Lending") so monthly totals self-correct —
-- Hadi chose the offset-txn design explicitly over excluding Lending from
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
