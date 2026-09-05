"""Main application window.

Paste T-SQL, convert, read the verdict. One job.

THREADING CONTRACT
Tkinter widgets are not thread-safe. Conversion runs on a worker thread and
returns data only; `after(0, ...)` marshals every render back to the UI thread.
A worker never touches a widget.

The app makes NO network calls -- conversion is local (sqlglot) and validation
is local (pglast). Keep it that way.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core import applog, insights, orchestrator, scan
from core.version import VERSION, title as app_title
from core.models import ConversionResult, Severity
from ui.sql_text import SqlText

TITLE = app_title()

_SEVERITY_STYLE = {
    Severity.ERROR: ("⛔", "#B00020"),
    Severity.WARNING: ("⚠", "#B36B00"),
    Severity.INFO: ("ℹ", "#00568F"),
}


class App(ttk.Frame):
    """Hosts the two modes: single-query Convert, and whole-project Scan."""

    def __init__(self, root: tk.Tk) -> None:
        super().__init__(root, padding=6)
        self.root = root
        self.grid(row=0, column=0, sticky="nsew")
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        notebook = ttk.Notebook(self)
        notebook.grid(row=0, column=0, sticky="nsew")
        self.convert_tab = ConvertTab(notebook)
        self.scan_tab = ScanTab(notebook)
        notebook.add(self.convert_tab, text="  Convert a query  ")
        notebook.add(self.scan_tab, text="  Scan C# project  ")

        root.bind("<Control-Return>", lambda _e: self.convert_tab.convert())

    # convenience for tests and launch()
    @property
    def status(self):
        return self.convert_tab.status


class ConvertTab(ttk.Frame):
    def __init__(self, parent) -> None:
        super().__init__(parent, padding=6)

        self.show_insights = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Ready.")
        self._result: ConversionResult | None = None
        self._source_sql = ""
        self._source_name = "(pasted)"
        self._in_flight = False

        self._build_toolbar()
        self._build_panes()
        self._build_warnings()
        self._build_insights()
        ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken",
                  padding=(6, 2)).grid(row=4, column=0, sticky="ew", pady=(6, 0))

        self.rowconfigure(1, weight=3)
        self.rowconfigure(2, weight=1)
        self.rowconfigure(3, weight=1)
        self.columnconfigure(0, weight=1)

    # -- construction ------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.convert_btn = ttk.Button(bar, text="Convert  (Ctrl+Enter)",
                                      command=self.convert)
        self.convert_btn.pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left", padx=4)
        ttk.Button(bar, text="Open .sql…",
                   command=self.open_file).pack(side="left", padx=4)
        ttk.Checkbutton(bar, text="Show Query Insights",
                        variable=self.show_insights,
                        command=self._refresh_insights
                        ).pack(side="left", padx=(16, 0))
        ttk.Button(bar, text="Open log folder",
                   command=self.open_log_folder).pack(side="right")
        ttk.Label(bar, text=f"v{VERSION}",
                  foreground="#666").pack(side="right", padx=(0, 10))

    def _build_panes(self) -> None:
        self.panes = ttk.PanedWindow(self, orient="horizontal")
        self.panes.grid(row=1, column=0, sticky="nsew")
        self.input_pane = self._titled_pane("MS-SQL (input)", readonly=False)
        self.output_pane = self._titled_pane("PostgreSQL", readonly=True,
                                             with_actions=True)
        self.panes.add(self.input_pane, weight=1)
        self.panes.add(self.output_pane, weight=1)

    def _titled_pane(self, title: str, *, readonly: bool,
                     with_actions: bool = False) -> ttk.Frame:
        frame = ttk.Frame(self.panes)
        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="ew")
        label = ttk.Label(header, text=title, font=("Segoe UI", 9, "bold"))
        label.pack(side="left")

        editor = SqlText(frame, readonly=readonly)
        editor.grid(row=1, column=0, sticky="nsew")
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        if with_actions:
            ttk.Button(header, text="Copy", command=self.copy).pack(side="right")
            ttk.Button(header, text="Save…",
                       command=self.save).pack(side="right", padx=4)
        frame.editor = editor          # type: ignore[attr-defined]
        frame.header_label = label     # type: ignore[attr-defined]
        return frame

    def _build_warnings(self) -> None:
        box = ttk.LabelFrame(self, text="Warnings", padding=4)
        box.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.warnings = tk.Text(box, height=5, wrap="word",
                                font=("Segoe UI", 9), borderwidth=0)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.warnings.yview)
        self.warnings.configure(yscrollcommand=scroll.set, state="disabled")
        self.warnings.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        for severity, (_icon, colour) in _SEVERITY_STYLE.items():
            self.warnings.tag_configure(severity.value, foreground=colour)

    def _build_insights(self) -> None:
        box = ttk.LabelFrame(self, text="Query Insights", padding=4)
        box.grid(row=3, column=0, sticky="nsew", pady=(6, 0))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.insights = tk.Text(box, height=9, wrap="word",
                                font=("Segoe UI", 9), borderwidth=0)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.insights.yview)
        self.insights.configure(yscrollcommand=scroll.set, state="disabled")
        self.insights.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.insights.tag_configure("head", font=("Segoe UI", 9, "bold"))
        self.insights.tag_configure("attention", foreground="#B00020")
        self.insights.tag_configure("valid", foreground="#0A7A34")
        self.insights.tag_configure("invalid", foreground="#B00020")

    # -- behaviour ---------------------------------------------------------

    def convert(self) -> None:
        if self._in_flight:
            return
        sql = self.input_pane.editor.get().strip()  # type: ignore[attr-defined]
        if not sql:
            self.status.set("Nothing to convert — paste some T-SQL first.")
            return

        self._in_flight = True
        self._result = None
        self._source_sql = sql
        self.convert_btn.state(["disabled"])
        self._set_text(self.warnings, [])
        self._set_text(self.insights, [])
        self.output_pane.editor.set("")  # type: ignore[attr-defined]
        self.status.set(f"Converting {len(sql):,} characters…")

        orchestrator.convert_async(
            sql,
            on_result=lambda r: self.after(0, self._render, r),
            on_done=lambda: self.after(0, self._finish),
        )

    def _render(self, result: ConversionResult) -> None:
        """UI thread only."""
        self._result = result
        self.output_pane.editor.set(result.sql)  # type: ignore[attr-defined]

        if not result.ok:
            badge = "  — FAILED"
        elif result.has_errors:
            badge = "  — DO NOT USE"
        elif result.warnings:
            badge = "  — review warnings"
        else:
            badge = ""
        self.output_pane.header_label.configure(  # type: ignore[attr-defined]
            text=f"PostgreSQL  ({result.duration_ms:,} ms){badge}")

        applog.log_conversion(self._source_sql, result, self._source_name)
        self._refresh_warnings()
        self._refresh_insights()

    def _refresh_warnings(self) -> None:
        lines: list[tuple[str, str]] = []
        if self._result and self._result.warnings:
            for warning in self._result.sorted_warnings:
                icon, _ = _SEVERITY_STYLE[warning.severity]
                lines.append((f"  {icon} {warning}\n", warning.severity.value))
        elif self._result:
            lines = [("No issues detected.\n", Severity.INFO.value)]
        self._set_text(self.warnings, lines)

    def _refresh_insights(self) -> None:
        if not self.show_insights.get() or self._result is None \
                or not self._result.sql:
            self._set_text(self.insights, [])
            return

        data = insights.analyze(self._source_sql, self._result.sql,
                                self._result.warnings, self._result.validation)
        lines: list[tuple[str, str]] = []
        if data.syntax:
            tag = "valid" if data.syntax == "VALID" else "invalid"
            lines.append((f"PostgreSQL syntax: {data.syntax}\n\n", tag))
        for title, items, tag in (
                ("What this query does", data.what, ""),
                ("What changed from T-SQL", data.changed, ""),
                ("Needs attention", data.attention, "attention")):
            if not items:
                continue
            lines.append((f"{title}\n", "head"))
            bullet = "!" if tag == "attention" else "-"
            lines += [(f"   {bullet} {i}\n", tag) for i in items]
        if data.stats:
            lines.append(("Query facts\n", "head"))
            lines += [(f"   - {k}: {v}\n", "") for k, v in data.stats.items()]
        self._set_text(self.insights, lines)

    def _finish(self) -> None:
        self._in_flight = False
        self.convert_btn.state(["!disabled"])
        if self._result is None:
            self.status.set("Done.")
            return
        mark = "✓" if self._result.is_usable else "⚠"
        verdict = "USABLE" if self._result.is_usable else "NEEDS REVIEW"
        syntax = ""
        if self._result.validation is not None:
            syntax = f"   syntax: {self._result.validation.verdict}"
        self.status.set(
            f"{mark} {verdict}   {self._result.duration_ms:,} ms{syntax}")

    def _set_text(self, widget: tk.Text, chunks) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for text, tag in chunks:
            widget.insert("end", text, tag or ())
        widget.configure(state="disabled")

    # -- commands ----------------------------------------------------------

    def clear(self) -> None:
        for pane in (self.input_pane, self.output_pane):
            pane.editor.clear()  # type: ignore[attr-defined]
        self._result = None
        self._source_sql = ""
        self._set_text(self.warnings, [])
        self._set_text(self.insights, [])
        self.status.set("Ready.")

    def open_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Open a .sql file",
            filetypes=[("SQL files", "*.sql"), ("All files", "*.*")])
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            messagebox.showerror(TITLE, f"Could not open the file:\n{exc}")
            return
        self.input_pane.editor.set(text)  # type: ignore[attr-defined]
        self._source_name = path
        applog.log_message(f"Loaded {path} ({len(text):,} chars)")
        self.status.set(f"Loaded {path}")

    def open_log_folder(self) -> None:
        folder = applog.log_dir() or applog.configure()
        if folder is None:
            messagebox.showinfo(TITLE, "No writable location for logs was found.")
            return
        import os
        os.startfile(str(folder))  # noqa: S606 - Windows-only utility

    def copy(self) -> None:
        if not self._result or not self._result.sql:
            self.status.set("Nothing to copy.")
            return
        self.clipboard_clear()
        self.clipboard_append(self._result.sql)
        note = "  (has ERRORS — review before use)" if self._result.has_errors else ""
        self.status.set(f"Copied PostgreSQL to clipboard.{note}")

    def save(self) -> None:
        if not self._result or not self._result.sql:
            self.status.set("Nothing to save.")
            return
        path = filedialog.asksaveasfilename(
            title="Save PostgreSQL", defaultextension=".sql",
            filetypes=[("SQL files", "*.sql"), ("All files", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(self._result.sql, encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(TITLE, f"Could not save the file:\n{exc}")
            return
        self.status.set(f"Saved to {path}")


# ==========================================================================
# Scan C# project
# ==========================================================================

class ScanTab(ttk.Frame):
    """Scan a C# project and rewrite MS-SQL string literals as PostgreSQL.

    This writes to the user's source, so the UI leads with Dry run and states
    plainly what a real run will do before doing it.
    """

    def __init__(self, parent) -> None:
        super().__init__(parent, padding=6)
        self.folder = tk.StringVar()
        self.recursive = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Choose your C# project folder.")
        self._running = False
        self._stop = False

        self._build_form()
        self._build_table()
        ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken",
                  padding=(6, 2)).grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)

    def _build_form(self) -> None:
        form = ttk.Frame(self)
        form.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Project folder:").grid(row=0, column=0, sticky="w",
                                                     padx=(0, 6))
        ttk.Entry(form, textvariable=self.folder).grid(row=0, column=1, sticky="ew")
        ttk.Button(form, text="Browse...",
                   command=self._pick).grid(row=0, column=2, padx=4)

        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Checkbutton(bar, text="Include subfolders",
                        variable=self.recursive).pack(side="left")
        self.dry_btn = ttk.Button(bar, text="Dry run  (nothing is written)",
                                  command=lambda: self.run(dry_run=True))
        self.dry_btn.pack(side="left", padx=(16, 4))
        self.run_btn = ttk.Button(bar, text="Rewrite files in place...",
                                  command=lambda: self.run(dry_run=False))
        self.run_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Open folder",
                   command=self._open_folder).pack(side="left", padx=4)

    def _build_table(self) -> None:
        box = ttk.Frame(self)
        box.grid(row=2, column=0, sticky="nsew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        columns = ("status", "queries", "rewritten", "attention", "detail")
        self.tree = ttk.Treeview(box, columns=columns, show="tree headings")
        self.tree.heading("#0", text="File")
        self.tree.column("#0", width=340, anchor="w")
        for name, title, width in (("status", "Status", 100),
                                   ("queries", "Queries", 70),
                                   ("rewritten", "Rewritten", 80),
                                   ("attention", "Attention", 80),
                                   ("detail", "Detail", 420)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, anchor="w")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        self.tree.tag_configure(scan.REWRITTEN, foreground="#0A7A34")
        self.tree.tag_configure(scan.SKIPPED, foreground="#B36B00")
        self.tree.tag_configure(scan.FAILED, foreground="#B00020")

    # -- behaviour ---------------------------------------------------------

    def _pick(self) -> None:
        path = filedialog.askdirectory(title="Your C# project folder")
        if not path:
            return
        self.folder.set(path)
        count = len(scan.find_cs_files(Path(path), self.recursive.get()))
        self.status.set(f"{count} .cs file(s) found. Start with a dry run.")

    def _open_folder(self) -> None:
        path = self.folder.get()
        if path and Path(path).is_dir():
            os.startfile(path)  # noqa: S606 - Windows-only utility

    def stop(self) -> None:
        self._stop = True
        self.status.set("Stopping after the current file...")

    def _confirm_rewrite(self, root: str, count: int) -> bool:
        return messagebox.askokcancel(
            TITLE,
            "This will REWRITE .cs files under:\n\n"
            f"{root}\n\n"
            f"{count} file(s) will be scanned. A .bak copy is saved beside "
            "every file that changes, and any query that does not convert "
            "cleanly is left exactly as it is.\n\n"
            "Best done on a clean git branch so you can review the diff.\n\n"
            "Continue?")

    def run(self, *, dry_run: bool) -> None:
        if self._running:
            return
        root = self.folder.get().strip()
        if not root or not Path(root).is_dir():
            messagebox.showwarning(TITLE, "Choose a valid project folder.")
            return
        files = scan.find_cs_files(Path(root), self.recursive.get())
        if not files:
            messagebox.showinfo(TITLE, "No .cs files found in that folder.")
            return
        if not dry_run and not self._confirm_rewrite(root, len(files)):
            return

        self._running = True
        self._stop = False
        for button in (self.dry_btn, self.run_btn):
            button.state(["disabled"])
        self.stop_btn.state(["!disabled"])
        self.tree.delete(*self.tree.get_children())
        self.status.set(f"Scanning {len(files)} file(s)...")

        recursive = self.recursive.get()

        def worker() -> None:
            report = scan.run(
                root, recursive=recursive, dry_run=dry_run,
                on_progress=lambda item, i, total: self.after(
                    0, self._add_row, item, i, total),
                should_stop=lambda: self._stop)
            try:
                scan.write_report(report)
            except OSError:
                pass
            applog.log_message(
                f"Scan {root}: {report.queries} queries, "
                f"{report.rewritten} rewritten, {report.needs_attention} need "
                f"attention, {report.failed} failed"
                + (" (dry run)" if dry_run else ""))
            self.after(0, self._finish, report)

        threading.Thread(target=worker, daemon=True).start()

    def _add_row(self, item, index: int, total: int) -> None:
        """UI thread only. Files with no SQL are not listed -- only signal."""
        if item.findings or item.status == scan.FAILED:
            detail = item.error
            if not detail:
                notes = [f.reason for f in item.findings if f.reason]
                detail = notes[0] if notes else ""
            try:
                shown = item.path.relative_to(Path(self.folder.get()))
            except ValueError:
                shown = item.path
            self.tree.insert(
                "", "end", text=str(shown), tags=(item.status,),
                values=(item.status, len(item.findings), item.rewritten,
                        item.needs_attention, detail))
            self.tree.yview_moveto(1.0)
        self.status.set(f"{index}/{total} scanned...")

    def _finish(self, report) -> None:
        self._running = False
        for button in (self.dry_btn, self.run_btn):
            button.state(["!disabled"])
        self.stop_btn.state(["disabled"])
        mode = "DRY RUN - nothing written" if report.dry_run else "files rewritten"
        self.status.set(
            f"{mode}.  {report.scanned} scanned, {report.queries} queries, "
            f"{report.rewritten} converted, {report.needs_attention} need "
            f"attention, {report.failed} failed "
            f"({report.duration_ms / 1000:.1f}s). scan-report.md written.")



def launch() -> None:
    folder = applog.configure()
    root = tk.Tk()
    root.title(TITLE)
    root.geometry("1400x900")
    root.minsize(900, 600)
    app = App(root)
    if folder is not None:
        app.status.set(f"Ready.   Logging to {folder}")
    root.mainloop()
