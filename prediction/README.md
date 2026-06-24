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

Write a record of every fetched market and what happened to it:

```bash
python3 main.py --checked-markets-jsonl data/checked_markets.jsonl
```

Scan all near-closing markets instead of applying the decision-maker keyword filter:

```bash
python3 main.py --include-all-markets
```

Add custom keywords:

```bash
python3 main.py --keyword "white house" --keyword "board vote"
```

Exclude markets by keyword:

```bash
python3 main.py --include-all-markets --exclude-keyword weather --exclude-keyword hurricane
```

Exclude common natural-event and weather markets:

```bash
python3 main.py --include-all-markets --exclude-natural-events
```

Use stricter anomaly thresholds:

```bash
python3 main.py --ratio-threshold 10 --z-threshold 4
```

## Example: Check The Past 1 Hour Versus 24 Hours

To compare the past hour against a 24-hour baseline:

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
  --exclude-natural-events \
  --min-trades 3 \
  --ratio-threshold 3 \
  --z-threshold 2 \
  --jsonl data/kalshi_unusual_last_1h.jsonl \
  --checked-markets-jsonl data/kalshi_checked_last_1h.jsonl \
  --checked-markets-jsonl data/checked_markets.jsonl
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

- `--lookback-hours 24`: build the baseline from trades in the past 24 hours
- `--recent-hours 1`: treat the past hour as the "recent activity" window
- `--close-within-hours 24`: only scan open markets closing in the next 24 hours
- `--include-all-markets`: scan every near-closing market instead of only keyword-matched decision-maker markets
- `--keyword "white house"`: include only markets whose text matches this keyword, unless `--include-all-markets` is set
- `--exclude-keyword weather`: remove markets whose text matches this keyword
- `--exclude-natural-events`: remove common weather and natural-event markets
- `--min-trades 3`: include low-activity markets with at least 3 trades
- `--ratio-threshold 3`: flag markets where the largest trade is at least 3x the average trade size
- `--z-threshold 2`: flag statistically large trades with a lower exploratory bar
- `--jsonl data/flagged.jsonl`: save only markets that were flagged as unusual
- `--checked-markets-jsonl data/checked.jsonl`: save every fetched market with its scan status

## Checked Market Records

Use `--checked-markets-jsonl` when you want an audit trail of what the script considered:

```bash
python3 main.py \
  --lookback-hours 24 \
  --recent-hours 1 \
  --include-all-markets \
  --exclude-natural-events \
  --jsonl data/flagged.jsonl \
  --checked-markets-jsonl data/checked_markets.jsonl
```

Each JSONL row includes fields such as:

- `scan_status`: `filtered_out`, `not_analyzed_max_markets_limit`, `analyzed_not_flagged`, `analyzed_flagged`, or `skipped_api_error`
- `reason`: why the market was included or excluded
- `ticker`, `title`, `close_time`, `hours_to_close`
- `matched_keywords` and `excluded_keywords`
- `trade_count` for markets where trades were fetched
- `flagged`: whether the market appeared in the anomaly output

## Rate Limits And Retries

Kalshi may return `429 Too Many Requests` if the scan makes requests faster than the API rate limit allows. The scanner now handles that more gracefully:

- retry `429` and temporary `5xx` errors with exponential backoff
- retry temporary network failures
- skip only the market whose trade fetch still fails after retries
- keep analyzing later markets
- print a skipped-market summary at the end

Default retry settings:

```bash
python3 main.py --max-retries 5 --retry-base-seconds 1 --retry-max-seconds 30
```

For larger scans, either reduce the scan size or make the retry behavior more patient:

```bash
python3 main.py \
  --include-all-markets \
  --max-markets 500 \
  --max-pages 10 \
  --max-retries 8 \
  --retry-base-seconds 2 \
  --retry-max-seconds 60
```

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
