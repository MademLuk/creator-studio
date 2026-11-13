"""Starts the Creator Studio helper from its folder.
The Claude app passes that folder in CREATOR_STUDIO_DIR (the person chooses it when installing the extension);
run by hand from the repository, the folder is found from this file's place."""
import os
from pathlib import Path

root = Path(os.environ.get("CREATOR_STUDIO_DIR") or Path(__file__).resolve().parents[2]).expanduser()
python = root / ".venv" / "bin" / "python"
os.execv(str(python), [str(python), str(root / "server.py")])
