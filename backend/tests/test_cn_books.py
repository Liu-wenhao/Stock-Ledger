import unittest
from unittest.mock import patch

from app.models.entities import ArmsReading
from app.services import sentiment


class CnBooksTest(unittest.TestCase):
    def test_quote_books_split_star_chinext_and_hs300(self):
        hs300 = frozenset({"sh600519", "sz000001"})
        with patch.object(sentiment, "load_hs300_symbols", return_value=hs300):
            self.assertEqual(sentiment._quote_books("cn", 1, "sh688981"), ["cn_all", "cn_star"])
            self.assertEqual(sentiment._quote_books("cn", 0, "sz300750"), ["cn_all", "cn_cyb"])
            self.assertEqual(sentiment._quote_books("cn", 1, "sh600519"), ["cn_all", "cn_hs300"])
            self.assertEqual(sentiment._quote_books("cn", 0, "sz000001"), ["cn_all", "cn_hs300"])
            self.assertEqual(sentiment._quote_books("cn", 1, "sh601012"), ["cn_all"])

    def test_hero_books_no_longer_use_exchange_split(self):
        keys = [key for key, _label, _pair in sentiment._BOOKS["cn"]]
        self.assertEqual(keys, ["cn_all", "cn_star", "cn_cyb"])
        self.assertEqual(sentiment._NARROW_OF["cn_all"], "cn_hs300")


class UsBooksTest(unittest.TestCase):
    def test_full_market_label_preserves_book_keys(self):
        self.assertEqual(sentiment._book_meta("us_all"), ("全市场", ""))
        keys = [key for key, _label, _pair in sentiment._BOOKS["us"]]
        self.assertEqual(keys, ["us_all", "nyse", "nasdaq"])

    def test_saved_reading_uses_current_label(self):
        reading = ArmsReading(book="us_all", count=1234, universe="美股全市场 1234 家")
        payload = sentiment._public_reading(reading)
        self.assertEqual(payload["universe"], "全市场 1234 家")
        self.assertEqual(payload["key"], "us_all")
        self.assertEqual(payload["pair"], "")
        self.assertEqual(reading.universe, "美股全市场 1234 家")


if __name__ == "__main__":
    unittest.main()
