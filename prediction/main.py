from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


KALSHI_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

DEFAULT_DECISION_KEYWORDS = (
    "administration",
    # "announce",
    # "approval",
    # "approve",
    # "cabinet",
    # "ceo",
    # "chair",
    # "commission",
    # "congress",
    # "court",
    # "decision",
    # "department",
    # "director",
    # "fed",
    # "fda",
    # "judge",
    # "nominate",
    # "nomination",
    "president",
    # "prime minister",
    # "rate cut",
    # "rate hike",
    # "resign",
    # "ruling",
    # "sec",
    # "senate",
    # "speaker",
    # "supreme court",
    # "tariff",
    # "treasury",
    # "veto",
    "trump",
    "white house",
)

DEFAULT_NATURAL_EVENT_KEYWORDS = (
    "air quality",
    "avalanche",
    "blizzard",
    "cyclone",
    "drought",
    "earthquake",
    "degree",
    "degrees",
    "fahrenheit",
    "flood",
    "hail",
    "heat",
    "high temp",
    "high temperature",
    "hurricane",
    "landfall",
    "low temp",
    "low temperature",
    "natural disaster",
    "precipitation",
    "rain",
    "snow",
    "storm",
    "temp",
    "temperature",
    "tornado",
    "tropical storm",
    "tsunami",
    "volcano",
    "weather",
    "wildfire",
)


@dataclass(frozen=True)
class ScanConfig:
    close_within_hours: int
    lookback_hours: int
    recent_hours: int
    min_trades: int
    max_markets: int
    max_pages: int
    z_threshold: float
    ratio_threshold: float
    keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    include_all_markets: bool
    max_retries: int
    retry_base_seconds: float
    retry_max_seconds: float


class KalshiAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def request_json(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    base_url: str = KALSHI_BASE_URL,
    timeout: int = 20,
    max_retries: int = 5,
    retry_base_seconds: float = 1.0,
    retry_max_seconds: float = 30.0,
) -> dict[str, Any]:
    params = {key: value for key, value in (params or {}).items() if value is not None}
    query = f"?{urlencode(params)}" if params else ""
    request = Request(f"{base_url}{path}{query}", headers={"Accept": "application/json"})

    for attempt in range(max_retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if retryable and attempt < max_retries:
                sleep_seconds = min(retry_base_seconds * (2**attempt), retry_max_seconds)
                print(
                    f"Kalshi API returned HTTP {exc.code}; retrying in {sleep_seconds:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(sleep_seconds)
                continue
            raise KalshiAPIError(
                f"Kalshi API returned HTTP {exc.code}: {body}",
                status_code=exc.code,
                retryable=retryable,
            ) from exc
        except URLError as exc:
            if attempt < max_retries:
                sleep_seconds = min(retry_base_seconds * (2**attempt), retry_max_seconds)
                print(
                    f"Could not reach Kalshi API ({exc.reason}); retrying in {sleep_seconds:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(sleep_seconds)
                continue
            raise KalshiAPIError(f"Could not reach Kalshi API: {exc.reason}", retryable=True) from exc

    raise KalshiAPIError("Kalshi API request failed after retries.", retryable=True)


def paginate(
    path: str,
    collection_key: str,
    params: dict[str, Any],
    *,
    max_pages: int,
    config: ScanConfig,
) -> Iterable[dict[str, Any]]:
    cursor = None
    for _ in range(max_pages):
        page = request_json(
            path,
            params | {"cursor": cursor} if cursor else params,
            max_retries=config.max_retries,
            retry_base_seconds=config.retry_base_seconds,
            retry_max_seconds=config.retry_max_seconds,
        )
        yield from page.get(collection_key, [])

        cursor = page.get("cursor")
        if not cursor:
            break


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def to_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    return float(value)


def unix_ts(dt: datetime) -> int:
    return int(dt.timestamp())


def market_text(market: dict[str, Any]) -> str:
    fields = (
        "title",
        "subtitle",
        "yes_sub_title",
        "no_sub_title",
        "rules_primary",
        "rules_secondary",
        "early_close_condition",
    )
    return " ".join(str(market.get(field) or "") for field in fields).lower()


def keyword_matches(market: dict[str, Any], keywords: tuple[str, ...]) -> list[str]:
    text = market_text(market)
    matches = set()
    for keyword in keywords:
        pattern = re.escape(keyword.lower()).replace(r"\ ", r"\s+")
        if re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", text):
            matches.add(keyword)
    return sorted(matches)


def market_filter_decision(market: dict[str, Any], config: ScanConfig) -> tuple[bool, str]:
    if excluded_keywords := keyword_matches(market, config.exclude_keywords):
        return False, f"excluded_keyword:{','.join(excluded_keywords)}"
    if config.include_all_markets:
        return True, "included_all_markets"
    if matched_keywords := keyword_matches(market, config.keywords):
        return True, f"matched_keyword:{','.join(matched_keywords)}"
    return False, "no_required_keyword_match"


def fetch_open_markets(config: ScanConfig) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    return list(
        paginate(
            "/markets",
            "markets",
            {
                "limit": 1000,
                "status": "open",
                "min_close_ts": unix_ts(now),
                "max_close_ts": unix_ts(now + timedelta(hours=config.close_within_hours)),
                "mve_filter": "exclude",
            },
            max_pages=config.max_pages,
            config=config,
        )
    )


def sort_markets_by_close(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(markets, key=lambda market: parse_time(market.get("close_time")) or datetime.max.replace(tzinfo=UTC))


def fetch_candidate_markets(config: ScanConfig) -> list[dict[str, Any]]:
    markets = [market for market in fetch_open_markets(config) if market_filter_decision(market, config)[0]]
    return sort_markets_by_close(markets)[
        : config.max_markets
    ]


def fetch_trades(ticker: str, config: ScanConfig) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    return list(
        paginate(
            "/markets/trades",
            "trades",
            {
                "limit": 1000,
                "ticker": ticker,
                "min_ts": unix_ts(now - timedelta(hours=config.lookback_hours)),
                "max_ts": unix_ts(now),
            },
            max_pages=config.max_pages,
            config=config,
        )
    )


def trade_notional(trade: dict[str, Any]) -> float:
    count = to_float(trade.get("count_fp"))
    yes_price = to_float(trade.get("yes_price_dollars"))
    no_price = to_float(trade.get("no_price_dollars"))
    price = yes_price if yes_price > 0 else no_price
    return count * price


def robust_zscore(value: float, values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = mean(values)
    sigma = pstdev(values)
    if math.isclose(sigma, 0.0):
        return 0.0
    return (value - mu) / sigma


def analyze_market(market: dict[str, Any], trades: list[dict[str, Any]], config: ScanConfig) -> dict[str, Any] | None:
    if len(trades) < config.min_trades:
        return None

    now = datetime.now(UTC)
    recent_cutoff = now - timedelta(hours=config.recent_hours)
    counts = [to_float(trade.get("count_fp")) for trade in trades]
    notionals = [trade_notional(trade) for trade in trades]
    recent_trades = [
        trade
        for trade in trades
        if (created_time := parse_time(trade.get("created_time"))) and created_time >= recent_cutoff
    ]

    max_count = max(counts)
    avg_count = mean(counts)
    max_notional = max(notionals)
    avg_notional = mean(notionals)
    total_count = sum(counts)
    total_notional = sum(notionals)
    recent_count = sum(to_float(trade.get("count_fp")) for trade in recent_trades)
    recent_notional = sum(trade_notional(trade) for trade in recent_trades)

    max_count_ratio = max_count / avg_count if avg_count else 0.0
    max_notional_ratio = max_notional / avg_notional if avg_notional else 0.0
    max_count_z = robust_zscore(max_count, counts)
    max_notional_z = robust_zscore(max_notional, notionals)
    concentration = max_count / total_count if total_count else 0.0

    if max_count_ratio < config.ratio_threshold and max_count_z < config.z_threshold:
        return None

    close_time = parse_time(market.get("close_time"))
    hours_to_close = None
    if close_time:
        hours_to_close = max(0.0, (close_time - now).total_seconds() / 3600)

    score = (
        max_count_z
        + math.log1p(max_count_ratio)
        + concentration * 5.0
        + min(5.0, recent_count / avg_count) if avg_count else 0.0
    )

    return {
        "score": round(score, 3),
        "ticker": market.get("ticker"),
        "title": market.get("title"),
        "close_time": market.get("close_time"),
        "hours_to_close": round(hours_to_close, 2) if hours_to_close is not None else None,
        "matched_keywords": ", ".join(keyword_matches(market, config.keywords)),
        "trade_count": len(trades),
        "block_trade_count": sum(1 for trade in trades if trade.get("is_block_trade")),
        "total_contracts": round(total_count, 2),
        "recent_contracts": round(recent_count, 2),
        "avg_trade_contracts": round(avg_count, 2),
        "max_trade_contracts": round(max_count, 2),
        "max_trade_contract_ratio": round(max_count_ratio, 2),
        "max_trade_contract_z": round(max_count_z, 2),
        "total_notional": round(total_notional, 2),
        "recent_notional": round(recent_notional, 2),
        "avg_trade_notional": round(avg_notional, 2),
        "max_trade_notional": round(max_notional, 2),
        "max_trade_notional_ratio": round(max_notional_ratio, 2),
        "max_trade_notional_z": round(max_notional_z, 2),
        "single_trade_share": round(concentration, 3),
        "last_price_dollars": market.get("last_price_dollars"),
        "volume_24h_fp": market.get("volume_24h_fp"),
        "open_interest_fp": market.get("open_interest_fp"),
    }


def print_table(rows: list[dict[str, Any]], limit: int) -> None:
    if not rows:
        print("No flagged markets found with the current thresholds.")
        return

    columns = [
        ("score", "score"),
        ("ticker", "ticker"),
        ("hours_to_close", "hrs"),
        ("trade_count", "n"),
        ("max_trade_contracts", "max_ct"),
        ("avg_trade_contracts", "avg_ct"),
        ("max_trade_contract_ratio", "ratio"),
        ("max_trade_contract_z", "z"),
        ("single_trade_share", "share"),
        ("block_trade_count", "blocks"),
        ("title", "title"),
    ]
    shown = rows[:limit]
    widths = {
        key: min(
            max(len(label), *(len(str(row.get(key, ""))) for row in shown)),
            72 if key == "title" else 18,
        )
        for key, label in columns
    }

    print(" ".join(label.ljust(widths[key]) for key, label in columns))
    print(" ".join("-" * widths[key] for key, _ in columns))
    for row in shown:
        values = []
        for key, _ in columns:
            value = str(row.get(key, ""))
            if len(value) > widths[key]:
                value = value[: widths[key] - 1] + "..."
            values.append(value.ljust(widths[key]))
        print(" ".join(values))


def write_jsonl(rows: list[dict[str, Any]], output: Path | None) -> None:
    if not output:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, sort_keys=True) + "\n")
    print(f"\nWrote {len(rows)} flagged rows to {output}")


def market_audit_record(
    market: dict[str, Any],
    config: ScanConfig,
    *,
    scan_status: str,
    reason: str,
    passed_market_filters: bool,
    selected_for_trade_fetch: bool,
    trade_fetch_attempted: bool,
    selection_rank: int | None = None,
    trade_count: int | None = None,
    flagged: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    close_time = parse_time(market.get("close_time"))
    now = datetime.now(UTC)
    hours_to_close = None
    if close_time:
        hours_to_close = max(0.0, (close_time - now).total_seconds() / 3600)

    return {
        "recorded_time": now.isoformat(),
        "scan_status": scan_status,
        "reason": reason,
        "passed_market_filters": passed_market_filters,
        "selected_for_trade_fetch": selected_for_trade_fetch,
        "trade_fetch_attempted": trade_fetch_attempted,
        "selection_rank": selection_rank,
        "ticker": market.get("ticker"),
        "title": market.get("title"),
        "close_time": market.get("close_time"),
        "hours_to_close": round(hours_to_close, 2) if hours_to_close is not None else None,
        "matched_keywords": keyword_matches(market, config.keywords),
        "excluded_keywords": keyword_matches(market, config.exclude_keywords),
        "trade_count": trade_count,
        "flagged": flagged,
        "error": error,
        "last_price_dollars": market.get("last_price_dollars"),
        "volume_24h_fp": market.get("volume_24h_fp"),
        "open_interest_fp": market.get("open_interest_fp"),
    }


def write_checked_markets_jsonl(records: list[dict[str, Any]], output: Path | None) -> None:
    if not output:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, sort_keys=True) + "\n")
    print(f"\nWrote {len(records)} checked-market records to {output}")


def assessment_trade_records(
    market: dict[str, Any],
    trades: list[dict[str, Any]],
    *,
    selection_rank: int,
    filter_reason: str,
) -> list[dict[str, Any]]:
    fetched_time = datetime.now(UTC).isoformat()
    return [
        {
            "fetched_time": fetched_time,
            "selection_rank": selection_rank,
            "market_filter_reason": filter_reason,
            "ticker": market.get("ticker"),
            "title": market.get("title"),
            "close_time": market.get("close_time"),
            "trade_index": trade_index,
            "trade_contracts": to_float(trade.get("count_fp")),
            "trade_notional": round(trade_notional(trade), 4),
            "trade_created_time": trade.get("created_time"),
            "yes_price_dollars": trade.get("yes_price_dollars"),
            "no_price_dollars": trade.get("no_price_dollars"),
            "is_block_trade": trade.get("is_block_trade"),
            "raw_trade": trade,
        }
        for trade_index, trade in enumerate(trades, start=1)
    ]


def write_assessment_trades_jsonl(records: list[dict[str, Any]], output: Path | None) -> None:
    if not output:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, sort_keys=True) + "\n")
    print(f"\nWrote {len(records)} assessment trade records to {output}")


def print_skipped_summary(skipped_markets: list[dict[str, str]]) -> None:
    if not skipped_markets:
        return

    print(f"\nSkipped {len(skipped_markets)} markets after retry failures.", file=sys.stderr)
    for skipped in skipped_markets[:10]:
        print(
            f"  {skipped['ticker']}: {skipped['reason']}",
            file=sys.stderr,
        )
    if len(skipped_markets) > 10:
        print(f"  ...and {len(skipped_markets) - 10} more", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Scan public Kalshi market/trade data for unusually large trades in near-closing, "
            "decision-sensitive markets."
        )
    )
    parser.add_argument("--close-within-hours", type=int, default=72)
    parser.add_argument("--lookback-hours", type=int, default=168)
    parser.add_argument("--recent-hours", type=int, default=6)
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument("--max-markets", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--z-threshold", type=float, default=3.0)
    parser.add_argument("--ratio-threshold", type=float, default=5.0)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--retry-base-seconds", type=float, default=1.0)
    parser.add_argument("--retry-max-seconds", type=float, default=30.0)
    parser.add_argument("--include-all-markets", action="store_true")
    parser.add_argument(
        "--keyword",
        action="append",
        default=[],
        help="Require this keyword when not using --include-all-markets. Can be supplied multiple times.",
    )
    parser.add_argument(
        "--exclude-keyword",
        action="append",
        default=[],
        help="Exclude markets containing this keyword. Can be supplied multiple times.",
    )
    parser.add_argument("--exclude-natural-events", action="store_true")
    parser.add_argument("--jsonl", type=Path, help="Optional path to write full flagged rows as JSONL.")
    parser.add_argument(
        "--checked-markets-jsonl",
        type=Path,
        help="Optional path to write every fetched market and whether it was analyzed, filtered, skipped, or flagged.",
    )
    parser.add_argument(
        "--assessment-trades-jsonl",
        type=Path,
        help="Optional path to write raw trade records used for market assessment.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    exclude_keywords = tuple(args.exclude_keyword)
    if args.exclude_natural_events:
        exclude_keywords = tuple(sorted(set(exclude_keywords + DEFAULT_NATURAL_EVENT_KEYWORDS)))

    config = ScanConfig(
        close_within_hours=args.close_within_hours,
        lookback_hours=args.lookback_hours,
        recent_hours=args.recent_hours,
        min_trades=args.min_trades,
        max_markets=args.max_markets,
        max_pages=args.max_pages,
        z_threshold=args.z_threshold,
        ratio_threshold=args.ratio_threshold,
        keywords=tuple(sorted(set(DEFAULT_DECISION_KEYWORDS + tuple(args.keyword)))),
        exclude_keywords=exclude_keywords,
        include_all_markets=args.include_all_markets,
        max_retries=args.max_retries,
        retry_base_seconds=args.retry_base_seconds,
        retry_max_seconds=args.retry_max_seconds,
    )

    try:
        fetched_markets = sort_markets_by_close(fetch_open_markets(config))
    except KalshiAPIError as exc:
        print(exc, file=sys.stderr)
        return 1

    checked_market_records = []
    passed_markets = []
    for market in fetched_markets:
        included, reason = market_filter_decision(market, config)
        if included:
            passed_markets.append((market, reason))
        else:
            checked_market_records.append(
                market_audit_record(
                    market,
                    config,
                    scan_status="filtered_out",
                    reason=reason,
                    passed_market_filters=False,
                    selected_for_trade_fetch=False,
                    trade_fetch_attempted=False,
                )
            )

    markets_to_analyze = passed_markets[: config.max_markets]
    for market, reason in passed_markets[config.max_markets :]:
        checked_market_records.append(
            market_audit_record(
                market,
                config,
                scan_status="not_analyzed_max_markets_limit",
                reason=reason,
                passed_market_filters=True,
                selected_for_trade_fetch=False,
                trade_fetch_attempted=False,
            )
        )

    rows = []
    skipped_markets = []
    assessment_trades = []
    for index, (market, filter_reason) in enumerate(markets_to_analyze, start=1):
        ticker = str(market["ticker"])
        print(f"\rAnalyzing {index}/{len(markets_to_analyze)} markets...", end="", file=sys.stderr)
        try:
            trades = fetch_trades(str(market["ticker"]), config)
        except KalshiAPIError as exc:
            skipped_markets.append({"ticker": ticker, "reason": str(exc)})
            checked_market_records.append(
                market_audit_record(
                    market,
                    config,
                    scan_status="skipped_api_error",
                    reason=filter_reason,
                    passed_market_filters=True,
                    selected_for_trade_fetch=True,
                    trade_fetch_attempted=True,
                    selection_rank=index,
                    error=str(exc),
                )
            )
            print(f"\nSkipping {ticker} after retry failure: {exc}", file=sys.stderr)
            continue

        assessment_trades.extend(
            assessment_trade_records(
                market,
                trades,
                selection_rank=index,
                filter_reason=filter_reason,
            )
        )
        row = analyze_market(market, trades, config)
        checked_market_records.append(
            market_audit_record(
                market,
                config,
                scan_status="analyzed_flagged" if row else "analyzed_not_flagged",
                reason=filter_reason,
                passed_market_filters=True,
                selected_for_trade_fetch=True,
                trade_fetch_attempted=True,
                selection_rank=index,
                trade_count=len(trades),
                flagged=bool(row),
            )
        )
        if row:
            rows.append(row)
        time.sleep(0.1)
    print("\r", end="", file=sys.stderr)

    rows.sort(key=lambda row: row["score"], reverse=True)
    print_table(rows, args.limit)
    write_jsonl(rows, args.jsonl)
    write_checked_markets_jsonl(checked_market_records, args.checked_markets_jsonl)
    write_assessment_trades_jsonl(assessment_trades, args.assessment_trades_jsonl)
    print_skipped_summary(skipped_markets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
