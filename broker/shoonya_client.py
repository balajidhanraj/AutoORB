"""Thread-safe wrapper around Shoonya NorenRestApiOAuth REST, WebSocket, and PAPER simulator."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import requests
import websocket

from config.settings import IST, AppSettings
from utils.logger import get_logger

logger = get_logger("broker.client")

try:
    from NorenRestApiPy.NorenApi import NorenApi as _UpstreamNorenApi  # type: ignore
except ImportError:
    _UpstreamNorenApi = None


class CompatibleNorenApi:
    """Lightweight REST + WebSocket compatible Shoonya OAuth API client fallback."""

    def __init__(
        self,
        host: str = "https://api.shoonya.com/NorenWClientAPI/",
        websocket_url: str = "wss://api.shoonya.com/NorenWSAPI/",
        api_key: str = "",
        secret_key: str = "",
        redirect_uri: str = "http://127.0.0.1:8080/callback",
        user_id: str = "",
    ) -> None:
        self.host = host.rstrip("/") + "/"
        self.websocket_url = websocket_url
        self.api_key = api_key
        self.secret_key = secret_key
        self.redirect_uri = redirect_uri
        self.access_token: str = ""
        self.susertoken: str = ""
        self.username: str = user_id.strip().upper()
        self.account_id: str = user_id.strip().upper()
        self._session = requests.Session()
        self._ws_app: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_connected = False
        self._reauth_callback: Optional[Callable[[], bool]] = None

    def _resolve_client_id(self) -> str:
        """Resolve Shoonya OAuth client_id (typically <USER_ID>_U, not the 64-char secret)."""
        if self.api_key and self.api_key != self.secret_key and len(self.api_key) != 64:
            if not self.api_key.endswith("_U") and self.username and self.api_key == self.username:
                return f"{self.api_key}_U"
            return self.api_key
        if self.username:
            return f"{self.username}_U"
        return self.api_key

    def getOAuthURL(self) -> str:  # noqa: N802
        client_id = self._resolve_client_id()
        return f"https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id={client_id}"

    def fetch_oauth_code_direct(
        self,
        user_id: str,
        password: str,
        totp_code: str,
        client_id: Optional[str] = None,
    ) -> str:
        """Obtain the OAuth authorization code via Shoonya's QuickAuth NOREN_API handshake."""
        uid = user_id.strip().upper()
        self.username = uid
        self.account_id = self.account_id or uid
        if client_id and client_id != self.secret_key and len(client_id) != 64:
            cid = client_id
        else:
            cid = self._resolve_client_id() or f"{uid}_U"

        pwd_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        app_hash = hashlib.sha256(f"{uid}|S3cur3!d".encode("utf-8")).hexdigest()
        payload = {
            "apkversion": "W2_20260827",
            "uid": uid,
            "pwd": pwd_hash,
            "factor2": totp_code,
            "appkey": app_hash,
            "imei": str(uuid.uuid4()),
            "addldivinf": "Mozilla/5.0",
            "app_key": cid,
            "source": "API",
            "vc": "NOREN_API",
        }
        resp = self._session.post(
            f"{self.host}QuickAuth",
            data=f"jData={json.dumps(payload)}",
            timeout=15,
        )
        data = resp.json()
        if data.get("stat") == "Ok":
            if data.get("susertoken"):
                self.susertoken = str(data["susertoken"]).strip()
            if data.get("code"):
                return str(data["code"]).strip()
        raise RuntimeError(f"QuickAuth OAuth code request failed: {data}")

    def getAccessToken(self, code: str) -> Dict[str, Any]:  # noqa: N802
        cid = self._resolve_client_id()
        uid = self.username
        checksum = hashlib.sha256((cid + self.secret_key + code).encode("utf-8")).hexdigest()
        payload = {
            "code": code,
            "checksum": checksum,
            "uid": uid,
        }
        resp = self._session.post(
            f"{self.host}GenAcsTok",
            data=f"jData={json.dumps(payload)}",
            timeout=15,
        )
        data = resp.json()
        if isinstance(data, dict) and data.get("susertoken") and not data.get("access_token"):
            data["access_token"] = data["susertoken"]
        return data

    def injectOAuthHeader(
        self,
        access_token: str,
        user_id: str,
        account_id: str,
        susertoken: str = "",
        use_bearer: bool = True,
    ) -> None:  # noqa: N802
        self.access_token = access_token if use_bearer else ""
        if susertoken:
            self.susertoken = susertoken
        elif not getattr(self, "susertoken", ""):
            self.susertoken = access_token
        self.username = user_id
        self.account_id = account_id or user_id
        if use_bearer and access_token:
            self._session.headers.update({"Authorization": f"Bearer {access_token}"})
        else:
            self._session.headers.pop("Authorization", None)

    def _post(self, endpoint: str, body: Dict[str, Any], _retry: bool = True) -> Any:
        token_to_send = getattr(self, "susertoken", "") or getattr(self, "access_token", "")
        if token_to_send:
            data = f"jData={json.dumps(body)}&jKey={token_to_send}"
        else:
            data = f"jData={json.dumps(body)}"
        try:
            resp = self._session.post(f"{self.host}{endpoint}", data=data, timeout=15)
            if resp.status_code == 401 and _retry and self._reauth_callback:
                logger.warning("Shoonya API returned 401 on %s; attempting automatic re-auth...", endpoint)
                if self._reauth_callback():
                    return self._post(endpoint, body, _retry=False)

            # Shoonya Noren API frequently returns HTTP 400/404 with a JSON body {"stat":"Not_Ok","emsg":"..."}.
            # Parse JSON before raise_for_status() so callers get Shoonya's real rejection reason instead of raw 400/404.
            res: Any = None
            try:
                res = resp.json()
            except ValueError:
                res = None

            if res is None:
                resp.raise_for_status()
                return {}

            if isinstance(res, dict) and res.get("stat") == "Not_Ok" and _retry and self._reauth_callback:
                emsg = str(res.get("emsg") or "").lower()
                if any(err in emsg for err in ("session", "invalid session", "not logged in", "expired")):
                    logger.warning("Shoonya API session invalid on %s (%s); triggering re-auth...", endpoint, emsg)
                    if self._reauth_callback():
                        return self._post(endpoint, body, _retry=False)
            return res
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401 and _retry and self._reauth_callback:
                logger.warning("Shoonya API 401 HTTPError on %s; triggering re-auth...", endpoint)
                if self._reauth_callback():
                    return self._post(endpoint, body, _retry=False)
            raise

    def get_limits(self, _retry: bool = True) -> Dict[str, Any]:
        return self._post("Limits", {"uid": self.username, "actid": self.account_id}, _retry=_retry)

    def get_user_details(self) -> Dict[str, Any]:
        resp = self._post("UserDetails", {"uid": self.username})
        return resp if isinstance(resp, dict) else {}

    def searchscrip(self, exchange: str, searchtext: str) -> Dict[str, Any]:
        return self._post("SearchScrip", {"uid": self.username, "exch": exchange, "stext": searchtext})

    def get_quotes(self, exchange: str, token: str) -> Dict[str, Any]:
        resp = self._post("GetQuotes", {"uid": self.username, "exch": exchange, "token": str(token)})
        return resp if isinstance(resp, dict) else {}

    def get_time_price_series(
        self,
        exchange: str,
        token: str,
        starttime: Optional[int] = None,
        endtime: Optional[int] = None,
        interval: Optional[str] = None,
        intrv: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "uid": self.username,
            "exch": exchange,
            "token": str(token),
            "intrv": str(intrv or interval or "5"),
        }
        if starttime is not None:
            payload["st"] = str(int(starttime))
        if endtime is not None:
            payload["et"] = str(int(endtime))
        resp = self._post("TPSeries", payload)
        return resp if isinstance(resp, list) else []

    def place_order(self, **kwargs: Any) -> Dict[str, Any]:
        payload = {
            "ordersource": "API",
            "uid": self.username,
            "actid": self.account_id,
            "exch": kwargs["exchange"],
            "tsym": kwargs["tradingsymbol"],
            "qty": str(kwargs["quantity"]),
            "prc": str(kwargs.get("price", 0)),
            "trgprc": str(kwargs.get("trigger_price", 0)),
            "dscqty": str(kwargs.get("discloseqty", 0)),
            "prd": kwargs.get("product_type", "M"),
            "trantype": kwargs["buy_or_sell"],
            "prctyp": kwargs.get("price_type", "LMT"),
            "ret": kwargs.get("retention", "DAY"),
            "remarks": kwargs.get("remarks", "ORB_UI"),
            "amo": kwargs.get("amo", "NO"),
        }
        return self._post("PlaceOrder", payload)

    def modify_order(self, **kwargs: Any) -> Dict[str, Any]:
        payload = {
            "ordersource": "API",
            "uid": self.username,
            "norenordno": str(kwargs["orderno"]),
            "exch": kwargs["exchange"],
            "tsym": kwargs["tradingsymbol"],
            "qty": str(kwargs["newquantity"]),
            "prctyp": kwargs.get("newprice_type", "SL-LMT"),
            "prc": str(kwargs.get("newprice", 0)),
            "trgprc": str(kwargs.get("newtrigger_price", 0)),
        }
        return self._post("ModifyOrder", payload)

    def cancel_order(self, orderno: str) -> Dict[str, Any]:
        return self._post("CancelOrder", {"ordersource": "API", "uid": self.username, "norenordno": str(orderno)})

    def get_positions(self) -> List[Dict[str, Any]]:
        resp = self._post("PositionBook", {"uid": self.username, "actid": self.account_id})
        return resp if isinstance(resp, list) else []

    def get_order_book(self) -> List[Dict[str, Any]]:
        resp = self._post("OrderBook", {"uid": self.username})
        return resp if isinstance(resp, list) else []

    def start_websocket(
        self,
        order_update_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        subscribe_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        socket_open_callback: Optional[Callable[[], None]] = None,
        socket_close_callback: Optional[Callable[[], None]] = None,
        socket_error_callback: Optional[Callable[[Any], None]] = None,
    ) -> None:
        def _on_open(ws: Any) -> None:
            token_to_send = getattr(self, "susertoken", None) or self.access_token
            connect_msg = {
                "t": "c",
                "uid": self.username,
                "actid": self.account_id,
                "susertoken": token_to_send,
                "source": "API",
            }
            ws.send(json.dumps(connect_msg))

        def _on_message(_ws: Any, message: str) -> None:
            try:
                data = json.loads(message)
            except Exception:
                return
            msg_type = data.get("t")
            if msg_type in {"ck", "ak"}:
                if data.get("s") in ("OK", "Ok"):
                    self._ws_connected = True
                    logger.info("Shoonya WebSocket handshake confirmed (%s=OK)", msg_type)
                    if socket_open_callback:
                        socket_open_callback()
                else:
                    logger.error("Shoonya WebSocket auth rejected: %s", data)
                    if self._reauth_callback:
                        logger.warning("Shoonya WebSocket auth rejected; triggering background re-auth...")
                        threading.Thread(target=self._reauth_callback, name="ws-auth-refresh", daemon=True).start()
                    if socket_error_callback:
                        socket_error_callback(data)
            elif msg_type in {"tk", "tf", "dk", "df"} and subscribe_callback:
                subscribe_callback(data)
            elif msg_type in {"om"} and order_update_callback:
                order_update_callback(data)

        def _on_error(_ws: Any, error: Any) -> None:
            if socket_error_callback:
                socket_error_callback(error)

        def _on_close(_ws: Any, *_args: Any) -> None:
            self._ws_connected = False
            if socket_close_callback:
                socket_close_callback()

        self._ws_app = websocket.WebSocketApp(
            self.websocket_url,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )
        self._ws_thread = threading.Thread(
            target=self._ws_app.run_forever,
            kwargs={"ping_interval": 20, "ping_timeout": 10},
            name="shoonya-ws",
            daemon=True,
        )
        self._ws_thread.start()

    def close_websocket(self) -> None:
        if self._ws_app is not None:
            try:
                self._ws_app.close()
            except Exception:
                pass
            self._ws_app = None
            self._ws_connected = False

    def subscribe(self, instrument: Any) -> None:
        if self._ws_app is None or not self._ws_connected:
            return
        keys = instrument if isinstance(instrument, list) else [instrument]
        payload = {"t": "t", "k": "#".join(str(k) for k in keys)}
        self._ws_app.send(json.dumps(payload))


class ShoonyaClient:
    """Thread-safe Shoonya broker client supporting LIVE and PAPER modes with WS watchdog."""

    def __init__(self, settings: AppSettings, raw_api: Optional[Any] = None) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self.ltp_cache: Dict[str, float] = {}
        self.volume_cache: Dict[str, int] = {}
        self.subscribed_tokens: set[str] = set()
        self.last_tick_monotonic: float = 0.0
        self.ws_connected: bool = False

        # Paper simulator state
        self._paper_orders: Dict[str, Dict[str, Any]] = {}
        self._paper_positions: Dict[str, Dict[str, Any]] = {}
        self._token_by_tsym: Dict[str, str] = {}
        self._broker_snapshot_cache: Optional[Dict[str, Any]] = None
        self._broker_snapshot_ts: float = 0.0

        # External callback hooks
        self._tick_listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._order_listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._reconnect_callbacks: List[Callable[[], None]] = []
        self._reauth_callback: Optional[Callable[[], bool]] = None
        self._last_reconnect_monotonic: float = 0.0

        if raw_api is not None:
            self.api = raw_api
        else:
            self.api = CompatibleNorenApi(
                host=settings.shoonya_host,
                websocket_url=settings.shoonya_ws_host,
                api_key=settings.shoonya_api_key,
                secret_key=settings.shoonya_secret_key,
                redirect_uri=settings.shoonya_redirect_uri,
                user_id=settings.shoonya_user_id,
            )

    def register_tick_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._tick_listeners.append(callback)

    def register_order_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._order_listeners.append(callback)

    def register_reconnect_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._reconnect_callbacks.append(callback)

    def register_reauth_callback(self, callback: Callable[[], bool]) -> None:
        with self._lock:
            self._reauth_callback = callback
            if hasattr(self.api, "_reauth_callback"):
                self.api._reauth_callback = callback

    def reconnect_websocket(self) -> None:
        """Close existing WebSocket and restart connection with fresh session credentials."""
        logger.info("Restarting Shoonya WebSocket connection with fresh credentials...")
        with self._lock:
            self.ws_connected = False
        try:
            if hasattr(self.api, "close_websocket"):
                self.api.close_websocket()
        except Exception:
            pass
        self.start_websocket()

    def register_symbol_token(self, tradingsymbol: str, exchange: str, token: str) -> None:
        """Map tradingsymbol to exchange|token for paper order fill simulation."""
        with self._lock:
            self._token_by_tsym[tradingsymbol] = f"{exchange}|{token}"

    def update_ltp(self, key: str, ltp: float, volume: Optional[int] = None) -> None:
        """Update cached LTP & volume for a token key (e.g., NSE|26000) and evaluate paper fills."""
        with self._lock:
            self.ltp_cache[key] = float(ltp)
            if volume is not None:
                self.volume_cache[key] = int(volume)
            self.last_tick_monotonic = time.monotonic()

        if self.settings.mode == "PAPER":
            self._evaluate_paper_orders_for_key(key, float(ltp))

    def get_ltp(self, key: str) -> Optional[float]:
        with self._lock:
            return self.ltp_cache.get(key)

    def get_volume(self, key: str) -> int:
        with self._lock:
            return self.volume_cache.get(key, 0)

    def start_websocket(self) -> None:
        """Connect to Shoonya WebSocket and begin streaming subscribed tokens."""
        def _on_tick(tick: Dict[str, Any]) -> None:
            exch = tick.get("e", "")
            tok = tick.get("tk", "")
            key = f"{exch}|{tok}" if exch and tok else ""
            raw_lp = tick.get("lp")
            if key and raw_lp is not None:
                try:
                    lp = float(raw_lp)
                    vol = int(float(tick["v"])) if tick.get("v") is not None else None
                    self.update_ltp(key, lp, vol)
                except (ValueError, TypeError):
                    pass

            for listener in list(self._tick_listeners):
                try:
                    listener(tick)
                except Exception as exc:
                    logger.error("Tick listener error: %s", exc)

        def _on_order_update(order_msg: Dict[str, Any]) -> None:
            for listener in list(self._order_listeners):
                try:
                    listener(order_msg)
                except Exception as exc:
                    logger.error("Order listener error: %s", exc)

        def _on_open() -> None:
            with self._lock:
                self.ws_connected = True
                self.last_tick_monotonic = time.monotonic()
                tokens = list(self.subscribed_tokens)
            logger.info("Shoonya WebSocket connected; subscribing to %d tokens", len(tokens))
            if tokens:
                try:
                    self.api.subscribe(tokens)
                except Exception as exc:
                    logger.error("Failed re-subscribing tokens on WS open: %s", exc)

        def _on_close() -> None:
            with self._lock:
                self.ws_connected = False
            logger.warning("Shoonya WebSocket closed")

        def _on_error(err: Any) -> None:
            logger.error("Shoonya WebSocket error: %s", err)

        self.api.start_websocket(
            order_update_callback=_on_order_update,
            subscribe_callback=_on_tick,
            socket_open_callback=_on_open,
            socket_close_callback=_on_close,
            socket_error_callback=_on_error,
        )

    def subscribe(self, keys: List[str]) -> None:
        with self._lock:
            new_keys = [k for k in keys if k]
            self.subscribed_tokens.update(new_keys)
        if new_keys and self.ws_connected:
            try:
                self.api.subscribe(new_keys)
            except Exception as exc:
                logger.error("WebSocket subscribe error: %s", exc)

    def check_ws_watchdog(self, now_ist: Optional[datetime] = None) -> bool:
        """Reconnect WebSocket and trigger candle backfill if feed stalls >15s in market hours."""
        dt = now_ist or datetime.now(IST)
        in_market = self.settings.market_open_time <= dt.time() <= self.settings.market_close_time
        if not in_market or not self.subscribed_tokens:
            return False

        now_m = time.monotonic()
        if not self.ws_connected and (now_m - self._last_reconnect_monotonic) < self.settings.ws_watchdog_timeout_sec:
            return False

        elapsed = now_m - self.last_tick_monotonic if self.last_tick_monotonic > 0 else 999.0
        if self.ws_connected and elapsed <= self.settings.ws_watchdog_timeout_sec:
            return False

        self._last_reconnect_monotonic = now_m
        logger.warning("WebSocket watchdog triggered (connected=%s, silent=%.1fs); reconnecting...", self.ws_connected, elapsed)
        try:
            if hasattr(self.api, "close_websocket"):
                self.api.close_websocket()
        except Exception:
            pass
        self.start_websocket()
        for cb in list(self._reconnect_callbacks):
            try:
                cb()
            except Exception as exc:
                logger.error("Reconnect callback failed: %s", exc)
        return True

    def searchscrip(self, exchange: str, searchtext: str) -> Dict[str, Any]:
        return self.api.searchscrip(exchange=exchange, searchtext=searchtext)

    def get_quotes(self, exchange: str, token: str) -> Dict[str, Any]:
        if hasattr(self.api, "get_quotes"):
            return self.api.get_quotes(exchange=exchange, token=token)
        return {}

    def get_time_price_series(
        self,
        exchange: str,
        token: str,
        starttime: int,
        endtime: Optional[int] = None,
        intrv: str = "5",
    ) -> List[Dict[str, Any]]:
        return self.api.get_time_price_series(
            exchange=exchange,
            token=token,
            starttime=starttime,
            endtime=endtime,
            intrv=intrv,
        )

    def place_order(
        self,
        buy_or_sell: str,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        price_type: str = "LMT",
        price: float = 0.0,
        trigger_price: float = 0.0,
        product_type: str = "I",
        retention: str = "DAY",
        discloseqty: int = 0,
        remarks: str = "",
    ) -> Dict[str, Any]:
        """Place an order in LIVE mode or simulate fill/resting order in PAPER mode."""
        if self.settings.mode == "LIVE":
            return self.api.place_order(
                buy_or_sell=buy_or_sell,
                product_type=product_type,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                quantity=quantity,
                discloseqty=discloseqty,
                price_type=price_type,
                price=price,
                trigger_price=trigger_price,
                retention=retention,
                remarks=remarks or self.settings.sebi_algo_id,
            )

        return self._paper_place_order(
            buy_or_sell=buy_or_sell,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            quantity=quantity,
            price_type=price_type,
            price=price,
            trigger_price=trigger_price,
            remarks=remarks or self.settings.sebi_algo_id,
        )

    def modify_order(
        self,
        orderno: str,
        exchange: str,
        tradingsymbol: str,
        newquantity: int,
        newprice_type: str = "SL-LMT",
        newprice: float = 0.0,
        newtrigger_price: float = 0.0,
    ) -> Dict[str, Any]:
        if self.settings.mode == "LIVE":
            return self.api.modify_order(
                orderno=orderno,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                newquantity=newquantity,
                newprice_type=newprice_type,
                newprice=newprice,
                newtrigger_price=newtrigger_price,
            )

        with self._lock:
            order = self._paper_orders.get(orderno)
            if not order:
                return {"stat": "Not_Ok", "emsg": "Paper order not found"}
            if order["status"] in {"COMPLETE", "CANCELED", "REJECTED"}:
                return {"stat": "Not_Ok", "emsg": f"Cannot modify order in status {order['status']}"}
            order["qty"] = int(newquantity)
            order["prctyp"] = newprice_type
            order["prc"] = float(newprice)
            order["trgprc"] = float(newtrigger_price)
            logger.info(
                "[PAPER] Modified order %s (%s): qty=%s type=%s prc=%.2f trg=%.2f",
                orderno,
                tradingsymbol,
                newquantity,
                newprice_type,
                newprice,
                newtrigger_price,
            )
            return {"stat": "Ok", "result": orderno}

    def cancel_order(self, orderno: str) -> Dict[str, Any]:
        if self.settings.mode == "LIVE":
            return self.api.cancel_order(orderno=orderno)

        with self._lock:
            order = self._paper_orders.get(orderno)
            if not order:
                return {"stat": "Not_Ok", "emsg": "Paper order not found"}
            if order["status"] in {"COMPLETE", "CANCELED"}:
                return {"stat": "Ok", "result": orderno}
            order["status"] = "CANCELED"
            logger.info("[PAPER] Canceled order %s", orderno)
            return {"stat": "Ok", "result": orderno}

    def get_order_book(self) -> List[Dict[str, Any]]:
        if self.settings.mode == "LIVE":
            return self.api.get_order_book()
        with self._lock:
            return [dict(o) for o in self._paper_orders.values()]

    def get_order_status(self, orderno: str) -> Optional[Dict[str, Any]]:
        for order in self.get_order_book():
            if str(order.get("norenordno")) == str(orderno):
                return order
        return None

    def get_positions(self) -> List[Dict[str, Any]]:
        if self.settings.mode == "LIVE":
            return self.api.get_positions()
        with self._lock:
            return [dict(pos) for pos in self._paper_positions.values()]

    @staticmethod
    def _to_float(val: Any, default: float = 0.0) -> float:
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    def update_credentials(self, settings: AppSettings) -> None:
        """Apply updated Shoonya credentials and clear cached session state."""
        with self._lock:
            self.settings = settings
            self._broker_snapshot_cache = None
            self._broker_snapshot_ts = 0.0
            uid = settings.shoonya_user_id.strip().upper()
            actid = (settings.shoonya_account_id or uid).strip().upper()
            if hasattr(self.api, "username"):
                self.api.username = uid
            if hasattr(self.api, "account_id"):
                self.api.account_id = actid
            if hasattr(self.api, "api_key"):
                self.api.api_key = settings.shoonya_api_key.strip()
            if hasattr(self.api, "secret_key"):
                self.api.secret_key = settings.shoonya_secret_key.strip()
            if settings.shoonya_access_token:
                if hasattr(self.api, "access_token"):
                    self.api.access_token = settings.shoonya_access_token
                if hasattr(self.api, "injectOAuthHeader"):
                    susertoken = getattr(self.api, "susertoken", "") or settings.shoonya_access_token
                    self.api.injectOAuthHeader(settings.shoonya_access_token, uid, actid, susertoken=susertoken)
            else:
                if hasattr(self.api, "access_token"):
                    self.api.access_token = ""
                sess = getattr(self.api, "_session", None)
                if sess is not None and hasattr(sess, "headers"):
                    sess.headers.pop("Authorization", None)

    def get_live_broker_snapshot(self, force: bool = False) -> Dict[str, Any]:
        """Fetch live Shoonya account limits, order book, and positions (10s cached)."""
        empty_snapshot: Dict[str, Any] = {
            "connected": False,
            "user_id": self.settings.shoonya_user_id,
            "account_id": self.settings.shoonya_account_id or self.settings.shoonya_user_id,
            "profile_name": "",
            "account_name": "",
            "cash": 0.0,
            "payin": 0.0,
            "payout": 0.0,
            "collateral": 0.0,
            "margin_used": 0.0,
            "net_available": 0.0,
            "request_time": "",
            "orders": [],
            "positions": [],
            "error": "",
        }
        if not (getattr(self.api, "access_token", "") or getattr(self.api, "susertoken", "")):
            return empty_snapshot

        with self._lock:
            if (
                not force
                and self._broker_snapshot_cache is not None
                and (time.monotonic() - self._broker_snapshot_ts) < 10.0
            ):
                return dict(self._broker_snapshot_cache)

        snapshot = dict(empty_snapshot)
        try:
            limits = self.api.get_limits()
            if isinstance(limits, dict) and limits.get("stat") == "Ok":
                cash = self._to_float(limits.get("cash"))
                payin = self._to_float(limits.get("payin"))
                payout = self._to_float(limits.get("payout"))
                collateral = self._to_float(limits.get("brkcollamt"))
                margin_used = self._to_float(limits.get("marginused"))
                net_avail = round(cash + payin + collateral - margin_used, 2)
                snapshot.update(
                    {
                        "connected": True,
                        "profile_name": str(limits.get("prfname") or "SHOONYA"),
                        "cash": cash,
                        "payin": payin,
                        "payout": payout,
                        "collateral": collateral,
                        "margin_used": margin_used,
                        "net_available": net_avail,
                        "request_time": str(limits.get("request_time") or ""),
                    }
                )
            elif isinstance(limits, dict):
                snapshot["error"] = str(limits.get("emsg") or "Unable to fetch Limits")
        except Exception as exc:
            snapshot["error"] = str(exc)

        if hasattr(self.api, "get_user_details"):
            try:
                user_info = self.api.get_user_details()
                if isinstance(user_info, dict) and user_info.get("stat") == "Ok":
                    snapshot["account_name"] = str(
                        user_info.get("uname") or user_info.get("cliname") or ""
                    )
                    if user_info.get("actid"):
                        snapshot["account_id"] = str(user_info["actid"])
                    if user_info.get("uid"):
                        snapshot["user_id"] = str(user_info["uid"])
            except Exception:
                pass

        try:
            raw_orders = self.api.get_order_book() or []
            norm_orders = []
            for o in raw_orders:
                if not isinstance(o, dict):
                    continue
                norm_orders.append(
                    {
                        "norenordno": str(o.get("norenordno") or ""),
                        "norentm": str(o.get("norentm") or ""),
                        "exch": str(o.get("exch") or ""),
                        "tsym": str(o.get("tsym") or ""),
                        "trantype": str(o.get("trantype") or ""),
                        "prd": str(o.get("s_prdt_ali") or o.get("prd") or ""),
                        "prctyp": str(o.get("prctyp") or ""),
                        "qty": str(o.get("qty") or "0"),
                        "fillshares": str(o.get("fillshares") or "0"),
                        "prc": str(o.get("avgprc") or o.get("prc") or "0"),
                        "status": str(o.get("status") or ""),
                        "ordersource": str(o.get("ordersource") or ""),
                        "reason": str(o.get("rejreason") or o.get("remarks") or ""),
                    }
                )
            snapshot["orders"] = norm_orders
        except Exception as exc:
            logger.debug("Live OrderBook fetch failed: %s", exc)

        try:
            raw_positions = self.api.get_positions() or []
            norm_positions = []
            for p in raw_positions:
                if not isinstance(p, dict):
                    continue
                norm_positions.append(
                    {
                        "exch": str(p.get("exch") or ""),
                        "tsym": str(p.get("tsym") or ""),
                        "prd": str(p.get("s_prdt_ali") or p.get("prd") or ""),
                        "netqty": str(p.get("netqty") or "0"),
                        "netavgprc": str(p.get("netavgprc") or "0.00"),
                        "lp": str(p.get("lp") or "0.00"),
                        "rpnl": str(p.get("rpnl") or "0.00"),
                        "urmtom": str(p.get("urmtom") or "0.00"),
                    }
                )
            snapshot["positions"] = norm_positions
        except Exception as exc:
            logger.debug("Live PositionBook fetch failed: %s", exc)

        with self._lock:
            self._broker_snapshot_cache = dict(snapshot)
            self._broker_snapshot_ts = time.monotonic()
        return snapshot

    def place_live_broker_order(
        self,
        exchange: str,
        tradingsymbol: str,
        buy_or_sell: str,
        quantity: int,
        price_type: str = "LMT",
        price: float = 0.0,
        product_type: str = "M",
    ) -> Dict[str, Any]:
        """Place a manual/test order directly on the live Shoonya broker API."""
        if not (getattr(self.api, "access_token", "") or getattr(self.api, "susertoken", "")):
            return {"stat": "Not_Ok", "emsg": "Shoonya session is not authenticated. Connect account first."}

        prd = str(product_type).strip().upper()
        if prd in ("NRML", "NORMAL", "C", "CNC"):
            prd = "M"
        elif prd in ("MIS", "INTRADAY"):
            prd = "I"

        now_ist = datetime.now(IST)
        mkt_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        mkt_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
        is_market_hours = (now_ist.weekday() < 5) and (mkt_open <= now_ist <= mkt_close)
        amo_flag = "NO" if is_market_hours else "YES"

        norm_prctyp = price_type.strip().upper()
        norm_price = float(price)
        norm_side = buy_or_sell.strip().upper()
        norm_exch = exchange.strip().upper()
        norm_tsym = tradingsymbol.strip().upper()

        # SEBI ALGO_CHK rule on Shoonya rejects MKT orders via API ("MKT Order type not allowed for API order").
        # Convert MKT to a marketable LMT order using live LTP +/- protective buffer.
        if norm_prctyp == "MKT":
            norm_prctyp = "LMT"
            if norm_price <= 0:
                ref_ltp = 0.0
                key = self._token_by_tsym.get(norm_tsym, "")
                if key:
                    ref_ltp = float(self.ltp_cache.get(key) or 0.0)
                if ref_ltp <= 0:
                    try:
                        sc = self.api.searchscrip(norm_exch, norm_tsym)
                        vals = sc.get("values") if isinstance(sc, dict) else None
                        if isinstance(vals, list) and vals:
                            tok = str(vals[0].get("token") or "")
                            if tok:
                                q = self.api.get_quotes(norm_exch, tok)
                                if isinstance(q, dict):
                                    ref_ltp = float(q.get("lp") or q.get("c") or 0.0)
                    except Exception:
                        pass
                if ref_ltp > 0:
                    norm_price = round(ref_ltp + 5.0, 1) if norm_side == "B" else round(max(0.05, ref_ltp - 5.0), 1)
                else:
                    return {
                        "stat": "Not_Ok",
                        "emsg": "Shoonya SEBI ALGO_CHK disallows MKT API orders. Switch to LMT and enter a Limit Price.",
                    }

        try:
            resp = self.api.place_order(
                buy_or_sell=norm_side,
                product_type=prd,
                exchange=norm_exch,
                tradingsymbol=norm_tsym,
                quantity=int(quantity),
                discloseqty=0,
                price_type=norm_prctyp,
                price=norm_price,
                trigger_price=0.0,
                retention="DAY",
                remarks=self.settings.sebi_algo_id or "UI_MANUAL",
                amo=amo_flag,
            )
        except Exception as exc:
            resp = {"stat": "Not_Ok", "emsg": str(exc)}
        self.get_live_broker_snapshot(force=True)
        return resp if isinstance(resp, dict) else {"stat": "Not_Ok", "emsg": str(resp)}

    def _paper_place_order(
        self,
        buy_or_sell: str,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        price_type: str,
        price: float,
        trigger_price: float,
        remarks: str,
    ) -> Dict[str, Any]:
        with self._lock:
            orderno = f"PAPER-{uuid.uuid4().hex[:10].upper()}"
            key = self._token_by_tsym.get(tradingsymbol, "")
            ltp = self.ltp_cache.get(key) if key else None
            order = {
                "norenordno": orderno,
                "exch": exchange,
                "tsym": tradingsymbol,
                "trantype": buy_or_sell,
                "qty": int(quantity),
                "fillshares": 0,
                "prctyp": price_type,
                "prc": float(price),
                "trgprc": float(trigger_price),
                "avgprc": 0.0,
                "status": "OPEN" if price_type == "LMT" else "TRIGGER_PENDING",
                "remarks": remarks,
            }
            self._paper_orders[orderno] = order

            # Immediate realistic paper fill if market or marketable limit
            slip = self.settings.paper_slippage_pts
            if price_type == "MKT":
                ref_px = ltp if ltp is not None else float(price)
                fill_px = round((ref_px + slip) if buy_or_sell == "B" else max(0.05, ref_px - slip), 2)
                self._fill_paper_order(order, fill_px)
            elif price_type == "LMT" and ltp is not None:
                if buy_or_sell == "B" and price >= ltp:
                    fill_px = round(min(price, ltp + slip), 2)
                    self._fill_paper_order(order, fill_px)
                elif buy_or_sell == "S" and price <= ltp:
                    fill_px = round(max(price, max(0.05, ltp - slip)), 2)
                    self._fill_paper_order(order, fill_px)

            logger.info(
                "[PAPER] Placed %s %s %s qty=%d type=%s prc=%.2f trg=%.2f -> status=%s avg=%.2f",
                orderno,
                buy_or_sell,
                tradingsymbol,
                quantity,
                price_type,
                price,
                trigger_price,
                order["status"],
                order["avgprc"],
            )
            return {"stat": "Ok", "norenordno": orderno}

    def _fill_paper_order(self, order: Dict[str, Any], fill_price: float) -> None:
        order["status"] = "COMPLETE"
        order["fillshares"] = order["qty"]
        order["avgprc"] = float(fill_price)
        tsym = order["tsym"]
        qty = int(order["qty"])
        signed_qty = qty if order["trantype"] == "B" else -qty

        pos = self._paper_positions.setdefault(
            tsym,
            {"tsym": tsym, "exch": order["exch"], "netqty": "0", "netavgprc": "0.0"},
        )
        prev_qty = int(pos["netqty"])
        new_qty = prev_qty + signed_qty
        pos["netqty"] = str(new_qty)
        if new_qty != 0:
            pos["netavgprc"] = f"{fill_price:.2f}"

        for listener in list(self._order_listeners):
            try:
                listener(dict(order))
            except Exception as exc:
                logger.error("Paper order listener error: %s", exc)

    def _evaluate_paper_orders_for_key(self, key: str, ltp: float) -> None:
        with self._lock:
            for order in list(self._paper_orders.values()):
                if order["status"] not in {"OPEN", "TRIGGER_PENDING"}:
                    continue
                tsym_key = self._token_by_tsym.get(order["tsym"])
                if tsym_key != key:
                    continue

                slip = self.settings.paper_slippage_pts
                if order["prctyp"] == "SL-LMT" and order["trantype"] == "S" and ltp <= order["trgprc"]:
                    fill_px = round(max(order["prc"], max(0.05, ltp - slip)), 2)
                    self._fill_paper_order(order, fill_px)
                elif order["prctyp"] == "LMT":
                    if order["trantype"] == "B" and ltp <= order["prc"]:
                        self._fill_paper_order(order, round(min(order["prc"], ltp + slip), 2))
                    elif order["trantype"] == "S" and ltp >= order["prc"]:
                        self._fill_paper_order(order, round(max(order["prc"], ltp - slip), 2))
