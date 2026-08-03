"""Injectable native dialog adapter."""

from __future__ import annotations

from tkinter import filedialog, messagebox


class Dialogs:
    def ask_directory(self, *, title: str) -> str:
        return filedialog.askdirectory(title=title)

    def ask_file(self, *, title: str) -> str:
        return filedialog.askopenfilename(
            title=title,
            filetypes=(("SQLite", "*.sqlite3 *.sqlite *.db"), ("All files", "*.*")),
        )

    def confirm(self, *, title: str, message: str) -> bool:
        return bool(messagebox.askyesno(title=title, message=message))

    def info(self, *, title: str, message: str) -> None:
        messagebox.showinfo(title=title, message=message)

    def error(self, *, title: str, message: str) -> None:
        messagebox.showerror(title=title, message=message)

    def rebuild_or_exit(self, *, title: str, message: str) -> bool:
        return bool(messagebox.askyesno(title=title, message=message))


__all__ = ["Dialogs"]
