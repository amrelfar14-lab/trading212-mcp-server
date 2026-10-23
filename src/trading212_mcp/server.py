"""Server factory and CLI. Importing never opens an account client."""

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, InputRequiredResult
from pydantic import AnyUrl, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, prompts, resources, tools
from .cache import META_KEY, observations
from .client import Trading212Client
from .errors import Trading212Error
from .settings import Settings


class ReadOnlyTrading212Client(Trading212Client):
    """Second safety layer: refuse any non-GET call to Trading 212, even if a
    write tool were somehow still registered."""

    def _make_request(self, method: str, url: str, **kwargs: Any) -> Any:
        if method.upper() != "GET":
            raise Trading212Error("This server is read-only; account changes are disabled.")
        return super()._make_request(method, url, **kwargs)


async def _write_tool_names(server: MCPServer[Any]) -> list[str]:
    return [
        tool.name
        for tool in await server.list_tools()
        if tool.annotations is None or tool.annotations.read_only_hint is not True
    ]


def remove_write_tools(server: MCPServer[Any]) -> list[str]:
    """Unregister every tool not annotated read-only (orders, pies, exports)."""
    names = asyncio.run(_write_tool_names(server))
    for name in names:
        server.remove_tool(name)
    return names


class TradingServer(MCPServer[Trading212Client]):
    """Attach per-call freshness metadata after SDK serialization."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Trading212Client, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        events: list[dict[str, object]] = []
        token = observations.set(events)
        try:
            result = await super().call_tool(name, arguments, context)
            if isinstance(result, CallToolResult) and events:
                result.meta = {**(result.meta or {}), META_KEY: events}
            return result
        finally:
            observations.reset(token)

    async def read_resource(
        self,
        uri: AnyUrl | str,
        context: Context[Trading212Client, Any] | None = None,
    ) -> Iterable[ReadResourceContents] | InputRequiredResult:
        events: list[dict[str, object]] = []
        token = observations.set(events)
        try:
            result = await super().read_resource(uri, context)
            if isinstance(result, InputRequiredResult):
                return result
            contents = list(result)
            for item in contents:
                item.meta = {**(item.meta or {}), META_KEY: events}
            return contents
        finally:
            observations.reset(token)


def create_server(
    settings: Settings | None = None,
    client_factory: Callable[[Settings], Trading212Client] = Trading212Client,
) -> TradingServer:
    active: Trading212Client | None = None
    active_lifespans = 0

    def current_client() -> Trading212Client:
        if active is None:
            raise RuntimeError("Server lifespan is not running")
        return active

    @asynccontextmanager
    async def lifespan(
        server: MCPServer[Trading212Client],
    ) -> AsyncIterator[Trading212Client]:
        nonlocal active, active_lifespans
        # SSE opens a lifespan per connection. Keep the shared client alive until
        # the last connection exits; streamable HTTP uses one application lifespan.
        if active is None:
            active = client_factory(
                settings if settings is not None else Settings.from_env()
            )
        client = active
        active_lifespans += 1
        try:
            yield client
        finally:
            active_lifespans -= 1
            if active_lifespans == 0:
                active = None
                client.close()

    server = TradingServer("Trading212", version=__version__, lifespan=lifespan)
    tools.register(server, current_client)
    resources.register(server, current_client)
    prompts.register(server, current_client)
    return server


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    for name in ("httpx", "httpcore", "hishel"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    # Forgive phone-keyboard habits: capitalised first letters, stray spaces.
    for name in ("ENVIRONMENT", "TRANSPORT", "READ_ONLY"):
        if name in os.environ:
            os.environ[name] = os.environ[name].strip().lower()
    for name in ("TRADING212_API_KEY", "TRADING212_API_SECRET", "MCP_SECRET"):
        if name in os.environ:
            os.environ[name] = os.environ[name].strip()
    try:
        settings = Settings.from_env()
    except ValidationError as exc:
        # Field names only; values are hidden (hide_input_in_errors=True).
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors()
        )
        raise SystemExit(
            "Invalid configuration (" + problems + "). Expected secrets: "
            "TRADING212_API_KEY, TRADING212_API_SECRET, "
            "ENVIRONMENT=live|demo, TRANSPORT=streamable-http, MCP_SECRET."
        ) from None
    mcp_secret = os.getenv("MCP_SECRET", "").strip()
    read_only = bool(mcp_secret) or os.getenv("READ_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    server = create_server(
        settings,
        client_factory=ReadOnlyTrading212Client if read_only else Trading212Client,
    )
    if read_only:
        removed = remove_write_tools(server)
        logging.getLogger(__name__).warning(
            "Read-only mode: removed %d write tools (%s)",
            len(removed),
            ", ".join(removed),
        )

    if mcp_secret:
        # Hosted mode (e.g. Fly.io): listen on all interfaces, and require the
        # secret in the URL path. Always read-only.
        if settings.transport != "streamable-http":
            raise SystemExit("MCP_SECRET requires TRANSPORT=streamable-http.")
        if len(mcp_secret) < 24 or not re.fullmatch(r"[A-Za-z0-9_-]+", mcp_secret):
            raise SystemExit(
                "MCP_SECRET must be at least 24 characters: letters, digits, - or _."
            )

        @server.custom_route("/health", methods=["GET"])
        async def health(_: Request) -> JSONResponse:
            return JSONResponse({"ok": True, "environment": settings.environment})

        port = int(os.getenv("PORT", "8080"))
        logging.getLogger(__name__).warning(
            "Hosted mode on port %d; connector URL is https://<your-host>/<MCP_SECRET>/mcp",
            port,
        )
        server.run(
            transport="streamable-http",
            host="0.0.0.0",  # noqa: S104 - deliberate in hosted mode
            port=port,
            streamable_http_path=f"/{mcp_secret}/mcp",
            stateless_http=True,
            json_response=True,
            # Requests arrive via the host's proxy with its public hostname;
            # access control is the secret path, not the Host header.
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
    elif settings.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport=settings.transport,
            host="127.0.0.1",
            port=8000,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1:8000", "localhost:8000"],
                allowed_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
            ),
        )
