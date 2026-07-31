-- Budget write policies for the PWA editor (rename = UPDATE, row delete =
-- DELETE, cell save = INSERT/UPDATE upsert). 0001 created read-only
-- policies; the insert/update pair was applied by hand from the PR #33
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
