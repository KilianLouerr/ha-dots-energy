"""Authentication against the Dots Energy Auth0 tenant.

Dots does not allow the password grant for its portal client, so a login walks
the Auth0 Universal Login page the way a browser does (authorization code flow
with PKCE). A captcha or bot challenge is never solved or bypassed: the login
stops with CaptchaRequired so the user can use the refresh-token route.

Nothing secret (password, e-mail, tokens, authorization codes) is ever logged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
from yarl import URL

from .const import AUDIENCE, AUTH_BASE, AUTH_DOMAIN, CLIENT_ID, REDIRECT_URI, REQUEST_TIMEOUT, SCOPE

_LOGGER = logging.getLogger(__name__)

_TOKEN_URL = f"{AUTH_BASE}/oauth/token"
_MAX_REDIRECTS = 10
_REFRESH_MARGIN = timedelta(minutes=5)
_ACTIVE_CAPTCHA = re.compile(
    r'<(?:div|input)[^>]*(?:data-captcha-sitekey="[^"]+"|name="captcha")[^>]*>',
    re.IGNORECASE,
)


class AuthError(Exception):
    """Login or token handling failed."""


class InvalidCredentials(AuthError):
    """E-mail/password or refresh token is no longer accepted."""


class CaptchaRequired(AuthError):
    """The login page asks for a captcha; automatic login is not possible."""


class CannotConnect(AuthError):
    """Dots (Auth0) could not be reached or answered with a server error."""


@dataclass
class TokenSet:
    """Tokens returned by Auth0."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)[:64]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _default_login_session() -> aiohttp.ClientSession:
    """A short-lived session with its own cookie jar, used for one login."""
    return aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(),
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
    )


def captcha_required(html: str) -> bool:
    """Return True when the login HTML renders an active captcha widget."""
    return bool(_ACTIVE_CAPTCHA.search(html))


class DotsAuth:
    """Keep a valid access token for the Dots API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        email: str | None,
        password: str | None,
        refresh_token: str | None,
        on_tokens: Callable[[TokenSet], None],
        login_session_factory: (
            Callable[[], AbstractAsyncContextManager[aiohttp.ClientSession]] | None
        ) = None,
    ) -> None:
        self._session = session
        self._login_session_factory = login_session_factory or _default_login_session
        self._email = email
        self._password = password
        self._refresh_token = refresh_token
        self._on_tokens = on_tokens
        self._tokens: TokenSet | None = None
        self._lock = asyncio.Lock()

    @property
    def can_login(self) -> bool:
        """Whether e-mail and password are available for a fresh login."""
        return bool(self._email and self._password)

    async def async_get_access_token(self, force_refresh: bool = False) -> str:
        """Return a valid access token, refreshing or logging in when needed."""
        async with self._lock:
            tokens = self._tokens
            if (
                not force_refresh
                and tokens is not None
                and tokens.expires_at - _REFRESH_MARGIN > datetime.now(UTC)
            ):
                return tokens.access_token
            if self._refresh_token:
                try:
                    return (await self._async_refresh()).access_token
                except InvalidCredentials:
                    if not self.can_login:
                        raise
                    _LOGGER.info("Dots refresh token rejected, logging in again")
            elif not self.can_login:
                raise InvalidCredentials("No refresh token and no credentials")
            return (await self._async_login()).access_token

    async def async_refresh(self) -> TokenSet:
        """Exchange the refresh token for new tokens."""
        async with self._lock:
            return await self._async_refresh()

    async def async_login(self) -> TokenSet:
        """Log in with e-mail and password through the Universal Login page."""
        async with self._lock:
            return await self._async_login()

    def _store(self, tokens: TokenSet) -> TokenSet:
        self._tokens = tokens
        if tokens.refresh_token:
            self._refresh_token = tokens.refresh_token
        self._on_tokens(tokens)
        return tokens

    async def _async_token_request(self, body: dict[str, str]) -> dict:
        try:
            async with self._session.post(
                _TOKEN_URL,
                json=body,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = {}
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(f"Token endpoint unreachable: {type(err).__name__}") from err
        if status >= 500:
            raise CannotConnect(f"Token endpoint returned HTTP {status}")
        if status != 200:
            error = data.get("error") if isinstance(data, dict) else None
            if error in ("invalid_grant", "unauthorized_client", "access_denied"):
                raise InvalidCredentials(f"Token request rejected ({error})")
            raise AuthError(f"Token endpoint returned HTTP {status} ({error})")
        if not isinstance(data, dict) or "access_token" not in data:
            raise AuthError("Token response without access token")
        return data

    def _token_set(self, data: dict) -> TokenSet:
        expires_in = int(data.get("expires_in") or 86400)
        return TokenSet(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or self._refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        )

    async def _async_refresh(self) -> TokenSet:
        if not self._refresh_token:
            raise InvalidCredentials("No refresh token")
        data = await self._async_token_request(
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": self._refresh_token,
            }
        )
        return self._store(self._token_set(data))

    async def _async_login(self) -> TokenSet:
        if not self.can_login:
            raise InvalidCredentials("No credentials for login")
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        authorize_url = f"{AUTH_BASE}/authorize?" + urlencode(
            {
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPE,
                "audience": AUDIENCE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        try:
            async with self._login_session_factory() as login_session:
                login_url = await self._async_follow(
                    login_session, authorize_url, stop_at_login=True
                )
                async with login_session.get(login_url, allow_redirects=False) as resp:
                    if resp.status >= 500:
                        raise CannotConnect(f"Login page returned HTTP {resp.status}")
                    if resp.status != 200:
                        raise AuthError(f"Login page returned HTTP {resp.status}")
                    html = await resp.text()
                if captcha_required(html):
                    raise CaptchaRequired("Login page requires a captcha")
                login_state = parse_qs(urlparse(login_url).query).get("state", [""])[0]
                async with login_session.post(
                    login_url,
                    data={
                        "state": login_state,
                        "username": self._email,
                        "password": self._password,
                        "action": "default",
                    },
                    allow_redirects=False,
                ) as resp:
                    status = resp.status
                    location = resp.headers.get("Location")
                    body = await resp.text() if status != 302 else ""
                if status >= 500:
                    raise CannotConnect(f"Login returned HTTP {status}")
                if status in (400, 401):
                    if captcha_required(body):
                        raise CaptchaRequired("Login requires a captcha")
                    raise InvalidCredentials("Login rejected")
                if status != 302 or not location:
                    raise AuthError(f"Unexpected login response HTTP {status}")
                next_url = str(URL(login_url).join(URL(location)))
                callback = (
                    next_url
                    if next_url.startswith(REDIRECT_URI)
                    else await self._async_follow(login_session, next_url, stop_at_login=False)
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(f"Login unreachable: {type(err).__name__}") from err

        query = parse_qs(urlparse(callback).query)
        if query.get("state", [""])[0] != state:
            raise AuthError("Login state mismatch")
        if "error" in query or "code" not in query:
            raise AuthError("Login returned no authorization code")
        data = await self._async_token_request(
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": query["code"][0],
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            }
        )
        return self._store(self._token_set(data))

    async def _async_follow(
        self, session: aiohttp.ClientSession, url: str, *, stop_at_login: bool
    ) -> str:
        """Follow Auth0 redirects; return the login page URL or the callback URL."""
        current = url
        for _ in range(_MAX_REDIRECTS):
            if stop_at_login and urlparse(current).path == "/u/login":
                return current
            async with session.get(current, allow_redirects=False) as resp:
                status = resp.status
                location = resp.headers.get("Location")
            if status >= 500:
                raise CannotConnect(f"Auth0 returned HTTP {status} during login")
            if status not in (301, 302, 303, 307, 308) or not location:
                raise AuthError(f"Unexpected HTTP {status} during login redirects")
            nxt = str(URL(current).join(URL(location)))
            if nxt.startswith(REDIRECT_URI):
                if stop_at_login:
                    raise AuthError("Redirected to the portal before the login page")
                return nxt
            if urlparse(nxt).hostname != AUTH_DOMAIN:
                raise AuthError("Login redirected to an unexpected host")
            current = nxt
        raise AuthError("Too many redirects during login")
