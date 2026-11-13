# PolyBot

Copy-trading bots for prediction markets. Each bot watches a list of wallets and mirrors their trades at a scaled-down size and with an added risk management. Three of the four only simulate trades against a paper balance. The fourth places real orders on Polymarket.

## Disclaimer
This is a personal project. Do not use it on your personal money. I am not responsible for any financial loss.

This code is an experiment and comes with no warranty. Nothing here is financial advice.

Prediction markets are restricted or banned in a number of countries. This project was made before Polymarket got banned in France, and I do not condone any use of Polymarket. This project is here simply because I want to show it.

## What's in the repo

| Script | Market | Mode | Notes |
| --- | --- | --- | --- |
| `polybot.py` | Polymarket | Paper | The most complete bot. Start here. |
| `RPolyBot/polybot_real.py` | Polymarket | Live | Same strategy, real orders through the CLOB API. |
| `AzuroBot/azbot.py` | Azuro on Polygon | Paper | Picks its own bettors to follow from the Azuro subgraph. |
| `Limitless/limitlessbot.py` | Limitless on Base | Paper | Experimental. Reads on-chain events and keeps nothing on disk. |

There are three small helper scripts. `RPolyBot/get_keys.py` derives Polymarket API credentials from a private key, `RPolyBot/test_stop_loss.py` holds the unit tests for the stop-loss, and `AzuroBot/inspect_bettor.py` lists the fields of the `Bettor` entity in the Azuro subgraph.

## How the Polymarket bots work

The paper bot and the live bot share the same loop.

1. On start-up the bot downloads the open positions of every wallet in `TARGET_TRADERS` from the Polymarket data API and saves them as a baseline. Positions that already exist are never copied. Only changes from that point on are.
2. On every poll it downloads the positions again and compares them with the previous snapshot.
3. A position that grew is a buy. A position that shrank is a sell. A position that disappeared from a resolved market is a settlement.

### Buys

The bot asks the CLOB for the current price, falls back to the Gamma API, and as a last resort reuses the last price it saw.

Before buying it runs a slippage check. The data API reports the trader's average entry price for the position. If the market is already more than `SLIPPAGE_TOLERANCE` above that entry, the bot skips the trade instead of buying after the move.

Position size is proportional. The bot works out what share of the trader's portfolio the purchase represents and puts the same share of its own portfolio into the trade, capped at `MAX_POSITION_PCT`.

A worked example with the paper bot's defaults: a trader adds 500 shares at 0.41, which is 23.7% of a portfolio worth 865 dollars. With a paper balance of 1,000 dollars and a 5% cap, the bot spends 50 dollars and gets 121.95 shares.

The two bots treat small trades differently. The paper bot skips anything under 1 dollar, unless the trader put more than 2% of their portfolio into the trade, and it never trades less than 2 dollars. The live bot works in shares. Below `MIN_TRADE_THRESHOLD` shares it rounds up to that minimum when the computed size is above 1.5 shares, as long as the order fits in the cash balance and costs no more than five times the intended amount. Anything smaller is skipped.

### Sells and settlements

When a trader cuts a position by some fraction, the bot sells the same fraction of its own holding.

When a position disappears and the Gamma API shows the market as resolved, the bot settles instead of selling. Winning shares pay 1 dollar each and losing shares pay nothing. If it cannot tell which side won, it guesses from the last price (0.50 or above counts as a win) and logs a warning.

### State and logs

Each bot writes its files to the folder it is started from, so run each one from its own folder. The paper and live Polymarket bots use the same file names and would overwrite each other otherwise.

| File | Content |
| --- | --- |
| `bot_state.json` | Last known positions of every followed wallet |
| `portfolio_state.json` | The bot's own positions, plus the cash balance in the paper bot |
| `trade_history.json` | Executed trades, capped at `MAX_TRADE_HISTORY` entries in the paper bot |
| `trader_stats.json` | Trade count, volume and P&L per followed wallet |
| `polybot.log` | Everything the bot prints |

State is written to a temporary file and then moved into place, so a crash in the middle of a write does not corrupt it. Stop the bot with Ctrl+C. It finishes the current cycle, then prints a report per trader and the final portfolio.

## What the live bot adds

`RPolyBot/polybot_real.py` keeps the logic above and swaps the paper portfolio for real execution.

- Orders are signed locally and posted with [py-clob-client](https://github.com/Polymarket/py-clob-client). The limit price is the quoted price plus `PRICE_BUFFER` for buys and minus `PRICE_BUFFER` for sells, so the order crosses the spread.
- The cash balance is read from the USDC.e contract on Polygon. The bot tries a list of public RPC endpoints, then the one in `POLYGON_RPC`.
- A stop-loss runs on every cycle. Any position trading at least `STOP_LOSS_THRESHOLD` below its entry price is sold in full.
- `DAILY_TRADE_CAP` limits the number of copied buys and sells in a rolling 24 hours. Once the cap is reached, copying pauses until older trades age out.
- The wait between polls is randomised between half and double `POLL_INTERVAL`.

## Azuro bot

`AzuroBot/azbot.py` does not need a wallet list. Once an hour it queries the Azuro subgraph for bettors with a total profit above 500 and a bet in the last seven days. It keeps those with at least 10 bets and a win rate of 60% or more, and follows up to 30 of them.

For each followed bettor it polls for new bets and copies them on paper. A bet is skipped when the current odds are more than `SLIPPAGE_TOLERANCE` worse than the odds the bettor got. Open positions are settled from the bet status in the subgraph. The portfolio is saved to `paper_portfolio.json` and the list of followed bettors to `azbot_state.json`.

## Limitless bot

`Limitless/limitlessbot.py` watches `OrderMatched` events on the Limitless exchange contract on Base and reacts when the taker is one of the wallets in `TARGET_TRADERS`. It copies buys only and ignores those under 2 USDC. The paper trade is sized by the share of the wallet's USDC that went into the buy, capped at 5% of the paper balance. The bot then compares the current best ask with the price the wallet paid and records the trade unless the ask is more than 5% higher. Nothing is saved to disk.

## Getting started

You need Python 3.10 or newer. The code was written on 3.12.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, activate the environment with `.venv\Scripts\activate` instead.

The paper Polymarket bot only needs `requests`. `aiohttp` is for the Azuro bot. `web3`, `python-dotenv` and `py-clob-client` are for the live and Limitless bots.

Run the paper bot from the repo root:

```bash
python polybot.py
```

Run the Azuro or Limitless bot from its own folder:

```bash
cd AzuroBot
python azbot.py
```

```bash
cd Limitless
python limitlessbot.py
```

### Running the live bot

1. Copy `.env.example` to `.env`, either in the repo root or in `RPolyBot/`, and fill in `PRIVATE_KEY` and `WALLET_ADDRESS`.
2. From `RPolyBot/`, run `python get_keys.py`. It prints the three `POLY_API_*` values. Paste them into `.env`.
3. From `RPolyBot/`, run `python polybot_real.py`.

The client is set up for a Polymarket account whose funds sit in a proxy wallet (`signature_type=2` in py-clob-client). `WALLET_ADDRESS` is the address of that proxy wallet and `PRIVATE_KEY` is the key of the wallet that controls it.

Never commit `.env`. It is listed in `.gitignore`, along with the log and state files.

## Configuration

The wallets to follow are the `TARGET_TRADERS` list at the top of each Polymarket and Limitless script. Edit the list to follow other wallets.

`polybot.py` and `azbot.py` read plain environment variables and do not load a `.env` file. The live and Limitless bots load `.env` through python-dotenv.

### Paper Polymarket bot

| Variable | Default | Meaning |
| --- | --- | --- |
| `POLL_INTERVAL` | 5 | Seconds between polls |
| `STARTING_BALANCE` | 1000 | Paper balance in dollars on first start |
| `SLIPPAGE_TOLERANCE` | 0.05 | Largest premium accepted over the trader's average entry price |
| `MAX_POSITION_PCT` | 0.05 | Largest share of the portfolio put into one trade |
| `REQUEST_DELAY` | 0.2 | Pause in seconds before every HTTP request |
| `MAX_RETRIES` | 2 | Attempts per HTTP request |
| `RETRY_DELAY` | 1.0 | Base wait in seconds between attempts |
| `MAX_TRADE_HISTORY` | 500 | Trades kept in `trade_history.json` |
| `MAX_TRADER_TRADES` | 100 | Trades kept per wallet in `trader_stats.json` |
| `HEARTBEAT_INTERVAL` | 6 | Polls between status lines in the log |

### Live Polymarket bot

| Variable | Default | Meaning |
| --- | --- | --- |
| `PRIVATE_KEY` | none | Key used to sign orders |
| `WALLET_ADDRESS` | none | Address that holds the funds |
| `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` | none | CLOB API credentials printed by `get_keys.py` |
| `POLYGON_RPC` | `https://polygon-rpc.com` | Extra Polygon RPC endpoint |
| `POLL_INTERVAL` | 15 | Base seconds between polls, before randomisation |
| `MIN_TRADE_THRESHOLD` | 5.0 | Minimum order size in shares |
| `MAX_POSITION_PCT` | 0.04 | Largest share of the portfolio put into one trade |
| `DAILY_TRADE_CAP` | 60 | Copied buys and sells allowed per rolling 24 hours |
| `SLIPPAGE_TOLERANCE` | 0.06 | Largest premium accepted over the trader's average entry price |
| `STOP_LOSS_THRESHOLD` | 0.20 | Drop from the entry price that triggers a full sell |
| `PRICE_BUFFER` | 0.02 | Amount added to buy prices and taken off sell prices |

`REQUEST_DELAY`, `MAX_RETRIES`, `RETRY_DELAY`, `MAX_TRADER_TRADES` and `HEARTBEAT_INTERVAL` work as in the paper bot.

### Azuro

| Variable | Default | Meaning |
| --- | --- | --- |
| `GRAPH_ENDPOINT` | Azuro Polygon v3 subgraph | GraphQL endpoint to query |
| `POLL_INTERVAL` | 10 | Target seconds between polls (see known limitations) |
| `STARTING_BALANCE` | 1000 | Paper balance on first start |
| `MAX_POSITION_PCT` | 0.05 | Largest share of the balance put into one bet |
| `SLIPPAGE_TOLERANCE` | 0.05 | Largest drop in odds accepted since the copied bet |
| `MAX_RETRIES` | 2 | Retries after a failed query |
| `RETRY_DELAY` | 1.0 | Seconds between retries |
| `MAX_TRACKED_TRADERS` | 30 | Bettors followed at once |

The selection thresholds (`MIN_WINRATE`, `MIN_TOTAL_BETS`) and the minimum bet (`MIN_BET_SIZE`) are constants in the script.

### Limitless

`BASE_RPC_URL` sets the Base RPC endpoint and defaults to `https://mainnet.base.org`. Everything else is a constant at the top of the script: `PAPER_BALANCE`, `MAX_BET_PCT`, `SLIPPAGE_TOLERANCE` and `MIN_WHALE_TRADE_VALUE`.

## Tests

```bash
cd RPolyBot
python -m unittest test_stop_loss -v
```

The tests mock the CLOB client and web3, so they need no credentials and make no network calls.

## Known limitations

- The bots poll, so they always trade after the wallet they follow. The delay is up to one poll interval plus the time the requests take.
- Paper trades assume the whole order fills at the quoted price. Fees and order book depth are ignored, so paper results flatter the strategy.
- The slippage check uses the trader's average entry price for the whole position. It does not know the price of their latest fill.
- The live bot does not redeem resolved positions on-chain. It only updates its own books, so winnings still have to be claimed on Polymarket.
- Stop-loss sells in the live bot are written to the trade history, but their P&L is not added to the per-wallet totals in `trader_stats.json`.
- The Azuro bot sizes bets against a fixed placeholder balance of 10,000 for the followed bettor, because the on-chain balance lookup is not implemented. Its main loop also sleeps one second at most, so `POLL_INTERVAL` has little effect.
- The Limitless bot never closes a position and loses its state on restart. It infers buy or sell from the size of the raw amounts in the event. Check `LIMITLESS_EXCHANGE` against the current Limitless exchange contract before relying on it.

## Licence

MIT. See [LICENSE](LICENSE).
