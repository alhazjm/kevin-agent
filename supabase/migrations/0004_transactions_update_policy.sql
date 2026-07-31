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
