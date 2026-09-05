"""Orchestrator tests -- dispatch, crash isolation, and validation wiring."""

import threading

from core import orchestrator
from core.models import Severity


def codes(result):
    return {w.code for w in result.warnings}


def test_convert_returns_a_result():
    result = orchestrator.convert("SELECT TOP 1 * FROM t")
    assert result.ok
    assert "LIMIT" in result.sql.upper()


def test_engine_crash_is_contained(monkeypatch):
    """An engine blowing up must never take down the app."""
    from engines import sqlglot_engine

    def boom(_sql):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(sqlglot_engine, "convert", boom)
    result = orchestrator.convert("SELECT 1")
    assert not result.ok
    assert "ENGINE-CRASH" in codes(result)


def test_no_network_call_is_made(monkeypatch):
    """The app is fully offline. Any socket use is a regression."""
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("the converter must not open a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    result = orchestrator.convert("SELECT TOP 5 a FROM t WHERE b = @Id")
    assert result.ok


def test_output_is_validated_against_postgresql():
    result = orchestrator.convert("SELECT TOP 1 * FROM t")
    assert result.validation is not None
    assert result.validation.valid


def test_invalid_output_becomes_unusable():
    """sqlglot renders a DECLAREd local as $x, which PostgreSQL rejects."""
    result = orchestrator.convert("DECLARE @x NVARCHAR(MAX); SELECT @x;")
    assert not result.validation.valid
    assert any(w.code == "PG-SYNTAX" and w.severity is Severity.ERROR
               for w in result.warnings)
    assert not result.is_usable


def test_async_delivers_result_and_signals_done():
    received = []
    finished = threading.Event()

    orchestrator.convert_async(
        "SELECT TOP 1 * FROM t",
        on_result=received.append,
        on_done=finished.set,
    )
    assert finished.wait(timeout=30), "on_done never fired"
    assert len(received) == 1


def test_async_on_done_fires_even_when_the_engine_crashes(monkeypatch):
    from engines import sqlglot_engine
    monkeypatch.setattr(sqlglot_engine, "convert",
                        lambda _s: (_ for _ in ()).throw(RuntimeError("nope")))
    finished = threading.Event()
    orchestrator.convert_async("SELECT 1", on_result=lambda r: None,
                               on_done=finished.set)
    assert finished.wait(timeout=10)


def test_empty_input_fails_cleanly():
    result = orchestrator.convert("   ")
    assert not result.ok
    assert result.sql == ""
