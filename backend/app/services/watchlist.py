"""Symbols shown on the A-share and Hong Kong boards before any trade exists."""

from __future__ import annotations

CN_SYMBOLS = (
    "SSE:513400",
    "SZSE:159611",
    "SZSE:159567",
    "SZSE:159559",
    "SZSE:159934",
    "SZSE:159869",
    "SSE:601012",
    "SZSE:159871",
    "SSE:512480",
    "SSE:600362",
    "SZSE:159912",
)

HK_SYMBOLS = (
    "HKEX:03121",
    "HKEX:07709",
)


def watch_symbols(market: str) -> list[str]:
    if market == "cn":
        return list(CN_SYMBOLS)
    if market == "hk":
        return list(HK_SYMBOLS)
    return []
