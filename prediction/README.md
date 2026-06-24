# Kalshi Trade Anomaly Exploration

This repo contains a small scanner for exploring whether public Kalshi trade data shows unusually large trades in short-dated markets that may be sensitive to decisions by a small number of people.

## Important Data Limitation

Kalshi's public `GET /markets/trades` API returns completed trades with ticker, timestamp, price, contract count, and `is_block_trade`. It does not expose trader identity, account IDs, or the number of unique people behind a market. That means this project cannot directly compute "unique persons betting versus average bet size" from public data.

Instead, this first-pass scanner uses observable proxies:

- unusually large single trade size versus the market's recent average trade size
- large single trade share of total observed volume
- recent trade concentration shortly before market close
- block-trade counts
- text heuristics for markets likely to depend on concentrated human decisions

Treat flagged rows as leads for research, not evidence of insider trading.

Official docs used:

- [Get Markets](https://docs.kalshi.com/api-reference/market/get-markets)
- [Get Trades](https://docs.kalshi.com/api-reference/market/get-trades)
- [Get Market Orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook)

## Run

No Kalshi login or API key is required for this first version. It uses public market and trade endpoints only.

```bash
python3 main.py
```

Write full flagged rows as JSONL:

```bash
python3 main.py --jsonl data/flagged_markets.jsonl
```

Scan all near-closing markets instead of applying the decision-maker keyword filter:

```bash
python3 main.py --include-all-markets
```

Add custom keywords:

```bash
python3 main.py --keyword "white house" --keyword "board vote"
```

Use stricter anomaly thresholds:

```bash
python3 main.py --ratio-threshold 10 --z-threshold 4
```

## ExampleL: Check The Past 1 Hour versus 24 Hours

To scan for unusual trades in only the past hour:

```bash
python3 main.py --lookback-hours 24 --recent-hours 1 --include-all-markets
```

For a more focused version that only scans markets closing within the next 24 hours:

```bash
python3 main.py \
  --lookback-hours 24 \
  --recent-hours 1 \
  --close-within-hours 24 \
  --include-all-markets
```

With only one hour of data, the default thresholds can be too strict because many markets have very few trades. For exploration, start with lower thresholds:

```bash
python3 main.py \
  --lookback-hours 24 \
  --recent-hours 1 \
  --close-within-hours 72 \
  --include-all-markets \
  --min-trades 3 \
  --ratio-threshold 3 \
  --z-threshold 2 \
  --jsonl data/kalshi_unusual_last_1h.jsonl
```

Save the full flagged rows:

```bash
python3 main.py \
  --lookback-hours 24 \
  --recent-hours 1 \
  --close-within-hours 24 \
  --include-all-markets \
  --jsonl data/kalshi_unusual_last_1h.jsonl
```

Useful options:

- `--lookback-hours 24`: analyze trades from the past hour only
- `--recent-hours 1`: treat the past hour as the "recent activity" window
- `--close-within-hours 24`: only scan open markets closing in the next 24 hours
- `--include-all-markets`: scan every near-closing market instead of only keyword-matched decision-maker markets
- `--min-trades 3`: include low-activity markets with at least 3 trades
- `--ratio-threshold 3`: flag markets where the largest trade is at least 3x the average trade size
- `--z-threshold 2`: flag statistically large trades with a lower exploratory bar

## Interpreting Columns

- `max_ct`: largest observed trade size in contracts during the lookback window
- `avg_ct`: average trade size in contracts during the lookback window
- `ratio`: `max_ct / avg_ct`
- `z`: z-score of the largest trade size among observed trades
- `share`: largest single trade divided by total observed contracts
- `blocks`: count of trades marked by Kalshi as block trades

## Next Improvements

- Persist daily snapshots so each market can be compared against its own longer history.
- Pull 1-minute candlesticks to measure price jumps around large trades.
- Add authenticated orderbook access to estimate visible liquidity before/after a trade.
- Replace keyword heuristics with a small market classifier for "few decision-maker" exposure.
