# Creator Studio

Video link in, subtitles + thumbnail + posts out. A free, open tool from Fundacja Po-Nad-To (MIT licence). Beta.

**For everyone:** [`index.html`](index.html) is the tool's own page: what it does, what you need (a Mac with an Apple chip,
the Claude desktop app, Python 3.11, about 3 GB free) and how to set it up once. In short:

1. Download this folder (Code, then Download ZIP) and put it where you keep your projects.
2. In Terminal, in that folder: `python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. Double-click `extension/creator-studio.mcpb`, press Install, and choose this folder when the Claude app asks.
4. Open the studio page inside the Claude desktop app.

Your video stays on your Mac: transcription and frames run locally. Only the text goes to Claude, to be cleaned and translated.

---

The notes below are for maintaining the tool.

- **Studio (the page, Po-Nad-To's copy):** https://claude.ai/artifact/M9E5AECWWs3eKiaYXVE3p6
- **Bookmark target:** `CREATOR_STUDIO.html` in this folder: start page with the Creator Studio icon and one Open button (fixed name, never rename).
- **Mac helper:** `server.py`, a local MCP server installed in the Claude desktop app as the `creator-studio`
  extension (see "Installing the helper"). Update packages now and then: `.venv/bin/pip install -U yt-dlp mlx mlx-metal`
  (yt-dlp must stay recent or YouTube downloads break; never upgrade `mcp` past 1.x).

## Flow

1. Link (YouTube, Vimeo, Drive, Dropbox, direct .mp4), a file path on this Mac, or an .mp3/.wav dropped in the studio
2. Mac helper: download (yt-dlp) → transcribe (mlx-whisper large-v3-turbo, in 10-minute pieces aligned on sentence ends (30 s overlap, only finished sentences kept), real progress, transcript readable while it runs) → 10 strongest frames (ffmpeg)
3. Claude: clean (filler words, stutters, spelling, grammar, syntax, punctuation) → translate
4. You: check subtitles → pick thumbnail → approve posts

"Transcript only" on the Link tab skips the Claude steps (for long recordings you will summarise yourself).
Subtitles has "Export .txt": plain text in paragraphs, no timecodes. The helper also writes
`transcript.<lang>.txt` next to the `.srt` in the job folder.
For a big local file, paste its path in the Link field (Finder: right-click, hold Option, "Copy as Pathname")
instead of dropping it: nothing is uploaded, the helper reads it from disk.

The helper only answers when the studio is open **inside the Claude desktop app**. In a normal browser the
studio still works with dropped .srt files and videos.

## Files

- `jobs/<id>/` one folder per video: `video.mp4`, `transcript.<lang>.srt`, `frame00-09.jpg`, `result.json`.
  Delete old job folders to free space.
- `requirements.txt` Python packages (`.venv`, Python 3.11). Reinstall: `.venv/bin/pip install -r requirements.txt`
- `test/smoke.py` end-to-end check with a generated French test video.

The first transcription downloads the speech model once (about 1.5 GB, in `~/.cache/huggingface`).
Speed on the M4: about 11 minutes per hour of audio (3 h 03 min measured at 33 min, 3 GB peak memory, 2026-11-04).
Piece length: `CREATOR_PIECE_SECONDS` (default 600).

## Studio source

`studio/creator-studio.html` is the published page. Edit it, then republish to the same artifact URL.

## Installing the helper in the Claude app

The Claude app rewrites `claude_desktop_config.json` every minute, so a hand-made entry does not last.
The helper is installed as an extension instead: double-click `extension/creator-studio.mcpb` and press Install;
the app asks for the Creator Studio folder and passes it to the helper (`CREATOR_STUDIO_DIR`). Rebuild after changing the bundle: zip `extension/bundle/manifest.json`, `launch.py`, `icon.png` into `extension/creator-studio.mcpb`:

```bash
cd extension/bundle && zip -q ../creator-studio.mcpb manifest.json launch.py icon.png
```

Keep `display_name` = `creator-studio` in the manifest: the app announces the helper by that name and the studio calls `host:creator-studio`.

## Setup on a new Mac

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip uninstall -y torch   # pulled in by mlx-whisper but not used: saves ~600 MB (tested 2026-10-13)
```

No path is written into the files: the extension asks for the folder at install. If the folder moves, reinstall the extension and choose the new place.

## Desktop apps (macOS)

`launchers/` holds the sources of two small apps installed in /Applications (built with `osacompile`, icon copied over `Contents/Resources/applet.icns`):

- **Creator Studio.app**: opens the studio inside the Claude desktop app via its own link scheme
  (`claude://claude.ai/artifact/<id>`), so the Mac helper is available. Icon: the Ludizm mark inside a play shape
  (`cs-A.svg` outline, `cs-B.svg` filled; drawn from the Ludizm vector paths, unchanged).
- **KIUB Dashboard.app**: opens `00_KIUB_DASHBOARD.html` as a chromeless Brave window
  (`Brave Browser --app=file://…`, launched from the binary so it reaches a running Brave). Icon: the dashboard page's own icon.

Rebuild: `osacompile -o "/Applications/<Name>.app" launchers/<name>.applescript`, then copy the `.icns` over `applet.icns`.
