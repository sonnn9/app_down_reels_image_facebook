"""
FB Downloader - GUI tool tai anh va reels tu Facebook.
- Anh: dung gallery-dl (bundled qua PyInstaller).
- Reels: dung yt-dlp (bundled qua PyInstaller).
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "FB Downloader"
APP_VERSION = "1.0"


# ---------- Path helpers (work both in dev and PyInstaller --onefile) ----------

def app_base_dir() -> Path:
    """Folder where the exe lives (or the project root in dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_resource(name: str) -> Path:
    """Files bundled INTO the exe via PyInstaller (--add-data)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / name
    return Path(__file__).resolve().parent.parent / name


CONFIG_DIR = app_base_dir()
DEFAULT_DOWNLOADS = CONFIG_DIR / "downloads"
DEFAULT_COOKIES = CONFIG_DIR / "cookies.txt"
DEFAULT_LINKS = CONFIG_DIR / "links.txt"
DEFAULT_REELS = CONFIG_DIR / "reels.txt"
SETTINGS_FILE = CONFIG_DIR / "fb_downloader_settings.json"
GALLERY_CONF = CONFIG_DIR / "gallery-dl.conf"
BUNDLED_GALLERY_CONF = bundled_resource("gallery-dl.conf")


# ---------- Settings ----------

DEFAULT_SETTINGS = {
    "output_dir": str(DEFAULT_DOWNLOADS),
    "cookies_path": str(DEFAULT_COOKIES),
    "download_videos_with_images": False,
    "image_links": "",
    "reels_links": "",
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    # Bootstrap links from legacy files if first run
    if not s["image_links"] and DEFAULT_LINKS.exists():
        try:
            s["image_links"] = DEFAULT_LINKS.read_text(encoding="utf-8")
        except Exception:
            pass
    if not s["reels_links"] and DEFAULT_REELS.exists():
        try:
            s["reels_links"] = DEFAULT_REELS.read_text(encoding="utf-8")
        except Exception:
            pass
    return s


def save_settings(s: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------- Link parsing ----------

def parse_links(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


# ---------- Subprocess flags (no console window on Windows) ----------

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def ensure_gallery_conf() -> Path:
    """Make sure gallery-dl.conf exists next to the exe; copy from bundle if not."""
    if GALLERY_CONF.exists():
        return GALLERY_CONF
    if BUNDLED_GALLERY_CONF.exists():
        GALLERY_CONF.write_bytes(BUNDLED_GALLERY_CONF.read_bytes())
        return GALLERY_CONF
    # Fallback: write a minimal config
    GALLERY_CONF.write_text(json.dumps({
        "extractor": {
            "base-directory": "./downloads/",
            "archive": "./downloads/.archive.sqlite3",
            "facebook": {
                "directory": ["facebook", "{username|set_id|fbid}"],
                "filename": "{id}_{num:>02}.{extension}",
                "videos": False,
                "sleep": [1, 3],
                "sleep-request": [1, 2],
            },
        },
        "downloader": {"retries": 5, "timeout": 30.0, "part": True, "progress": 2.0},
    }, indent=2), encoding="utf-8")
    return GALLERY_CONF


# ---------- Downloader worker ----------

class DownloadJob:
    def __init__(self, kind: str, links: list[str], settings: dict, log: queue.Queue):
        self.kind = kind  # "image" | "reels"
        self.links = links
        self.settings = settings
        self.log_q = log
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.ok = 0
        self.fail = 0

    def emit(self, msg: str) -> None:
        self.log_q.put(msg)

    def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def run(self) -> None:
        try:
            if self.kind == "image":
                self._run_images()
            else:
                self._run_reels()
        except Exception as e:
            self.emit(f"[ERR] {e!r}")
        finally:
            self.emit(f"[DONE] {self.ok} thanh cong, {self.fail} loi.")
            self.log_q.put(("__finished__", self.kind))

    # ---- image (gallery-dl) ----
    def _run_images(self) -> None:
        conf = ensure_gallery_conf()
        out_dir = Path(self.settings["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        cookies = Path(self.settings["cookies_path"])

        # gallery-dl is bundled as a Python module inside the exe. We invoke it via
        # `<this exe> -m gallery_dl` when frozen so PyInstaller's runtime can find it,
        # OR via the current python interpreter in dev mode.
        cmd_base = self._gallerydl_invoker()

        for i, url in enumerate(self.links, 1):
            if self._stop.is_set():
                self.emit("[STOP] Da dung theo yeu cau.")
                return
            self.emit(f"")
            self.emit(f"[{i}/{len(self.links)}] {url}")

            cmd = list(cmd_base) + ["--config", str(conf)]
            if cookies.exists():
                cmd += ["--cookies", str(cookies)]
            if self.settings.get("download_videos_with_images"):
                cmd += ["-o", "extractor.facebook.videos=true"]
            cmd += ["-d", str(out_dir), url]

            rc = self._spawn(cmd)
            if rc == 0:
                self.ok += 1
            else:
                self.fail += 1
                self.emit(f"  -> exit code {rc}")

    # ---- reels (yt-dlp) ----
    def _run_reels(self) -> None:
        out_dir = Path(self.settings["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd_base = self._ytdlp_invoker()
        cookies = Path(self.settings["cookies_path"])

        # Run all links in one yt-dlp invocation (faster, cleaner archive handling)
        # but still loop per-link so user can stop mid-way.
        for i, url in enumerate(self.links, 1):
            if self._stop.is_set():
                self.emit("[STOP] Da dung theo yeu cau.")
                return
            self.emit(f"")
            self.emit(f"[{i}/{len(self.links)}] {url}")

            cmd = list(cmd_base) + [
                "--ignore-config",
                "-P", str(out_dir),
                "-o", "%(id)s.%(ext)s",
                "--windows-filenames",
                "--no-download-archive",
            ]
            if cookies.exists():
                cmd += ["--cookies", str(cookies)]
            cmd += [url]

            rc = self._spawn(cmd)
            if rc == 0:
                self.ok += 1
            else:
                self.fail += 1
                self.emit(f"  -> exit code {rc}")

    # ---- shared spawn ----
    def _spawn(self, cmd: list[str]) -> int:
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
        except FileNotFoundError as e:
            self.emit(f"[ERR] Khong chay duoc: {e}")
            return 127

        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            if self._stop.is_set():
                self.proc.terminate()
                break
            self.emit(line.rstrip())
        rc = self.proc.wait()
        self.proc = None
        return rc

    # ---- invokers ----
    def _gallerydl_invoker(self) -> list[str]:
        # Frozen exe: re-exec self with -m gallery_dl (PyInstaller bundles the module).
        if getattr(sys, "frozen", False):
            return [sys.executable, "--__run_module__", "gallery_dl"]
        # Dev mode: use current Python.
        return [sys.executable, "-m", "gallery_dl"]

    def _ytdlp_invoker(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--__run_module__", "yt_dlp"]
        return [sys.executable, "-m", "yt_dlp"]


# ---------- GUI ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("900x680")
        self.minsize(720, 520)
        try:
            self.iconbitmap(default=str(bundled_resource("icon.ico")))
        except Exception:
            pass

        self.settings = load_settings()
        self.log_q: queue.Queue = queue.Queue()
        self.job: DownloadJob | None = None

        self._build_ui()
        self._tick_log()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI ----
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        top = ttk.Frame(self)
        top.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        self.nb = ttk.Notebook(top)
        self.nb.pack(fill="both", expand=True)

        self.tab_img = ttk.Frame(self.nb)
        self.tab_reels = ttk.Frame(self.nb)
        self.tab_settings = ttk.Frame(self.nb)
        self.nb.add(self.tab_img, text="  Anh  ")
        self.nb.add(self.tab_reels, text="  Reels / Video  ")
        self.nb.add(self.tab_settings, text="  Cai dat  ")

        self._build_tab(self.tab_img, "image",
                        "Dan link Facebook (anh/album/profile), moi link 1 dong.\nDong bat dau bang # se bi bo qua.")
        self._build_tab(self.tab_reels, "reels",
                        "Dan link Reels / Video Facebook, moi link 1 dong.\nDong bat dau bang # se bi bo qua.")
        self._build_settings_tab()

        # Shared log
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="both", expand=False, padx=10, pady=(4, 10))
        self.log_widget = tk.Text(log_frame, height=12, wrap="word",
                                  bg="#0c0c0c", fg="#e0e0e0",
                                  insertbackground="#e0e0e0", font=("Consolas", 9))
        self.log_widget.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_widget.yview)
        sb.pack(side="right", fill="y")
        self.log_widget.config(yscrollcommand=sb.set, state="disabled")

        # Status bar
        self.status_var = tk.StringVar(value="San sang.")
        ttk.Label(self, textvariable=self.status_var, anchor="w",
                  relief="sunken").pack(fill="x", side="bottom")

    def _build_tab(self, parent: ttk.Frame, kind: str, hint: str) -> None:
        ttk.Label(parent, text=hint, justify="left").pack(anchor="w", padx=10, pady=(10, 4))

        txt = tk.Text(parent, wrap="none", font=("Consolas", 10), undo=True)
        txt.pack(fill="both", expand=True, padx=10, pady=4)

        initial = self.settings["image_links"] if kind == "image" else self.settings["reels_links"]
        if initial:
            txt.insert("1.0", initial)

        if kind == "image":
            self.img_text = txt
        else:
            self.reels_text = txt

        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=10, pady=(4, 10))

        start_btn = ttk.Button(bar, text="Bat dau tai", command=lambda: self._start(kind))
        start_btn.pack(side="left")

        stop_btn = ttk.Button(bar, text="Dung", command=self._stop, state="disabled")
        stop_btn.pack(side="left", padx=(6, 0))

        ttk.Button(bar, text="Mo thu muc tai ve",
                   command=self._open_output).pack(side="left", padx=(6, 0))

        count_var = tk.StringVar(value="0 link")
        ttk.Label(bar, textvariable=count_var).pack(side="right")

        def update_count(_evt=None):
            n = len(parse_links(txt.get("1.0", "end-1c")))
            count_var.set(f"{n} link")

        txt.bind("<KeyRelease>", update_count)
        update_count()

        if kind == "image":
            self.img_start_btn = start_btn
            self.img_stop_btn = stop_btn
        else:
            self.reels_start_btn = start_btn
            self.reels_stop_btn = stop_btn

    def _build_settings_tab(self) -> None:
        f = ttk.Frame(self.tab_settings, padding=14)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="Thu muc luu file tai ve:").grid(row=0, column=0, sticky="w", pady=4)
        self.out_var = tk.StringVar(value=self.settings["output_dir"])
        ttk.Entry(f, textvariable=self.out_var, width=70).grid(row=0, column=1, padx=6, sticky="we")
        ttk.Button(f, text="Chon...", command=self._pick_output).grid(row=0, column=2)

        ttk.Label(f, text="File cookies.txt:").grid(row=1, column=0, sticky="w", pady=4)
        self.cookies_var = tk.StringVar(value=self.settings["cookies_path"])
        ttk.Entry(f, textvariable=self.cookies_var, width=70).grid(row=1, column=1, padx=6, sticky="we")
        ttk.Button(f, text="Chon...", command=self._pick_cookies).grid(row=1, column=2)

        self.videos_var = tk.BooleanVar(value=self.settings["download_videos_with_images"])
        ttk.Checkbutton(f, text="Tai ca video kem voi anh (tab Anh)",
                        variable=self.videos_var).grid(row=2, column=1, sticky="w", pady=(10, 4))

        info = ttk.LabelFrame(f, text="Huong dan nhanh", padding=8)
        info.grid(row=3, column=0, columnspan=3, sticky="we", pady=(16, 0))
        ttk.Label(info, justify="left", text=(
            "1. Cai extension 'Get cookies.txt LOCALLY' tren Chrome/Edge.\n"
            "2. Dang nhap facebook.com, click extension -> Export Netscape -> luu vao file cookies.txt o tren.\n"
            "3. Dan link vao tab Anh hoac Reels, bam 'Bat dau tai'.\n"
            "4. Anh/video luu vao thu muc 'downloads/' ben canh app."
        )).pack(anchor="w")

        f.columnconfigure(1, weight=1)

    # ---- actions ----
    def _pick_output(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get() or str(CONFIG_DIR))
        if d:
            self.out_var.set(d)

    def _pick_cookies(self):
        f = filedialog.askopenfilename(
            initialdir=str(Path(self.cookies_var.get()).parent) if self.cookies_var.get() else str(CONFIG_DIR),
            filetypes=[("Cookies files", "*.txt"), ("All files", "*.*")],
        )
        if f:
            self.cookies_var.set(f)

    def _open_output(self):
        out = Path(self.out_var.get())
        out.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(out))  # noqa
        else:
            subprocess.Popen(["xdg-open", str(out)])

    def _collect_settings(self) -> dict:
        s = dict(self.settings)
        s["output_dir"] = self.out_var.get().strip() or str(DEFAULT_DOWNLOADS)
        s["cookies_path"] = self.cookies_var.get().strip() or str(DEFAULT_COOKIES)
        s["download_videos_with_images"] = bool(self.videos_var.get())
        s["image_links"] = self.img_text.get("1.0", "end-1c")
        s["reels_links"] = self.reels_text.get("1.0", "end-1c")
        return s

    def _start(self, kind: str):
        if self.job is not None:
            messagebox.showinfo(APP_NAME, "Dang co mot tac vu chay. Dung truoc khi chay tac vu moi.")
            return

        s = self._collect_settings()
        self.settings = s
        save_settings(s)

        text = s["image_links"] if kind == "image" else s["reels_links"]
        links = parse_links(text)
        if not links:
            messagebox.showwarning(APP_NAME, "Chua co link nao.")
            return

        cookies = Path(s["cookies_path"])
        if not cookies.exists():
            ok = messagebox.askyesno(
                APP_NAME,
                f"Khong tim thay cookies.txt:\n{cookies}\n\n"
                "Facebook se chan request neu khong co cookies. Van tiep tuc?",
            )
            if not ok:
                return

        self._append_log(f"==> Bat dau tai {len(links)} link ({'anh' if kind == 'image' else 'reels'})")
        self.status_var.set(f"Dang tai... ({kind})")

        self._set_buttons_running(kind, True)

        self.job = DownloadJob(kind, links, s, self.log_q)
        self.job.thread = threading.Thread(target=self.job.run, daemon=True)
        self.job.thread.start()

    def _stop(self):
        if self.job:
            self.job.stop()
            self.status_var.set("Dang dung...")

    def _set_buttons_running(self, kind: str, running: bool):
        if kind == "image":
            self.img_start_btn.config(state="disabled" if running else "normal")
            self.img_stop_btn.config(state="normal" if running else "disabled")
            # also disable reels start while image is running
            self.reels_start_btn.config(state="disabled" if running else "normal")
        else:
            self.reels_start_btn.config(state="disabled" if running else "normal")
            self.reels_stop_btn.config(state="normal" if running else "disabled")
            self.img_start_btn.config(state="disabled" if running else "normal")

    def _append_log(self, msg: str):
        self.log_widget.config(state="normal")
        self.log_widget.insert("end", msg + "\n")
        self.log_widget.see("end")
        self.log_widget.config(state="disabled")

    def _tick_log(self):
        finished_kind = None
        try:
            while True:
                item = self.log_q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__finished__":
                    finished_kind = item[1]
                else:
                    self._append_log(str(item))
        except queue.Empty:
            pass

        if finished_kind is not None:
            self._set_buttons_running(finished_kind, False)
            self.status_var.set("San sang.")
            self.job = None

        self.after(80, self._tick_log)

    def _on_close(self):
        save_settings(self._collect_settings())
        if self.job:
            self.job.stop()
        self.destroy()


# ---------- Entry point with --__run_module__ shim ----------

def main():
    # PyInstaller shim: let the frozen exe re-exec itself to run a bundled module
    # (gallery_dl / yt_dlp) as if it were `python -m <module>`.
    if len(sys.argv) >= 3 and sys.argv[1] == "--__run_module__":
        mod = sys.argv[2]
        sys.argv = [mod] + sys.argv[3:]
        import runpy
        runpy.run_module(mod, run_name="__main__", alter_sys=True)
        return

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
