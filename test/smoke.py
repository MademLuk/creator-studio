"""End-to-end check: serve test.mp4 over HTTP, process the link through the MCP server."""
import asyncio, json, sys, threading, time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 8765), partial(SimpleHTTPRequestHandler, directory=str(HERE)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()


async def main():
    serve()
    params = StdioServerParameters(command=str(ROOT / ".venv/bin/python"), args=[str(ROOT / "server.py")])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", [(t.name, t.annotations.readOnlyHint if t.annotations else None) for t in tools.tools])
            job = json.loads((await s.call_tool("start", {"source": "http://127.0.0.1:8765/test.mp4"})).content[0].text)["job"]
            while True:
                t0 = time.time(); st = json.loads((await s.call_tool("status", {"job": job})).content[0].text); dt = time.time() - t0
                print(f"  {st['stage']:<18} {st['progress']:.2f}  reply {dt*1000:.0f} ms", flush=True)
                if st["done"]:
                    break
                await asyncio.sleep(5)
            if st["error"]:
                sys.exit("FAILED: " + st["error"])
            res = json.loads((await s.call_tool("result", {"job": job})).content[0].text)
            print("language:", res["language"], "duration:", res["duration"], "size:", res["width"], res["height"])
            for c in res["cues"]:
                print(f"  {c['s']:6.2f}-{c['e']:6.2f}  {c['t']}")
            print("frames:", [(f["index"], f["t"]) for f in res["frames"]])
            fr = json.loads((await s.call_tool("frame", {"job": job, "index": 0})).content[0].text)
            print("frame0:", fr["width"], "x", fr["height"], len(fr["jpeg"]) // 1024, "KB base64")


asyncio.run(main())
