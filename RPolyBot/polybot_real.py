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
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from web3 import Web3

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

load_dotenv()

RPC_ENDPOINTS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://matic-mainnet.chainstacklabs.com",
    os.getenv("POLYGON_RPC", "https://polygon-rpc.com"),
]


def get_w3_connection():
    for rpc in RPC_ENDPOINTS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                chain_id = w3.eth.chain_id
                if chain_id == 137:
                    logger.info(f"Connected to RPC: {rpc} (Chain ID: {chain_id})")
                    return w3, rpc
        except Exception:
            pass
    return None, None


W3, ACTIVE_RPC = get_w3_connection()
if not W3:
    logger.error(
        "Could not connect to any Polygon RPC. Please check your internet or .env"
    )

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

TARGET_TRADERS = [
    "0xc9b6227a295985591fe576ff2e054267a78a9b6a",
    "0x6bab41a0dc40d6dd4c1a915b8c01969479fd1292",
    "0xf58b1c1340d6f8c0871e8ea8ee7b80ec6b8a5f34",
    "0x7a0da16a1205ee51a56fa862e8baa61e350eff14",
    "0xdd225a03cd7ed89e3931906c67c75ab31cf89ef1",
    "0x6ade597c0e2b43c0bf3542cada8a5e330d73f5b0",
]

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 15))
MIN_TRADE_THRESHOLD = float(os.getenv("MIN_TRADE_THRESHOLD", 5.0))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", 0.04))
DAILY_TRADE_CAP = int(os.getenv("DAILY_TRADE_CAP", 60))
SLIPPAGE_TOLERANCE = float(os.getenv("SLIPPAGE_TOLERANCE", 0.06))
STOP_LOSS_THRESHOLD = float(os.getenv("STOP_LOSS_THRESHOLD", 0.20))
PRICE_BUFFER = float(os.getenv("PRICE_BUFFER", 0.02))
STARTING_BALANCE = 0.0

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

    def can_trade(self):
        if "daily_trades" not in self.stats:
            self.stats["daily_trades"] = []

        now = datetime.now()
        self.stats["daily_trades"] = [
            t
            for t in self.stats["daily_trades"]
            if (now - datetime.fromisoformat(t)).total_seconds() < 86400
        ]

        if len(self.stats["daily_trades"]) >= DAILY_TRADE_CAP:
            logger.warning(
                f"Daily Trade Cap Reached ({len(self.stats['daily_trades'])}/{DAILY_TRADE_CAP})"
            )
            return False
        return True

    def _load_stats(self):
        try:
            if os.path.exists(self.stats_file):
                with open(self.stats_file, "r") as f:
                    return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"Could not parse trader stats (corrupted JSON): {e}")
        except Exception as e:
            logger.warning(f"Could not load trader stats: {e}")
        return {"daily_trades": []}

    def _save_stats(self):
        _atomic_save_json(self.stats, self.stats_file)

    def _get_trader_label(self, address):
        for i, trader in enumerate(TARGET_TRADERS):
            if trader.lower() == address.lower():
                return f"Trader {i + 1}"
        return f"Trader {address[:8]}"

    def record_trade(self, trader_address, action, trade_value, pnl=0):
        if action in ["BUY", "SELL"]:
            if "daily_trades" not in self.stats:
                self.stats["daily_trades"] = []
            self.stats["daily_trades"].append(datetime.now().isoformat())

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
            if not isinstance(stats, dict):
                continue
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


class RealPortfolio:
    def __init__(
        self, state_file="portfolio_state.json", history_file="trade_history.json"
    ):
        self.state_file = state_file
        self.history_file = history_file
        self.balance = 0.0
        self.positions = self._load_portfolio_state()
        self.trade_history = self._load_history()
        self.client = self._init_client()
        if self.client:
            logger.info("Polymarket CLOB Client Authenticated")
        else:
            logger.warning(
                "CLOB Client initialization failed - trading will be disabled!"
            )

        self.usdc_contract_address = Web3.to_checksum_address(
            "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        )
        self.erc20_abi = json.loads(
            '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]'
        )

    def fetch_usdc_balance(self):
        if not WALLET_ADDRESS:
            logger.warning("No WALLET_ADDRESS configured in .env")
            return False

        try:
            global W3
            if W3 is None or not W3.is_connected():
                W3, _ = get_w3_connection()

            if not W3:
                logger.error("No RPC connection for balance fetch")
                return False

            contract = W3.eth.contract(
                address=self.usdc_contract_address, abi=self.erc20_abi
            )
            wallet = Web3.to_checksum_address(WALLET_ADDRESS)
            balance_raw = contract.functions.balanceOf(wallet).call()

            self.balance = balance_raw / 1_000_000
            logger.info(
                f"Fetched Wallet Balance: ${self.balance:.2f} (Address: {WALLET_ADDRESS[:6]}...{WALLET_ADDRESS[-4:]})"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to fetch USDC balance: {e}")
            return False

    def get_portfolio_value(self, price_fetcher):
        self.fetch_usdc_balance()
        total = self.balance

        try:
            url = f"https://data-api.polymarket.com/positions?user={WALLET_ADDRESS}"

            resp = requests.get(url, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                positions_value = 0.0

                for pos in data:
                    asset_id = (
                        pos.get("asset_id") or pos.get("token_id") or pos.get("asset")
                    )
                    size = float(
                        pos.get("size", 0)
                        or pos.get("amount", 0)
                        or pos.get("balance", 0)
                        or 0
                    )

                    if size > 0.01 and asset_id:
                        price = price_fetcher(asset_id)
                        if price:
                            positions_value += size * price

                total += positions_value
                return total

        except Exception as e:
            logger.warning(f"Failed to fetch real portfolio value: {e}")

        for asset_id, position in self.positions.items():
            try:
                price = price_fetcher(asset_id)
                if price and price > 0:
                    total += position["qty"] * price
            except Exception:
                pass

        return total if total > 0 else self.balance

    def _init_client(self):
        try:
            api_key = os.getenv("POLY_API_KEY")
            api_secret = os.getenv("POLY_API_SECRET")
            api_passphrase = os.getenv("POLY_API_PASSPHRASE")

            pk = os.getenv("PRIVATE_KEY", "").strip()
            if not pk:
                logger.error("NO PRIVATE_KEY found in .env")
                return None

            if not pk.startswith("0x"):
                pk = "0x" + pk

            if not all([api_key, api_secret, api_passphrase, WALLET_ADDRESS]):
                logger.warning("Missing CLOB API Credentials or WALLET_ADDRESS in .env")
                return None

            creds = ApiCreds(api_key, api_secret, api_passphrase)

            client = ClobClient(
                host="https://clob.polymarket.com",
                key=pk,
                chain_id=137,
                creds=creds,
                signature_type=2,
                funder=Web3.to_checksum_address(WALLET_ADDRESS),
            )
            try:
                client.get_ok()
            except Exception as e:
                logger.warning(f"CLOB Health check failed, but client initialized: {e}")

            return client

        except Exception as e:
            logger.error(f"Failed to initialize CLOB Client: {e}")
            return None

    def execute_trade(self, action, asset_id, quantity, price, title="Unknown"):
        logger.info(f"[REAL EXECUTION] {action} {quantity} of {title} @ ${price}")

        if not self.client:
            logger.info(
                "CLOB Client not initialized, attempting lazy initialization..."
            )
            self.client = self._init_client()
            if not self.client:
                logger.error("CLOB Client could not be initialized. Cannot trade.")
                return False, 0

        try:
            side = BUY if action.upper() == "BUY" else SELL

            if action.upper() == "BUY":
                limit_price = round(price + PRICE_BUFFER, 4)
                if limit_price > 0.999:
                    limit_price = 0.999
            else:
                limit_price = round(price - PRICE_BUFFER, 4)
                if limit_price < 0.01:
                    limit_price = 0.01

            if limit_price != price:
                logger.info(f"Price Buffered: ${price} -> ${limit_price} ({action})")

            order_args = OrderArgs(
                price=limit_price, size=quantity, side=side, token_id=asset_id
            )

            try:
                resp = self.client.create_and_post_order(order_args)
            except TypeError:
                resp = self.client.create_and_post_order(order_args, OrderType.FOK)

            if resp and resp.get("success"):
                order_id = resp.get("orderID", "unknown")
                logger.info(f"Trade Executed! ID: {order_id}")

                if action.upper() == "BUY":
                    if asset_id in self.positions:
                        pos = self.positions[asset_id]
                        new_qty = pos["qty"] + quantity
                        new_avg = (
                            (pos["qty"] * pos["avg_price"]) + (quantity * price)
                        ) / new_qty
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
                    pnl = 0
                else:
                    pnl = 0
                    if asset_id in self.positions:
                        pos = self.positions[asset_id]
                        pnl = (price - pos["avg_price"]) * quantity
                        pos["qty"] -= quantity
                        if pos["qty"] <= 0.001:
                            del self.positions[asset_id]

                self.trade_history.append(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "action": action,
                        "asset_id": asset_id,
                        "title": title,
                        "price": price,
                        "qty": quantity,
                        "pnl": pnl,
                        "order_id": order_id,
                    }
                )

                self._save_portfolio_state()
                self._save_history()
                return True, pnl
            else:
                error_msg = resp.get("errorMsg", "Unknown error")
                logger.error(f"Trade Failed: {error_msg}")
                return False, 0

        except Exception as e:
            logger.error(f"Execution Error: {e}")
            return False, 0

    def _load_portfolio_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load portfolio state: {e}")
        return {}

    def _save_portfolio_state(self):
        _atomic_save_json(self.positions, self.state_file)

    def _load_history(self):
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load trade history: {e}")
        return []

    def _save_history(self):
        _atomic_save_json(self.trade_history, self.history_file)


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
                response = self.session.get(url, timeout=30)

                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 404:
                    return None
                elif response.status_code == 429:
                    wait = RETRY_DELAY * (2**attempt) + random.uniform(0, 0.5)
                    logger.warning(f"Rate limited (429), waiting {wait:.1f}s...")
                    time.sleep(wait)
                elif response.status_code == 403:
                    logger.error(f"403 Forbidden for {url[:60]}...")
                    return None
                else:
                    logger.warning(f"HTTP {response.status_code} for {url[:60]}...")
            except Exception as e:
                logger.warning(f"Request failed: {str(e)[:100]}")
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
            self.portfolio.fetch_usdc_balance()
            port_val = self.portfolio.get_portfolio_value(
                lambda aid: self.get_market_price(aid, verbose=False) or 0
            )
            logger.info(
                f"Heartbeat: {self._poll_count} polls | Portfolio: ${port_val:.2f} | Cash: ${self.portfolio.balance:.2f} | Positions: {len(self.portfolio.positions)}"
            )

    def _check_stop_losses(self):
        if not self.portfolio.positions:
            return

        logger.debug(
            f"Checking stop-loss for {len(self.portfolio.positions)} positions..."
        )

        asset_ids = list(self.portfolio.positions.keys())

        for asset_id in asset_ids:
            try:
                if asset_id not in self.portfolio.positions:
                    continue

                pos = self.portfolio.positions[asset_id]
                avg_price = pos.get("avg_price")
                qty = pos.get("qty", 0)
                title = pos.get("title", "Unknown")

                if not avg_price or qty <= 0:
                    continue

                current_price = self.get_market_price(asset_id, verbose=False)
                if not current_price:
                    continue

                if current_price <= avg_price * (1 - STOP_LOSS_THRESHOLD):
                    loss_pct = ((avg_price - current_price) / avg_price) * 100
                    logger.info(
                        f"STOP-LOSS TRIGGERED: {title} @ ${current_price:.4f} (Entry: ${avg_price:.4f}, Loss: {loss_pct:.1f}%)"
                    )

                    success, pnl = self.portfolio.execute_trade(
                        "SELL", asset_id, qty, current_price, title
                    )
                    if success:
                        trader_address = "STOPLOSS_SYSTEM"
                        for t, state in self.previous_states.items():
                            if asset_id in state:
                                trader_address = t
                                break

                        self.trader_stats.record_trade(
                            trader_address,
                            "STOPLOSS_SELL",
                            qty * current_price,
                            pnl=pnl,
                        )

            except Exception as e:
                logger.error(f"Error checking stop-loss for {asset_id}: {e}")

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
                self._check_stop_losses()
                self._log_heartbeat()

                if not self.trader_stats.can_trade():
                    logger.warning("Daily Trade Cap Reached. Pausing...")
                    time.sleep(60)
                    continue

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
                            raw_qty = my_raw_trade_value / price

                            if raw_qty < MIN_TRADE_THRESHOLD:
                                if raw_qty > 1.5:
                                    scaled_qty = MIN_TRADE_THRESHOLD
                                    my_trade_value = scaled_qty * price
                                    method = "Forced Min (Threshold)"

                                    if my_trade_value > self.portfolio.balance:
                                        logger.info(
                                            f"Skipping forced buy - insufficient cash (${my_trade_value:.2f} > ${self.portfolio.balance:.2f})"
                                        )
                                        continue
                                    if my_trade_value > (my_raw_trade_value * 5):
                                        logger.info(
                                            f"Skipping forced buy - too far from intended allocation (${my_trade_value:.2f} vs intentional ${my_raw_trade_value:.2f})"
                                        )
                                        continue
                                else:
                                    logger.info(
                                        f"Skipping dust trade ({raw_qty:.2f} shares <= 1.5)"
                                    )
                                    continue
                            else:
                                scaled_qty = raw_qty
                                my_trade_value = my_raw_trade_value
                                method = "Matched %"

                            logger.info(
                                f"Detected BUY from {trader[:8]}... | {data['title']} | +{diff:.4f}"
                            )
                            logger.info(
                                f"   Method: {method} | Trader Alloc: {trader_alloc_pct:.1%} -> I Alloc: {my_alloc_pct:.1%}"
                            )
                            logger.info(
                                f"   Executing: ${my_trade_value:.2f} ({scaled_qty:.4f} shares)"
                            )

                            if self.trader_stats.can_trade():
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

                jitter = random.uniform(0.5, 2.0)
                wait_time = POLL_INTERVAL * jitter
                time.sleep(wait_time)

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

    my_portfolio = RealPortfolio()
    trader_stats = TraderStats()
    bot = PolymarketWatcher(my_portfolio, trader_stats)

    logger.info("Fetching wallet balance...")
    if my_portfolio.fetch_usdc_balance():
        logger.info(f"Starting with {my_portfolio.balance:.2f} USDC")
        STARTING_BALANCE = my_portfolio.balance
    else:
        logger.warning("Could not fetch balance - using 0.0")
        my_portfolio.balance = 0.0
        STARTING_BALANCE = 0.0

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
        overall_pnl_pct = (
            (overall_pnl / STARTING_BALANCE) * 100 if STARTING_BALANCE > 0 else 0
        )
        logger.info(f"Overall P&L: ${overall_pnl:+.2f} ({overall_pnl_pct:+.1f}%)")
        logger.info("=" * 50)
