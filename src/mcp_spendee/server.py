from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

from mcp_spendee.client import (
    ResourceId,
    SpendeeGateway,
    TransactionType,
)
from mcp_spendee.config import Settings
from mcp_spendee.diagnostics import CallTrace, configure_logging, current_trace, event, span

mcp = FastMCP(
    "Spendee",
    instructions=(
        "Read personal finance data from Spendee. This server is read-only."
    ),
)

_settings = Settings.from_env()
_gateway = SpendeeGateway(_settings)


async def _run_tool(function: Callable[..., Any], **kwargs: Any) -> Any:
    trace = CallTrace()
    token = current_trace.set(trace)
    started = time.monotonic()
    try:
        try:
            request_id = str(mcp.get_context().request_id)
        except (LookupError, ValueError):
            request_id = None
        # MCP request IDs are protocol correlation IDs, never create request_id values.
        event(
            "tool_start",
            tool=function.__name__,
            mcp_request_id=request_id,
        )

        def worker() -> Any:
            event("worker_start", queue_ms=round((time.monotonic() - started) * 1000))
            try:
                with span("worker", tool=function.__name__):
                    result = function(**kwargs)
                    if isinstance(result, dict) and result.get("status") in {
                        "preview",
                        "created",
                        "existing",
                        "moved",
                        "already_moved",
                        "created_labels_failed",
                    }:
                        event(
                            "transaction_result",
                            status=result["status"],
                            after_cancel=trace.cancelled.is_set(),
                        )
                    return result
            finally:
                event("worker_finished", after_cancel=trace.cancelled.is_set())

        try:
            result = await anyio.to_thread.run_sync(worker, abandon_on_cancel=True)
        except anyio.get_cancelled_exc_class():
            trace.cancelled.set()
            event(
                "tool_cancelled",
                elapsed_ms=round((time.monotonic() - started) * 1000),
                worker_may_be_running=True,
            )
            raise
        except Exception as exc:
            event(
                "tool_error",
                elapsed_ms=round((time.monotonic() - started) * 1000),
                error_type=type(exc).__name__,
            )
            raise
        event(
            "tool_end",
            elapsed_ms=round((time.monotonic() - started) * 1000),
            rows=len(result) if isinstance(result, list) else None,
        )
        return result
    finally:
        current_trace.reset(token)


@mcp.tool()
def spendee_status() -> dict[str, object]:
    """Check whether credentials and local Spendee settings are configured."""
    return _settings.status()


@mcp.tool()
async def list_wallets() -> list[dict[str, Any]]:
    """List Spendee wallets with Firestore-compatible IDs and currencies."""
    return await _run_tool(_gateway.list_wallets)


@mcp.tool()
async def list_labels() -> list[dict[str, str]]:
    """List modern Spendee labels from Firestore."""
    return await _run_tool(_gateway.list_labels)


@mcp.tool()
async def list_categories(
    wallet_id: ResourceId | None = None,
    category_type: TransactionType | None = None,
) -> list[dict[str, Any]]:
    """List Spendee categories, optionally filtered by wallet and expense/income type."""
    return await _run_tool(
        _gateway.list_categories,
        wallet_id=wallet_id,
        category_type=category_type,
    )


@mcp.tool()
async def list_transactions(
    wallet_id: ResourceId | None = None,
    offset: int = 0,
    limit: int | None = 100,
    include_labels: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """List transactions newest first, filtered in Firestore before fetching.

    date_from/date_to accept YYYY-MM-DD (inclusive UTC calendar dates) or
    ISO timestamps with an explicit offset (inclusive start, exclusive end).
    There is no implicit timezone for these filters. For duplicate
    prechecks use source dates with a one-day margin on each side, a wallet_id,
    include_labels=false and limit=null to fetch the complete date range.
    limit=null requires both bounds. Otherwise limit (1..1000, default 100)
    and offset apply per wallet; without wallet_id each wallet is queried.
    """
    return await _run_tool(
        _gateway.list_transactions,
        wallet_id=wallet_id,
        offset=offset,
        limit=limit,
        include_labels=include_labels,
        date_from=date_from,
        date_to=date_to,
    )


def main() -> None:
    """Run the MCP server over stdio."""
    configure_logging()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
