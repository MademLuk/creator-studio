"""Creator Studio helper: video link -> transcript (.srt) + 10 strongest frames.

Runs on this Mac as a local MCP server. The Creator Studio page (claude.ai
artifact) calls it through the Claude desktop app as `host:creator-studio`.

Each job runs in its own process because a 40-minute video takes minutes;
the page starts a job, polls `status`, then reads `result` and each `frame`.
State lives in jobs/<id>/status.json so the server can answer while a job runs.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR = JOBS_DIR / "uploads"  # audio files dropped in the studio, sent in chunks
UPLOADS_DIR.mkdir(exist_ok=True)
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
# mlx-whisper shells out to a binary literally named "ffmpeg"
_bin = ROOT / ".venv" / "bin"
if not (_bin / "ffmpeg").exists():
    try:
        (_bin / "ffmpeg").symlink_to(FFMPEG)
    except OSError:
        pass
os.environ["PATH"] = f"{_bin}{os.pathsep}{os.environ.get('PATH', '')}"

WHISPER_MODEL = os.environ.get("CREATOR_WHISPER", "mlx-community/whisper-large-v3-turbo")

mcp = FastMCP("creator-studio")
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
RO = ToolAnnotations(readOnlyHint=True)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------- helpers ----------

def set_job(jid: str, **kw):
    """Update a job's state and persist it, so the MCP server (another process) can read it."""
    path = JOBS_DIR / jid / "status.json"
    with LOCK:
        if jid not in JOBS:  # another process (start vs runner) may have written first: keep its fields
            try:
                JOBS[jid] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                JOBS[jid] = {"id": jid}
        JOBS[jid].update(kw)
        state = dict(JOBS[jid])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def probe(path: Path) -> dict:
    """Duration and size from ffmpeg's own header dump (no ffprobe needed)."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    err = r.stderr
    dur = 0.0
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    w = h = 0
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if m:
        w, h = int(m[1]), int(m[2])
    return {"duration": dur, "width": w, "height": h}


def download(jid: str, url: str, folder: Path) -> tuple[Path, str]:
    import yt_dlp

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                set_job(jid, progress=0.02 + 0.18 * d.get("downloaded_bytes", 0) / total)

    opts = {
        "outtmpl": str(folder / "video.%(ext)s"),
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b",
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    files = sorted(folder.glob("video.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("The download finished but no video file was found.")
    return files[0], (info or {}).get("title") or "video"


def extract_audio(src: Path, dst: Path):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                    "-vn", "-ac", "1", "-ar", "16000", str(dst)], check=True)


def words_to_cues(segments: list[dict], max_chars=84, max_dur=6.0) -> list[dict]:
    """Group word timestamps into subtitle-sized cues (2 lines x 42 chars)."""
    # Whisper words carry their own leading space ("bonjour", " à") and
    # none inside elisions ("Aujourd", "'hui"), so join them as they come.
    words = []
    for s in segments:
        for w in s.get("words") or []:
            t = w.get("word") or ""
            if t.strip():
                words.append({"s": float(w["start"]), "e": float(w["end"]), "t": t})
        if not s.get("words") and (s.get("text") or "").strip():
            words.append({"s": float(s["start"]), "e": float(s["end"]), "t": " " + s["text"].strip()})
    cues, cur = [], []
    joined = lambda ws: "".join(x["t"] for x in ws).strip()

    def flush():
        if cur:
            cues.append({"s": round(cur[0]["s"], 3), "e": round(cur[-1]["e"], 3), "t": joined(cur)})
            cur.clear()

    for w in words:
        if cur and w["t"].startswith(" "):  # only break between words, never inside one
            text_len = len(joined(cur + [w]))
            gap = w["s"] - cur[-1]["e"]
            if text_len > max_chars or w["e"] - cur[0]["s"] > max_dur or gap > 1.2:
                flush()
        cur.append(w)
        if re.search(r"[.!?…]$", w["t"].strip()) and len(joined(cur)) > 24:
            flush()
    flush()
    # keep a small gap and a minimum on-screen time
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["s"] if i + 1 < len(cues) else None
        if c["e"] - c["s"] < 1.0:
            c["e"] = c["s"] + 1.0
        if nxt is not None and c["e"] > nxt - 0.05:
            c["e"] = max(c["s"] + 0.3, nxt - 0.05)
    return cues


PIECE_SECONDS = int(os.environ.get("CREATOR_PIECE_SECONDS", "600"))


def snap_to_quiet(pcm: np.ndarray, sr: int, at: int, window: float = 1.0) -> int:
    """The quietest 100 ms within `window` seconds of sample `at`: a safe place to start the next piece."""
    hop = sr // 10
    lo, hi = max(0, at - int(window * sr)), min(len(pcm), at + int(window * sr))
    region = pcm[lo:hi].astype(np.float32)
    if len(region) < hop * 2:
        return at
    frames = region[: (len(region) // hop) * hop].reshape(-1, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    return lo + int(rms.argmin()) * hop + hop // 2


def join_pieces(before: list[dict], after: list[dict]) -> list[dict]:
    """Stitch a sentence that a piece boundary cut in two. Whisper ends a cut-off line
    with "..." (or no final punctuation) and capitalises the continuation."""
    if before and after:
        last, first = before[-1], after[0]
        a, b = last["t"].rstrip(), first["t"].lstrip()
        cut_off = a.endswith("...") or a.endswith("…") or not re.search(r"[.!?»\")]$", a)
        if cut_off and len(a) + len(b) <= 100 and first["s"] - last["e"] < 2.0:
            a = re.sub(r"(\.\.\.|…)$", "", a).rstrip()
            head = b.split(" ", 1)[0]
            if not head.isupper():  # keep acronyms; a sentence-start capital becomes lower case
                b = b[0].lower() + b[1:]
            before[-1] = {"s": last["s"], "e": first["e"], "t": f"{a} {b}"}
            return before + after[1:]
    return before + after


def tc(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_txt(cues: list[dict]) -> str:
    """Plain text without timecodes: a new paragraph after a pause or when one gets long."""
    paras, cur = [], ""
    for i, c in enumerate(cues):
        t = c["t"].strip()
        if not t:
            continue
        if cur and ((i and c["s"] - cues[i - 1]["e"] > 1.5) or len(cur) > 700):
            paras.append(cur)
            cur = ""
        cur = f"{cur} {t}" if cur else t
    if cur:
        paras.append(cur)
    return "\n\n".join(paras) + "\n"


def to_srt(cues: list[dict]) -> str:
    return "\n".join(f"{i + 1}\n{tc(c['s'])} --> {tc(c['e'])}\n{c['t']}\n" for i, c in enumerate(cues))


def grab(src: Path, t: float, w: int, h: int, gray: bool) -> np.ndarray:
    pix = "gray" if gray else "rgb24"
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(src),
                        "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", pix, "-"],
                       capture_output=True)
    n = w * h * (1 if gray else 3)
    if len(r.stdout) < n:
        return None
    a = np.frombuffer(r.stdout[:n], dtype=np.uint8)
    return a.reshape((h, w)) if gray else a.reshape((h, w, 3))


def score(g: np.ndarray) -> dict:
    g = g.astype(np.float32)
    mean, std = float(g.mean()), float(g.std())
    lap = 4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    sharp = float(lap.var())
    expo = 0.15 if mean < 30 or mean > 230 else 0.6 if mean < 55 or mean > 205 else 1.0
    sig = g[5::10, 5::10][:9, :16].flatten()
    weak = expo < 0.5 or (mean < 45 and std < 40)  # black screens, credit rolls
    return {"score": (sharp ** 0.5) * expo * (0.4 + min(std, 70) / 70), "sig": sig, "weak": weak}


def pick_frames(jid: str, src: Path, dur: float, folder: Path, n_pick=10, k=50) -> list[dict]:
    a, z = dur * 0.02, dur * 0.98
    cands = []
    for i in range(k):
        t = a + (z - a) * (i + 0.5) / k
        g = grab(src, t, 160, 90, gray=True)
        if g is not None:
            cands.append({"t": t, **score(g)})
        set_job(jid, progress=0.82 + 0.12 * (i + 1) / k)
    if not cands:
        return []
    picks: list[dict] = []

    def fresh(c):
        return (not any(c is p for p in picks) and not c["weak"]
                and all(np.abs(p["sig"] - c["sig"]).mean() > 7 for p in picks))

    pool = sorted(cands, key=lambda c: -c["score"])
    for s in range(n_pick):
        seg = sorted(cands[s * len(cands) // n_pick:(s + 1) * len(cands) // n_pick], key=lambda c: -c["score"])
        choice = next((c for c in seg if fresh(c)), None) or next((c for c in pool if fresh(c)), None) \
            or next((c for c in seg if not any(c is p for p in picks)), None)
        if choice:
            picks.append(choice)
    picks.sort(key=lambda c: c["t"])
    meta = probe(src)
    W = min(1920, meta["width"] or 1920)
    H = int(round(W * (meta["height"] or 1080) / (meta["width"] or 1920) / 2)) * 2
    out = []
    for i, p in enumerate(picks):
        rgb = grab(src, p["t"], W, H, gray=False)
        if rgb is None:
            continue
        path = folder / f"frame{i:02d}.jpg"
        Image.fromarray(rgb).save(path, quality=86)
        out.append({"index": len(out), "t": round(p["t"], 2), "score": round(p["score"], 2), "file": path.name})
        set_job(jid, progress=0.94 + 0.06 * (i + 1) / len(picks))
    return out


# ---------- the pipeline ----------

def alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def find_job(source: str):
    """A finished (or still running) job for the same source, so Start twice does not redo the work."""
    for d in sorted(JOBS_DIR.iterdir(), reverse=True):
        sj = d / "status.json"
        if not sj.exists():
            continue
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if st.get("source") != source:
            continue
        if st.get("done") and not st.get("error") and (d / "result.json").exists():
            return d.name
        if not st.get("done") and alive(st.get("pid") or st.get("runner_pid")):
            return d.name
    return None


def run(jid: str, source: str, language: str | None):
    folder = JOBS_DIR / jid
    folder.mkdir(exist_ok=True)
    set_job(jid, pid=os.getpid())
    try:
        if re.match(r"^https?://", source):
            set_job(jid, stage="Downloading", progress=0.02)
            video, title = download(jid, source, folder)
        elif source.startswith("upload:"):
            up = UPLOADS_DIR / source[len("upload:"):]
            files = [p for p in up.glob("*") if p.is_file()] if up.is_dir() else []
            if not files:
                raise RuntimeError("The uploaded file was not found. Drop it again.")
            video = files[0]
            title = video.stem
        else:
            video = Path(os.path.expanduser(source))
            if not video.exists():
                raise RuntimeError(f"No file at {video}")
            title = video.stem
        meta = probe(video)
        set_job(jid, title=title, **meta)

        set_job(jid, stage="Extracting audio", progress=0.22)
        audio = folder / "audio.wav"
        extract_audio(video, audio)

        set_job(jid, stage="Transcribing", progress=0.25)
        import mlx_whisper
        import wave
        with wave.open(str(audio), "rb") as wf:
            sr = wf.getframerate()
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        n, L, ext = len(pcm), PIECE_SECONDS * sr, 30 * sr
        pieces_est = max(1, round(n / L))
        set_job(jid, pieces=pieces_est, piece=0, lines=0)
        cues, lang, a, k = [], (language or None), 0, 0
        while a < n:
            # Each piece is transcribed with 30 s of extra audio. Only the sentences Whisper
            # finished before the planned cut are kept, and the next piece starts where the
            # last kept sentence ends, so no sentence is ever cut in two.
            a_start = a
            target = a + L
            if n - target < L * 0.2:
                target = n
            z = min(n, target + ext)
            t_piece = time.time()
            seg = pcm[a:z].astype(np.float32) / 32768.0
            if np.abs(seg).max() < 0.005:  # silence: Whisper would invent text
                a = target
            else:
                res = mlx_whisper.transcribe(seg, path_or_hf_repo=WHISPER_MODEL, word_timestamps=True,
                                             language=lang, condition_on_previous_text=False)
                if not lang:
                    lang = res.get("language")  # detected on the first spoken piece, then kept
                off = a / sr
                segs = res.get("segments") or []
                for sg in segs:
                    sg["start"] += off
                    sg["end"] += off
                    for w in sg.get("words") or []:
                        w["start"] += off
                        w["end"] += off
                if target < n:
                    keep = [sg for sg in segs if sg["end"] <= target / sr + 0.05]
                    if keep:
                        a = snap_to_quiet(pcm, sr, int(keep[-1]["end"] * sr))
                    else:  # one sentence longer than the planned range: cut at the quietest instant
                        keep = [sg for sg in segs if sg["start"] < target / sr]
                        a = snap_to_quiet(pcm, sr, target)
                else:
                    keep, a = segs, n
                cues = join_pieces(cues, words_to_cues(keep))
                log(f"piece {k + 1}: audio {a_start / sr:.1f}-{z / sr:.1f}s, kept {len(keep)}/{len(segs)} segments "
                    f"up to {a / sr:.1f}s, {time.time() - t_piece:.0f}s")
            k += 1
            pieces_est = max(pieces_est, k + (1 if a < n else 0))
            (folder / "partial.json").write_text(json.dumps({"cues": cues, "piece": k, "pieces": pieces_est},
                                                            ensure_ascii=False), encoding="utf-8")
            set_job(jid, piece=k, pieces=pieces_est, lines=len(cues), progress=0.25 + 0.55 * min(1.0, a / n))
        del pcm
        lang = lang or ""
        stem = f"transcript{'.' + lang if lang else ''}"
        (folder / f"{stem}.srt").write_text(to_srt(cues), encoding="utf-8")
        (folder / f"{stem}.txt").write_text(to_txt(cues), encoding="utf-8")

        frames = []
        if video.suffix.lower() not in AUDIO_EXT:  # audio files have no frames (an mp3 cover is not one)
            set_job(jid, stage="Picking frames", progress=0.82)
            frames = pick_frames(jid, video, meta["duration"], folder)

        result = {"title": title, "language": lang, **meta, "cues": cues, "frames": frames, "video": str(video)}
        (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        audio.unlink(missing_ok=True)
        set_job(jid, stage="Done", progress=1.0, done=True)
    except Exception as e:  # report to the page instead of dying silently
        log(traceback.format_exc())
        set_job(jid, stage="Failed", error=str(e) or e.__class__.__name__, done=True)


def load_job(jid: str) -> dict:
    sj = JOBS_DIR / re.sub(r"[^\w\-]", "", jid) / "status.json"
    if sj.exists():
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except ValueError:  # caught mid-write; the next poll reads it
            return {"id": jid, "stage": "Working", "progress": 0.0, "done": False, "error": None}
        pid = st.get("pid") or st.get("runner_pid")
        if not st.get("done") and pid and not alive(pid):
            st.update(stage="Failed", done=True, error="The job stopped unexpectedly. See jobs/" + jid + "/log.txt")
        return st
    rj = JOBS_DIR / jid / "result.json"
    if rj.exists():
        return {"id": jid, "stage": "Done", "progress": 1.0, "done": True}
    raise ValueError("Unknown job. Start it again.")


# ---------- tools ----------

@mcp.tool(structured_output=False)
def start(source: str, language: str = "") -> str:
    """Start processing a video. `source` is a link (YouTube, Vimeo, Google Drive, Dropbox,
    direct .mp4), a path to a video or audio file on this Mac, or "upload:<id>" after `upload_begin`. `language` is an optional two-letter
    code for the spoken language; leave empty to detect it. Returns {"job": id}. Then poll
    `status`, and when done read `result` and each `frame`."""
    source = source.strip()
    if not source:
        raise ValueError("Paste a video link or a file path.")
    known = find_job(source)
    if known:
        return json.dumps({"job": known, "reused": True})
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    (JOBS_DIR / jid).mkdir(parents=True, exist_ok=True)
    set_job(jid, source=source, stage="Starting", progress=0.0, done=False, error=None)
    # The heavy work runs in its own process: the MCP server stays free to answer
    # status calls, and the job keeps going if the Claude app restarts the server.
    log = open(JOBS_DIR / jid / "log.txt", "w")
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--run", jid, source, language.strip().lower()],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd=str(ROOT))
    set_job(jid, runner_pid=proc.pid)
    return json.dumps({"job": jid})


@mcp.tool(structured_output=False)
def upload_begin(name: str) -> str:
    """Prepare to receive an audio file (.mp3, .wav, .m4a) dropped in the studio. Returns
    {"upload": id}. Send the bytes with `upload_chunk`, then call `start` with source "upload:<id>"."""
    ext = Path(name).suffix.lower()
    if ext not in AUDIO_EXT:
        raise ValueError("Use an .mp3, .wav or .m4a file.")
    uid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    folder = UPLOADS_DIR / uid
    folder.mkdir(parents=True)
    safe = re.sub(r"[^\w.\- ]", "_", Path(name).stem)[:80] or "audio"
    (folder / f"{safe}{ext}").touch()
    return json.dumps({"upload": uid})


@mcp.tool(structured_output=False)
def upload_chunk(upload: str, data: str) -> str:
    """Append one base64 chunk to an upload, in order. Returns {"bytes": total so far}."""
    folder = UPLOADS_DIR / re.sub(r"[^\w\-]", "", upload)
    files = list(folder.glob("*")) if folder.is_dir() else []
    if not files:
        raise ValueError("Unknown upload. Drop the file again.")
    with open(files[0], "ab") as fh:
        fh.write(base64.b64decode(data))
    return json.dumps({"bytes": files[0].stat().st_size})


@mcp.tool(structured_output=False, annotations=RO)
def status(job: str) -> str:
    """Progress of a job: {stage, progress 0..1, done, error, title, duration, piece, pieces, lines}."""
    j = load_job(job)
    return json.dumps({k: j.get(k) for k in ("id", "stage", "progress", "done", "error", "title", "duration", "piece", "pieces", "lines")})


@mcp.tool(structured_output=False, annotations=RO)
def partial(job: str) -> str:
    """Transcript so far of a running job: {cues [{s,e,t}], piece, pieces}. Empty before the first piece."""
    pj = JOBS_DIR / re.sub(r"[^\w\-]", "", job) / "partial.json"
    if not pj.exists():
        return json.dumps({"cues": [], "piece": 0, "pieces": 0})
    try:
        return pj.read_text(encoding="utf-8")
    except OSError:
        return json.dumps({"cues": [], "piece": 0, "pieces": 0})


@mcp.tool(structured_output=False, annotations=RO)
def result(job: str) -> str:
    """Finished job: title, spoken language, duration, size, subtitle cues [{s,e,t}] and the
    10 frames [{index, t, score}] (read pixels with `frame`)."""
    rj = JOBS_DIR / job / "result.json"
    if not rj.exists():
        raise ValueError("This job is not finished yet.")
    return rj.read_text(encoding="utf-8")


@mcp.tool(structured_output=False, annotations=RO)
def frame(job: str, index: int, width: int = 1600) -> str:
    """One of the 10 frames as a base64 JPEG: {index, t, width, height, jpeg}."""
    data = json.loads((JOBS_DIR / job / "result.json").read_text(encoding="utf-8"))
    f = next((x for x in data["frames"] if x["index"] == int(index)), None)
    if not f:
        raise ValueError("No frame with that index.")
    im = Image.open(JOBS_DIR / job / f["file"])
    if width and im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=86)
    return json.dumps({"index": f["index"], "t": f["t"], "width": im.width, "height": im.height,
                       "jpeg": base64.b64encode(buf.getvalue()).decode()})


@mcp.tool(structured_output=False, annotations=RO)
def recent(limit: int = 8) -> str:
    """The most recent finished jobs: [{job, title, duration, language}]."""
    out = []
    for d in sorted(JOBS_DIR.iterdir(), reverse=True):
        rj = d / "result.json"
        if rj.exists():
            r = json.loads(rj.read_text(encoding="utf-8"))
            out.append({"job": d.name, "title": r.get("title"), "duration": r.get("duration"), "language": r.get("language")})
        if len(out) >= limit:
            break
    return json.dumps(out)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--run":
        run(sys.argv[2], sys.argv[3], (sys.argv[4] if len(sys.argv) > 4 else "") or None)
    else:
        mcp.run()
