"""Stock models on daily bars (docs/ALPACA.md "Step 9", contract section 4).

data: the bar type, the cache body parser and the no-lookahead history slice;
families: the StockModel families (momentum, meanrev, trend, buyhold);
backtest: the daily walk-forward backtest (decide on bars before d, trade at d's close);
metrics: streaming accumulators and the metrics dict;
search: the random search over the families.
Standard library only, like the rest of fleet/.
"""
