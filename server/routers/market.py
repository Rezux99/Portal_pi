"""
market.py — Router para /api/market
Datos de mercado reales via yfinance (sin API key).
"""

from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/market", tags=["market"])


class QuoteResponse(BaseModel):
    symbol: str
    name: str
    price: float
    change: float
    changePct: float
    high: float
    low: float
    open: float
    prevClose: float
    volume: int
    marketCap: Optional[float] = None


class CandleResponse(BaseModel):
    symbol: str
    interval: str
    candles: List[dict]  # {t, o, h, l, c, v}


class MoversResponse(BaseModel):
    gainers: List[QuoteResponse]
    losers: List[QuoteResponse]


# Símbolos compatibles con yfinance (añade sufijos donde haga falta)
YF_MAP = {
    "SPX": "^GSPC",
    "NDX": "^NDX",
    "DJI": "^DJI",
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "BNB": "BNB-USD",
}

# Nombres legibles
NAME_MAP = {
    "AAPL": "Apple Inc.", "MSFT": "Microsoft Corp.", "NVDA": "NVIDIA Corp.",
    "TSLA": "Tesla, Inc.", "AMZN": "Amazon.com, Inc.", "GOOGL": "Alphabet, Inc.",
    "META": "Meta Platforms", "AMD": "Advanced Micro Devices", "INTC": "Intel Corp.",
    "NFLX": "Netflix, Inc.", "JPM": "JPMorgan Chase", "V": "Visa Inc.",
    "MA": "Mastercard Inc.", "WMT": "Walmart Inc.", "DIS": "Walt Disney Co.",
    "ORCL": "Oracle Corp.", "ADBE": "Adobe Inc.", "CRM": "Salesforce Inc.",
    "MU": "Micron Technology", "AVGO": "Broadcom Inc.", "QCOM": "Qualcomm Inc.",
    "CSCO": "Cisco Systems", "IBM": "IBM Corp.", "XOM": "Exxon Mobil",
    "CVX": "Chevron Corp.", "PEP": "PepsiCo Inc.", "KO": "Coca-Cola Co.",
    "NKE": "Nike Inc.", "JNJ": "Johnson & Johnson", "PFE": "Pfizer Inc.",
    "SPX": "S&P 500", "NDX": "Nasdaq 100", "DJI": "Dow Jones",
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "BNB": "BNB",
}

DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META",
    "BTC", "ETH", "SOL", "SPX", "NDX", "DJI",
]

MOVER_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META",
    "AMD", "INTC", "NFLX", "JPM", "V", "MA", "WMT", "DIS",
    "ORCL", "ADBE", "CRM", "MU", "AVGO", "QCOM", "CSCO", "IBM",
    "XOM", "CVX", "PEP", "KO", "NKE", "JNJ", "PFE",
]


def _yf_symbol(sym: str) -> str:
    return YF_MAP.get(sym, sym)


def _name(sym: str) -> str:
    return NAME_MAP.get(sym, sym)


@router.get("/quotes", response_model=dict)
def get_quotes(symbols: str = Query("", description="Comma-separated symbols")):
    """Devuelve cotizaciones reales para una lista de símbolos."""
    import yfinance as yf

    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else DEFAULT_SYMBOLS
    result = {}

    for sym in syms:
        try:
            yf_sym = _yf_symbol(sym)
            ticker = yf.Ticker(yf_sym)
            info = ticker.fast_info

            price = float(info.last_price) if hasattr(info, "last_price") and info.last_price else 0
            prev = float(info.previous_close) if hasattr(info, "previous_close") and info.previous_close else price
            change = price - prev if prev else 0
            change_pct = (change / prev * 100) if prev else 0

            result[sym] = QuoteResponse(
                symbol=sym,
                name=_name(sym),
                price=round(price, 2),
                change=round(change, 2),
                changePct=round(change_pct, 2),
                high=float(info.day_high) if hasattr(info, "day_high") and info.day_high else 0,
                low=float(info.day_low) if hasattr(info, "day_low") and info.day_low else 0,
                open=float(info.open) if hasattr(info, "open") and info.open else 0,
                prevClose=round(prev, 2),
                volume=int(info.last_volume) if hasattr(info, "last_volume") and info.last_volume else 0,
            )
        except Exception as e:
            result[sym] = {"error": str(e)}

    return result


@router.get("/quote/{symbol}", response_model=QuoteResponse)
def get_quote(symbol: str):
    """Cotización de un símbolo."""
    import yfinance as yf

    try:
        yf_sym = _yf_symbol(symbol.upper())
        ticker = yf.Ticker(yf_sym)
        info = ticker.fast_info
        sym = symbol.upper()

        price = float(info.last_price) if hasattr(info, "last_price") and info.last_price else 0
        prev = float(info.previous_close) if hasattr(info, "previous_close") and info.previous_close else price
        change = price - prev if prev else 0
        change_pct = (change / prev * 100) if prev else 0

        return QuoteResponse(
            symbol=sym,
            name=_name(sym),
            price=round(price, 2),
            change=round(change, 2),
            changePct=round(change_pct, 2),
            high=float(info.day_high) if hasattr(info, "day_high") and info.day_high else 0,
            low=float(info.day_low) if hasattr(info, "day_low") and info.day_low else 0,
            open=float(info.open) if hasattr(info, "open") and info.open else 0,
            prevClose=round(prev, 2),
            volume=int(info.last_volume) if hasattr(info, "last_volume") and info.last_volume else 0,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"yfinance error: {e}")


@router.get("/candles/{symbol}", response_model=CandleResponse)
def get_candles(
    symbol: str,
    interval: str = Query("5m", description="1m, 5m, 15m, 1h, 1d"),
    range: str = Query("1d", description="1d, 5d, 1mo, 3mo, 6mo, 1y"),
):
    """Velas OHLC reales via yfinance."""
    import yfinance as yf

    try:
        yf_sym = _yf_symbol(symbol.upper())
        ticker = yf.Ticker(yf_sym)
        hist = ticker.history(interval=interval, period=range)

        candles = []
        for ts, row in hist.iterrows():
            candles.append({
                "t": int(ts.timestamp()),
                "o": round(float(row["Open"]), 2),
                "h": round(float(row["High"]), 2),
                "l": round(float(row["Low"]), 2),
                "c": round(float(row["Close"]), 2),
                "v": int(row["Volume"]) if row["Volume"] else 0,
            })

        return CandleResponse(
            symbol=symbol.upper(),
            interval=interval,
            candles=candles,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"yfinance error: {e}")


@router.get("/movers", response_model=MoversResponse)
def get_movers():
    """Top gainers y losers del día (de un universo de ~30 acciones)."""
    import yfinance as yf
    import time

    quotes = []
    # yfinance en lotes para no saturar
    batch_size = 10
    for i in range(0, len(MOVER_SYMBOLS), batch_size):
        batch = MOVER_SYMBOLS[i:i + batch_size]
        yf_syms = " ".join(_yf_symbol(s) for s in batch)
        try:
            data = yf.download(yf_syms, period="2d", interval="1d", progress=False, group_by="ticker")
            for sym in batch:
                try:
                    yf_sym = _yf_symbol(sym)
                    if len(batch) == 1:
                        # datos de un solo símbolo
                        if len(data) >= 2:
                            prev_close = float(data["Close"].iloc[-2])
                            curr_price = float(data["Close"].iloc[-1])
                        elif len(data) == 1:
                            prev_close = float(data["Close"].iloc[0])
                            curr_price = prev_close
                        else:
                            continue
                    else:
                        if (yf_sym, "Close") in data.columns:
                            col = data[(yf_sym, "Close")]
                        elif yf_sym in data.columns.get_level_values(0):
                            col = data[(yf_sym, "Close")]
                        else:
                            continue
                        if len(col) >= 2:
                            prev_close = float(col.iloc[-2])
                            curr_price = float(col.iloc[-1])
                        elif len(col) == 1:
                            prev_close = float(col.iloc[0])
                            curr_price = prev_close
                        else:
                            continue

                    change = curr_price - prev_close
                    change_pct = (change / prev_close * 100) if prev_close else 0
                    quotes.append(QuoteResponse(
                        symbol=sym,
                        name=_name(sym),
                        price=round(curr_price, 2),
                        change=round(change, 2),
                        changePct=round(change_pct, 2),
                        high=curr_price,
                        low=curr_price,
                        open=prev_close,
                        prevClose=round(prev_close, 2),
                        volume=0,
                    ))
                except Exception:
                    continue
            time.sleep(0.1)
        except Exception:
            continue

    sorted_by_pct = sorted(quotes, key=lambda q: q.changePct, reverse=True)
    return MoversResponse(
        gainers=sorted_by_pct[:5],
        losers=list(reversed(sorted_by_pct[-5:])),
    )
