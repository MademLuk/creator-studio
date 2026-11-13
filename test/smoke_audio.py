"""Audio upload check: send test/voice.mp3 in chunks like the studio does, then transcribe it."""
import asyncio, base64, json, sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FILE = HERE / (sys.argv[1] if len(sys.argv) > 1 else "voice.mp3")


async def main():
    params = StdioServerParameters(command=str(ROOT / ".venv/bin/python"), args=[str(ROOT / "server.py")])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            call = lambda t, a: s.call_tool(t, a)
            up = json.loads((await call("upload_begin", {"name": FILE.name})).content[0].text)["upload"]
            raw = FILE.read_bytes()
            for i in range(0, len(raw), 10_000):  # small chunks on purpose
                res = json.loads((await call("upload_chunk", {"upload": up, "data": base64.b64encode(raw[i:i + 10_000]).decode()})).content[0].text)
            print("uploaded", res["bytes"], "of", len(raw))
            job = json.loads((await call("start", {"source": "upload:" + up})).content[0].text)["job"]
            while True:
                st = json.loads((await call("status", {"job": job})).content[0].text)
                if st["done"]:
                    break
                await asyncio.sleep(2)
            if st["error"]:
                sys.exit("FAILED: " + st["error"])
            res = json.loads((await call("result", {"job": job})).content[0].text)
            print("language:", res["language"], "duration:", res["duration"], "frames:", len(res["frames"]))
            for c in res["cues"]:
                print(f"  {c['s']:6.2f}-{c['e']:6.2f}  {c['t']}")


asyncio.run(main())
