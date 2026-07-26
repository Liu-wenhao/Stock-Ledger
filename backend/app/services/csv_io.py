from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models.entities import DailyNav, Trade
from app.schemas.trade import VALID_SIDES
from app.services.market import ensure_symbol_meta


def _parse_decimal(v: str | None) -> Decimal:
    if v is None or str(v).strip() == "":
        return Decimal("0")
    try:
        return Decimal(str(v).strip().replace(",", ""))
    except InvalidOperation:
        return Decimal("0")


def _parse_time(v: str) -> datetime:
    v = v.strip()
    for fmt in (
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized datetime: {v}")


def import_csv(
    db: Session,
    content: str | bytes,
    portfolio_id: int = 1,
    replace: bool = True,
) -> dict:
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig")
    else:
        text = content.lstrip("\ufeff")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV missing header")

    # Normalize headers
    field_map = {h.strip(): h for h in reader.fieldnames}
    required = ["Symbol", "Side", "Qty", "Fill Price", "Commission", "Closing Time"]
    for r in required:
        if r not in field_map:
            # try case-insensitive
            lower = {h.strip().lower(): h for h in reader.fieldnames}
            if r.lower() not in lower:
                raise ValueError(f"CSV missing column: {r}")
            field_map[r] = lower[r.lower()]

    rows_to_add: list[Trade] = []
    for raw in reader:
        side = (raw.get(field_map["Side"]) or "").strip()
        if side not in VALID_SIDES:
            raise ValueError(f"Invalid Side: {side}")
        symbol = (raw.get(field_map["Symbol"]) or "").strip() or None
        if side in {"Buy", "Sell"} and not symbol:
            raise ValueError("Buy/Sell requires Symbol")
        trade = Trade(
            portfolio_id=portfolio_id,
            symbol=symbol.upper() if symbol else None,
            side=side,
            qty=_parse_decimal(raw.get(field_map["Qty"])),
            fill_price=_parse_decimal(raw.get(field_map["Fill Price"])),
            commission=_parse_decimal(raw.get(field_map["Commission"])),
            closing_time=_parse_time(raw.get(field_map["Closing Time"]) or ""),
            notes=(raw.get("Notes") or raw.get("notes") or None),
        )
        rows_to_add.append(trade)

    deleted = 0
    if replace:
        deleted = db.query(Trade).filter(Trade.portfolio_id == portfolio_id).delete()
        db.query(DailyNav).filter(DailyNav.portfolio_id == portfolio_id).delete()

    for t in rows_to_add:
        db.add(t)

    db.commit()

    seen: set[str] = set()
    for t in rows_to_add:
        if t.symbol and t.symbol not in seen:
            seen.add(t.symbol)
            try:
                ensure_symbol_meta(db, t.symbol, fetch_remote=False)
            except Exception:
                pass

    return {"imported": len(rows_to_add), "deleted": deleted}


def export_csv(db: Session, portfolio_id: int = 1) -> str:
    trades = (
        db.query(Trade)
        .filter(Trade.portfolio_id == portfolio_id)
        .order_by(Trade.closing_time.asc(), Trade.id.asc())
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Symbol", "Side", "Qty", "Fill Price", "Commission", "Closing Time", "Notes"])
    for t in trades:
        writer.writerow(
            [
                t.symbol or "",
                t.side,
                format(t.qty, "f"),
                format(t.fill_price, "f"),
                format(t.commission, "f"),
                t.closing_time.strftime("%Y/%m/%d %H:%M"),
                t.notes or "",
            ]
        )
    return buf.getvalue()
