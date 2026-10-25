"""Resilient access to enterprise systems.

Every backend call goes through a SystemClient, which adds per-system timeouts,
retries with jittered backoff (reads always; writes only with an idempotency key),
a circuit breaker, a short-lived per-conversation read cache and tracing.
Use cases never talk HTTP directly, so a new workflow inherits all of this.
"""

import asyncio
import random
import time
from contextvars import ContextVar
from typing import Any

import httpx
import yaml

from .telemetry import record_span, trace_now_ms


class BackendError(Exception):
    def __init__(self, system: str, display_name: str, message: str, status: int | None = None):
        super().__init__(message)
        self.system = system
        self.display_name = display_name
        self.status = status


class SystemUnavailable(BackendError):
    """Timeouts, 5xx after retries, or an open circuit."""


class NotFound(BackendError):
    pass


class Rejected(BackendError):
    """The system understood the request and refused it (4xx)."""


class CircuitBreaker:
    def __init__(self, failures: int = 5, reset_s: float = 20):
        self.threshold = failures
        self.reset_s = reset_s
        self.consecutive_failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "open" if time.monotonic() - self.opened_at < self.reset_s else "half-open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self):
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.opened_at = time.monotonic()


# Read cache scoped to the current conversation; set by the orchestrator.
session_cache: ContextVar[dict | None] = ContextVar("session_cache", default=None)


class SystemClient:
    def __init__(self, name: str, config: dict, http: httpx.AsyncClient, base_url: str):
        client_cfg = config.get("client", {})
        self.name = name
        self.display_name = config.get("display_name", name)
        self.base_url = base_url.rstrip("/")
        self.http = http
        self.timeout_s = client_cfg.get("timeout_s", 2.0)
        self.retries = client_cfg.get("retries", 2)
        self.cache_ttl_s = client_cfg.get("cache_ttl_s", 0)
        self.breaker = CircuitBreaker(**client_cfg.get("breaker", {}))

    async def get(self, path: str, params: dict | None = None, cache: bool = True) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = (self.name, path, tuple(sorted(params.items())))
        store = session_cache.get() if cache and self.cache_ttl_s else None
        if store is not None and key in store and store[key][0] > time.monotonic():
            record_span("backend", f"{self.name} GET {path}", trace_now_ms(), system=self.name, cache_hit=True)
            return store[key][1]
        data = await self._request("GET", path, params=params)
        if store is not None:
            store[key] = (time.monotonic() + self.cache_ttl_s, data)
        return data

    async def post(self, path: str, json: dict, idempotency_key: str | None = None) -> Any:
        data = await self._request("POST", path, json=json, idempotency_key=idempotency_key)
        store = session_cache.get()
        if store is not None:
            store.clear()  # a write can change what any system reports for this customer
        return data

    async def _request(self, method: str, path: str, params=None, json=None, idempotency_key=None) -> Any:
        retryable = method == "GET" or idempotency_key is not None
        attempts = 1 + (self.retries if retryable else 0)
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        last_reason = "unknown error"
        for attempt in range(1, attempts + 1):
            if not self.breaker.allow():
                record_span("backend", f"{self.name} {method} {path}", trace_now_ms(), ok=False,
                            system=self.name, outcome="circuit_open")
                raise SystemUnavailable(self.name, self.display_name, f"{self.display_name} is temporarily unavailable")
            start = trace_now_ms()
            try:
                response = await self.http.request(method, self.base_url + path, params=params, json=json,
                                                   headers=headers, timeout=self.timeout_s)
            except httpx.TimeoutException:
                last_reason = f"timed out after {self.timeout_s}s"
            except httpx.TransportError as exc:
                last_reason = f"connection error ({type(exc).__name__})"
            else:
                status = response.status_code
                if status < 400:
                    self.breaker.record_success()
                    record_span("backend", f"{self.name} {method} {path}", start, system=self.name,
                                status=status, attempt=attempt)
                    return response.json()
                if status < 500 and status != 408:
                    self.breaker.record_success()
                    record_span("backend", f"{self.name} {method} {path}", start, system=self.name,
                                status=status, attempt=attempt)
                    detail = _detail(response)
                    if status == 404:
                        raise NotFound(self.name, self.display_name, detail, status)
                    raise Rejected(self.name, self.display_name, detail, status)
                last_reason = f"HTTP {status}"
            self.breaker.record_failure()
            record_span("backend", f"{self.name} {method} {path}", start, ok=False, system=self.name,
                        outcome=last_reason, attempt=attempt)
            if attempt < attempts:
                await asyncio.sleep(min(0.1 * 2 ** (attempt - 1), 0.8) * (0.5 + random.random()))
        raise SystemUnavailable(self.name, self.display_name,
                                f"{self.display_name} did not respond ({last_reason}, {attempts} attempt(s))")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        return body.get("detail") or body.get("error") or response.text
    except ValueError:
        return response.text


class Connectors:
    """Registry of system clients, built from config/systems.yaml."""

    def __init__(self, base_url: str, config_path, transport: httpx.AsyncBaseTransport | None = None):
        config = yaml.safe_load(open(config_path).read())
        self.http = httpx.AsyncClient(limits=httpx.Limits(max_connections=500, max_keepalive_connections=100),
                                      transport=transport)
        self.systems = {name: SystemClient(name, cfg, self.http, base_url) for name, cfg in config["systems"].items()}

    def __getitem__(self, name: str) -> SystemClient:
        return self.systems[name]

    def health(self) -> dict:
        return {name: {"display_name": c.display_name, "breaker": c.breaker.state,
                       "consecutive_failures": c.breaker.consecutive_failures} for name, c in self.systems.items()}

    async def close(self):
        await self.http.aclose()
