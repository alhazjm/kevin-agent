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
