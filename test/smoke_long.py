"""Long-file check: start the helper on a local file path (no upload), time the whole run."""
import asyncio, json, sys, time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
ROOT = Path(__file__).resolve().parent.parent
path = str(ROOT / "test" / (sys.argv[1] if len(sys.argv) > 1 else "long3h.mp3"))
async def main():
    async with stdio_client(StdioServerParameters(command=str(ROOT/".venv/bin/python"), args=[str(ROOT/"server.py")], env={**__import__("os").environ})) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            t0 = time.time()
            job = json.loads((await s.call_tool("start", {"source": path})).content[0].text)["job"]
            last = ""
            while True:
                st = json.loads((await s.call_tool("status", {"job": job})).content[0].text)
                if st["stage"] != last:
                    print(f"{time.time()-t0:7.0f}s  {st['stage']}", flush=True); last = st["stage"]
                if st["done"]: break
                await asyncio.sleep(10)
            if st["error"]: sys.exit("FAILED: " + st["error"])
            res = json.loads((await s.call_tool("result", {"job": job})).content[0].text)
            print(f"done in {time.time()-t0:.0f}s | duration {res['duration']/3600:.2f} h | language {res['language']} | lines {len(res['cues'])} | first: {res['cues'][0]['t']} | last: {res['cues'][-1]['t']}")
asyncio.run(main())
