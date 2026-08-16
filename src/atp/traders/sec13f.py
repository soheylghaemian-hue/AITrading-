"""SEC 13F trader-intelligence provider (§ Phase R1.2 — DATA ONLY, read-only).

"What high-quality market participants are doing" — sourced from SEC Form 13F, the quarterly holdings
that institutional managers (>$100M AUM) are legally required to disclose. This is INSTITUTIONAL
POSITIONING intelligence, NOT copy-trading: it reads public filings, never a broker, never an order.

Data comes from SEC EDGAR — a free, public-domain government API. SEC requires a descriptive
User-Agent with a contact (set ATP_SEC_USER_AGENT); without it the provider is unconfigured → NO DATA
(never fabricated). 13F reports LONG holdings (and put/call options), not returns — so performance is
NO DATA and a manager's quality falls back to its verified filing track record. No credentials, no
broker access, no execution anywhere.

Activate in prod: ATP_TRADER_PROVIDER=sec13f + ATP_SEC_USER_AGENT="GIGBAY research@gigbay.de".
"""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .provider import (
    StrategyMetadata, TraderInfo, TraderPerformance, TraderPosition, TraderProvider,
)

_DATA_BASE = "https://data.sec.gov"
_WWW_BASE = "https://www.sec.gov"

# A curated default set of notable institutional 13F filers (CIK → hint). Overridable via ATP_TRADER_CIKS
# (comma-separated CIKs). A CIK that fails to load is skipped — never fabricated.
DEFAULT_CIKS: list[str] = [
    "0001067983",  # Berkshire Hathaway (Buffett)
    "0001037389",  # Renaissance Technologies
    "0001656456",  # Appaloosa (Tepper)
    "0001135730",  # Coatue Management
    "0001167483",  # Tiger Global Management
    "0001103804",  # Viking Global Investors
]

# CUSIP → ticker for the symbols GIGBAY tracks (13F reports by CUSIP). Overridable via ATP_TRADER_CUSIPS
# ("CUSIP:SYM,CUSIP:SYM"). Only holdings of these symbols become positions.
DEFAULT_CUSIPS: dict[str, str] = {
    "037833100": "AAPL", "67066G104": "NVDA", "594918104": "MSFT", "023135106": "AMZN",
    "02079K305": "GOOGL", "02079K107": "GOOG", "30303M102": "META", "88160R101": "TSLA",
    "78462F103": "SPY", "11135F101": "AVGO", "084670702": "BRK.B",
}


def _cusip_map() -> dict[str, str]:
    raw = os.environ.get("ATP_TRADER_CUSIPS")
    if not raw:
        return dict(DEFAULT_CUSIPS)
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if ":" in pair:
            c, s = pair.split(":", 1)
            out[c.strip().upper()] = s.strip().upper()
    return out or dict(DEFAULT_CUSIPS)


def _env_ciks() -> list[str] | None:
    raw = os.environ.get("ATP_TRADER_CIKS")
    if not raw:
        return None
    return [c.strip().zfill(10) for c in raw.split(",") if c.strip()]


# ---------------------------------------------------------------- pure parsers
def parse_submissions(payload: dict | None) -> dict | None:
    """Parse a data.sec.gov submissions JSON → manager identity + latest 13F + filing track record.
    None on unusable input; a filer with no 13F returns latest=None (→ NO DATA)."""
    if not payload:
        return None
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accs = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    idxs = [i for i, f in enumerate(forms) if f in ("13F-HR", "13F-HR/A")]
    name = payload.get("name")
    cik = str(payload.get("cik") or "").zfill(10)
    if not idxs:
        return {"name": name, "cik": cik, "latest": None, "first_13f_date": None, "filing_count": 0}
    latest_i = min(idxs)                                    # 'recent' is newest-first
    first_date = min(dates[i] for i in idxs if i < len(dates))
    return {"name": name, "cik": cik,
            "latest": {"accession": accs[latest_i], "date": dates[latest_i]},
            "first_13f_date": first_date, "filing_count": len(idxs)}


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _text(el, name: str) -> str | None:
    for c in el.iter():
        if _localname(c.tag) == name and c.text is not None:
            return c.text.strip()
    return None


def parse_info_table(xml_text: str | None, cusip_to_symbol: dict[str, str]) -> list[dict]:
    """Parse a 13F information-table XML → aggregated holdings for the WATCHED symbols only. Multiple
    rows for the same issuer (per sub-manager) are summed. Puts count as bearish, shares/calls as
    bullish. Missing/foreign holdings are dropped. No fabrication — absent fields stay 0/None."""
    if not xml_text:
        return []
    try:
        # Parse WITH namespaces (13F info tables use either a default or a prefixed namespace); match on
        # the local tag name via _localname. Stripping namespaces by regex leaves unbound prefixes.
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    agg: dict[str, dict] = {}
    for it in root.iter():
        if _localname(it.tag) != "infoTable":
            continue
        cusip = (_text(it, "cusip") or "").strip().upper()
        sym = cusip_to_symbol.get(cusip)
        if not sym:
            continue
        try:
            shares = int(float(_text(it, "sshPrnamt") or 0))
        except (TypeError, ValueError):
            shares = 0
        try:
            value = int(float(_text(it, "value") or 0))     # market value in whole USD (SEC, 2023+)
        except (TypeError, ValueError):
            value = 0
        put_call = (_text(it, "putCall") or "").strip().lower()
        a = agg.setdefault(sym, {"symbol": sym, "cusip": cusip, "long_shares": 0, "put_shares": 0, "value": 0})
        if put_call == "put":
            a["put_shares"] += shares
        else:                                               # equity or call → long/bullish
            a["long_shares"] += shares
        a["value"] += value
    return list(agg.values())


def _track_record_days(first_13f_date: str | None, today: date | None = None) -> int | None:
    if not first_13f_date:
        return None
    try:
        d0 = datetime.strptime(first_13f_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    today = today or datetime.now(timezone.utc).date()
    return max(0, (today - d0).days)


def holding_to_position(cik: str, h: dict, ts: str) -> TraderPosition | None:
    """Map one aggregated holding → a TraderPosition. Direction from the net of long vs put exposure.
    entry_price ≈ reported market value / shares (SEC value is in $000)."""
    long_sh, put_sh, val = h["long_shares"], h["put_shares"], h["value"]
    net_shares = long_sh + put_sh
    if net_shares <= 0 and val <= 0:
        return None
    direction = "LONG" if long_sh > put_sh else "SHORT" if put_sh > long_sh else "NEUTRAL"
    size = long_sh if direction == "LONG" else put_sh if direction == "SHORT" else net_shares
    # SEC reports market value in whole dollars (2023+), so value / shares ≈ the period-end price.
    entry_price = round(val / size, 2) if size > 0 and val > 0 else None
    return TraderPosition(trader_id=cik, symbol=h["symbol"], direction=direction,
                          entry_price=entry_price, position_size=float(size), timestamp=ts)


class Sec13FTraderProvider(TraderProvider):
    """Real institutional 13F positioning from SEC EDGAR. Read-only HTTP GET; no order/trade/broker/IBKR
    access. `configured` is True only when a SEC User-Agent is set (SEC requires it)."""
    name = "sec13f"

    def __init__(self, ciks: list[str] | None = None, user_agent: str | None = None,
                 *, timeout: float = 20.0) -> None:
        self._ciks = ciks or _env_ciks() or list(DEFAULT_CIKS)
        self._ua = user_agent if user_agent is not None else os.environ.get("ATP_SEC_USER_AGENT")
        self._cusip = _cusip_map()
        self._timeout = timeout
        self._cache: dict[str, dict] = {}                  # cik -> {"info":submissions, "holdings":[...]}

    @property
    def configured(self) -> bool:
        return bool(self._ua)

    # -- HTTP (mockable at atp.traders.sec13f.urlopen); errors → None/empty (NO DATA, never fabricated) --
    def _fetch(self, url: str) -> bytes | None:
        if not self._ua:
            return None
        try:
            # No Accept-Encoding → SEC returns identity (uncompressed); urllib doesn't auto-inflate gzip.
            req = Request(url, headers={"User-Agent": self._ua, "Accept": "application/json, */*"})
            with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — fixed https SEC hosts
                return resp.read()
        except (HTTPError, URLError, TimeoutError):
            return None
        except Exception:
            return None

    def _load(self, cik: str) -> dict | None:
        """Fetch + parse a filer's submissions and latest 13F holdings (once per run). Cached."""
        cik = cik.zfill(10)
        if cik in self._cache:
            return self._cache[cik]
        raw = self._fetch(f"{_DATA_BASE}/submissions/CIK{cik}.json")
        info = parse_submissions(json.loads(raw.decode("utf-8"))) if raw else None
        if not info or not info.get("latest"):
            self._cache[cik] = {"info": info, "holdings": []}
            return self._cache[cik]
        acc_nodash = info["latest"]["accession"].replace("-", "")
        cik_int = str(int(cik))
        idx_raw = self._fetch(f"{_WWW_BASE}/Archives/edgar/data/{cik_int}/{acc_nodash}/index.json")
        holdings: list[dict] = []
        if idx_raw:
            try:
                items = json.loads(idx_raw.decode("utf-8")).get("directory", {}).get("item", [])
            except (ValueError, TypeError):
                items = []
            # The info table is the XML that is NOT primary_doc.xml (the cover page).
            table = next((i["name"] for i in items
                          if i.get("name", "").lower().endswith(".xml") and i.get("name") != "primary_doc.xml"), None)
            if table:
                xml_raw = self._fetch(f"{_WWW_BASE}/Archives/edgar/data/{cik_int}/{acc_nodash}/{table}")
                if xml_raw:
                    holdings = parse_info_table(xml_raw.decode("utf-8", "replace"), self._cusip)
        self._cache[cik] = {"info": info, "holdings": holdings}
        return self._cache[cik]

    def get_traders(self) -> list[TraderInfo]:
        out: list[TraderInfo] = []
        for cik in self._ciks:
            data = self._load(cik)
            info = data.get("info") if data else None
            if not info or not info.get("name"):
                continue
            out.append(TraderInfo(
                id=info["cik"], name=info["name"], source="SEC 13F",
                market_focus="US Equities (Institutional)", strategy_type="Institutional 13F",
                track_record_days=_track_record_days(info.get("first_13f_date"))))
        return out

    def get_performance(self, trader_id: str) -> TraderPerformance | None:
        # 13F discloses HOLDINGS, not returns — so every return/risk metric is NO DATA (left None, never
        # fabricated). We still return a performance record (rather than None) so the Quality Engine can
        # score the one real quality dimension 13F provides: the manager's VERIFIED filing track record
        # (from get_traders' track_record_days). A performance-rich source (Darwinex / Collective2) would
        # fill in the return/Sharpe/drawdown metrics.
        return TraderPerformance(trader_id=trader_id.zfill(10))

    def get_positions(self, trader_id: str) -> list[TraderPosition]:
        data = self._load(trader_id)
        if not data:
            return []
        ts = datetime.now(timezone.utc).isoformat()
        out: list[TraderPosition] = []
        for h in data.get("holdings", []):
            p = holding_to_position(trader_id.zfill(10), h, ts)
            if p is not None:
                out.append(p)
        return out

    def get_strategy_metadata(self, trader_id: str) -> StrategyMetadata | None:
        return StrategyMetadata(trader_id=trader_id.zfill(10), strategy_type="Institutional 13F",
                                market_focus="US Equities (Institutional)",
                                description="Quarterly institutional holdings disclosed on SEC Form 13F.",
                                tags=["institutional", "13F", "SEC", "holdings"])

