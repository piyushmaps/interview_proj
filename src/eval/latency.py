"""Latency and throughput against a running API.

Streaming and asynchronous handling are often described as "making it faster",
but they change two different things, and measuring the wrong one hides both:

* **Streaming** does not finish sooner. It changes *when the first thing
  appears*. So it is measured as time to first event and time to first word,
  next to the total.
* **Asynchronous handling** does not speed up one request. It changes *how many
  requests one server carries at once*. So it is measured by firing many
  requests together and timing the batch.

Run the API with the answer cache off, or every repeat after the first is a
cache hit and the numbers describe the cache instead of the pipeline:

    CACHE_ENABLED=false uvicorn src.api.main:app --port 8020
    python -m src.eval.latency --base-url http://127.0.0.1:8020
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass

import httpx

DEFAULT_QUESTION = "Who owns the Payments capability?"


@dataclass
class StreamTiming:
    first_event: float | None
    first_word: float | None
    total: float


async def time_ask(client: httpx.AsyncClient, question: str) -> float:
    started = time.perf_counter()
    response = await client.post("/ask", json={"question": question})
    response.raise_for_status()
    return time.perf_counter() - started


async def time_stream(client: httpx.AsyncClient, question: str) -> StreamTiming | None:
    """Time a streamed answer, or None if the server has no streaming endpoint."""
    started = time.perf_counter()
    first_event = first_word = None
    event = None
    async with client.stream("POST", "/ask/stream", json={"question": question}) as response:
        if response.status_code == 404:
            return None
        response.raise_for_status()
        async for line in response.aiter_lines():
            now = time.perf_counter() - started
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip()
                if first_event is None:
                    first_event = now
            elif line.startswith("data:") and event == "token" and first_word is None:
                if json.loads(line.removeprefix("data:")).get("text"):
                    first_word = now
    return StreamTiming(first_event, first_word, time.perf_counter() - started)


async def burst(client: httpx.AsyncClient, question: str, n: int) -> tuple[float, list[float], int]:
    """Fire `n` requests at the same moment. Returns wall time, per-request times, failures."""
    started = time.perf_counter()
    results = await asyncio.gather(
        *(time_ask(client, question) for _ in range(n)), return_exceptions=True
    )
    wall = time.perf_counter() - started
    times = [r for r in results if isinstance(r, float)]
    return wall, times, len(results) - len(times)


def _fmt(seconds: float | None) -> str:
    return "—" if seconds is None else f"{seconds:5.2f}s"


async def main_async(base_url: str, question: str, repeats: int, concurrency: int) -> None:
    limits = httpx.Limits(max_connections=concurrency + 10)
    async with httpx.AsyncClient(base_url=base_url, timeout=180, limits=limits) as client:
        health = (await client.get("/health")).json()
        if isinstance(health.get("cache"), dict):
            print("WARNING: the answer cache is on — repeats will be cache hits.\n")

        print(f"question: {question!r}\n")
        print("ONE REQUEST AT A TIME")
        ask_times = [await time_ask(client, question) for _ in range(repeats)]
        print(f"  /ask         total        {_fmt(statistics.median(ask_times))}"
              f"   (median of {repeats})")

        streams = [await time_stream(client, question) for _ in range(repeats)]
        if streams[0] is None:
            print("  /ask/stream  not available on this server")
        else:
            med = lambda xs: statistics.median(x for x in xs if x is not None)
            print(f"  /ask/stream  first event  {_fmt(med(s.first_event for s in streams))}")
            print(f"               first word   {_fmt(med(s.first_word for s in streams))}")
            print(f"               total        {_fmt(med(s.total for s in streams))}")

        print(f"\n{concurrency} REQUESTS AT THE SAME MOMENT")
        wall, times, failed = await burst(client, question, concurrency)
        print(f"  whole batch finished in   {_fmt(wall)}")
        if times:
            print(f"  median request            {_fmt(statistics.median(times))}")
            print(f"  slowest request           {_fmt(max(times))}")
            print(f"  throughput                {len(times) / wall:5.1f} requests per second")
        if failed:
            print(f"  failed                    {failed}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure API latency and throughput.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=60)
    args = parser.parse_args()
    asyncio.run(main_async(args.base_url, args.question, args.repeats, args.concurrency))


if __name__ == "__main__":
    main()
