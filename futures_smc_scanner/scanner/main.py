import argparse
import logging
import sys
import time
from typing import Callable, List, Optional

from .config import ScannerConfig
from .data_client import PerpetualDataClient
from .models import CoinScanResult, ScanReport
from .reporter import build_scan_report, save_reports, send_telegram_report
from .smc_engine import analyze_symbol

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("futures_smc_scanner")


def run_single_scan(
    config: ScannerConfig,
    client: Optional[PerpetualDataClient] = None,
    clock_ms_fn: Optional[Callable[[], int]] = None,
) -> ScanReport:
    # Hard safety invariant: live trading is strictly disabled and no orders are ever placed.
    if config.live_trading_enabled:
        raise RuntimeError("LIVE_TRADING_ENABLED must remain False; order execution is disabled.")

    now_fn = clock_ms_fn or (lambda: int(time.time() * 1000))
    now_ms = now_fn()
    data_client = client or PerpetualDataClient(config=config, clock_ms_fn=now_fn)

    coin_results: List[CoinScanResult] = []
    system_errors: List[str] = []

    for symbol in config.watchlist:
        try:
            ctx, tf_candles, issues = data_client.fetch_symbol_data(symbol=symbol, timeframes=config.timeframes)
            res = analyze_symbol(
                symbol=symbol,
                ctx=ctx,
                tf_candles=tf_candles,
                data_issues=issues,
                config=config,
            )
            coin_results.append(res)
        except Exception as exc:
            err_msg = f"Unexpected failure analyzing {symbol}: {exc}"
            logger.error(err_msg)
            system_errors.append(err_msg)
            coin_results.append(
                analyze_symbol(
                    symbol=symbol,
                    ctx=None,
                    tf_candles={tf: [] for tf in config.timeframes},
                    data_issues=[err_msg],
                    config=config,
                )
            )

    report = build_scan_report(
        coins=coin_results,
        config=config,
        now_ms=now_ms,
        system_errors=system_errors,
    )
    save_reports(report, config.output_dir)

    if config.telegram_bot_token and config.telegram_chat_id:
        send_telegram_report(report, config.telegram_bot_token, config.telegram_chat_id)

    return report


def run_loop(config: ScannerConfig, interval_minutes: int = 15) -> None:
    if interval_minutes not in (5, 15):
        raise ValueError("Automated loop interval must be 5 or 15 minutes.")

    sleep_seconds = interval_minutes * 60
    logger.info("Starting automated loop every %d minutes (LIVE_TRADING_ENABLED=False).", interval_minutes)
    while True:
        try:
            run_single_scan(config=config)
        except Exception as exc:
            logger.error("Scan cycle error: %s", exc)
        time.sleep(sleep_seconds)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Perpetual Futures SMC & Multi-TF Scanner")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--once", action="store_true", help="Run a single scan and exit")
    mode_group.add_argument("--loop", action="store_true", help="Run continuously on interval")
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=15,
        choices=[5, 15],
        help="Interval in minutes for automated loop (5 or 15)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory to write scan_report.json and scan_report.txt",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    config = ScannerConfig.from_env(output_dir=args.output_dir)
    if args.loop:
        run_loop(config=config, interval_minutes=args.interval_minutes)
        return 0
    run_single_scan(config=config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
