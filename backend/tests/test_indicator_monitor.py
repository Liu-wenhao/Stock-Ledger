from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.entities import DailyBar, IndicatorTrendCache
from app.services import dingtalk
from app.services.bars import _cn_bar_from_quote
from app.services.indicator_monitor import (
    _decode,
    _encode,
    _market_open,
    _monitored_symbols,
    _ready_state,
    _transition_labels,
    _valid_bar,
    cached_indicator_history,
)
from app.services.price_history import history_version
from app.services.signals import INDICATOR_COLUMNS_VERSION


class IndicatorMonitorTest(unittest.TestCase):
    def test_payload_cache_round_trip(self):
        engine = create_engine("sqlite://")
        IndicatorTrendCache.__table__.create(engine)
        DailyBar.__table__.create(engine)
        db = sessionmaker(bind=engine)()
        payload = {"symbol": "NASDAQ:QQQ", "points": [{"date": "2026-11-19", "close": 42.5}]}
        payload["_history_source_version"] = history_version(db, "NASDAQ:QQQ")
        payload["_indicator_columns_version"] = INDICATOR_COLUMNS_VERSION
        db.add(IndicatorTrendCache(symbol="NASDAQ:QQQ", payload_blob=_encode(payload)))
        db.commit()

        self.assertEqual(_decode(db.get(IndicatorTrendCache, "NASDAQ:QQQ").payload_blob), payload)
        self.assertEqual(cached_indicator_history(db, "nasdaq:qqq"), payload)
        payload.pop("_indicator_columns_version")
        db.get(IndicatorTrendCache, "NASDAQ:QQQ").payload_blob = _encode(payload)
        db.commit()
        self.assertIsNone(cached_indicator_history(db, "NASDAQ:QQQ"))
        db.close()

    def test_market_calendar_and_bar_validation(self):
        us_open = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
        self.assertTrue(_market_open("us", us_open))
        self.assertFalse(_market_open("cn", us_open))
        self.assertEqual(_valid_bar({"o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100}), (True, ""))
        self.assertFalse(_valid_bar({"o": 10, "h": 9, "l": 8, "c": 10.5, "v": 100})[0])

    def test_ready_rules_become_monitor_state(self):
        payload = {
            "points": [
                {
                    "date": "2026-11-19",
                    "close": 10,
                    "stop_candle": "锤子线",
                    "signal": "squeeze",
                    "vol_squeeze": True,
                    "strategies": [{"id": "buy-1", "name": "均线交叉", "ready": True}],
                    "sells": [{"id": "sell-1", "name": "均线死叉", "ready": False}],
                }
            ]
        }
        state, labels = _ready_state(payload, None, None)
        self.assertEqual(
            state,
            {
                "buy:buy-1": "买入/加仓信号：均线交叉",
                "candle:stop:锤子线": "止损K线：锤子线",
                "volume:squeeze": "量能信号：缩量推升",
                "volatility:squeeze": "波动率信号：布林带宽创120日新低，进入变盘蓄势",
            },
        )
        self.assertEqual(labels, list(state.values()))

    def test_buy_and_sell_rule_disappearance_is_reported(self):
        notified = {
            "buy:ma": "买入/加仓信号：均线交叉",
            "sell:rsi": "卖出信号：RSI超买",
            "volume:squeeze": "量能信号：缩量推升",
        }
        self.assertEqual(
            _transition_labels({}, notified),
            [
                "买入/加仓规则由成立变为消失：均线交叉",
                "卖出规则由成立变为消失：RSI超买",
            ],
        )

    def test_monitor_uses_position_scope_without_watchlist(self):
        db = MagicMock()
        eligible = ["NASDAQ:RKLB", "SSE:601012"]
        with patch("app.services.indicator_monitor.position_symbols", return_value=eligible) as scope:
            self.assertEqual(_monitored_symbols(db), eligible)
        self.assertEqual(scope.call_args.args[0], db)
        self.assertEqual(len(scope.call_args.args), 2)
        self.assertEqual(scope.call_args.kwargs, {"include_closed": False})

    def test_cn_quote_becomes_session_bar(self):
        fields = [""] * 40
        fields[3] = "18.20"
        fields[5] = "18.00"
        fields[6] = "1200"
        fields[30] = "2026-11-20 10:15:00"
        fields[33] = "18.50"
        fields[34] = "17.90"
        fields[37] = "220.5"
        text = f'v_sh601012="{"~".join(fields)}";'
        bar = _cn_bar_from_quote(text, "sh601012", datetime(2026, 11, 20).date())
        self.assertEqual(bar["d"], "2026-11-20")
        self.assertEqual(bar["c"], 18.2)
        self.assertEqual(bar["v"], 120000)
        self.assertEqual(bar["dol"], 2205000)


class DingTalkTest(unittest.TestCase):
    @patch("app.services.dingtalk.requests.post")
    def test_signed_markdown(self, post):
        response = MagicMock()
        response.json.return_value = {"errcode": 0, "errmsg": "ok"}
        post.return_value = response
        config = SimpleNamespace(
            dingtalk_webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
            dingtalk_secret="secret",
        )
        with patch("app.services.dingtalk.get_settings", return_value=config):
            self.assertTrue(dingtalk.send_markdown("标题", "正文"))
        called_url = post.call_args.args[0]
        self.assertIn("timestamp=", called_url)
        self.assertIn("sign=", called_url)


if __name__ == "__main__":
    unittest.main()
