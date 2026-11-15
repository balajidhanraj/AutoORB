"""Option chain lookup, strike rounding, Black-76 delta estimation, and Tuesday expiry resolver."""

from __future__ import annotations

import calendar
import csv
import io
import math
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

import requests

from broker.shoonya_client import ShoonyaClient
from config.settings import INDEX_SPECS, IST, IndexSpec
from utils.logger import get_logger

logger = get_logger("broker.instrument_mapper")

NFO_MASTER_ZIP_URL = "https://api.shoonya.com/NFO_symbols.txt.zip"


@dataclass(frozen=True)
class InstrumentContract:
    """Cached NFO option contract metadata."""

    index_name: str
    tradingsymbol: str
    token: str
    exchange: str
    lot_size: int
    strike: int
    option_type: str  # "CE" or "PE"
    expiry: date


def round_to_strike(spot_price: float, step: int) -> int:
    """Round an index spot price to the nearest valid option strike step."""
    if step <= 0:
        raise ValueError("Strike step must be positive")
    return int(round(spot_price / step) * step)


def _adjust_for_holiday(target_date: date, min_date: date, holidays: Set[date]) -> date:
    """Shift Tuesday expiry backwards if it lands on an exchange holiday."""
    current = target_date
    while current in holidays and current > min_date:
        current -= timedelta(days=1)
    return current


def get_target_expiry(
    index_name: str,
    ref_date: Optional[date] = None,
    holidays: Optional[Set[date]] = None,
) -> date:
    """Resolve current weekly Tuesday (NIFTY) or monthly last-Tuesday (BANKNIFTY) expiry."""
    spec = INDEX_SPECS[index_name]
    today = ref_date or datetime.now(IST).date()
    holiday_set = holidays or set()

    if spec.expiry_type == "WEEKLY_TUESDAY":
        days_ahead = (1 - today.weekday()) % 7  # Tuesday == 1
        candidate = today + timedelta(days=days_ahead)
        candidate = _adjust_for_holiday(candidate, today, holiday_set)
        if candidate < today:
            candidate = today + timedelta(days=((1 - today.weekday()) % 7 or 7))
        return candidate

    # MONTHLY_LAST_TUESDAY
    year, month = today.year, today.month
    for _ in range(2):
        last_day = calendar.monthrange(year, month)[1]
        last_dt = date(year, month, last_day)
        offset = (last_dt.weekday() - 1) % 7
        last_tuesday = last_dt - timedelta(days=offset)
        last_tuesday = _adjust_for_holiday(last_tuesday, today, holiday_set)
        if last_tuesday >= today:
            return last_tuesday
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return today


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_option_delta(
    spot_price: float,
    strike: float,
    option_type: str,
    vix: float = 15.0,
    days_to_expiry: float = 3.0,
) -> float:
    """Estimate absolute option delta (0.0 to 1.0) using Black-76 with India VIX as IV proxy."""
    if spot_price <= 0 or strike <= 0:
        return 0.5
    sigma = max(0.08, vix / 100.0)
    t_years = max(1.0 / 365.0, days_to_expiry / 365.0)
    vol_sqrt_t = sigma * math.sqrt(t_years)
    d1 = (math.log(spot_price / strike) + 0.5 * (sigma**2) * t_years) / vol_sqrt_t
    call_delta = _norm_cdf(d1)
    if option_type.upper() == "CE":
        return call_delta
    return abs(call_delta - 1.0)


def format_shoonya_option_symbol(
    index_name: str,
    expiry: date,
    option_type: str,
    strike: int,
) -> str:
    """Build standard Shoonya NFO option tradingsymbol (e.g. NIFTY17NOV26C24500)."""
    opt_char = "C" if option_type.upper() == "CE" else "P"
    exp_str = expiry.strftime("%d%b%y").upper()
    return f"{index_name}{exp_str}{opt_char}{strike}"


class InstrumentMapper:
    """Caches active NFO OPTIDX contracts and resolves ATM/1-Strike ITM contracts."""

    def __init__(self, client: ShoonyaClient) -> None:
        self.client = client
        self._contracts: Dict[Tuple[str, date, str, int], InstrumentContract] = {}
        self._resolved_expiries: Dict[str, date] = {}

    @property
    def cached_count(self) -> int:
        return len(self._contracts)

    def _resolve_expiry(self, index_name: str, ref_date: Optional[date] = None) -> date:
        if ref_date is None and index_name in self._resolved_expiries:
            return self._resolved_expiries[index_name]
        return get_target_expiry(index_name, ref_date=ref_date)

    def select_strike(
        self,
        index_name: str,
        spot_price: float,
        option_type: str,
        vix: float = 15.0,
        ref_date: Optional[date] = None,
    ) -> int:
        """Pick ATM or 1-Strike ITM strike targeting 0.55 - 0.65 option delta."""
        spec = INDEX_SPECS[index_name]
        step = spec.strike_step
        atm = round_to_strike(spot_price, step)
        opt = option_type.upper()

        if opt == "CE":
            candidates = [atm, atm - step]
            fallback_itm = atm - step
        else:
            candidates = [atm, atm + step]
            fallback_itm = atm + step

        expiry = self._resolve_expiry(index_name, ref_date=ref_date)
        today = ref_date or datetime.now(IST).date()
        dte = max(0.5, float((expiry - today).days) + 0.5)

        in_band: List[Tuple[float, int]] = []
        for strike in candidates:
            delta = estimate_option_delta(spot_price, float(strike), opt, vix=vix, days_to_expiry=dte)
            if spec.target_delta_min <= delta <= spec.target_delta_max:
                in_band.append((abs(delta - 0.60), strike))

        if in_band:
            in_band.sort(key=lambda item: item[0])
            return in_band[0][1]

        return fallback_itm

    def refresh_instrument_cache(
        self,
        ref_date: Optional[date] = None,
        use_master_zip: bool = True,
    ) -> int:
        """Download & cache active NFO OPTIDX contracts once before market open."""
        today = ref_date or datetime.now(IST).date()
        target_expiries = {
            name: get_target_expiry(name, ref_date=today)
            for name in INDEX_SPECS
        }
        loaded = 0

        if use_master_zip:
            loaded = self._load_from_master_zip(target_expiries, min_date=today)

        if loaded == 0:
            loaded = self._load_from_searchscrip(target_expiries)

        logger.info("Instrument cache refreshed: %d NFO contracts cached", loaded)
        return loaded

    def _load_from_master_zip(
        self,
        target_expiries: Dict[str, date],
        min_date: Optional[date] = None,
    ) -> int:
        try:
            resp = requests.get(NFO_MASTER_ZIP_URL, timeout=20)
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                txt_names = [n for n in zf.namelist() if n.endswith(".txt")]
                if not txt_names:
                    return 0
                raw_text = zf.read(txt_names[0]).decode("utf-8", errors="ignore")
        except Exception as exc:
            logger.warning("NFO master zip download failed, falling back to searchscrip: %s", exc)
            return 0

        today = min_date or datetime.now(IST).date()
        parsed_rows: List[Tuple[str, date, str, int, int, str, str]] = []
        expiries_by_symbol: Dict[str, Set[date]] = {sym: set() for sym in target_expiries}

        reader = csv.DictReader(io.StringIO(raw_text))
        for row in reader:
            if row.get("Instrument") != "OPTIDX":
                continue
            symbol = (row.get("Symbol") or "").strip().upper()
            if symbol not in target_expiries:
                continue
            exp_raw = (row.get("Expiry") or "").strip().upper()
            try:
                exp_date = datetime.strptime(exp_raw, "%d-%b-%Y").date()
            except ValueError:
                continue
            if exp_date < today:
                continue
            try:
                strike = int(float(row["StrikePrice"]))
                lot_size = int(float(row["LotSize"]))
            except (KeyError, ValueError):
                continue
            opt_type = (row.get("OptionType") or "").strip().upper()
            tsym = (row.get("TradingSymbol") or "").strip()
            token = str(row.get("Token") or "").strip()
            if not tsym or not token or opt_type not in {"CE", "PE"}:
                continue
            expiries_by_symbol[symbol].add(exp_date)
            parsed_rows.append((symbol, exp_date, opt_type, strike, lot_size, tsym, token))

        # Resolve active expiry per symbol from live master data (handles exchange holiday shifts)
        active_expiries: Dict[str, date] = {}
        for symbol, fallback_exp in target_expiries.items():
            candidates = sorted(expiries_by_symbol.get(symbol, set()))
            if not candidates:
                active_expiries[symbol] = fallback_exp
            elif fallback_exp in candidates:
                active_expiries[symbol] = fallback_exp
            else:
                # Pick nearest active expiry in the same month/week (e.g., Monday holiday shift)
                active_expiries[symbol] = candidates[0]
            self._resolved_expiries[symbol] = active_expiries[symbol]

        loaded = 0
        for symbol, exp_date, opt_type, strike, lot_size, tsym, token in parsed_rows:
            if exp_date != active_expiries[symbol]:
                continue
            self._contracts[(symbol, exp_date, opt_type, strike)] = InstrumentContract(
                index_name=symbol,
                tradingsymbol=tsym,
                token=token,
                exchange="NFO",
                lot_size=lot_size,
                strike=strike,
                option_type=opt_type,
                expiry=exp_date,
            )
            loaded += 1
        return loaded

    def _load_from_searchscrip(self, target_expiries: Dict[str, date]) -> int:
        loaded = 0
        for index_name, target_exp in target_expiries.items():
            try:
                resp = self.client.searchscrip(exchange="NFO", searchtext=index_name)
            except Exception as exc:
                logger.warning("searchscrip failed for %s: %s", index_name, exc)
                continue
            values = resp.get("values", []) if isinstance(resp, dict) else []
            for item in values:
                if item.get("instname") != "OPTIDX":
                    continue
                tsym = (item.get("tsym") or "").strip()
                token = str(item.get("token") or "").strip()
                opt_type = (item.get("optt") or "").strip().upper()
                if not tsym or not token or opt_type not in {"CE", "PE"}:
                    continue
                try:
                    strike = int(float(item.get("strike") or item.get("strprc") or 0))
                    lot_size = int(float(item.get("ls") or INDEX_SPECS[index_name].lot_size))
                    exp_date = self._parse_expiry_str(item.get("exd", ""))
                except ValueError:
                    continue
                if exp_date != target_exp:
                    continue
                self._contracts[(index_name, exp_date, opt_type, strike)] = InstrumentContract(
                    index_name=index_name,
                    tradingsymbol=tsym,
                    token=token,
                    exchange="NFO",
                    lot_size=lot_size,
                    strike=strike,
                    option_type=opt_type,
                    expiry=exp_date,
                )
                loaded += 1
        return loaded

    @staticmethod
    def _parse_expiry_str(raw: str) -> date:
        cleaned = raw.strip().upper()
        for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d%b%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(cleaned, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Unsupported expiry format: {raw}")

    def select_option_contract(
        self,
        index_name: str,
        spot_price: float,
        option_type: str,
        vix: float = 15.0,
        ref_date: Optional[date] = None,
    ) -> InstrumentContract:
        """Select the cached NFO option contract matching target delta/strike and expiry."""
        spec: IndexSpec = INDEX_SPECS[index_name]
        opt = option_type.upper()
        expiry = self._resolve_expiry(index_name, ref_date=ref_date)
        strike = self.select_strike(index_name, spot_price, opt, vix=vix, ref_date=ref_date)

        contract = self._contracts.get((index_name, expiry, opt, strike))
        if contract is not None:
            return contract

        # Deterministic synthetic fallback for PAPER mode / offline tests
        tsym = format_shoonya_option_symbol(index_name, expiry, opt, strike)
        token = f"SIM_{index_name}_{opt}_{strike}"
        return InstrumentContract(
            index_name=index_name,
            tradingsymbol=tsym,
            token=token,
            exchange=spec.opt_exchange,
            lot_size=spec.lot_size,
            strike=strike,
            option_type=opt,
            expiry=expiry,
        )
