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
