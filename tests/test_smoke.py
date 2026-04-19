"""Smoke test so the unified runner has something to execute.

Intentionally trivial — just proves the pytest wiring works end-to-end.
"""


def test_smoke_arithmetic() -> None:
    assert 2 + 2 == 4


def test_smoke_string() -> None:
    assert "qoder".upper() == "QODER"
