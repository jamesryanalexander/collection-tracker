from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterator, Optional

import config

BASE_URL = "https://api.echomtg.com/api"
USER_AGENT = "CollectionTracker/1.0 (local desktop app)"
TOKEN_LIFETIME = dt.timedelta(hours=24)
TOKEN_SAFETY_MARGIN = dt.timedelta(minutes=5)
FAR_FUTURE = dt.datetime(2999, 1, 1, tzinfo=dt.timezone.utc)


class EchoMTGError(RuntimeError):
    pass


class EchoMTGAuthError(EchoMTGError):
    pass


@dataclass
class TokenInfo:
    token: str
    expires_at: dt.datetime

    def is_expired(self) -> bool:
        return dt.datetime.now(dt.timezone.utc) >= (self.expires_at - TOKEN_SAFETY_MARGIN)


def _request(path: str, token: Optional[str] = None, method: str = "GET",
             params: Optional[dict] = None, body: Optional[dict] = None) -> dict:
    url = f"{BASE_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"

    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            raise EchoMTGAuthError(f"EchoMTG auth failed ({exc.code}): {detail}") from exc
        raise EchoMTGError(f"EchoMTG request failed ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise EchoMTGError(f"EchoMTG request failed: {exc}") from exc


def get_permanent_api_key() -> Optional[str]:
    return config.get_permanent_api_key()


def load_cached_token() -> Optional[TokenInfo]:
    path = config.TOKEN_CACHE_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return TokenInfo(
            token=data["token"],
            expires_at=dt.datetime.fromisoformat(data["expires_at"]),
        )
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def save_cached_token(info: TokenInfo) -> None:
    config.DATA_DIR.mkdir(exist_ok=True)
    path = config.TOKEN_CACHE_PATH
    path.write_text(json.dumps({"token": info.token, "expires_at": info.expires_at.isoformat()}))
    os.chmod(path, 0o600)


def clear_cached_token() -> None:
    path = config.TOKEN_CACHE_PATH
    if path.exists():
        path.unlink()


def login(email: str, password: str) -> TokenInfo:
    result = _request("/user/auth/", method="POST", body={"email": email, "password": password})
    if result.get("status") != "success" or "token" not in result:
        raise EchoMTGAuthError(result.get("message", "Login failed."))
    info = TokenInfo(
        token=result["token"],
        expires_at=dt.datetime.now(dt.timezone.utc) + TOKEN_LIFETIME,
    )
    save_cached_token(info)
    return info


def get_active_token() -> TokenInfo:
    """
    1. permanent key present in config -> synthetic far-future TokenInfo, login flow skipped entirely
    2. else cached token file, if present and not expired
    3. else raise EchoMTGAuthError -> routes redirect to /login
    """
    permanent_key = get_permanent_api_key()
    if permanent_key:
        return TokenInfo(token=permanent_key, expires_at=FAR_FUTURE)

    cached = load_cached_token()
    if cached and not cached.is_expired():
        return cached

    raise EchoMTGAuthError("No valid EchoMTG token available; login required.")


def fetch_all_inventory(token: str, page_size: int = 250) -> Iterator[dict]:
    """Paginated GET against /inventory/view/, yielding one dict per owned copy.
    Stops once meta.current_page >= meta.total_pages."""
    start = 0
    while True:
        result = _request(
            "/inventory/view/",
            token=token,
            params={"start": start, "limit": page_size, "sort": "name", "direction": "asc"},
        )
        if result.get("status") != "success":
            raise EchoMTGError(result.get("message", "Inventory fetch failed."))

        items = result.get("items", [])
        yield from items

        meta = result.get("meta", {})
        current_page = meta.get("current_page", 1)
        total_pages = meta.get("total_pages", 1)
        if not items or current_page >= total_pages:
            break
        start += page_size
