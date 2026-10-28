"""Temporary probe. Delete after use."""
import json
import re
import urllib.request

UA = {"User-Agent": "stock-ledger", "Referer": "https://finance.sina.com.cn/"}


def get(url, timeout=12):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def show(label, text, n=400):
    print("\n==", label, "len", len(text))
    print(text[:n].replace("\n", " | "))


urls = [
    ("vix_k", "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK?symbol=.vix&num=5"),
    ("ixic_k", "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK?symbol=.ixic&num=3"),
    ("inx_k", "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK?symbol=.inx&num=3"),
    ("quote", "https://hq.sinajs.cn/list=gb_$vix,gb_$ixic,gb_$inx,gb_vix"),
    ("cat", "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_CategoryService.getList?page=1&num=3&sort=&asc=0&market=&id="),
    ("cat2", "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/IO.XSRV2.CallbackList['x']/US_CategoryService.getList?page=1&num=2&sort=volume&asc=0&market=NASDAQ&id="),
]
for label, url in urls:
    try:
        show(label, get(url))
    except Exception as exc:
        print("\n==", label, "FAIL", type(exc).__name__, exc)

try:
    text = get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", timeout=15)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    print("\n== VIX CSV rows", len(lines), "tail", lines[-1])
except Exception as exc:
    print("\n== VIX CSV FAIL", type(exc).__name__, exc)
