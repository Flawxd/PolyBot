import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
from datetime import datetime
from typing import List, Optional

import aiohttp

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
if sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
file_handler = logging.FileHandler("azbot.log", encoding="utf-8")
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(console_handler)
logger.addHandler(file_handler)

GRAPH_ENDPOINT = os.getenv(
    "GRAPH_ENDPOINT",
    "https://thegraph.azuro.org/subgraphs/name/azuro-protocol/azuro-api-polygon-v3",
)
POLYGON_RPC = "https://polygon-rpc.com"
USDT_CONTRACT = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 10))
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", 1000.0))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", 0.05))
SLIPPAGE_TOLERANCE = float(os.getenv("SLIPPAGE_TOLERANCE", 0.05))
MIN_BET_SIZE = 2.0
MIN_WINRATE = 0.60
MIN_TOTAL_BETS = 10
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 2))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", 1.0))
MAX_TRACKED_TRADERS = int(os.getenv("MAX_TRACKED_TRADERS", 30))
REFRESH_TRADERS_INTERVAL = 3600

shutdown_requested = False

QUERY_PROFITABLE_CANDIDATES = """
{
  bettors(first: 1000, orderBy: totalProfit, orderDirection: desc, where: {totalProfit_gt: "500"}) {
    id
    totalProfit
    betsCount
    wonBetsCount
  }
}
"""

QUERY_USER_BETS = """
query GetUserBets($user: String!, $timestamp: BigInt!) {
  bets(
    where: {actor: $user, createdBlockTimestamp_gt: $timestamp}
    orderBy: createdBlockTimestamp
    orderDirection: desc
    first: 5
  ) {
    id
    actor { id }
    amount
    odds
    createdBlockTimestamp
    status
    selections {
      outcome {
        outcomeId
        condition {
          game {
            sport { name }
            league { name }
            participants { name }
          }
        }
      }
    }
  }
}
"""

QUERY_BET_STATUS = """
query GetBetStatus($ids: [ID!]!) {
  bets(where: {id_in: $ids}) {
    id
    status
    result
    payout
  }
}
"""

QUERY_ODDS = """
query GetOdds($id: ID!) {
  outcome(id: $id) {
    currentOdds
  }
}
"""


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
        return False


class AzbotState:
    def __init__(self, state_file="azbot_state.json"):
        self.state_file = state_file
        self.trader_cursors = {}
        self.target_traders = []
        self.last_refresh_ts = 0
        self._load()

    def _load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    self.trader_cursors = data.get("trader_cursors", {})
                    self.target_traders = data.get("target_traders", [])
                    self.last_refresh_ts = data.get("last_refresh_ts", 0)
                    logger.info(
                        f"Loaded state: {len(self.target_traders)} traders tracked"
                    )
            except Exception as e:
                logger.warning(f"Could not load state: {e}")

    def save(self):
        data = {
            "trader_cursors": self.trader_cursors,
            "target_traders": self.target_traders,
            "last_refresh_ts": self.last_refresh_ts,
        }
        _atomic_save_json(data, self.state_file)

    def get_cursor(self, trader):
        return self.trader_cursors.get(trader, int(time.time()) - 3600)

    def update_cursor(self, trader, timestamp):
        if timestamp > self.trader_cursors.get(trader, 0):
            self.trader_cursors[trader] = timestamp


class AsyncAzuroClient:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.session: Optional[aiohttp.ClientSession] = None

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        if self.session:
            await self.session.close()

    async def query_graph(self, query: str, variables: dict = None) -> Optional[dict]:
        session = await self.get_session()
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with session.post(
                    self.endpoint,
                    json={"query": query, "variables": variables},
                    timeout=10,
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if "errors" in data:
                            logger.error(f"GraphQL Error: {data['errors']}")
                            return None
                        return data.get("data")
            except Exception as e:
                logger.warning(f"Request failed (attempt {attempt + 1}): {e}")

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
        return None

    async def get_smart_traders(self) -> List[dict]:
        logger.info("Scanning for Sharp Traders (Deep Scan up to 5000)...")

        active_since = int(time.time()) - 604800
        candidates = []

        for page in range(5):
            skip = page * 1000

            query = QUERY_PROFITABLE_CANDIDATES.replace(
                'where: {totalProfit_gt: "500"}',
                f'where: {{totalProfit_gt: "500", lastBetTimestamp_gt: "{active_since}"}}',
            ).replace("first: 1000", f"first: 1000, skip: {skip}")

            data = await self.query_graph(query)
            if not data or "bettors" not in data:
                break

            batch = data["bettors"]
            candidates.extend(batch)
            logger.info(
                f"   ...scanned {len(batch)} candidates (Total: {len(candidates)})"
            )

            if len(batch) < 1000:
                break

        qualified_traders = []

        for user in candidates:
            wins_count = int(user.get("wonBetsCount", 0))
            total_bets = int(user.get("betsCount", 0))
            profit = float(user.get("totalProfit", 0))

            if total_bets < MIN_TOTAL_BETS:
                continue

            winrate = wins_count / total_bets if total_bets > 0 else 0

            if winrate >= MIN_WINRATE:
                disp_id = user["id"][:6]
                if "_" in user["id"]:
                    parts = user["id"].split("_")
                    if len(parts) > 1:
                        disp_id = f"{parts[0][:6]}..{parts[1][:4]}"

                logger.info(
                    f"Found Sharp: {disp_id} | Winrate: {winrate:.1%} ({wins_count}/{total_bets}) | Profit: ${profit:.0f}"
                )

                qualified_traders.append(
                    {
                        "id": user["id"],
                        "winrate": winrate,
                        "profit": profit,
                        "wins": wins_count,
                        "total": total_bets,
                    }
                )

            if len(qualified_traders) >= MAX_TRACKED_TRADERS:
                break

        return qualified_traders

    async def get_recent_bets(self, user_address: str, since_timestamp: int):
        variables = {"user": user_address.lower(), "timestamp": int(since_timestamp)}
        data = await self.query_graph(QUERY_USER_BETS, variables)
        return data.get("bets", []) if data else []

    async def get_bets_status(self, bet_ids: List[str]):
        variables = {"ids": bet_ids}
        data = await self.query_graph(QUERY_BET_STATUS, variables)
        return data.get("bets", []) if data else []

    async def get_market_odds(self, outcome_id: str) -> float:
        variables = {"id": outcome_id}
        data = await self.query_graph(QUERY_ODDS, variables)
        if data and "outcome" in data and data["outcome"]:
            return float(data["outcome"].get("currentOdds", 0))
        return 0.0

    async def get_token_balance(self, wallet_address: str) -> float:
        return 10000.0


class PaperPortfolio:
    def __init__(self, filename="paper_portfolio.json"):
        self.filename = filename
        self.balance = STARTING_BALANCE
        self.positions = {}
        self.history = []
        self.realized_pnl = 0.0
        self._load()

    def _load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, "r") as f:
                    data = json.load(f)
                    self.balance = data.get("balance", self.balance)
                    self.positions = data.get("positions", {})
                    self.realized_pnl = data.get("realized_pnl", 0.0)
            except Exception:
                pass

    def _save(self):
        data = {
            "balance": self.balance,
            "positions": self.positions,
            "realized_pnl": self.realized_pnl,
        }
        _atomic_save_json(data, self.filename)

    async def open_position(self, signal_bet, trader_label, client: AsyncAzuroClient):
        bet_id = signal_bet["id"]
        if bet_id in self.positions:
            return

        selections = signal_bet.get("selections", [])
        if not selections:
            return

        selection = selections[0]
        outcome_data = selection.get("outcome", {})
        if not outcome_data:
            return

        outcome_id = outcome_data.get("outcomeId")
        signal_odds = float(signal_bet.get("odds", 0))
        whale_bet_amount = float(signal_bet.get("amount", 0))
        whale_address = signal_bet["actor"]["id"]

        current_odds = await client.get_market_odds(outcome_id)
        if current_odds == 0:
            return

        if current_odds < signal_odds:
            slippage = (signal_odds - current_odds) / signal_odds
            if slippage > SLIPPAGE_TOLERANCE:
                logger.warning(f"Slippage too high ({slippage:.1%}). Skip.")
                return

        try:
            whale_wallet_balance = await asyncio.wait_for(
                client.get_token_balance(whale_address), timeout=3.0
            )
        except asyncio.TimeoutError:
            whale_wallet_balance = 0.0

        if whale_wallet_balance < 10.0:
            percent_to_copy = 0.01
        else:
            total_whale_equity = whale_wallet_balance + whale_bet_amount
            percent_to_copy = whale_bet_amount / total_whale_equity

        final_percent = min(percent_to_copy, MAX_POSITION_PCT)
        my_bet_size = self.balance * final_percent

        if my_bet_size < MIN_BET_SIZE:
            return

        self.balance -= my_bet_size
        match_name = self._get_match_name(signal_bet)

        self.positions[bet_id] = {
            "entry_time": datetime.now().isoformat(),
            "amount": my_bet_size,
            "odds": current_odds,
            "match": match_name,
            "copied_from": trader_label,
            "status": "Active",
        }

        logger.info(
            f"COPIED {trader_label}: ${my_bet_size:.2f} ({final_percent:.1%}) on {match_name}"
        )
        self._save()

    async def update_positions(self, client: AsyncAzuroClient):
        active_ids = [
            bid for bid, p in self.positions.items() if p["status"] == "Active"
        ]
        if not active_ids:
            return

        updates = await client.get_bets_status(active_ids)
        updated = False

        for bet in updates:
            if bet.get("status") == "Resolved":
                pid = bet["id"]
                if pid not in self.positions:
                    continue

                pos = self.positions[pid]
                is_win = bet.get("result") == "Won"

                if is_win:
                    winnings = pos["amount"] * pos["odds"]
                    self.balance += winnings
                    self.realized_pnl += winnings - pos["amount"]
                    pos["status"] = "Won"
                    logger.info(f"WON: {pos['match']}")
                else:
                    self.realized_pnl -= pos["amount"]
                    pos["status"] = "Lost"
                    logger.info(f"LOST: {pos['match']}")
                updated = True

        if updated:
            self._save()
            self.print_summary()

    def _get_match_name(self, bet):
        try:
            selections = bet.get("selections", [])
            if selections:
                p = selections[0]["outcome"]["condition"]["game"]["participants"]
                return f"{p[0]['name']} vs {p[1]['name']}"
        except Exception:
            pass
        return "Unknown Match"

    def print_summary(self):
        active_val = sum(
            p["amount"] for p in self.positions.values() if p["status"] == "Active"
        )
        total = self.balance + active_val
        logger.info(
            f"Cash: ${self.balance:.2f} | Equity: ${total:.2f} | PnL: ${self.realized_pnl:+.2f}"
        )


async def main():
    global shutdown_requested

    loop = asyncio.get_running_loop()

    def signal_handler():
        global shutdown_requested
        logger.info("\nShutdown requested...")
        shutdown_requested = True

    try:
        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)
    except NotImplementedError:
        pass

    logger.info("Starting Azuro Smart CopyBot (Async/Avg Optimized)...")

    client = AsyncAzuroClient(GRAPH_ENDPOINT)
    state = AzbotState()
    portfolio = PaperPortfolio()
    portfolio.print_summary()

    if state.target_traders:
        if isinstance(state.target_traders[0], str):
            logger.warning(
                "Detected legacy state format. Forcing refresh to get stats..."
            )
            state.target_traders = []
            state.last_refresh_ts = 0
            state.save()
        else:
            logger.info(f"Tracking {len(state.target_traders)} cached traders:")
            seen = set()
            for t in state.target_traders:
                if t["id"] in seen:
                    continue
                seen.add(t["id"])

                disp_id = t["id"][:6]
                if "_" in t["id"]:
                    parts = t["id"].split("_")
                    if len(parts) > 1:
                        disp_id = f"{parts[0][:6]}..{parts[1][:4]}"

                logger.info(
                    f"   {disp_id} | Winrate: {t['winrate']:.1%} | Profit: ${t['profit']:.0f} | ({t['wins']}/{t['total']})"
                )

            logger.info(
                f"   (Next refresh in {int(REFRESH_TRADERS_INTERVAL - (time.time() - state.last_refresh_ts))}s)"
            )

    try:
        last_heartbeat_ts = time.time()

        while not shutdown_requested:
            start_loop = time.time()

            if time.time() - last_heartbeat_ts > 120:
                logger.info("Check:")
                portfolio.print_summary()
                last_heartbeat_ts = time.time()

            try:
                await portfolio.update_positions(client)
            except Exception as e:
                logger.error(f"Error updating positions: {e}")

            if time.time() - state.last_refresh_ts > REFRESH_TRADERS_INTERVAL:
                new_traders = await client.get_smart_traders()
                if new_traders:
                    state.target_traders = new_traders
                    now_ts = int(time.time())
                    for t in new_traders:
                        tid = t["id"] if isinstance(t, dict) else t
                        if tid not in state.trader_cursors:
                            state.trader_cursors[tid] = now_ts - 3600

                    state.last_refresh_ts = now_ts
                    state.save()
                    portfolio.print_summary()
                else:
                    try:
                        logger.warning(
                            "No sharp traders found or API error. Keeping old list."
                        )
                    except Exception:
                        pass

            if state.target_traders:

                async def check_trader(trader_obj):
                    trader_id = (
                        trader_obj["id"] if isinstance(trader_obj, dict) else trader_obj
                    )
                    last_check = state.get_cursor(trader_id)
                    bets = await client.get_recent_bets(trader_id, last_check)

                    new_max_ts = last_check
                    if bets:
                        bets.sort(key=lambda b: int(b["createdBlockTimestamp"]))

                        for bet in bets:
                            bet_ts = int(bet["createdBlockTimestamp"])
                            if bet_ts <= last_check:
                                continue

                            await portfolio.open_position(
                                bet, f"Sharp {trader_id[:6]}", client
                            )
                            new_max_ts = max(new_max_ts, bet_ts)

                        if new_max_ts > last_check:
                            state.update_cursor(trader_id, new_max_ts)

                await asyncio.gather(
                    *[check_trader(t) for t in state.target_traders],
                    return_exceptions=True,
                )
                state.save()

            elapsed = time.time() - start_loop
            sleep_time = max(0.5, POLL_INTERVAL - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(min(sleep_time, 1.0))

    except asyncio.CancelledError:
        logger.info("Task cancelled")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        await client.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
