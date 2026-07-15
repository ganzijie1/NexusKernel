from __future__ import annotations

from scripts.security_scan import scan


def test_repository_passes_security_contract_scan() -> None:
    assert scan() == []
