"""UI construction smoke tests.

Builds the real widget tree in a hidden Tk root without entering mainloop.

These catch the class of bug that unit tests miss entirely: a typo in a widget
option, a bad grid/pack mix, a renamed callback. Previously the only way to find
those was to launch the exe and look at it.
"""

import tkinter as tk

import pytest


@pytest.fixture(scope="module")
def root():
    """One Tk root for the whole module.

    Creating a fresh root per test exhausts Tk resources and fails
    intermittently with TclError -- which the skip guard then swallowed, so
    tests silently stopped running. One shared root is stable.
    """
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    r.withdraw()          # build everything, show nothing
    yield r
    r.destroy()


@pytest.fixture
def app(root):
    """A fresh App per test, torn down so widgets never leak between tests."""
    from ui.main_window import App
    instance = App(root)
    yield instance
    instance.destroy()


@pytest.fixture
def convert(app):
    """The Convert tab -- most behaviour tests target this directly."""
    return app.convert_tab


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------

def test_app_builds(convert):
    assert convert.input_pane is not None
    assert convert.output_pane is not None


def test_two_panes_input_and_output(convert):
    assert len(convert.panes.panes()) == 2


def test_insights_are_on_by_default(convert):
    assert convert.show_insights.get() is True


# --------------------------------------------------------------------------
# Behaviour that does not need mainloop
# --------------------------------------------------------------------------

def test_empty_convert_reports_and_does_not_hang(convert):
    convert.convert()
    assert "Nothing to convert" in convert.status.get()
    assert convert._in_flight is False


def test_clear_resets_everything(convert):
    convert.input_pane.editor.set("SELECT 1")
    convert.clear()
    assert convert.input_pane.editor.get().strip() == ""
    assert convert.status.get() == "Ready."


def test_render_shows_clean_paste_ready_sql(convert):
    """The SQL pane never contains analysis -- that lives in Query Insights."""
    from core.models import ConversionResult
    convert._source_sql = "SELECT TOP 1 * FROM t"
    convert._render(ConversionResult(sql="SELECT * FROM t LIMIT 1;"))
    assert convert.output_pane.editor.get().strip() == "SELECT * FROM t LIMIT 1;"


def test_insights_panel_populates_and_toggles(convert):
    from core.models import ConversionResult
    convert._source_sql = "SELECT TOP 1 * FROM t"
    convert._result = ConversionResult(sql="SELECT * FROM t LIMIT 1;")

    convert.show_insights.set(True)
    convert._refresh_insights()
    text = convert.insights.get("1.0", "end")
    assert "PostgreSQL syntax" in text
    assert "What this query does" in text

    convert.show_insights.set(False)
    convert._refresh_insights()
    assert convert.insights.get("1.0", "end").strip() == ""


def test_warnings_panel_renders_every_severity(convert):
    from core.models import ConversionResult
    result = ConversionResult(sql="SELECT 1;")
    from core.models import Severity
    result.add("L003", Severity.WARNING, "BIT is not boolean", 2)
    result.add("PG-SYNTAX", Severity.ERROR, "syntax error", 1)
    convert._result = result
    convert._refresh_warnings()
    text = convert.warnings.get("1.0", "end")
    assert "L003" in text and "PG-SYNTAX" in text


def test_readonly_pane_rejects_edits(convert):
    pane = convert.output_pane.editor
    pane.set("SELECT 1")
    event = tk.Event()
    event.state = 0
    event.keysym = "a"
    assert pane._block_edit(event) == "break"


def test_large_input_skips_highlighting(convert):
    """tag_add is too slow at scale, and this tool targets large queries."""
    from ui.sql_text import HIGHLIGHT_LIMIT_CHARS
    pane = convert.input_pane.editor
    pane.set("SELECT 1; " * (HIGHLIGHT_LIMIT_CHARS // 5))
    assert pane.text.tag_ranges("keyword") == ()


def test_small_input_is_highlighted(convert):
    pane = convert.input_pane.editor
    pane.set("SELECT a FROM t")
    assert pane.text.tag_ranges("keyword") != ()


def test_copy_and_save_are_safe_with_no_result(convert):
    convert.copy()
    assert "Nothing to copy" in convert.status.get()
    convert.save()
    assert "Nothing to save" in convert.status.get()


# --------------------------------------------------------------------------
# Scan tab
# --------------------------------------------------------------------------

def test_scan_tab_exists(app):
    assert app.scan_tab is not None
    assert app.scan_tab.tree.get_children() == ()


def test_scan_rejects_a_missing_folder(app, monkeypatch, tmp_path):
    warned = {}
    monkeypatch.setattr("ui.main_window.messagebox.showwarning",
                        lambda title, msg: warned.setdefault("msg", msg))
    app.scan_tab.folder.set(str(tmp_path / "nope"))
    app.scan_tab.run(dry_run=True)
    assert "valid project folder" in warned.get("msg", "")
    assert app.scan_tab._running is False


def test_scan_reports_when_no_cs_files(app, monkeypatch, tmp_path):
    told = {}
    monkeypatch.setattr("ui.main_window.messagebox.showinfo",
                        lambda title, msg: told.setdefault("msg", msg))
    app.scan_tab.folder.set(str(tmp_path))
    app.scan_tab.run(dry_run=True)
    assert "No .cs files" in told.get("msg", "")


def test_rewrite_asks_for_confirmation_first(app, monkeypatch, tmp_path):
    """A real run edits customer source -- it must never start unprompted."""
    (tmp_path / "Q.cs").write_text(
        'var q = @"SELECT a FROM t";', encoding="utf-8")
    asked = {}

    def refuse(title, msg):
        asked["msg"] = msg
        return False        # user clicks Cancel

    monkeypatch.setattr("ui.main_window.messagebox.askokcancel", refuse)
    app.scan_tab.folder.set(str(tmp_path))
    app.scan_tab.run(dry_run=False)

    assert "REWRITE" in asked.get("msg", "")
    assert ".bak" in asked["msg"]
    assert app.scan_tab._running is False, "cancelling must not start the run"


def test_dry_run_does_not_ask_for_confirmation(app, monkeypatch, tmp_path):
    (tmp_path / "Q.cs").write_text(
        'var q = @"SELECT a FROM t";', encoding="utf-8")
    monkeypatch.setattr("ui.main_window.messagebox.askokcancel",
                        lambda *a: pytest.fail("dry run must not prompt"))
    app.scan_tab.folder.set(str(tmp_path))
    app.scan_tab.run(dry_run=True)
