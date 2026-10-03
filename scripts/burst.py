import asyncio
import os
import sys

import httpx

URL, KEY = os.environ["URL"], os.environ["API_KEY"]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 30


async def one(client, i):
    r = await client.post(
        f"{URL}/chat",
        headers={"X-API-Key": KEY},
        json={"message": f"Give one fun fact #{i}."},
        timeout=90,
    )
    return r.status_code


async def main():
    async with httpx.AsyncClient() as client:
        codes = await asyncio.gather(*(one(client, i) for i in range(N)))
    print({c: codes.count(c) for c in sorted(set(codes))})


asyncio.run(main())
