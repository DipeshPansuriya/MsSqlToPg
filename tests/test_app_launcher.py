"""Launcher tests.

REGRESSION GUARD for a packaging-only crash: PyInstaller's --windowed build has
no console, so sys.stdin / sys.stdout / sys.stderr are all None. A bare
`sys.stdin.isatty()` raises AttributeError before the window opens. This bug is
invisible under `python app.py` and to every other test in this suite, because a
console always exists there.
"""

import sys

import app


def test_stdin_none_does_not_crash(monkeypatch):
    """Exactly the --windowed environment."""
    monkeypatch.setattr(sys, "stdin", None)
    assert app.stdin_is_piped() is False


def test_stdin_closed_does_not_crash(monkeypatch):
    class Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stdin", Closed())
    assert app.stdin_is_piped() is False


def test_stdin_without_console_does_not_crash(monkeypatch):
    class NoConsole:
        def isatty(self):
            raise OSError("no console")

    monkeypatch.setattr(sys, "stdin", NoConsole())
    assert app.stdin_is_piped() is False


def test_interactive_terminal_is_not_piped(monkeypatch):
    class Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    assert app.stdin_is_piped() is False


def test_piped_stdin_is_detected(monkeypatch):
    class Pipe:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", Pipe())
    assert app.stdin_is_piped() is True


def test_windowed_launch_reaches_the_gui(monkeypatch):
    """With stdin None and no CLI args, main() must go to the GUI, not the CLI."""
    launched = {"gui": False}

    import ui.main_window
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "argv", ["app.py"])
    monkeypatch.setattr(ui.main_window, "launch",
                        lambda: launched.__setitem__("gui", True))

    assert app.main() == 0
    assert launched["gui"], "windowed build must open the GUI"
