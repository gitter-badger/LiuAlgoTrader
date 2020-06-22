import asyncio
import getopt
import os
import pprint
import sys
import traceback
import uuid
from datetime import datetime, timedelta
from typing import List

import alpaca_trade_api as tradeapi
import pandas as pd
import pygit2
import pytz

from common import config, market_data, trading_data
from common.database import create_db_connection
from common.decorators import timeit
from common.tlog import tlog
from models.algo_run import AlgoRun
from models.new_trades import NewTrade
from strategies.base import Strategy
from strategies.momentum_long import MomentumLong


def get_batch_list():
    @timeit
    async def get_batch_list_worker():
        await create_db_connection()
        data = await AlgoRun.get_batches()
        pp = pprint.PrettyPrinter(indent=4)
        pp.pprint(data)

    try:
        if not asyncio.get_event_loop().is_closed():
            asyncio.get_event_loop().close()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(asyncio.new_event_loop())
        loop.run_until_complete(get_batch_list_worker())
    except KeyboardInterrupt:
        tlog("get_batch_list() - Caught KeyboardInterrupt")
    except Exception as e:
        tlog(
            f"get_batch_list() - exception of type {type(e).__name__} with args {e.args}"
        )
        traceback.print_exc()


"""
starting
"""


def show_usage():
    print(
        f"usage: {sys.argv[0]} -b -d SYMBOL -v --batch-list --version --debug-symbol SYMBOL\n"
    )
    print("-v, --version\t\tDetailed version details")
    print(
        "-b, --batch-list\tDisplay list of trading sessions, list limited to last 30 days"
    )
    print(
        "-d, --debug-symbol\tWrite verbose debug information for symbol SYMBOL during back-testing"
    )


def show_version(filename: str, version: str) -> None:
    """Display welcome message"""
    print(f"filename:{filename}\ngit version:{version}\n")


def backtest(batch_id: str, debug_symbols: List[str] = None) -> None:
    data_api: tradeapi = tradeapi.REST(
        base_url=config.prod_base_url,
        key_id=config.prod_api_key_id,
        secret_key=config.prod_api_secret,
    )
    portfolio_value: float = 100000.0

    uid = str(uuid.uuid4())

    async def backtest_run(
        run_id: int, start: datetime, duration: timedelta, strategy: str
    ) -> None:
        @timeit
        async def backtest_symbol(
            new_run_id: int, strategy: Strategy, symbol: str
        ) -> None:
            est = pytz.timezone("US/Eastern")
            start_time = pytz.utc.localize(start).astimezone(est)
            if start_time.second > 0:
                start_time = start_time.replace(second=0, microsecond=0)
            print(
                f"--> back-testing {symbol} from {str(start_time)} duration {duration}"
            )
            if debug_symbols and symbol in debug_symbols:
                print("--> using DEBUG mode")

            # load historical data
            symbol_data = data_api.polygon.historic_agg_v2(
                symbol,
                1,
                "minute",
                _from=str(start - timedelta(days=8)),
                to=str(start + timedelta(days=1)),
            ).df
            symbol_data["vwap"] = 0.0
            symbol_data["average"] = 0.0

            if debug_symbols and symbol in debug_symbols:
                tlog(symbol_data)

            market_data.minute_history[symbol] = symbol_data
            print(
                f"loaded {len(market_data.minute_history[symbol].index)} agg data points"
            )

            new_now = start_time
            position: int = 0
            while new_now < start_time + duration:
                minute_history_index = symbol_data["close"].index.get_loc(
                    new_now, method="nearest"
                )
                price = symbol_data["close"][minute_history_index]
                do, what = await strategy.run(
                    symbol,
                    position,
                    symbol_data[: minute_history_index + 1],
                    pd.Timestamp(new_now, unit="ms"),
                    portfolio_value,
                    debug=debug_symbols and symbol in debug_symbols,
                    backtesting=True,
                )
                if do:
                    if what["side"] == "buy":
                        position += int(float(what["qty"]))
                        trading_data.latest_cost_basis[symbol] = price
                    else:
                        position -= int(float(what["qty"]))

                    db_trade = NewTrade(
                        algo_run_id=new_run_id,
                        symbol=symbol,
                        qty=int(float(what["qty"])),
                        operation=what["side"],
                        price=price,
                        indicators=trading_data.buy_indicators[symbol]
                        if what["side"] == "buy"
                        else trading_data.sell_indicators[symbol],
                    )

                    await db_trade.save(
                        config.db_conn_pool,
                        str(new_now),
                        trading_data.stop_prices[symbol],
                        trading_data.target_prices[symbol],
                    )
                    print(what)

                new_now += timedelta(minutes=1)

        symbols = await NewTrade.get_run_symbols(run_id)
        if len(symbols) > 0:

            est = pytz.timezone("US/Eastern")
            start_time = pytz.utc.localize(start).astimezone(est)
            config.market_open = start_time.replace(
                hour=9, minute=30, second=0, microsecond=0
            )
            config.market_close = start_time.replace(
                hour=16, minute=0, second=0, microsecond=0
            )
            config.trade_buy_window = duration.seconds / 60
            s: Strategy
            if strategy == "momentum_long":
                s = MomentumLong(uid)
            else:
                raise Exception("Not Implemented Yet")

            new_run = AlgoRun(strategy, uid)
            await new_run.save()

            for symbol in symbols:
                await backtest_symbol(new_run.run_id, s, symbol)

    @timeit
    async def backtest_worker():
        await create_db_connection()
        run_details = await AlgoRun.get_batch_details(batch_id)

        if not len(run_details):
            print(f"can't load data for batch id {batch_id}")
        else:
            for run in run_details:
                await backtest_run(
                    run_id=run[0],
                    start=run[1],
                    duration=run[2],
                    strategy=run[3],
                )

    try:
        if not asyncio.get_event_loop().is_closed():
            asyncio.get_event_loop().close()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(asyncio.new_event_loop())
        loop.run_until_complete(backtest_worker())
    except KeyboardInterrupt:
        tlog("backtest() - Caught KeyboardInterrupt")
    except Exception as e:
        tlog(
            f"backtest() - exception of type {type(e).__name__} with args {e.args}"
        )
        traceback.print_exc()
    finally:
        print("=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=")
        print(f"new batch-id: {uid}")


if __name__ == "__main__":
    config.build_label = pygit2.Repository("./").describe(
        describe_strategy=pygit2.GIT_DESCRIBE_TAGS
    )
    config.filename = os.path.basename(__file__)

    if len(sys.argv) == 1:
        show_usage()
        sys.exit(0)

    try:
        opts, args = getopt.getopt(
            sys.argv[1:], "vb:d:", ["batch-list", "version", "debug-symbol="]
        )
        debug_symbols = []
        for opt, arg in opts:
            if opt in ("-v", "--version"):
                show_version(config.filename, config.build_label)
                break
            elif opt in ("--batch-list", "-b"):
                get_batch_list()
                break
            elif opt in ("--debug-symbol", "-d"):
                debug_symbols.append(arg)

        for arg in args:
            backtest(arg, debug_symbols)

    except getopt.GetoptError as e:
        print(f"Error parsing options:{e}\n")
        show_usage()
        sys.exit(0)

    sys.exit(0)
