import concurrent.futures
import json
import logging
import os
import random
import shutil
import signal
import sys
import tempfile
import time
from datetime import datetime
from threading import Lock

import requests

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
if sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

file_handler = logging.FileHandler("polybot.log", encoding="utf-8")
file_handler.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(console_handler)
logger.addHandler(file_handler)

TARGET_TRADERS = [
    "0xc9b6227a295985591fe576ff2e054267a78a9b6a",
    "0x6bab41a0dc40d6dd4c1a915b8c01969479fd1292",
    "0xf58b1c1340d6f8c0871e8ea8ee7b80ec6b8a5f34",
    "0x7a0da16a1205ee51a56fa862e8baa61e350eff14",
    "0xdd225a03cd7ed89e3931906c67c75ab31cf89ef1",
    "0x6ade597c0e2b43c0bf3542cada8a5e330d73f5b0",
]


POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 5))
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", 1000.0))
SLIPPAGE_TOLERANCE = float(os.getenv("SLIPPAGE_TOLERANCE", 0.05))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", 0.05))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", 0.2))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 2))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", 1.0))
MAX_TRADE_HISTORY = int(os.getenv("MAX_TRADE_HISTORY", 500))
MAX_TRADER_TRADES = int(os.getenv("MAX_TRADER_TRADES", 100))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", 6))

shutdown_requested = False


def _atomic_save_json(data, filepath):
    try:
        dir_path = os.path.dirname(filepath) or "."
        with tempfile.NamedTemporaryFile(
            "w", delete=False, suffix=".json", dir=dir_path
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        shutil.move(tmp.name, filepath)
        return True
    except Exception as e:
        logger.error(f"Failed to save {filepath}: {e}")
        try:
            if "tmp" in locals() and os.path.exists(tmp.name):
                os.unlink(tmp.name)
        except Exception:
            pass
        return False


class TraderStats:
    def __init__(self, stats_file="trader_stats.json"):
        self.stats_file = stats_file
        self.stats = self._load_stats()

    def _load_stats(self):
        try:
            if os.path.exists(self.stats_file):
                with open(self.stats_file, "r") as f:
                    return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"Could not parse trader stats (corrupted JSON): {e}")
        except Exception as e:
            logger.warning(f"Could not load trader stats: {e}")
        return {}

    def _save_stats(self):
        _atomic_save_json(self.stats, self.stats_file)

    def _get_trader_label(self, address):
        for i, trader in enumerate(TARGET_TRADERS):
            if trader.lower() == address.lower():
                return f"Trader {i + 1}"
        return f"Trader {address[:8]}"

    def record_trade(self, trader_address, action, trade_value, pnl=0):
        if trader_address not in self.stats:
            self.stats[trader_address] = {
                "label": self._get_trader_label(trader_address),
                "total_trades": 0,
                "buy_count": 0,
                "sell_count": 0,
                "resolution_count": 0,
                "total_volume": 0,
                "total_pnl": 0,
                "trades": [],
            }

        stats = self.stats[trader_address]
        stats["total_trades"] += 1
        stats["total_volume"] += trade_value

        if action == "BUY":
            stats["buy_count"] += 1
        elif action == "SELL":
            stats["sell_count"] += 1
            stats["total_pnl"] += pnl
        elif action == "RESOLUTION":
            stats["resolution_count"] = stats.get("resolution_count", 0) + 1
            stats["total_pnl"] += pnl

        stats["trades"].append(
            {
                "timestamp": datetime.now().isoformat(),
                "action": action,
                "value": trade_value,
                "pnl": pnl,
            }
        )

        if len(stats["trades"]) > MAX_TRADER_TRADES:
            stats["trades"] = stats["trades"][-MAX_TRADER_TRADES:]

        self._save_stats()

    def get_summary(self):
        summary = []
        for address, stats in self.stats.items():
            summary.append(
                {
                    "address": address,
                    "label": stats["label"],
                    "trades": stats["total_trades"],
                    "buys": stats["buy_count"],
                    "sells": stats["sell_count"],
                    "volume": stats["total_volume"],
                    "pnl": stats["total_pnl"],
                }
            )
        return sorted(summary, key=lambda x: x["pnl"], reverse=True)

    def print_report(self):
        summary = self.get_summary()
        if not summary:
            logger.info("No trader statistics available yet")
            return

        logger.info("")
        logger.info("=" * 60)
        logger.info("TRADER PERFORMANCE REPORT")
        logger.info("=" * 60)

        for trader in summary:
            logger.info(f"{trader['label']} ({trader['address'][:8]}...)")
            logger.info(
                f"   Trades: {trader['trades']} (Buy: {trader['buys']}, Sell: {trader['sells']})"
            )
            logger.info(f"   Volume: ${trader['volume']:.2f}")
            logger.info(f"   P&L:    ${trader['pnl']:+.2f}")
            logger.info("-" * 40)

        total_pnl = sum(t["pnl"] for t in summary)
        total_volume = sum(t["volume"] for t in summary)
        total_trades = sum(t["trades"] for t in summary)

        logger.info(
            f"TOTALS: {total_trades} trades | ${total_volume:.2f} volume | ${total_pnl:+.2f} P&L"
        )
        logger.info("=" * 60)


class PaperPortfolio:
    def __init__(
        self,
        balance,
        history_file="trade_history.json",
        state_file="portfolio_state.json",
    ):
        self.state_file = state_file
        self.history_file = history_file

        loaded_state = self._load_portfolio_state()
        if loaded_state:
            self.balance = loaded_state.get("balance", balance)
            self.positions = loaded_state.get("positions", {})
            logger.info(
                f"Restored portfolio: ${self.balance:.2f} cash, {len(self.positions)} positions"
            )
        else:
            self.balance = balance
            self.positions = {}
            logger.info(f"Starting fresh portfolio: ${self.balance:.2f}")

        self.trade_history = self._load_history()

    def _load_portfolio_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"Portfolio state file corrupted: {e}")
        except Exception as e:
            logger.warning(f"Could not load portfolio state: {e}")
        return None

    def _save_portfolio_state(self):
        state = {"balance": self.balance, "positions": self.positions}
        _atomic_save_json(state, self.state_file)

    def _load_history(self):
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, "r") as f:
                    return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"Trade history file corrupted: {e}")
        except Exception as e:
            logger.warning(f"Could not load trade history: {e}")
        return []

    def _save_history(self):
        if len(self.trade_history) > MAX_TRADE_HISTORY:
            self.trade_history = self.trade_history[-MAX_TRADE_HISTORY:]
        _atomic_save_json(self.trade_history, self.history_file)

    def execute_trade(self, action, asset_id, quantity, price, title="Unknown"):
        if price is None or price <= 0:
            logger.warning(f"Skipping trade - invalid price ({price}) for {title}")
            return False, 0

        cost = quantity * price
        pnl = 0

        if action == "BUY":
            if cost > self.balance:
                if self.balance > 1.0:
                    quantity = self.balance / price
                    cost = self.balance
                    logger.warning(f"Insufficient funds, adjusted buy to ${cost:.2f}")
                else:
                    logger.warning(f"Insufficient funds to buy {title}")
                    return False, 0

            self.balance -= cost

            if asset_id in self.positions:
                old_qty = self.positions[asset_id]["qty"]
                old_cost = old_qty * self.positions[asset_id]["avg_price"]
                new_qty = old_qty + quantity
                new_avg = (old_cost + cost) / new_qty
                self.positions[asset_id] = {
                    "qty": new_qty,
                    "avg_price": new_avg,
                    "title": title,
                }
            else:
                self.positions[asset_id] = {
                    "qty": quantity,
                    "avg_price": price,
                    "title": title,
                }

            logger.info(
                f"[BUY] {title}: {quantity:.4f} shares @ ${price:.4f} | Balance: ${self.balance:.2f}"
            )

            self.trade_history.append(
                {
                    "timestamp": datetime.now().isoformat(),
                    "action": action,
                    "asset_id": asset_id,
                    "asset": title,
                    "price": price,
                    "qty": quantity,
                }
            )
            self._save_history()

        elif action == "SELL":
            if asset_id not in self.positions:
                return False, 0

            current_holdings = self.positions[asset_id]["qty"]
            sell_qty = min(quantity, current_holdings)
            revenue = sell_qty * price

            self.balance += revenue

            avg_entry = self.positions[asset_id]["avg_price"]
            pnl = (price - avg_entry) * sell_qty
            pnl_pct = ((price / avg_entry) - 1) * 100 if avg_entry > 0 else 0

            self.positions[asset_id]["qty"] -= sell_qty
            if self.positions[asset_id]["qty"] <= 0.0001:
                del self.positions[asset_id]

            logger.info(
                f"[SELL] {title}: {sell_qty:.4f} shares @ ${price:.4f} | P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%) | Balance: ${self.balance:.2f}"
            )

            self.trade_history.append(
                {
                    "timestamp": datetime.now().isoformat(),
                    "action": action,
                    "asset_id": asset_id,
                    "asset": title,
                    "price": price,
                    "qty": sell_qty,
                    "pnl": pnl,
                }
            )
            self._save_history()

        self._save_portfolio_state()
        return True, pnl

    def get_portfolio_value(self, price_fetcher):
        total = self.balance
        for asset_id, data in self.positions.items():
            price = price_fetcher(asset_id)
            if price:
                total += data["qty"] * price
        return total


class PolymarketWatcher:
    def __init__(self, portfolio, trader_stats):
        self.state_file = "bot_state.json"
        self.previous_states = self._load_state()
        self.portfolio = portfolio
        self.trader_stats = trader_stats
        self.api_base = "https://data-api.polymarket.com"
        self._price_cache = {}
        self._state_lock = Lock()
        self._poll_count = 0

        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "PolyBot/1.0", "Accept": "application/json"}
        )

    def _load_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"State file corrupted (will start fresh): {e}")
        except Exception as e:
            logger.error(f"Could not load state file: {e}")
        return {}

    def _save_state(self):
        with self._state_lock:
            _atomic_save_json(self.previous_states, self.state_file)

    def _request_with_retry(self, url, retries=MAX_RETRIES):
        for attempt in range(retries):
            try:
                time.sleep(REQUEST_DELAY)
                response = self.session.get(url, timeout=10)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 404:
                    return None
                elif response.status_code == 429:
                    wait = RETRY_DELAY * (2**attempt) + random.uniform(0, 0.5)
                    logger.warning(f"Rate limited, waiting {wait:.1f}s...")
                    time.sleep(wait)
                else:
                    logger.warning(f"HTTP {response.status_code} for {url[:60]}...")
            except requests.exceptions.Timeout:
                logger.warning(f"Request timeout (attempt {attempt + 1}/{retries})")
                time.sleep(RETRY_DELAY)
            except requests.exceptions.RequestException as e:
                logger.warning(f"Request failed: {e}")
                time.sleep(RETRY_DELAY)
        return None

    def get_market_price(self, asset_id, verbose=True):
        if isinstance(asset_id, str) and asset_id.startswith("0x"):
            try:
                token_id_decimal = str(int(asset_id, 16))
            except ValueError:
                if verbose:
                    logger.error(f"Invalid hex token_id: {asset_id}")
                return None
        else:
            token_id_decimal = str(asset_id)

        url = f"https://clob.polymarket.com/price?token_id={token_id_decimal}&side=buy"
        data = self._request_with_retry(url, retries=1)

        if data and "price" in data:
            try:
                price = float(data["price"])
                if price > 0:
                    self._price_cache[asset_id] = price
                    return price
            except (ValueError, TypeError) as e:
                if verbose:
                    logger.debug(f"Could not parse CLOB price: {e}")

        url2 = (
            f"https://gamma-api.polymarket.com/markets?clob_token_id={token_id_decimal}"
        )
        data2 = self._request_with_retry(url2, retries=1)

        if data2 and isinstance(data2, list) and len(data2) > 0:
            market = data2[0]
            price = None
            if "outcomePrices" in market:
                try:
                    op = market["outcomePrices"]
                    if isinstance(op, str):
                        op = json.loads(op)
                    clob_ids = market.get("clobTokenIds", [])
                    if isinstance(clob_ids, str):
                        clob_ids = json.loads(clob_ids)
                    if token_id_decimal in clob_ids:
                        idx = clob_ids.index(token_id_decimal)
                        if idx < len(op):
                            price = float(op[idx])
                except (json.JSONDecodeError, ValueError, TypeError) as e:
                    if verbose:
                        logger.debug(f"Could not parse gamma prices: {e}")

            if price is None:
                price = market.get("lastTradePrice") or market.get("bestBid")

            if price is not None and float(price) > 0:
                self._price_cache[asset_id] = float(price)
                return float(price)

        if asset_id in self._price_cache:
            return self._price_cache[asset_id]

        return None

    def get_market_resolution(self, asset_id):
        if isinstance(asset_id, str) and asset_id.startswith("0x"):
            try:
                token_id_decimal = str(int(asset_id, 16))
            except ValueError:
                return False, None, "Unknown"
        else:
            token_id_decimal = str(asset_id)

        url = (
            f"https://gamma-api.polymarket.com/markets?clob_token_id={token_id_decimal}"
        )
        data = self._request_with_retry(url, retries=1)

        if not data or not isinstance(data, list) or len(data) == 0:
            return False, None, "Unknown"

        market = data[0]
        market_title = market.get("question", market.get("title", "Unknown Market"))

        is_resolved = market.get("resolved", False)
        if not is_resolved:
            resolution_source = market.get("resolutionSource")
            if resolution_source or (market.get("outcome") is not None):
                is_resolved = True

        if not is_resolved:
            return False, None, market_title

        try:
            outcome_prices = market.get("outcomePrices", [])
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)

            clob_ids = market.get("clobTokenIds", [])
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)

            if token_id_decimal in clob_ids:
                idx = clob_ids.index(token_id_decimal)
                if idx < len(outcome_prices):
                    final_price = float(outcome_prices[idx])
                    if final_price >= 0.95:
                        return True, True, market_title
                    elif final_price <= 0.05:
                        return True, False, market_title

            outcome = market.get("outcome")
            outcomes = market.get("outcomes", [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)

            if outcome and outcomes and token_id_decimal in clob_ids:
                idx = clob_ids.index(token_id_decimal)
                if idx < len(outcomes):
                    token_outcome_name = outcomes[idx]
                    is_winner = token_outcome_name.lower() == outcome.lower()
                    return True, is_winner, market_title

        except (json.JSONDecodeError, ValueError, TypeError, IndexError) as e:
            logger.debug(f"Could not determine resolution outcome: {e}")

        return True, None, market_title

    def get_user_positions(self, address):
        url = f"{self.api_base}/positions?user={address}"
        data = self._request_with_retry(url)
        if data is None:
            return None

        clean_map = {}
        try:
            for pos in data:
                asset_id = (
                    pos.get("asset_id") or pos.get("token_id") or pos.get("asset")
                )
                if not asset_id:
                    continue
                size = float(
                    pos.get("size", 0)
                    or pos.get("amount", 0)
                    or pos.get("balance", 0)
                    or 0
                )
                if size > 0.001:
                    avg_price = (
                        pos.get("avgPrice")
                        or pos.get("avg_price")
                        or pos.get("averagePrice")
                    )
                    try:
                        avg_price = float(avg_price) if avg_price else None
                    except (ValueError, TypeError):
                        avg_price = None

                    clean_map[asset_id] = {
                        "qty": size,
                        "title": pos.get("title", "Unknown Market"),
                        "avg_price": avg_price,
                    }
        except Exception as e:
            logger.warning(f"Error parsing positions for {address[:8]}: {e}")
            return None
        return clean_map

    def _calculate_portfolio_value(self, current_state):
        total = 0
        for asset_id, data in current_state.items():
            price = self.get_market_price(asset_id, verbose=False)
            if price:
                total += data["qty"] * price
        return total

    def _check_slippage(self, current_price, whale_avg_price, title):
        if whale_avg_price is None or whale_avg_price <= 0:
            return False, "No whale entry price available"

        if current_price <= 0:
            return True, "Invalid current price"

        price_diff_pct = (current_price - whale_avg_price) / whale_avg_price

        if price_diff_pct > SLIPPAGE_TOLERANCE:
            whale_pnl_pct = price_diff_pct * 100
            return (
                True,
                f"SKIP: Buying the top! Current ${current_price:.4f} > Whale entry ${whale_avg_price:.4f} (+{whale_pnl_pct:.1f}% above tolerance)",
            )
        elif price_diff_pct < 0:
            discount_pct = abs(price_diff_pct) * 100
            return (
                False,
                f"BETTER ENTRY: Current ${current_price:.4f} < Whale entry ${whale_avg_price:.4f} ({discount_pct:.1f}% discount!)",
            )
        else:
            return (
                False,
                f"Price OK: Current ${current_price:.4f} vs Whale ${whale_avg_price:.4f} ({price_diff_pct * 100:+.1f}%)",
            )

    def _log_heartbeat(self):
        self._poll_count += 1
        if self._poll_count % HEARTBEAT_INTERVAL == 0:
            port_val = self.portfolio.get_portfolio_value(
                lambda aid: self.get_market_price(aid, verbose=False) or 0
            )
            logger.info(
                f"Heartbeat: {self._poll_count} polls | Portfolio: ${port_val:.2f} | Cash: ${self.portfolio.balance:.2f} | Positions: {len(self.portfolio.positions)}"
            )

    def monitor(self):
        global shutdown_requested

        logger.info(f"Started watching {len(TARGET_TRADERS)} trader(s)...")
        logger.info(
            f"Config: Poll={POLL_INTERVAL}s, Slippage={SLIPPAGE_TOLERANCE * 100:.0f}%, MaxPos={MAX_POSITION_PCT * 100:.0f}%"
        )
        logger.info("Initializing state in parallel (fast)...")

        def fetch_state(trader):
            try:
                state = self.get_user_positions(trader)
                return trader, state
            except Exception as e:
                logger.warning(f"Failed to fetch state for {trader[:8]}: {e}")
                return trader, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_trader = {
                executor.submit(fetch_state, t): t for t in TARGET_TRADERS
            }
            for future in concurrent.futures.as_completed(future_to_trader):
                trader, state = future.result()
                if state:
                    with self._state_lock:
                        self.previous_states[trader] = state
                    logger.info(f"Loaded {len(state)} positions for {trader[:8]}...")

        self._save_state()
        logger.info("State initialized. Waiting for NEW trades...")
        logger.info(f"Current Balance: ${self.portfolio.balance:.2f}")

        while not shutdown_requested:
            try:
                self._log_heartbeat()

                for trader in TARGET_TRADERS:
                    if shutdown_requested:
                        break

                    current_state = self.get_user_positions(trader)
                    if current_state is None:
                        continue

                    prev_state = self.previous_states.get(trader, {})

                    for asset_id, data in current_state.items():
                        current_qty = data["qty"]
                        prev_data = prev_state.get(asset_id)
                        prev_qty = prev_data["qty"] if prev_data else 0

                        if current_qty > prev_qty:
                            diff = current_qty - prev_qty
                            price = self.get_market_price(asset_id, verbose=True)

                            if price is None or price <= 0:
                                logger.warning(
                                    f"Skipping BUY - invalid price for {data['title'][:30]}"
                                )
                                continue

                            whale_avg_price = data.get("avg_price")
                            should_skip, slippage_msg = self._check_slippage(
                                price, whale_avg_price, data["title"]
                            )
                            logger.info(f"   {slippage_msg}")

                            if should_skip:
                                continue

                            trader_portfolio_value = self._calculate_portfolio_value(
                                current_state
                            )
                            trade_value = diff * price

                            if trader_portfolio_value > 10.0:
                                trader_alloc_pct = trade_value / trader_portfolio_value
                            else:
                                trader_alloc_pct = 0.0

                            my_alloc_pct = min(trader_alloc_pct, MAX_POSITION_PCT)

                            my_current_port_val = self.portfolio.get_portfolio_value(
                                lambda aid: (
                                    self.get_market_price(aid, verbose=False) or 0
                                )
                            )

                            my_raw_trade_value = my_current_port_val * my_alloc_pct

                            MIN_TRADE_THRESHOLD = 1.00
                            if my_raw_trade_value < MIN_TRADE_THRESHOLD:
                                if trader_alloc_pct > 0.02:
                                    my_trade_value = 2.0
                                    method = "Forced Min (High Conviction)"
                                else:
                                    logger.info(
                                        f"Skipping dust trade (${my_raw_trade_value:.2f} < ${MIN_TRADE_THRESHOLD})"
                                    )
                                    continue
                            else:
                                my_trade_value = my_raw_trade_value
                                method = "Matched %"

                            if my_trade_value < 2.0:
                                my_trade_value = 2.0

                            scaled_qty = my_trade_value / price

                            logger.info(
                                f"Detected BUY from {trader[:8]}... | {data['title']} | +{diff:.4f}"
                            )
                            logger.info(
                                f"   Method: {method} | Trader Alloc: {trader_alloc_pct:.1%} -> I Alloc: {my_alloc_pct:.1%}"
                            )
                            logger.info(
                                f"   Executing: ${my_trade_value:.2f} ({scaled_qty:.4f} shares)"
                            )

                            success, _ = self.portfolio.execute_trade(
                                "BUY", asset_id, scaled_qty, price, data["title"]
                            )
                            if success:
                                self.trader_stats.record_trade(
                                    trader, "BUY", my_trade_value, pnl=0
                                )

                    for asset_id, data in prev_state.items():
                        prev_qty = data["qty"]
                        current_data = current_state.get(asset_id)
                        current_qty = current_data["qty"] if current_data else 0

                        if current_qty < prev_qty:
                            diff = prev_qty - current_qty

                            if (
                                current_qty == 0
                                and asset_id in self.portfolio.positions
                            ):
                                is_resolved, is_winner, market_title = (
                                    self.get_market_resolution(asset_id)
                                )

                                if is_resolved:
                                    my_position = self.portfolio.positions[asset_id]
                                    my_qty = my_position["qty"]
                                    my_avg_price = my_position["avg_price"]

                                    if is_winner is True:
                                        settlement_price = 1.0
                                        pnl = (settlement_price - my_avg_price) * my_qty
                                        self.portfolio.balance += (
                                            my_qty * settlement_price
                                        )
                                        logger.info(
                                            f"[RESOLUTION - WIN] {market_title}"
                                        )
                                        logger.info(
                                            f"   {my_qty:.4f} shares @ $1.00 = ${my_qty:.2f}"
                                        )
                                        logger.info(
                                            f"   P&L: ${pnl:+.2f} (entry @ ${my_avg_price:.4f})"
                                        )
                                    elif is_winner is False:
                                        settlement_price = 0.0
                                        pnl = -my_avg_price * my_qty
                                        logger.info(
                                            f"[RESOLUTION - LOSS] {market_title}"
                                        )
                                        logger.info(
                                            f"   {my_qty:.4f} shares @ $0.00 = $0.00"
                                        )
                                        logger.info(
                                            f"   P&L: ${pnl:+.2f} (entry @ ${my_avg_price:.4f})"
                                        )
                                    else:
                                        price = (
                                            self.get_market_price(
                                                asset_id, verbose=False
                                            )
                                            or 0
                                        )
                                        if price >= 0.5:
                                            settlement_price = 1.0
                                            pnl = (1.0 - my_avg_price) * my_qty
                                            self.portfolio.balance += my_qty
                                            logger.info(
                                                f"[RESOLUTION - LIKELY WIN] {market_title}"
                                            )
                                        else:
                                            settlement_price = 0.0
                                            pnl = -my_avg_price * my_qty
                                            logger.info(
                                                f"[RESOLUTION - LIKELY LOSS] {market_title}"
                                            )
                                        logger.info(
                                            "   Could not confirm outcome, estimated from price"
                                        )

                                    del self.portfolio.positions[asset_id]
                                    self.portfolio._save_portfolio_state()
                                    self.portfolio.trade_history.append(
                                        {
                                            "timestamp": datetime.now().isoformat(),
                                            "action": "RESOLUTION",
                                            "asset_id": asset_id,
                                            "asset": market_title,
                                            "price": settlement_price,
                                            "qty": my_qty,
                                            "pnl": pnl,
                                            "outcome": "WIN"
                                            if is_winner
                                            else "LOSS"
                                            if is_winner is False
                                            else "UNKNOWN",
                                        }
                                    )
                                    self.portfolio._save_history()
                                    self.trader_stats.record_trade(
                                        trader,
                                        "RESOLUTION",
                                        my_qty * (settlement_price or my_avg_price),
                                        pnl=pnl,
                                    )
                                    continue

                            price = self.get_market_price(asset_id, verbose=True)

                            if price is None or price <= 0:
                                continue

                            if asset_id in self.portfolio.positions:
                                their_sell_pct = (
                                    diff / prev_qty if prev_qty > 0 else 1.0
                                )
                                my_position_qty = self.portfolio.positions[asset_id][
                                    "qty"
                                ]
                                scaled_qty = my_position_qty * their_sell_pct

                                type_label = (
                                    "PARTIAL SELL" if current_qty > 0 else "FULL EXIT"
                                )
                                logger.info(
                                    f"Detected {type_label} from {trader[:8]}... | -{diff:.4f}"
                                )

                                if scaled_qty > 0:
                                    sell_value = scaled_qty * price
                                    success, pnl = self.portfolio.execute_trade(
                                        "SELL",
                                        asset_id,
                                        scaled_qty,
                                        price,
                                        data["title"],
                                    )
                                    if success:
                                        self.trader_stats.record_trade(
                                            trader, "SELL", sell_value, pnl=pnl
                                        )

                    with self._state_lock:
                        self.previous_states[trader] = current_state
                    self._save_state()

                time.sleep(POLL_INTERVAL)

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(POLL_INTERVAL)


def graceful_shutdown(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    logger.info("\nShutdown signal received, finishing current cycle...")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    logger.info("=" * 50)
    logger.info("PolyBot - Polymarket Copy Trading Bot")
    logger.info("=" * 50)

    my_portfolio = PaperPortfolio(STARTING_BALANCE)
    trader_stats = TraderStats()
    bot = PolymarketWatcher(my_portfolio, trader_stats)

    try:
        bot.monitor()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("\nStopping Bot...")

        trader_stats.print_report()

        logger.info("=" * 50)
        logger.info("FINAL PORTFOLIO STATUS")
        logger.info("=" * 50)
        logger.info(f"Cash Balance: ${my_portfolio.balance:.2f}")

        total_value = my_portfolio.balance
        if my_portfolio.positions:
            logger.info("Open Positions:")
            for asset_id, pos in my_portfolio.positions.items():
                price = bot.get_market_price(asset_id, verbose=False)
                if price:
                    pos_value = pos["qty"] * price
                    total_value += pos_value
                    unrealized_pnl = (price - pos["avg_price"]) * pos["qty"]
                    logger.info(
                        f" - {pos['title']}: {pos['qty']:.4f} @ avg ${pos['avg_price']:.4f} | Value: ${pos_value:.2f} | P&L: ${unrealized_pnl:+.2f}"
                    )
                else:
                    logger.info(
                        f" - {pos['title']}: {pos['qty']:.4f} @ avg ${pos['avg_price']:.4f}"
                    )
        else:
            logger.info("No open positions")

        logger.info(f"Total Portfolio Value: ${total_value:.2f}")
        overall_pnl = total_value - STARTING_BALANCE
        overall_pnl_pct = (overall_pnl / STARTING_BALANCE) * 100
        logger.info(f"Overall P&L: ${overall_pnl:+.2f} ({overall_pnl_pct:+.1f}%)")
        logger.info("=" * 50)
