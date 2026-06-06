#!/usr/bin/env python3
"""
Local Dub LLM v1 - Hindi -> English (or any supported pair) video dubber.

Input can be EITHER:
  * a video file  -> full pipeline (split, dub, rebuild, mux), or
  * a chunk folder -> resume: keep finished dub_*.mp4, dub only the missing
                      part_*.mp4, and rebuild the video straight from the parts
                      (no original file needed).

Pipeline:
  1. Probe / load chunks.
  2. (file mode) Split into fixed-length chunks (default 300s).
  3. Dub every not-yet-done chunk in PARALLEL via the dubbing endpoint.
  4. Rebuild a full-length target audio track, anchoring each dubbed chunk at its
     EXACT original timestamp (no drift -> the outro stays in sync).
  5. Mux that audio onto the original video (file mode) or onto the video
     rebuilt from the parts (folder mode). Video stream is copied untouched.

Live progress is shown as a clean, in-place dashboard that updates only when a
chunk's status actually changes.

Usage:
    python dub_video.py "C:\\path\\to\\video.mp4"
    python dub_video.py "C:\\path\\to\\VideoDubber\\work_myvideo\\chunks"
    python dub_video.py "video.mp4" --src Hindi --target English --dir "C:\\Users\\Me\\Downloads\\Dubs"
"""
import argparse
import concurrent.futures as cf
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE / "engine"
sys.path.insert(0, str(REPO))
try:
    from dub_engine import DubEngine
except Exception as e:
    print(f"ERROR: could not import dub_engine from {REPO}: {e}")
    sys.exit(2)

import urllib.parse as _urlparse
try:
    from dub_engine import BASE_URL as _BASE_URL
except Exception:
    _BASE_URL = ""
_HOST = _urlparse.urlparse(_BASE_URL).netloc if _BASE_URL else ""
_BRAND = _HOST.split(".")[-2] if _HOST.count(".") >= 1 else ""

def _scrub(text):
    """Keep the endpoint/brand out of public run logs."""
    t = str(text)
    if _HOST:
        t = t.replace(_HOST, "[engine]")
    if _BRAND:
        t = re.sub(re.escape(_BRAND), "engine", t, flags=re.I)
    return t


# =============================================================================
#  Console styling helpers (ANSI). Degrades gracefully if unsupported.
# =============================================================================
class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[90m"
    RED = "\033[91m"; GRN = "\033[92m"; YEL = "\033[93m"
    BLU = "\033[94m"; MAG = "\033[95m"; CYN = "\033[96m"; WHT = "\033[97m"

    @classmethod
    def disable(cls):
        for k in list(vars(cls)):
            if k.isupper():
                setattr(cls, k, "")


def enable_ansi():
    if not sys.stdout.isatty() and os.environ.get("FORCE_COLOR") != "1":
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


ANSI = enable_ansi()
if not ANSI:
    C.disable()

if ANSI:
    G_FULL, G_EMPTY = "█", "░"
    G_TL, G_TR, G_BL, G_BR, G_H, G_V = "╭", "╮", "╰", "╯", "─", "│"
    G_OK, G_FAIL, G_RETRY, G_WAIT, G_ARROW = "✓", "✗", "↻", "·", "→"
    SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
else:
    G_FULL, G_EMPTY = "#", "-"
    G_TL, G_TR, G_BL, G_BR, G_H, G_V = "+", "+", "+", "+", "-", "|"
    G_OK, G_FAIL, G_RETRY, G_WAIT, G_ARROW = "OK", "X", "~", ".", "->"
    SPIN = "|/-\\"

SPIN_GAME = "⢾⢽⢻⢿⣿⡿⢯⢷" if ANSI else SPIN
NEON = [C.MAG, C.CYN, C.BLU, C.GRN, C.YEL] if ANSI else [""]


def _visible_len(s):
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def bar(pct, width=18):
    pct = max(0, min(100, int(pct)))
    n = int(round(pct / 100 * width))
    return G_FULL * n + G_EMPTY * (width - n)


def find_ffmpeg():
    ff = shutil.which("ffmpeg"); fp = shutil.which("ffprobe")
    if ff and fp:
        return ff, fp
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for cand in base.glob("yt-dlp.FFmpeg_*/**/bin/ffmpeg.exe"):
        return str(cand), str(cand).replace("ffmpeg.exe", "ffprobe.exe")
    print("ERROR: ffmpeg/ffprobe not found on PATH. Install with: winget install Gyan.FFmpeg")
    sys.exit(2)


FF, FFPROBE = find_ffmpeg()

_POLL_INTERVAL = 3   # seconds between cloud status checks (lowered by --turbo)
_GAMING = False      # neon 'gaming mode' dashboard (unlocked by 3x Enter -> --gaming)


def probe_duration(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def log(msg, icon=None, color=None):
    ts = time.strftime("%H:%M:%S")
    tag = f"{color}{icon}{C.RESET} " if (icon and ANSI) else (f"{icon} " if icon else "")
    print(f"{C.DIM}[{ts}]{C.RESET} {tag}{msg}", flush=True)


def banner(src_lang, target_lang, genre, speakers, mode):
    width = 60
    top = f"{C.CYN}{G_TL}{G_H * width}{G_TR}{C.RESET}"
    bot = f"{C.CYN}{G_BL}{G_H * width}{G_BR}{C.RESET}"

    def row(left, right=""):
        pad = max(width - _visible_len(left) - _visible_len(right) - 2, 0)
        return f"{C.CYN}{G_V}{C.RESET} {left}{' ' * pad}{right} {C.CYN}{G_V}{C.RESET}"

    print()
    print(top)
    print(row(f"{C.BOLD}{C.WHT}LOCAL DUB LLM v1{C.RESET}",
              f"{C.DIM}local engine  {G_ARROW}  voice cloning{C.RESET}"))
    print(bot)
    spk = f"{speakers} speaker" + ("s" if speakers != 1 else "")
    tag = f"{C.YEL}resume{C.RESET}" if mode == "folder" else f"{C.GRN}new run{C.RESET}"
    print(f"  {C.BOLD}{src_lang}{C.RESET} {C.DIM}{G_ARROW}{C.RESET} {C.BOLD}{target_lang}{C.RESET}"
          f"   {C.DIM}genre{C.RESET} {genre}   {C.DIM}{spk}{C.RESET}   {tag}")
    print()


def split_video(src, workdir, chunk_secs):
    chunks_dir = workdir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    total_dur = probe_duration(src)
    if total_dur <= chunk_secs:
        log("Whole video fits in one chunk - skipping split/re-encode (faster).",
            icon=G_OK, color=C.GRN)
        info = [{"i": 0, "part": Path(src), "start": 0.0, "len": total_dur,
                 "dub": chunks_dir / "dub_000.mp4"}]
        return info, total_dur
    for stale in chunks_dir.glob("part_*.mp4"):
        stale.unlink()
    log(f"Splitting into ~{chunk_secs}s chunks (stream copy, no re-encode)...")
    cmd = [FF, "-y", "-i", str(src), "-c", "copy", "-map", "0",
           "-f", "segment", "-segment_time", str(chunk_secs),
           "-reset_timestamps", "1", "part_%03d.mp4"]
    r = subprocess.run(cmd, cwd=str(chunks_dir), capture_output=True, text=True)
    parts = sorted(chunks_dir.glob("part_*.mp4"))
    if r.returncode != 0 or not parts:
        log("Stream-copy split unavailable; falling back to exact re-encode...",
            icon="!", color=C.YEL)
        for stale in chunks_dir.glob("part_*.mp4"):
            stale.unlink()
        cmd = [FF, "-y", "-i", str(src),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-c:a", "aac", "-b:a", "160k",
               "-f", "segment", "-segment_time", str(chunk_secs),
               "-reset_timestamps", "1", "part_%03d.mp4"]
        r = subprocess.run(cmd, cwd=str(chunks_dir), capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr[-1500:])
            raise RuntimeError("ffmpeg split failed")
        parts = sorted(chunks_dir.glob("part_*.mp4"))
    info, acc = [], 0.0
    for i, p in enumerate(parts):
        d = probe_duration(p)
        info.append({"i": i, "part": p, "start": acc, "len": d,
                     "dub": chunks_dir / f"dub_{i:03d}.mp4"})
        acc += d
    log(f"Split into {C.BOLD}{len(info)}{C.RESET} chunks, total {acc/60:.1f} min.",
        icon=G_OK, color=C.GRN)
    return info, acc


def load_existing_chunks(folder):
    """Folder mode: rebuild the chunk list from an existing folder. `folder` may
    be the work folder (with a chunks/ subdir) or the chunks folder itself.
    Returns (info, total_secs, chunks_dir) or (None, 0, None)."""
    folder = Path(folder.strip().strip('"'))
    if not folder.exists():
        log(f"Folder not found: {folder}", icon="!", color=C.RED)
        return None, 0.0, None
    if list(folder.glob("part_*.mp4")):
        chunks_dir = folder
    elif (folder / "chunks").is_dir() and list((folder / "chunks").glob("part_*.mp4")):
        chunks_dir = folder / "chunks"
    else:
        log(f"No part_*.mp4 files found under: {folder}", icon="!", color=C.RED)
        return None, 0.0, None
    parts = sorted(chunks_dir.glob("part_*.mp4"))
    info, acc = [], 0.0
    for i, p in enumerate(parts):
        d = probe_duration(p)
        info.append({"i": i, "part": p, "start": acc, "len": d,
                     "dub": chunks_dir / f"dub_{i:03d}.mp4"})
        acc += d
    already = sum(1 for it in info if it["dub"].exists())
    log(f"Resuming from {C.BOLD}{chunks_dir}{C.RESET}", icon=G_OK, color=C.GRN)
    log(f"Found {C.BOLD}{len(info)}{C.RESET} parts; "
        f"{C.GRN}{already} already dubbed (kept){C.RESET}, "
        f"{C.YEL}{len(info) - already} to do{C.RESET}.")
    return info, acc, chunks_dir


def dub_chunk(item, src_lang, target_lang, genre, speakers, max_tries, progress):
    part, out = item["part"], item["dub"]
    name = part.stem
    if out.exists():                       # finished chunk -> never touched
        progress[item["i"]] = "done (cached)"
        return True
    dub = DubEngine(poll_interval=_POLL_INTERVAL)
    last = None
    for attempt in range(1, max_tries + 1):
        try:
            job = dub.create_job(src_lang=src_lang, target_langs=[target_lang],
                                 job_name=name, num_speakers=speakers, genre=genre)
            jid = job["job_id"]
            dub.upload(job["upload_url"], str(part))
            dub.start(jid)

            # Smooth the cloud's noisy progress: only ever move forward and
            # ignore the transient 0%% dips the API reports between sub-stages.
            seen = {"max": 0}

            def cb(st, _i=item["i"], seen=seen):
                prog = getattr(st, "progress", 0) or 0
                step = getattr(st, "current_step_label", "") or ""
                if prog > seen["max"]:
                    seen["max"] = prog
                progress[_i] = f"{step} {seen['max']}%"

            st = dub.wait(jid, on_progress=cb, timeout=1800)
            url = getattr(st, "dubbed_video_url", None)
            if not url:
                raise RuntimeError("no dubbed_video_url")
            dub.download(url, str(out))
            progress[item["i"]] = "done"
            return True
        except Exception as e:
            last = e
            progress[item["i"]] = f"retry {attempt} ({_scrub(e)[:40]})"
            time.sleep(5)
    progress[item["i"]] = f"FAILED: {_scrub(last)}"
    return False


def _fmt_dur(secs):
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# =============================================================================
#  Live dashboard
# =============================================================================
def parse_state(v):
    s = str(v).strip()
    if s == "pending" or s == "":
        return "pending", "Waiting", 0
    if s.lower().startswith("done"):
        return "done", "Done", 100
    if s.startswith("FAILED"):
        return "failed", "Failed", 0
    if s.startswith("retry"):
        m = re.match(r"retry (\d+)", s)
        return "retry", f"Retry {m.group(1) if m else '?'}", 0
    pct, label = 0, s
    m = re.search(r"(\d+)\s*%\s*$", s)
    if m:
        pct = int(m.group(1)); label = s[:m.start()].strip()
    if not label:
        label = "Working"
    label = label.replace("Processing Video", "Processing")
    return "active", label, pct


def chunk_row(i, v, spin_ch, single=False, bw=18, lw=22, accent=None):
    kind, label, pct = parse_state(v)
    idx = "  " if single else f"{C.DIM}{i:02d}{C.RESET} "
    label = label[:lw].ljust(lw)
    if kind == "done":
        return f"{idx}{C.GRN}{G_OK}{C.RESET} {C.GRN}{bar(100, bw)}{C.RESET} {C.GRN}{label}{C.RESET} {C.DIM}100%{C.RESET}"
    if kind == "failed":
        return f"{idx}{C.RED}{G_FAIL}{C.RESET} {C.RED}{bar(0, bw)}{C.RESET} {C.RED}{label}{C.RESET}     "
    if kind == "retry":
        return f"{idx}{C.YEL}{G_RETRY}{C.RESET} {C.YEL}{bar(pct, bw)}{C.RESET} {C.YEL}{label}{C.RESET}     "
    if kind == "pending":
        return f"{idx}{C.DIM}{G_WAIT}{C.RESET} {C.DIM}{bar(0, bw)}{C.RESET} {C.DIM}{label}{C.RESET}     "
    acc = accent or C.CYN
    return f"{idx}{acc}{spin_ch}{C.RESET} {acc}{bar(pct, bw)}{C.RESET} {C.WHT}{label}{C.RESET} {C.DIM}{pct:3d}%{C.RESET}"


def render_panel(progress, total, spin_ch, elapsed, stalled, frame=0):
    done = sum(1 for v in progress.values() if str(v).lower().startswith("done"))
    failed = sum(1 for v in progress.values() if str(v).startswith("FAILED"))
    pct = int(done / total * 100) if total else 0
    accent = NEON[frame % len(NEON)] if _GAMING else None
    obar_col = accent if _GAMING else C.GRN
    title = f"{accent}TURBO{C.RESET} {C.BOLD}MODE{C.RESET}" if _GAMING else f"{C.BOLD}Overall{C.RESET}"
    lines = [f"  {title}  {obar_col}{bar(pct, 30)}{C.RESET}  "
             f"{C.BOLD}{done}{C.DIM}/{total}{C.RESET}  {C.BOLD}{pct:3d}%{C.RESET}", ""]
    if total == 1:
        lines.append(chunk_row(0, progress.get(0, "pending"), spin_ch, single=True, accent=accent))
    elif total > 8:
        rows = [chunk_row(i, progress.get(i, "pending"), spin_ch, bw=9, lw=13, accent=accent)
                for i in range(total)]
        half = (total + 1) // 2
        left, right = rows[:half], rows[half:]
        for n in range(half):
            l = left[n]
            r = right[n] if n < len(right) else ""
            pad = max(0, 34 - _visible_len(l))
            lines.append(f"{l}{' ' * pad}  {r}")
    else:
        lines.extend(chunk_row(i, progress.get(i, "pending"), spin_ch, accent=accent)
                     for i in range(total))
    lines.append("")
    foot = f"  {C.CYN}{spin_ch}{C.RESET} {C.DIM}elapsed {C.RESET}{_fmt_dur(elapsed)}"
    if failed:
        foot += f"   {C.RED}{failed} failed{C.RESET}"
    if stalled >= 30:
        foot += f"   {C.DIM}still working - no change for {_fmt_dur(stalled)}{C.RESET}"
    lines.append(foot)
    return lines, done


def build_synced_audio(info, workdir):
    log("Building timestamp-anchored audio (no drift)...")
    usable = [it for it in info if Path(it["dub"]).exists()]
    if not usable:
        raise RuntimeError("no chunks were dubbed successfully - nothing to build")
    if len(usable) < len(info):
        log(f"NOTE: {len(info) - len(usable)} chunk(s) missing; those spans will be "
            f"silent. Re-run later to fill them (finished chunks are cached).",
            icon="!", color=C.YEL)
    in_args, filt_parts = [], []
    for n, it in enumerate(usable):
        in_args += ["-i", str(it["dub"])]
        ms = int(round(it["start"] * 1000))
        filt_parts.append(f"[{n}:a]adelay={ms}|{ms}[a{n}]")
    mix_inputs = "".join(f"[a{n}]" for n in range(len(usable)))
    filt_parts.append(f"{mix_inputs}amix=inputs={len(usable)}:normalize=0:dropout_transition=0[aout]")
    filt = ";".join(filt_parts)
    full_audio = workdir / "_synced_audio.m4a"
    cmd = [FF, "-y"] + in_args + ["-filter_complex", filt, "-map", "[aout]",
                                  "-c:a", "aac", "-b:a", "192k", str(full_audio)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-1500:])
        raise RuntimeError("audio build failed")
    return full_audio


def rebuild_source_from_parts(info, workdir):
    """Folder mode: stitch the part_*.mp4 back into one video so we have
    something to mux the dubbed audio onto (no original file required)."""
    log("Rebuilding source video from parts (stream copy)...")
    listfile = workdir / "_parts_concat.txt"
    with open(listfile, "w", encoding="utf-8") as f:
        for it in info:
            f.write("file '" + Path(it["part"]).as_posix().replace("'", "'\\''") + "'\n")
    out = workdir / "_rebuilt_source.mp4"
    cmd = [FF, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
           "-c", "copy", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log("Stream-copy concat failed; re-encoding the rebuilt video...",
            icon="!", color=C.YEL)
        cmd = [FF, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-an", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr[-1500:])
            raise RuntimeError("could not rebuild source video from parts")
    return out


def mux(src, audio, out_path):
    log("Muxing target audio onto the video (video copied untouched)...")
    cmd = [FF, "-y", "-i", str(src), "-i", str(audio),
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
           "-shortest", str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-1500:])
        raise RuntimeError("mux failed")


def main():
    ap = argparse.ArgumentParser(description="Dub a video (or resume a chunk folder).")
    ap.add_argument("input", help="A video FILE, or a chunk/work FOLDER to resume.")
    ap.add_argument("--src", default="Hindi")
    ap.add_argument("--target", default="English")
    ap.add_argument("--out", "--dir", dest="out", default=None)
    ap.add_argument("--chunk", type=int, default=0,
                    help="Chunk seconds. 0 = auto (300 normal, 120 turbo).")
    ap.add_argument("--workers", type=int, default=0,
                    help="Parallel dub jobs. 0 (default) = all chunks at once, no limit.")
    ap.add_argument("--genre", default="monologue")
    ap.add_argument("--speakers", type=int, default=1)
    ap.add_argument("--tries", type=int, default=6)
    ap.add_argument("--turbo", action="store_true",
                    help="Smaller chunks + faster polling: more pieces dub at once.")
    ap.add_argument("--poll", type=int, default=0,
                    help="Status poll seconds. 0 = auto (3 normal, 2 turbo).")
    ap.add_argument("--gaming", action="store_true",
                    help="Neon turbo dashboard (also turns on turbo).")
    ap.add_argument("--pieces", type=int, default=0,
                    help="Turbo: target number of pieces to split into (default 300).")
    args = ap.parse_args()

    # Turbo: smaller pieces => more run in parallel => shorter wall-clock.
    global _POLL_INTERVAL, _GAMING
    _GAMING = args.gaming
    turbo = args.turbo or args.gaming
    if args.chunk and args.chunk > 0:
        chunk_secs = args.chunk          # explicit override
    elif turbo:
        chunk_secs = None                # sized from duration -> ~target pieces
    else:
        chunk_secs = 300
    turbo_target = args.pieces if args.pieces and args.pieces > 0 else 300
    MIN_PIECE = 10                       # don't go below this many seconds/piece
    _POLL_INTERVAL = args.poll if args.poll and args.poll > 0 else (2 if turbo else 3)

    inp = Path(args.input.strip().strip('"'))
    if not inp.exists():
        print(f"{C.RED}ERROR: not found:{C.RESET} {inp}")
        sys.exit(1)

    out_dir = Path(args.out) if args.out else (Path.home() / "Downloads" / "Dubs")
    out_dir.mkdir(parents=True, exist_ok=True)

    folder_mode = inp.is_dir()
    banner(args.src, args.target, args.genre, args.speakers,
           "folder" if folder_mode else "file")

    if folder_mode:
        info, total_dur, chunks_dir = load_existing_chunks(str(inp))
        if not info:
            sys.exit(1)
        workdir = chunks_dir.parent
        src_video = None                      # rebuilt later from parts
        stem = workdir.name[5:] if workdir.name.startswith("work_") else workdir.name
        log(f"Output: {C.DIM}{out_dir}{C.RESET}")
    else:
        src_video = inp
        total_dur = probe_duration(src_video)
        log(f"Source: {C.BOLD}{src_video.name}{C.RESET}  ({total_dur/60:.1f} min)")
        log(f"Output: {C.DIM}{out_dir}{C.RESET}")
        if chunk_secs is None:            # turbo: aim for ~turbo_target pieces
            chunk_secs = max(MIN_PIECE, -(-int(total_dur) // turbo_target))
            log(f"TURBO split: targeting ~{turbo_target} pieces -> {chunk_secs}s each.",
                icon='*', color=C.MAG)
        workdir = HERE / ("work_" + "".join(c for c in src_video.stem if c.isalnum())[:24])
        workdir.mkdir(parents=True, exist_ok=True)
        info, _ = split_video(src_video, workdir, chunk_secs)
        stem = src_video.stem

    total = len(info)
    workers = args.workers if args.workers and args.workers > 0 else total  # no limit
    progress = {i: ("done (cached)" if it["dub"].exists() else "pending")
                for i, it in enumerate(info)}
    todo = sum(1 for it in info if not it["dub"].exists())
    log(f"Dubbing {C.BOLD}{todo}{C.RESET} remaining chunk(s) - "
        f"all {C.BOLD}{workers}{C.RESET} in parallel, no limit.")
    if _GAMING:
        log(f"TURBO MODE ON: {total} pieces, ALL running in parallel. Neon dashboard.",
            icon="*", color=C.MAG)
    elif turbo:
        log(f"TURBO: {total} pieces, ALL running in parallel, polling every {_POLL_INTERVAL}s.",
            icon="*", color=C.MAG)
    log("The engine may sit on one step for a few minutes - that is normal, not a freeze.",
        icon=G_WAIT, color=C.DIM)
    print()

    POLL = 1
    HEARTBEAT = 15
    run_start = time.time()
    last_sig = object()
    last_change = run_start
    change_count = 0
    last_print = 0.0
    spin_i = 0
    prev_lines = 0

    def draw():
        nonlocal prev_lines, spin_i, change_count
        spinset = SPIN_GAME if _GAMING else SPIN
        spin_ch = spinset[spin_i % len(spinset)]
        spin_i += 1
        now = time.time()
        # Colour advances on REAL status changes, not every frame.
        lines, _ = render_panel(progress, total, spin_ch,
                                now - run_start, now - last_change, frame=change_count)
        out = sys.stdout
        if ANSI:
            if prev_lines:
                out.write(f"\033[{prev_lines}A")
            for ln in lines:
                out.write("\033[2K" + ln + "\n")
            prev_lines = len(lines)
            out.flush()
        else:
            done = sum(1 for v in progress.values() if str(v).lower().startswith("done"))
            out.write(f"[{time.strftime('%H:%M:%S')}] {spin_ch} {done}/{total} done "
                      f"(elapsed {_fmt_dur(now - run_start)})\n")
            out.flush()

    failed = []
    with cf.ThreadPoolExecutor(max_workers=max(workers, 1)) as ex:
        futs = {ex.submit(dub_chunk, it, args.src, args.target, args.genre,
                          args.speakers, args.tries, progress): it for it in info}
        while True:
            not_done = [f for f in futs if not f.done()]
            sig = tuple(str(progress.get(i)) for i in range(total))
            now = time.time()
            changed = sig != last_sig
            if changed:
                last_change = now
                last_sig = sig
                change_count += 1
            if changed or not not_done or (now - last_print) >= HEARTBEAT:
                draw()
                last_print = now
            if not not_done:
                break
            time.sleep(POLL)
        for f, it in futs.items():
            try:
                if not f.result():
                    failed.append(it["i"])
            except Exception:
                failed.append(it["i"])

    print()
    if failed:
        log(f"WARNING: chunks failed: {failed}. Output will have gaps there. "
            f"Re-run on this chunk folder to retry - finished chunks are kept.",
            icon=G_FAIL, color=C.RED)
    else:
        log("All chunks dubbed.", icon=G_OK, color=C.GRN)

    audio = build_synced_audio(info, workdir)
    if src_video is None:
        src_video = rebuild_source_from_parts(info, workdir)
        total_dur = probe_duration(src_video)

    safe = "".join(c if c.isalnum() or c in " -_.()" else "_" for c in stem)
    out_path = out_dir / f"{safe} - {args.target} DUB.mp4"
    mux(src_video, audio, out_path)

    final_dur = probe_duration(out_path)
    print()
    print(f"{C.GRN}{G_TL}{G_H * 60}{G_TR}{C.RESET}")
    log(f"{C.BOLD}{C.GRN}DONE.{C.RESET}  Saved: {out_path}", icon=G_OK, color=C.GRN)
    log(f"Duration {final_dur/60:.1f} min (source {total_dur/60:.1f} min).")
    log("Voice is the model's synthetic target-language voice.")
    print(f"{C.GRN}{G_BL}{G_H * 60}{G_BR}{C.RESET}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run on the chunk folder to resume (finished chunks kept).")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
