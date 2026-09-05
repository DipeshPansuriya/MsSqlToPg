"""SQL text pane with lightweight regex syntax highlighting.

A regex colouriser rather than pygments: it keeps the dependency count at two and
the packaged executable small.

PERFORMANCE GUARD: Tkinter's tag_add is slow at scale, and this app is built for
large queries. Above HIGHLIGHT_LIMIT_CHARS highlighting is skipped entirely --
plain monospace text is far better than a UI that freezes for several seconds.
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk

# Measured: highlighting stays imperceptible below roughly this size.
HIGHLIGHT_LIMIT_CHARS = 60_000

FONT = ("Consolas", 10)

_KEYWORDS = r"""
SELECT|FROM|WHERE|JOIN|INNER|LEFT|RIGHT|FULL|OUTER|CROSS|LATERAL|ON|AND|OR|NOT|
IN|EXISTS|BETWEEN|LIKE|IS|NULL|AS|WITH|UNION|ALL|INTERSECT|EXCEPT|GROUP|BY|
ORDER|HAVING|LIMIT|OFFSET|TOP|DISTINCT|CASE|WHEN|THEN|ELSE|END|INSERT|INTO|
VALUES|UPDATE|SET|DELETE|CREATE|TABLE|TEMPORARY|TEMP|VIEW|INDEX|DROP|ALTER|ADD|
COLUMN|CONSTRAINT|PRIMARY|KEY|FOREIGN|REFERENCES|UNIQUE|DEFAULT|CHECK|DECLARE|
BEGIN|COMMIT|ROLLBACK|RETURN|IF|WHILE|LOOP|FUNCTION|PROCEDURE|LANGUAGE|RETURNS|
EXECUTE|EXEC|OVER|PARTITION|ROWS|RANGE|PRECEDING|FOLLOWING|CURRENT|ROW|MERGE|
MATCHED|USING|CAST|IDENTITY|GENERATED|ALWAYS|ASC|DESC|NULLS|FIRST|LAST|DO
"""
_KEYWORD_RE = re.compile(
    r"\b(" + _KEYWORDS.replace("\n", "").strip() + r")\b", re.IGNORECASE)
_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")

# Light and dark friendly enough for a utility; readable on the default ttk theme.
_COLOURS = {
    "keyword": "#0000C0",
    "string": "#A31515",
    "comment": "#008000",
    "number": "#098658",
}


class SqlText(ttk.Frame):
    """A scrollable, optionally read-only SQL editor pane."""

    def __init__(self, master, *, readonly: bool = False, **kwargs) -> None:
        super().__init__(master, **kwargs)
        self.readonly = readonly

        self.text = tk.Text(self, wrap="none", font=FONT, undo=not readonly,
                            borderwidth=1, relief="solid")
        yscroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        xscroll = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        for name, colour in _COLOURS.items():
            self.text.tag_configure(name, foreground=colour)

        if readonly:
            # Keep selection and copy working; block edits.
            self.text.bind("<Key>", self._block_edit)

        self._highlight_job: str | None = None
        if not readonly:
            self.text.bind("<<Modified>>", self._on_modified)

    # -- public API --------------------------------------------------------

    def get(self) -> str:
        return self.text.get("1.0", "end-1c")

    def set(self, value: str) -> None:
        state = self.text.cget("state")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)
        self.text.configure(state=state)
        self.highlight()

    def clear(self) -> None:
        self.set("")

    def highlight(self) -> None:
        content = self.get()
        for tag in _COLOURS:
            self.text.tag_remove(tag, "1.0", "end")

        if len(content) > HIGHLIGHT_LIMIT_CHARS:
            # Deliberately skipped -- see module docstring.
            return

        # Order matters: comments and strings win over keywords inside them.
        self._tag_matches(_KEYWORD_RE, content, "keyword")
        self._tag_matches(_NUMBER_RE, content, "number")
        self._tag_matches(_STRING_RE, content, "string")
        self._tag_matches(_COMMENT_RE, content, "comment")

    # -- internals ---------------------------------------------------------

    def _tag_matches(self, pattern: re.Pattern[str], content: str, tag: str) -> None:
        for match in pattern.finditer(content):
            start = f"1.0+{match.start()}c"
            end = f"1.0+{match.end()}c"
            if tag in ("string", "comment"):
                for other in ("keyword", "number"):
                    self.text.tag_remove(other, start, end)
            self.text.tag_add(tag, start, end)

    def _block_edit(self, event: tk.Event):
        allowed = event.state & 0x4 and event.keysym.lower() in ("c", "a")
        navigation = event.keysym in (
            "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next")
        if allowed or navigation:
            return None
        return "break"

    def _on_modified(self, _event: tk.Event) -> None:
        self.text.edit_modified(False)
        # Debounce: re-highlighting on every keystroke of a 15 KB query is wasteful.
        if self._highlight_job is not None:
            self.after_cancel(self._highlight_job)
        self._highlight_job = self.after(250, self.highlight)
