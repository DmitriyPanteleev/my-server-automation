#!/bin/python3

import asyncio
import aiomultiprocess

async def worker(queue):
    while True:
        item = await queue.get()
        if item is None:
            break
        print(f"Working on {item}")

async def main():
    queue = await aiomultiprocess.create_queue()
    async with aiomultiprocess.Pool() as pool:
        tasks = [pool.spawn(worker, queue) for _ in range(3)]
        for i in range(10):
            await queue.put(i)
        for _ in range(3):
            await queue.put(None)
        await asyncio.gather(*tasks)

asyncio.run(main())
