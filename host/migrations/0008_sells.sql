-- Step 6 Part B: selling a held position (docs/TRADING.md "Selling (step 6 Part B)").
-- Every existing order is a buy. A sell fill records the basis it removes from the
-- position, so positions stay a plain sum over fills (buys add, sells subtract).

ALTER TABLE orders ADD COLUMN side text NOT NULL DEFAULT 'buy' CHECK (side IN ('buy', 'sell'));

ALTER TABLE fills ADD COLUMN basis_cents bigint;
UPDATE fills SET basis_cents = ROUND(price * size * 100);

ALTER TABLE ledger DROP CONSTRAINT ledger_kind_check;
ALTER TABLE ledger ADD CONSTRAINT ledger_kind_check
  CHECK (kind IN ('fund', 'reserve', 'release', 'fill', 'sell', 'settle', 'adjust'));

ALTER TABLE bets DROP CONSTRAINT bets_result_check;
ALTER TABLE bets ADD CONSTRAINT bets_result_check CHECK (result IN ('win', 'loss', 'push', 'sold'));
ALTER TABLE bets ADD COLUMN order_side text NOT NULL DEFAULT 'buy' CHECK (order_side IN ('buy', 'sell'));
