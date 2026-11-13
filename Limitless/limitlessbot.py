import logging
import os
import time

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")

LIMITLESS_EXCHANGE = "0x2c866bd6d068390feb7e3245b518d664b11815d5"

USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

TARGET_TRADERS = [
    "0x6B61091504821442d4C7697d390fc6Ab293bAA7A",
    "0x9B2d71A825Fa72D8A112cF833a2822027440139C",
    "0x86e041e9cF500361a8Be4357e928672D91aD910b",
    "0x34f2276f21cac9c783698d53bC8983C041f00Fd6",
    "0x2C866BD6D068390FEB7E3245B518d664b11815d5",
    "0xa89236a515598ebcFa1c16AE472FAAc8e1010c97",
]

MIN_WHALE_TRADE_VALUE = 2.00
PAPER_BALANCE = 1000.0
MAX_BET_PCT = 0.05
SLIPPAGE_TOLERANCE = 0.05

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger(__name__)


class PaperAccount:
    def __init__(self, start_balance):
        self.balance = start_balance
        self.positions = {}

    def execute_trade(self, market_id, outcome_id, price, cost):
        if self.balance < cost:
            logger.warning("Insufficient funds for paper trade")
            return

        qty = cost / price
        self.balance -= cost

        pos_key = f"{market_id[:6]}...-{outcome_id}"
        self.positions[pos_key] = self.positions.get(pos_key, 0) + qty

        logger.info("PAPER BUY EXECUTED")
        logger.info(f"   Qty: {qty:.2f} Shares @ ${price:.4f}")
        logger.info(f"   New Balance: ${self.balance:.2f}")


def run_bot():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not w3.is_connected():
        logger.error("Failed to connect to RPC")
        return

    limitless_addr = Web3.to_checksum_address(LIMITLESS_EXCHANGE)
    usdc_addr = Web3.to_checksum_address(USDC_ADDRESS)
    target_traders_checksum = [Web3.to_checksum_address(t) for t in TARGET_TRADERS]

    CONTRACT_ABI = '[{"anonymous":false,"inputs":[{"indexed":true,"internalType":"bytes32","name":"orderHash","type":"bytes32"},{"indexed":true,"internalType":"address","name":"maker","type":"address"},{"indexed":true,"internalType":"address","name":"taker","type":"address"},{"indexed":false,"internalType":"uint256","name":"makerFillAmount","type":"uint256"},{"indexed":false,"internalType":"uint256","name":"takerFillAmount","type":"uint256"},{"indexed":false,"internalType":"bytes32","name":"marketId","type":"bytes32"},{"indexed":false,"internalType":"uint256","name":"outcome","type":"uint256"}],"name":"OrderMatched","type":"event"}, {"inputs":[{"internalType":"bytes32","name":"marketId","type":"bytes32"},{"internalType":"uint256","name":"outcome","type":"uint256"}],"name":"getBestAsk","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]'

    ERC20_ABI = '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]'

    contract = w3.eth.contract(address=limitless_addr, abi=CONTRACT_ABI)
    usdc_contract = w3.eth.contract(address=usdc_addr, abi=ERC20_ABI)
    paper_acc = PaperAccount(PAPER_BALANCE)

    logger.info(
        f"Bot Started | Watching {len(target_traders_checksum)} Whales on Limitless"
    )

    last_processed_block = w3.eth.block_number
    logger.info(f"Starting from Block: {last_processed_block}")

    while True:
        try:
            current_block = w3.eth.block_number
            if current_block > last_processed_block:
                events = contract.events.OrderMatched.get_logs(
                    from_block=last_processed_block + 1, to_block=current_block
                )

                for event in events:
                    args = event["args"]

                    active_trader = args.get("taker")

                    if active_trader not in target_traders_checksum:
                        continue

                    market_id = args.get("marketId")
                    outcome_id = args.get("outcome", 1)

                    taker_fill_raw = args.get("takerFillAmount", 0)
                    maker_fill_raw = args.get("makerFillAmount", 0)

                    if taker_fill_raw == 0 or maker_fill_raw == 0:
                        continue

                    if taker_fill_raw > maker_fill_raw:
                        logger.info(f"Whale {active_trader[:6]} SOLD (Skipping)")
                        continue

                    invested_raw = taker_fill_raw
                    shares_raw = maker_fill_raw

                    logger.info(f"ALERT: Whale {active_trader[:6]}... BOUGHT!")

                    invested_usdc = invested_raw / 1e6
                    if invested_usdc < MIN_WHALE_TRADE_VALUE:
                        continue

                    try:
                        whale_balance_raw = usdc_contract.functions.balanceOf(
                            active_trader
                        ).call()
                        whale_balance = whale_balance_raw / 1e6
                        whale_total_portfolio = whale_balance + invested_usdc
                    except Exception as e:
                        logger.warning(f"Could not fetch whale balance: {e}")
                        whale_total_portfolio = invested_usdc * 100

                    if whale_total_portfolio > 0:
                        whale_bet_percentage = invested_usdc / whale_total_portfolio
                    else:
                        whale_bet_percentage = 0.01

                    my_bet_percentage = min(whale_bet_percentage, MAX_BET_PCT)
                    my_bet_size = paper_acc.balance * my_bet_percentage

                    logger.info(f"Whale Portfolio: ${whale_total_portfolio:.2f}")
                    logger.info(
                        f"   Whale Conviction: {whale_bet_percentage * 100:.2f}% (${invested_usdc:.2f})"
                    )
                    logger.info(
                        f"   My Bet: {my_bet_percentage * 100:.2f}% of ${paper_acc.balance:.2f} = ${my_bet_size:.2f}"
                    )

                    whale_price = (invested_raw / 1e6) / (shares_raw / 1e18)

                    try:
                        current_ask_raw = contract.functions.getBestAsk(
                            market_id, outcome_id
                        ).call()
                        current_price = current_ask_raw / 1e18
                    except Exception as e:
                        logger.warning(
                            f"Could not fetch live price (Market closed?): {e}"
                        )
                        continue

                    if current_price == 0:
                        logger.warning("Market has no liquidity (Price 0)")
                        continue

                    price_diff_pct = (current_price - whale_price) / whale_price

                    logger.info(
                        f"COMPARISON: Whale ${whale_price:.4f} vs Now ${current_price:.4f}"
                    )

                    if price_diff_pct > SLIPPAGE_TOLERANCE:
                        logger.warning(
                            f"SKIP: Buying the top! (+{price_diff_pct * 100:.1f}%)"
                        )
                        continue

                    elif price_diff_pct < -0.01:
                        logger.info(
                            f"BETTER ENTRY: {abs(price_diff_pct * 100):.1f}% Discount!"
                        )

                    paper_acc.execute_trade(
                        market_id=market_id.hex(),
                        outcome_id=outcome_id,
                        price=current_price,
                        cost=my_bet_size,
                    )

                last_processed_block = current_block

            time.sleep(2)

        except Exception as e:
            logger.error(f"Loop Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
