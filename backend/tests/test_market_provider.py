from datetime import datetime
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from app.services.data_sources import fetch_quotes
from app.services.market import _parse_sina_quote, _parse_tencent_quote


class GlobalStockDataParserTests(TestCase):
    def test_tencent_us_quote_fields(self) -> None:
        fields = [""] * 71
        fields[0] = "200"
        fields[1] = "苹果"
        fields[2] = "AAPL.OQ"
        fields[3] = "312.70"
        fields[4] = "312.41"
        fields[5] = "311.45"
        fields[6] = "11849255"
        fields[30] = "2026-08-07 11:37:20"
        fields[32] = "0.09"
        fields[33] = "314.81"
        fields[34] = "310.74"
        fields[35] = "USD"
        response = f'v_usAAPL="{"~".join(fields)}";'
        quote = _parse_tencent_quote(response, "usAAPL")

        self.assertEqual(quote["price"], Decimal("312.7"))
        self.assertEqual(quote["volume"], Decimal("11849255"))
        self.assertEqual(quote["prev_close"], Decimal("312.41"))
        self.assertEqual(quote["open"], Decimal("311.45"))
        self.assertEqual(quote["high"], Decimal("314.81"))
        self.assertEqual(quote["low"], Decimal("310.74"))
        self.assertEqual(quote["price_as_of"], datetime(2026, 8, 7, 11, 37, 20))

    def test_sina_us_quote_fields(self) -> None:
        fields = [
            "苹果",
            "312.70",
            "0.09",
            "2026-08-07 11:37:20",
            "unused",
            "311.45",
            "314.81",
            "310.74",
            "0",
            "0",
            "4749062",
            *("0" for _ in range(15)),
            "312.41",
            "0",
            "0",
            "0",
        ]
        response = f'var hq_str_gb_aapl="{",".join(fields)}";'
        quote = _parse_sina_quote(response, "AAPL")

        self.assertEqual(quote["price"], Decimal("312.7"))
        self.assertEqual(quote["prev_close"], Decimal("312.41"))
        self.assertEqual(quote["change"], Decimal("0.290000"))
        self.assertEqual(quote["volume"], Decimal("4749062"))

    def test_us_live_quotes_skip_yahoo_when_sina_hits(self) -> None:
        calls: list[str] = []

        def sina(symbols):
            calls.append("sina")
            return {"NASDAQ:RKLB": {"price": Decimal("52")}}

        def yahoo(symbols):
            calls.append("yahoo")
            return {}

        with (
            patch("app.services.market._fetch_us_quotes", sina),
            patch("app.services.data_sources._yf_quotes", yahoo),
        ):
            quotes = fetch_quotes(["NASDAQ:RKLB"])

        self.assertEqual(calls, ["sina"])
        self.assertEqual(quotes["NASDAQ:RKLB"]["price"], Decimal("52"))
