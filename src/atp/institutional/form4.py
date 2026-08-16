"""SEC Form 4 insider-transaction provider (§ Phase R1.3 — DATA ONLY, read-only).

Insider intelligence: the open-market BUYS and SELLS that a company's officers, directors and 10%
owners disclose on SEC Form 4. Sourced from SEC EDGAR (free public API; User-Agent required). This is
disclosure intelligence, NOT copy-trading — no broker, no order, no execution. Only genuine open-market
purchases (code P → BUY) and sales (code S → SELL) are recorded; grants / option exercises / tax
withholding are NOT directional signals and are skipped. Missing data → NO DATA (never fabricated).
"""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..traders.sec13f import _localname, _sec_pace, _text  # reuse the XML helpers + SEC pacing (no rewrite)

_DATA_BASE = "https://data.sec.gov"
_WWW_BASE = "https://www.sec.gov"

# Ticker → issuer CIK for Form 4 (insiders file under the issuer). Overridable via ATP_INSIDER_CIKS.
DEFAULT_SYMBOL_CIK: dict[str, str] = {
    "NVDA": "0001045810", "AAPL": "0000320193", "MSFT": "0000789019", "AMZN": "0001018724",
    "GOOGL": "0001652044", "GOOG": "0001652044", "META": "0001326801", "TSLA": "0001318605",
    "AVGO": "0001730168",
}

# SEC Form 4 transaction codes → our directional signal (only open-market trades count).
CODE_TO_TYPE = {"P": "BUY", "S": "SELL"}


def _symbol_cik_map() -> dict[str, str]:
    raw = os.environ.get("ATP_INSIDER_CIKS")
    if not raw:
        return dict(DEFAULT_SYMBOL_CIK)
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if ":" in pair:
            s, c = pair.split(":", 1)
            out[s.strip().upper()] = c.strip().zfill(10)
    return out or dict(DEFAULT_SYMBOL_CIK)


def _num(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _field(el, name: str) -> str | None:
    """A Form 4 field is often <name><value>X</value></name>; sometimes direct text. Handle both."""
    for c in el.iter():
        if _localname(c.tag) == name:
            v = _text(c, "value")
            if v is not None:
                return v
            return c.text.strip() if c.text and c.text.strip() else None
    return None


# ---------------------------------------------------------------- pure parsers
def parse_issuer_form4_refs(payload: dict | None, limit: int = 40) -> list[dict]:
    """From an issuer's submissions JSON → the most recent Form 4 filings [{accession, primary_doc,
    date}] (newest first). Empty when none. No fabrication."""
    if not payload:
        return []
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accs = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    dates = recent.get("filingDate") or []
    out: list[dict] = []
    for i, f in enumerate(forms):
        if f == "4" and i < len(accs) and i < len(docs):
            out.append({"accession": accs[i], "primary_doc": docs[i],
                        "date": dates[i] if i < len(dates) else None})
            if len(out) >= max(1, limit):
                break
    return out


def parse_form4(xml_text: str | None) -> dict | None:
    """Parse one Form 4 ownership XML → the insider + their open-market BUY/SELL transactions. Grants /
    exercises / tax events are skipped (not directional). None on unusable input."""
    if not xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    name = _field(root, "rptOwnerName")
    symbol = _field(root, "issuerTradingSymbol")
    is_director = (_field(root, "isDirector") or "").strip() in ("1", "true")
    is_officer = (_field(root, "isOfficer") or "").strip() in ("1", "true")
    is_ten = (_field(root, "isTenPercentOwner") or "").strip() in ("1", "true")
    title = _field(root, "officerTitle") or ("Director" if is_director else "Officer" if is_officer
                                             else "10% Owner" if is_ten else None)
    txns: list[dict] = []
    for t in root.iter():
        if _localname(t.tag) != "nonDerivativeTransaction":
            continue
        code = (_field(t, "transactionCode") or "").strip().upper()
        ttype = CODE_TO_TYPE.get(code)
        if not ttype:                                       # only open-market P/S are directional signals
            continue
        txns.append({
            "symbol": (symbol or "").upper(), "insider_name": name, "title": title,
            "transaction_type": ttype, "shares": _num(_field(t, "transactionShares")),
            "price": _num(_field(t, "transactionPricePerShare")),
            "transaction_date": _field(t, "transactionDate")})
    return {"symbol": (symbol or "").upper(), "insider_name": name, "title": title, "transactions": txns}


class SecForm4Provider:
    """Real insider transactions from SEC EDGAR Form 4. Read-only HTTP GET; no order/trade/broker/IBKR."""
    name = "secform4"

    def __init__(self, symbol_cik: dict[str, str] | None = None, user_agent: str | None = None,
                 *, max_filings: int | None = None, timeout: float = 20.0) -> None:
        self._map = symbol_cik or _symbol_cik_map()
        self._ua = user_agent if user_agent is not None else os.environ.get("ATP_SEC_USER_AGENT")
        self._max = int(os.environ.get("ATP_INSIDER_MAX_FILINGS", str(max_filings or 30)))
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._ua)

    def _fetch(self, url: str) -> bytes | None:
        if not self._ua:
            return None
        _sec_pace()                                        # SEC <=10 req/s fair-access pacing
        try:
            req = Request(url, headers={"User-Agent": self._ua, "Accept": "application/json, */*"})
            with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed https SEC hosts
                return resp.read()
        except (HTTPError, URLError, TimeoutError):
            return None
        except Exception:
            return None

    def get_insider_transactions(self, symbol: str) -> list[dict]:
        """Recent open-market insider BUY/SELL transactions for a symbol. Empty when unavailable."""
        cik = self._map.get(symbol.upper())
        if not cik:
            return []
        subs_raw = self._fetch(f"{_DATA_BASE}/submissions/CIK{cik.zfill(10)}.json")
        if not subs_raw:
            return []
        try:
            refs = parse_issuer_form4_refs(json.loads(subs_raw.decode("utf-8")), self._max)
        except (ValueError, TypeError):
            return []
        cik_int = str(int(cik.zfill(10)))
        out: list[dict] = []
        for ref in refs:
            acc_nodash = ref["accession"].replace("-", "")
            doc = (ref.get("primary_doc") or "").split("/")[-1]   # strip the xsl render folder
            if not doc.endswith(".xml"):
                continue
            xml_raw = self._fetch(f"{_WWW_BASE}/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}")
            parsed = parse_form4(xml_raw.decode("utf-8", "replace")) if xml_raw else None
            if not parsed:
                continue
            for tx in parsed["transactions"]:
                tx = dict(tx)
                tx["accession"] = ref["accession"]
                if not tx["transaction_date"]:
                    tx["transaction_date"] = ref.get("date")
                tx.setdefault("symbol", symbol.upper())
                out.append(tx)
        return out
