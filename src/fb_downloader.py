"""
FB Downloader - GUI tool tai anh va reels tu Facebook.
- Anh: dung gallery-dl (bundled qua PyInstaller).
- Reels: dung yt-dlp (bundled qua PyInstaller).
"""
from __future__ import annotations

import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
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
    "reels_mode": "video",  # "video" | "mp3" | "both"
    "video_quality": "h264_1080",  # key trong VIDEO_QUALITIES
    "whisper_model": "turbo",     # ten model Whisper (turbo = large-v3-turbo)
    "whisper_language": "",       # "" = tu dong nhan dien; vd "vi", "en"
    "srt_folder": "",             # thu muc chua file MP3 de tach SRT (tab rieng)
    "image_links": "",
    "reels_links": "",
}


# Dinh dang video khi tai Reels: key -> (nhan hien thi, chuoi sort -S cua yt-dlp).
# H.264 + AAC (m4a) trong mp4 phat duoc tren hau het TV ma khong can convert.
# -S chi uu tien (khong ep): Facebook Reels thuong chi co AV1/VP9 o 1080p (H.264 chi
# co ban 360p), nen voi cac lua chon TV app se tu chuyen sang H.264 sau khi tai.
# res dat truoc vcodec de khong bi tut xuong ban H.264 do phan giai thap.
VIDEO_QUALITIES = {
    "h264_1080": ("TV: H.264 1080p + M4A (mp4)", "res:1080,vcodec:h264,acodec:m4a"),
    "h264_720": ("TV: H.264 720p + M4A (mp4)", "res:720,vcodec:h264,acodec:m4a"),
    "best": ("Tot nhat (giu nguyen, co the VP9/AV1)", None),
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

FFMPEG_EXE = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"


def find_ffmpeg_dir() -> Path | None:
    """Locate a folder containing ffmpeg to hand to yt-dlp via --ffmpeg-location.

    Search order: next to the exe, an adjacent `bin/` folder, then give up and let
    yt-dlp fall back to whatever is on PATH.
    """
    for cand in (CONFIG_DIR, CONFIG_DIR / "bin"):
        if (cand / FFMPEG_EXE).exists():
            return cand
    return None


def resolve_ffmpeg(ffmpeg_dir: Path | None) -> str | None:
    if ffmpeg_dir:
        return str(ffmpeg_dir / FFMPEG_EXE)
    return shutil.which("ffmpeg")


def probe_codecs(ffmpeg: str, path: Path) -> tuple[str | None, str | None]:
    """(video codec, audio codec) cua file, doc tu output `ffmpeg -i` (khong can ffprobe)."""
    try:
        res = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None, None
    v = re.search(r"Stream #.*?Video: (\w+)", res.stderr or "")
    a = re.search(r"Stream #.*?Audio: (\w+)", res.stderr or "")
    return (v.group(1) if v else None, a.group(1) if a else None)


# Thu GPU NVIDIA truoc (nhanh), loi thi chuyen sang CPU.
H264_ENCODERS = [
    ("GPU NVENC", ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0",
                   "-pix_fmt", "yuv420p"]),
    ("CPU libx264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]),
]


YTDLP_EXE = "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"

# yt-dlp phai duoc cap nhat lien tuc (YouTube doi co che thuong xuyen). Ket qua do
# tim duoc cache lai de khong phai spawn subprocess moi lan tai.
_ytdlp_choice: tuple[list[str], str] | None = None


def _version_key(ver: str) -> tuple[int, ...]:
    """'2026.08.19' -> (2026, 8, 19) de so sanh phien ban yt-dlp."""
    out = []
    for chunk in ver.strip().split("."):
        m = re.match(r"\d+", chunk)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def _exe_version(exe: Path) -> str | None:
    try:
        res = subprocess.run(
            [str(exe), "--version"],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    return lines[-1] if res.returncode == 0 and lines else None


def _bundled_ytdlp_version() -> str | None:
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return None


def _module_ytdlp_invoker() -> list[str]:
    """Chay yt-dlp dong goi ben trong exe (hoac module cua python trong dev)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--__run_module__", "yt_dlp"]
    return [sys.executable, "-m", "yt_dlp"]


def find_ytdlp_exe() -> Path | None:
    """Tim yt-dlp.exe roi: canh app, trong bin/, roi den PATH."""
    for cand in (CONFIG_DIR / YTDLP_EXE, CONFIG_DIR / "bin" / YTDLP_EXE):
        if cand.exists():
            return cand
    found = shutil.which(YTDLP_EXE)
    return Path(found) if found else None


def resolve_ytdlp() -> tuple[list[str], str]:
    """Chon ban yt-dlp MOI hon giua file roi canh app va ban dong goi trong exe.

    Uu tien ban moi hon vi YouTube lien tuc lam hong cac ban yt-dlp cu (HTTP 403).
    Nho vay chi can chay `yt-dlp.exe -U` canh app la sua duoc, khong phai build lai.
    """
    global _ytdlp_choice
    if _ytdlp_choice is not None:
        return _ytdlp_choice

    bundled_ver = _bundled_ytdlp_version()
    exe = find_ytdlp_exe()
    exe_ver = _exe_version(exe) if exe else None

    use_exe = exe is not None and (
        bundled_ver is None
        or (exe_ver is not None and _version_key(exe_ver) >= _version_key(bundled_ver))
    )
    if use_exe:
        _ytdlp_choice = ([str(exe)], f"{exe} ({exe_ver or 'khong ro phien ban'})")
    else:
        _ytdlp_choice = (
            _module_ytdlp_invoker(),
            f"ban dong goi trong app ({bundled_ver or 'khong ro phien ban'})",
        )
    return _ytdlp_choice


# Theo thu tu uu tien cua yt-dlp: deno > node > bun.
JS_RUNTIME_NAMES = ("deno", "node", "bun")


def find_js_runtime() -> tuple[str, Path] | None:
    """Tim JS runtime cho yt-dlp (deno/node/bun): canh app, bin/, roi PATH.

    yt-dlp can JS runtime de giai challenge cua YouTube; thieu no thi mot so format
    bi bo qua va link tai co the tra ve HTTP 403.
    """
    suffix = ".exe" if sys.platform == "win32" else ""
    for name in JS_RUNTIME_NAMES:
        exe = name + suffix
        for cand in (CONFIG_DIR / exe, CONFIG_DIR / "bin" / exe):
            if cand.exists():
                return name, cand
        found = shutil.which(exe)
        if found:
            return name, Path(found)
    return None


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
        cmd_base, ytdlp_desc = resolve_ytdlp()
        cookies = Path(self.settings["cookies_path"])
        mode = self.settings.get("reels_mode", "video")
        quality = self.settings.get("video_quality", "h264_1080")
        if quality not in VIDEO_QUALITIES:
            quality = "h264_1080"
        quality_label, quality_sort = VIDEO_QUALITIES[quality]
        want_tv = mode in ("video", "both") and quality_sort is not None

        mode_label = {"video": "chi video", "mp3": "chi MP3", "both": "video + MP3"}.get(mode, mode)
        self.emit(f"[MODE] Che do tai: {mode_label}. Ten file theo title video.")
        if mode in ("video", "both"):
            self.emit(f"[FORMAT] Dinh dang video: {quality_label}")
        self.emit(f"[YT-DLP] Dung: {ytdlp_desc}")

        js_runtime = find_js_runtime()
        if js_runtime:
            js_name, js_path = js_runtime
            self.emit(f"[JS] Dung JavaScript runtime: {js_name} ({js_path})")
        else:
            self.emit("[NOTE] Khong thay JavaScript runtime (deno/node). YouTube co the thieu format "
                      "hoac bao HTTP 403. Cai Deno (https://deno.com) hoac Node roi chay lai.")

        ffmpeg_dir = find_ffmpeg_dir()
        # MP3 can ffmpeg de chuyen am thanh; dinh dang TV can ffmpeg de ghep mp4 / chuyen H.264.
        if mode in ("mp3", "both") or want_tv:
            if ffmpeg_dir:
                self.emit(f"[FFMPEG] Dung ffmpeg tai: {ffmpeg_dir}")
            else:
                self.emit("[NOTE] Khong thay ffmpeg.exe canh app -> dung ffmpeg trong PATH. "
                          "Neu loi MP3/ghep mp4, copy ffmpeg.exe (va ffprobe.exe) vao canh file exe.")
        ffmpeg = resolve_ffmpeg(ffmpeg_dir) if want_tv else None
        if want_tv and not ffmpeg:
            self.emit("[NOTE] Khong tim thay ffmpeg -> khong chuyen duoc sang H.264. "
                      "File AV1/VP9 co the khong phat duoc tren TV.")

        for i, url in enumerate(self.links, 1):
            if self._stop.is_set():
                self.emit("[STOP] Da dung theo yeu cau.")
                return
            self.emit(f"")
            self.emit(f"[{i}/{len(self.links)}] {url}")

            cmd = list(cmd_base) + [
                "--ignore-config",
                "-P", str(out_dir),
                # Ten file dua tren title video (trim de tranh vuot gioi han Windows).
                "-o", "%(title).150B [%(id)s].%(ext)s",
                "--windows-filenames",
                "--no-download-archive",
            ]
            if mode == "mp3":
                # Chi lay am thanh, chuyen sang MP3.
                cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
            elif mode == "both":
                # Tai video, dong thoi tach ra file MP3 (giu ca hai).
                cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0", "--keep-video"]
            paths_file = None
            if want_tv:
                cmd += ["-S", quality_sort, "--merge-output-format", "mp4"]
            if want_tv or mode == "both":
                # Ghi lai duong dan file video (truoc buoc tach MP3) de chuyen H.264 / don file sau.
                fd, paths_file = tempfile.mkstemp(prefix="fbdl_", suffix=".txt")
                os.close(fd)
                cmd += ["--print-to-file", "post_process:filepath", paths_file]
            if ffmpeg_dir:
                cmd += ["--ffmpeg-location", str(ffmpeg_dir)]
            if js_runtime:
                cmd += ["--js-runtimes", f"{js_runtime[0]}:{js_runtime[1]}"]
            if cookies.exists():
                cmd += ["--cookies", str(cookies)]
            cmd += [url]

            rc = self._spawn(cmd)
            if rc == 0 and paths_file:
                for video in self._read_paths(paths_file):
                    if mode == "both":
                        self._remove_format_parts(video)
                    if self._stop.is_set() or not ffmpeg:
                        continue
                    if not self._make_tv_compatible(video, ffmpeg):
                        rc = 1
            if paths_file:
                try:
                    os.remove(paths_file)
                except OSError:
                    pass
            if rc == 0:
                self.ok += 1
            else:
                self.fail += 1
                self.emit(f"  -> exit code {rc}")
                if self.fail == 1:
                    self.emit("[GOI Y] Loi kieu 'HTTP Error 403' hay 'Requested format is not available' "
                              "thuong la do yt-dlp da cu. Copy yt-dlp.exe moi nhat vao canh app "
                              "(hoac chay `yt-dlp.exe -U`) roi tai lai.")

    @staticmethod
    def _read_paths(paths_file: str) -> list[Path]:
        try:
            lines = Path(paths_file).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines:
            p = Path(ln.strip())
            if ln.strip() and p.exists() and p not in out:
                out.append(p)
        return out

    def _remove_format_parts(self, video: Path) -> None:
        """--keep-video (che do video + MP3) giu ca file mang truoc khi ghep
        ('<ten>.f<format_id>.<ext>'); xoa chung, chi giu file video da ghep."""
        prefix = video.stem + ".f"
        for f in video.parent.iterdir():
            if f != video and f.name.startswith(prefix) and re.fullmatch(r"[\w-]+\.\w+", f.name[len(prefix):]):
                try:
                    f.unlink()
                except OSError:
                    pass

    def _make_tv_compatible(self, video: Path, ffmpeg: str) -> bool:
        """Dam bao file la H.264 + AAC trong mp4; chuyen ma neu can. True neu thanh cong."""
        vcodec, acodec = probe_codecs(ffmpeg, video)
        if vcodec == "h264" and acodec in ("aac", None) and video.suffix.lower() == ".mp4":
            self.emit(f"[TV] Da la H.264 + AAC, khong can chuyen: {video.name}")
            return True

        self.emit(f"[TV] Video dang la {vcodec or '?'} / {acodec or '?'} -> chuyen sang H.264 + AAC (mp4)...")
        final = video.with_suffix(".mp4")
        tmp = video.with_name(video.stem + ".tv-tmp.mp4")
        audio_args = ["-c:a", "copy"] if acodec == "aac" else ["-c:a", "aac", "-b:a", "192k"]
        video_encoders = [("copy", ["-c:v", "copy"])] if vcodec == "h264" else H264_ENCODERS

        for enc_name, enc_args in video_encoders:
            if self._stop.is_set():
                break
            started = time.time()
            rc = self._spawn([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                "-map", "0:v:0", "-map", "0:a:0?", *enc_args,
                *audio_args, "-movflags", "+faststart", str(tmp),
            ])
            if rc == 0 and tmp.exists():
                try:
                    os.replace(tmp, final)
                    if final != video:
                        video.unlink(missing_ok=True)
                except OSError as e:
                    self.emit(f"[TV][ERR] Khong ghi de duoc file: {e}")
                    return False
                self.emit(f"[TV] Xong ({enc_name}, {time.time() - started:.0f}s): {final.name}")
                return True
            self.emit(f"[TV] {enc_name} loi (exit {rc}), thu cach khac...")

        tmp.unlink(missing_ok=True)
        self.emit(f"[TV][ERR] Khong chuyen duoc sang H.264: {video.name}")
        return False

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


# ---------- SRT extraction worker (Whisper) ----------

def pick_whisper_device() -> str:
    """Uu tien GPU (CUDA) neu co, khong thi dung CPU."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def gpu_info() -> tuple[str, float] | None:
    """Ten GPU + dung luong VRAM (GB), hoac None neu khong co CUDA."""
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        props = torch.cuda.get_device_properties(0)
        return props.name, props.total_memory / 1024 ** 3
    except Exception:
        return None


# VRAM cao nhat (GB) do duoc thuc te khi chay openai-whisper fp32 tren card 4 GB.
# Model nao vuot qua VRAM cua card se bi tran sang RAM he thong va cham han di.
WHISPER_PEAK_VRAM_GB = {
    "tiny": 0.5, "base": 0.8, "small": 1.4, "medium": 4.3,
    "turbo": 4.6, "large-v3-turbo": 4.6,
    "large": 8.5, "large-v1": 8.5, "large-v2": 8.5, "large-v3": 8.5,
}


def whisper_vram_need(model_name: str) -> float | None:
    """VRAM uoc tinh cho model Whisper, None neu khong biet ten model."""
    return WHISPER_PEAK_VRAM_GB.get(model_name.strip().lower().removesuffix(".en"))


def suggest_smaller_model(vram_gb: float) -> str | None:
    """Model lon nhat con vua VRAM cua card."""
    for name in ("turbo", "medium", "small", "base", "tiny"):
        if WHISPER_PEAK_VRAM_GB[name] <= vram_gb:
            return name
    return None


class _ProgressCapture(io.TextIOBase):
    """Bat output tqdm cua Whisper (ghi ra stderr) de doc % tien do xu ly."""

    _PCT_RE = re.compile(r"(\d{1,3})%\|")

    def __init__(self, on_percent):
        self._on_percent = on_percent
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        *lines, self._buf = re.split(r"[\r\n]", self._buf)
        for line in lines + [self._buf]:
            m = self._PCT_RE.search(line)
            if m:
                self._on_percent(min(100, int(m.group(1))))
        return len(s)

    def flush(self) -> None:
        pass


class SrtJob:
    """Tach loi thoai (.srt) tu cac file MP3/audio co san bang Whisper."""

    def __init__(self, files: list[Path], settings: dict, log: queue.Queue):
        self.files = files
        self.settings = settings
        self.log_q = log
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._model = None
        self._device = "cpu"
        self.ok = 0
        self.fail = 0
        self.skipped = 0

    def emit(self, msg: str) -> None:
        self.log_q.put(msg)

    def emit_progress(self, idx: int, total: int, filename: str, percent: int) -> None:
        self.log_q.put(("__progress__", idx, total, filename, percent))

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:
            self.emit(f"[ERR] {e!r}")
        finally:
            self.emit(f"[DONE] {self.ok} thanh cong, {self.skipped} bo qua, {self.fail} loi.")
            self.log_q.put(("__finished__", "srt"))

    def _run(self) -> None:
        total = len(self.files)
        model = self._load_model()
        if not model:
            self.fail = total
            return
        ffmpeg_dir = find_ffmpeg_dir()
        if ffmpeg_dir:
            os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")

        for i, media in enumerate(self.files, 1):
            if self._stop.is_set():
                self.emit("[STOP] Da dung theo yeu cau.")
                return
            self.emit("")
            self.emit(f"[{i}/{total}] {media.name}")
            self.emit_progress(i, total, media.name, 0)

            srt_path = media.with_suffix(".srt")
            if srt_path.exists():
                self.emit(f"  -> Da co san {srt_path.name}, bo qua.")
                self.skipped += 1
                self.emit_progress(i, total, media.name, 100)
                continue

            if self._transcribe_one(media, i, total):
                self.ok += 1
            else:
                self.fail += 1
            self.emit_progress(i, total, media.name, 100)

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            import whisper  # openai-whisper
        except Exception as e:
            self.emit(f"[WHISPER][ERR] Chua cai openai-whisper ({e!r}). "
                      "Chay: pip install -U openai-whisper")
            return None
        name = self.settings.get("whisper_model", "turbo") or "turbo"
        device = pick_whisper_device()
        device_label = "GPU (CUDA)" if device == "cuda" else "CPU"
        if device == "cuda":
            info = gpu_info()
            if info:
                gpu_name, vram = info
                self.emit(f"[WHISPER] GPU: {gpu_name} ({vram:.1f} GB VRAM)")
                need = whisper_vram_need(name)
                if need and need > vram:
                    smaller = suggest_smaller_model(vram)
                    hint = f" Doi sang model '{smaller}' se nhanh hon dang ke." if smaller else ""
                    self.emit(f"[WHISPER] Canh bao: model '{name}' can khoang {need:.1f} GB VRAM "
                              f"nhung card chi co {vram:.1f} GB -> phai tran sang RAM he thong "
                              f"va chay cham.{hint}")
        else:
            self.emit("[WHISPER] Khong thay GPU CUDA -> chay tren CPU (cham hon nhieu). "
                      "Neu may co card NVIDIA, cai torch ban CUDA: xem README.")
        self.emit(f"[WHISPER] Dang nap model '{name}' tren {device_label} "
                  "(lan dau se tai model, vui long doi)...")
        try:
            model = whisper.load_model(name, device=device)
        except Exception as e:
            if device == "cuda":
                self.emit(f"[WHISPER] Khong dung duoc GPU ({e!r}), chuyen sang CPU...")
                try:
                    model = whisper.load_model(name, device="cpu")
                    device = "cpu"
                    device_label = "CPU"
                except Exception as e2:
                    self.emit(f"[WHISPER][ERR] Khong nap duoc model '{name}': {e2!r}")
                    return None
            else:
                self.emit(f"[WHISPER][ERR] Khong nap duoc model '{name}': {e!r}")
                return None
        self.emit(f"[WHISPER] Nap model xong ({device_label}).")
        self._model = model
        self._device = device
        return model

    def _transcribe_one(self, media: Path, idx: int, total: int) -> bool:
        lang = (self.settings.get("whisper_language") or "").strip() or None
        fp16 = self._device == "cuda"

        old_stderr = sys.stderr
        sys.stderr = _ProgressCapture(lambda pct: self.emit_progress(idx, total, media.name, pct))
        try:
            result = self._model.transcribe(str(media), language=lang, fp16=fp16, verbose=False)
        except Exception as e:
            self.emit(f"[WHISPER][ERR] Loi khi tach '{media.name}': {e!r}")
            return False
        finally:
            sys.stderr = old_stderr

        try:
            from whisper.utils import get_writer
            writer = get_writer("srt", str(media.parent))
            writer(result, str(media), {
                "max_line_width": None,
                "max_line_count": None,
                "highlight_words": False,
            })
        except Exception as e:
            self.emit(f"[WHISPER][ERR] Loi khi ghi SRT cho '{media.name}': {e!r}")
            return False

        self.emit(f"  -> Xong: {media.with_suffix('.srt').name} "
                  f"(ngon ngu: {result.get('language', '?')}).")
        return True


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
        self.job: DownloadJob | SrtJob | None = None

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
        self.tab_srt = ttk.Frame(self.nb)
        self.tab_settings = ttk.Frame(self.nb)
        self.nb.add(self.tab_img, text="  Anh  ")
        self.nb.add(self.tab_reels, text="  Reels / Video  ")
        self.nb.add(self.tab_srt, text="  Tach SRT  ")
        self.nb.add(self.tab_settings, text="  Cai dat  ")

        self._build_tab(self.tab_img, "image",
                        "Dan link Facebook (anh/album/profile), moi link 1 dong.\nDong bat dau bang # se bi bo qua.")
        self._build_tab(self.tab_reels, "reels",
                        "Dan link Reels / Video Facebook, moi link 1 dong.\nDong bat dau bang # se bi bo qua.")
        self._build_srt_tab()
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

        if kind == "reels":
            mode_frame = ttk.LabelFrame(parent, text="Tai gi?")
            mode_frame.pack(fill="x", padx=10, pady=(0, 4))
            self.reels_mode_var = tk.StringVar(value=self.settings.get("reels_mode", "video"))
            ttk.Radiobutton(mode_frame, text="Chi video (mp4)",
                            variable=self.reels_mode_var, value="video").pack(side="left", padx=(8, 12), pady=4)
            ttk.Radiobutton(mode_frame, text="Chi MP3 (am thanh)",
                            variable=self.reels_mode_var, value="mp3").pack(side="left", padx=(0, 12), pady=4)
            ttk.Radiobutton(mode_frame, text="Ca video + MP3",
                            variable=self.reels_mode_var, value="both").pack(side="left", padx=(0, 12), pady=4)
            ttk.Label(mode_frame, text="(Tach loi thoai .srt: sang tab 'Tach SRT')",
                      foreground="#666").pack(side="left", padx=(12, 0))

            quality_frame = ttk.LabelFrame(parent, text="Dinh dang video")
            quality_frame.pack(fill="x", padx=10, pady=(0, 4))
            quality = self.settings.get("video_quality", "h264_1080")
            if quality not in VIDEO_QUALITIES:
                quality = "h264_1080"
            self.video_quality_var = tk.StringVar(value=quality)
            for key, (label, _args) in VIDEO_QUALITIES.items():
                ttk.Radiobutton(quality_frame, text=label, variable=self.video_quality_var,
                                value=key).pack(side="left", padx=(8, 12), pady=4)

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

    def _build_srt_tab(self) -> None:
        parent = self.tab_srt
        self._srt_files: list[Path] = []

        ttk.Label(parent, justify="left", text=(
            "Chon thu muc chua cac file MP3 da tai (o tab Reels/Video), "
            "roi chon cac file can tach loi thoai ra .srt bang Whisper."
        )).pack(anchor="w", padx=10, pady=(10, 4))

        folder_frame = ttk.Frame(parent)
        folder_frame.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(folder_frame, text="Thu muc MP3:").pack(side="left")
        self.srt_folder_var = tk.StringVar(value=self.settings.get("srt_folder", ""))
        ttk.Entry(folder_frame, textvariable=self.srt_folder_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(folder_frame, text="Chon thu muc...",
                   command=self._pick_srt_folder).pack(side="left")

        list_frame = ttk.Frame(parent)
        list_frame.pack(fill="both", expand=True, padx=10, pady=4)
        self.srt_listbox = tk.Listbox(list_frame, selectmode="extended", font=("Consolas", 10))
        self.srt_listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.srt_listbox.yview)
        sb.pack(side="right", fill="y")
        self.srt_listbox.config(yscrollcommand=sb.set)

        sel_bar = ttk.Frame(parent)
        sel_bar.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Button(sel_bar, text="Chon tat ca", command=self._srt_select_all).pack(side="left")
        ttk.Button(sel_bar, text="Bo chon tat ca", command=self._srt_select_none).pack(
            side="left", padx=(6, 0))
        self.srt_count_var = tk.StringVar(value="0 file")
        ttk.Label(sel_bar, textvariable=self.srt_count_var).pack(side="right")

        opt_frame = ttk.LabelFrame(parent, text="Whisper")
        opt_frame.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(opt_frame, text="Model:").pack(side="left", padx=(8, 2))
        self.whisper_model_var = tk.StringVar(value=self.settings.get("whisper_model", "turbo"))
        ttk.Combobox(opt_frame, textvariable=self.whisper_model_var, width=12, state="readonly",
                     values=["tiny", "base", "small", "medium", "large-v3", "turbo"]
                     ).pack(side="left", padx=(0, 12))
        ttk.Label(opt_frame, text="Ngon ngu:").pack(side="left", padx=(0, 2))
        self.whisper_lang_var = tk.StringVar(value=self.settings.get("whisper_language", ""))
        ttk.Combobox(opt_frame, textvariable=self.whisper_lang_var, width=14, state="readonly",
                     values=["", "vi", "en", "ja", "ko", "zh", "fr", "es"]
                     ).pack(side="left", padx=(0, 6))
        ttk.Label(opt_frame, text="(de trong = tu dong nhan dien)").pack(side="left")

        progress_frame = ttk.Frame(parent)
        progress_frame.pack(fill="x", padx=10, pady=(0, 4))
        self.srt_progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.srt_progress.pack(fill="x", side="top")
        self.srt_progress_var = tk.StringVar(value="")
        ttk.Label(progress_frame, textvariable=self.srt_progress_var).pack(anchor="w", pady=(2, 0))

        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=10, pady=(4, 10))
        self.srt_start_btn = ttk.Button(bar, text="Bat dau tach", command=self._start_srt)
        self.srt_start_btn.pack(side="left")
        self.srt_stop_btn = ttk.Button(bar, text="Dung", command=self._stop, state="disabled")
        self.srt_stop_btn.pack(side="left", padx=(6, 0))

        if self.srt_folder_var.get():
            self._scan_srt_folder(select_all=True)

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

    def _pick_srt_folder(self):
        d = filedialog.askdirectory(
            initialdir=self.srt_folder_var.get() or self.out_var.get() or str(CONFIG_DIR))
        if d:
            self.srt_folder_var.set(d)
            self._scan_srt_folder(select_all=True)

    def _scan_srt_folder(self, select_all: bool = False):
        folder = Path(self.srt_folder_var.get())
        self.srt_listbox.delete(0, "end")
        self._srt_files = sorted(folder.glob("*.mp3")) if folder.is_dir() else []
        for f in self._srt_files:
            self.srt_listbox.insert("end", f.name)
        self.srt_count_var.set(f"{len(self._srt_files)} file")
        if select_all:
            self._srt_select_all()

    def _srt_select_all(self):
        self.srt_listbox.select_set(0, "end")

    def _srt_select_none(self):
        self.srt_listbox.select_clear(0, "end")

    def _collect_settings(self) -> dict:
        s = dict(self.settings)
        s["output_dir"] = self.out_var.get().strip() or str(DEFAULT_DOWNLOADS)
        s["cookies_path"] = self.cookies_var.get().strip() or str(DEFAULT_COOKIES)
        s["download_videos_with_images"] = bool(self.videos_var.get())
        s["reels_mode"] = self.reels_mode_var.get()
        s["video_quality"] = self.video_quality_var.get()
        s["whisper_model"] = self.whisper_model_var.get().strip() or "turbo"
        s["whisper_language"] = self.whisper_lang_var.get().strip()
        s["srt_folder"] = self.srt_folder_var.get().strip()
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

    def _start_srt(self):
        if self.job is not None:
            messagebox.showinfo(APP_NAME, "Dang co mot tac vu chay. Dung truoc khi chay tac vu moi.")
            return

        s = self._collect_settings()
        self.settings = s
        save_settings(s)

        sel = self.srt_listbox.curselection()
        if not sel:
            messagebox.showwarning(APP_NAME, "Chua chon file MP3 nao (bam 'Chon tat ca' de chon het).")
            return
        files = [self._srt_files[i] for i in sel]

        self._append_log(f"==> Bat dau tach SRT cho {len(files)} file")
        self.status_var.set("Dang tach SRT...")
        self.srt_progress.config(value=0)
        self.srt_progress_var.set("")

        self._set_buttons_running("srt", True)

        self.job = SrtJob(files, s, self.log_q)
        self.job.thread = threading.Thread(target=self.job.run, daemon=True)
        self.job.thread.start()

    def _stop(self):
        if self.job:
            self.job.stop()
            self.status_var.set("Dang dung...")

    def _set_buttons_running(self, kind: str, running: bool):
        other_state = "disabled" if running else "normal"
        self.img_start_btn.config(state=other_state)
        self.reels_start_btn.config(state=other_state)
        self.srt_start_btn.config(state=other_state)
        self.img_stop_btn.config(state="normal" if (running and kind == "image") else "disabled")
        self.reels_stop_btn.config(state="normal" if (running and kind == "reels") else "disabled")
        self.srt_stop_btn.config(state="normal" if (running and kind == "srt") else "disabled")

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
                if isinstance(item, tuple) and item:
                    if item[0] == "__finished__":
                        finished_kind = item[1]
                    elif item[0] == "__progress__":
                        _, idx, total, filename, percent = item
                        overall = ((idx - 1) * 100 + percent) / total if total else 0
                        self.srt_progress.config(value=overall)
                        self.srt_progress_var.set(f"File {idx}/{total}: {filename} - {percent}%")
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
