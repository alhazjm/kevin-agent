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
