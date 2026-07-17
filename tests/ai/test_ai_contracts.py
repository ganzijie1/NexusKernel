from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

import nexus_kernel.ai as ai
from nexus_kernel.ai import (
    AdkSessionLease,
    AgentIdentity,
    ChatMessage,
    ChatRequest,
    SessionKey,
    ToolHandler,
)
from nexus_kernel.ai.adk_sessions import AdkSessionLease as InternalAdkSessionLease
from nexus_kernel.ai._closing import ActiveCallGate


_INVALID_IDENTIFIERS = (
    "",
    "has space",
    "non-ascii-\u4f1a",
    "control\x00character",
    "a" * 129,
)


@pytest.mark.parametrize("principal_id", _INVALID_IDENTIFIERS)
def test_agent_identity_rejects_unstable_principal_ids(principal_id: str) -> None:
    with pytest.raises(ValueError, match="stable ASCII"):
        AgentIdentity(principal_id)


@pytest.mark.parametrize("principal_id", _INVALID_IDENTIFIERS)
def test_session_key_rejects_unstable_principal_ids(principal_id: str) -> None:
    with pytest.raises(ValueError, match="stable ASCII"):
        SessionKey(principal_id, "session")


@pytest.mark.parametrize("session_id", _INVALID_IDENTIFIERS)
def test_session_contracts_reject_unstable_session_ids(session_id: str) -> None:
    with pytest.raises(ValueError, match="stable ASCII"):
        SessionKey("principal", session_id)
    with pytest.raises(ValidationError):
        ChatRequest(
            session_id=session_id,
            messages=[ChatMessage(role="user", content="hello")],
        )


def test_identity_and_session_contracts_accept_stable_ascii_ids() -> None:
    principal_id = "Tenant_7:service.worker"
    session_id = "Session-9:turn_1.2"

    assert AgentIdentity(principal_id).principal_id == principal_id
    assert SessionKey(principal_id, session_id).session_id == session_id
    assert (
        ChatRequest(
            session_id=session_id,
            messages=[ChatMessage(role="user", content="hello")],
        ).session_id
        == session_id
    )


def test_ai_package_reexports_tool_handler_and_dedicated_adk_store_types() -> None:
    async def handler(arguments: dict[str, object], identity: AgentIdentity) -> object:
        return arguments, identity

    exported_handler: ToolHandler = handler

    assert callable(exported_handler)
    assert "ToolHandler" in ai.__all__
    assert "AdkSessionLease" in ai.__all__
    assert AdkSessionLease is InternalAdkSessionLease


@pytest.mark.asyncio
async def test_adk_lease_cleanup_contract_uses_public_methods() -> None:
    key = SessionKey("principal", "session")
    lease = AdkSessionLease(
        key,
        reset_session=True,
        cleanup_keys=(),
        remaining_events=1,
    )

    await lease.reserve_cleanup()
    lease.mark_touched()
    lease.mark_cleanup_required()

    assert lease.touched is True
    assert lease.requires_cleanup is True


@pytest.mark.asyncio
async def test_active_call_release_survives_repeated_cancellation() -> None:
    gate = ActiveCallGate()
    entered = asyncio.Event()

    async def call() -> None:
        async with gate.admit():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(call())
    await entered.wait()
    await gate._condition.acquire()
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    finally:
        gate._condition.release()

    with pytest.raises(asyncio.CancelledError):
        await task
    await gate.drain(0.1)
