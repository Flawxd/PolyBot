import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "RPolyBot"))
)

with (
    patch("py_clob_client.client.ClobClient"),
    patch("web3.Web3"),
    patch("dotenv.load_dotenv"),
):
    import polybot_real
    from polybot_real import PolymarketWatcher, RealPortfolio, TraderStats


class TestStopLoss(unittest.TestCase):
    def setUp(self):
        self.portfolio = MagicMock(spec=RealPortfolio)
        self.trader_stats = MagicMock(spec=TraderStats)
        self.watcher = PolymarketWatcher(self.portfolio, self.trader_stats)

        self.asset_id = "test_asset"
        self.portfolio.positions = {
            self.asset_id: {"qty": 100, "avg_price": 1.0, "title": "Test Market"}
        }
        polybot_real.STOP_LOSS_THRESHOLD = 0.20

    def test_stop_loss_triggered(self):
        self.watcher.get_market_price = MagicMock(return_value=0.75)
        self.portfolio.execute_trade.return_value = (True, -25.0)

        self.watcher._check_stop_losses()

        self.portfolio.execute_trade.assert_called_once_with(
            "SELL", self.asset_id, 100, 0.75, "Test Market"
        )
        self.trader_stats.record_trade.assert_called_once()

    def test_stop_loss_not_triggered(self):
        self.watcher.get_market_price = MagicMock(return_value=0.90)

        self.watcher._check_stop_losses()

        self.portfolio.execute_trade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
