#!/usr/bin/env python3
"""Benchmark proxy overhead: measure TTFB and total-stream latency.

Usage:
    python tools/bench_overhead.py --port 9299 --mock-port 9298 --n 300 --concurrency 1
    python tools/bench_overhead.py --port 9299 --mock-port 9298 --n 300 --concurrency 4 --tokens 20000

Compares proxy (port) vs direct-to-mock (mock-port) to isolate proxy overhead.
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import httpx

def generate_fixture(token_target: int) -> dict:
    """Generate a request body with approximately token_target tokens."""
    # ~4 chars per token for English text
    chars = token_target * 4
    filler = "The quick brown fox jumps over the lazy dog. " * (chars // 45 + 1)
    filler = filler[:chars]
    return {
        "model": "qwen",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": filler},
        ],
        "max_tokens": 100,
        "stream": True,
        "temperature": 0.7,
    }

async def measure_stream(client: httpx.AsyncClient, url: str, body: dict, headers: dict = None) -> dict:
    """Measure TTFB and total stream time for a single request."""
    t0 = time.monotonic()
    first_byte_t = None
    chunks = 0
    total_bytes = 0
    failed = False
    error = ""
    
    try:
        async with client.stream("POST", url, json=body, headers=headers or {}) as resp:
            async for line in resp.aiter_lines():
                if first_byte_t is None and line:
                    first_byte_t = time.monotonic()
                if line:
                    chunks += 1
                    total_bytes += len(line)
    except Exception as e:
        failed = True
        error = str(e)[:100]
    
    total_t = time.monotonic() - t0
    ttft = (first_byte_t - t0) if first_byte_t else total_t
    
    return {
        "ttft": ttft,
        "total": total_t,
        "chunks": chunks,
        "bytes": total_bytes,
        "cps": chunks / total_t if total_t > 0 else 0,
        "failed": failed,
        "error": error,
    }

async def run_benchmark(args):
    body = generate_fixture(args.tokens)
    results_proxy = []
    results_direct = []
    
    proxy_url = f"http://127.0.0.1:{args.port}/v1/chat/completions"
    direct_url = f"http://127.0.0.1:{args.mock_port}/v1/chat/completions"
    
    headers = {}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        if args.concurrency == 1:
            for i in range(args.n):
                r1 = await measure_stream(client, proxy_url, body, headers)
                r2 = await measure_stream(client, direct_url, body, headers)
                results_proxy.append(r1)
                results_direct.append(r2)
                if (i + 1) % 50 == 0:
                    print(f"  Progress: {i+1}/{args.n}", file=sys.stderr)
        else:
            # Concurrent execution
            sem = asyncio.Semaphore(args.concurrency)
            async def _one(idx):
                async with sem:
                    r1 = await measure_stream(client, proxy_url, body, headers)
                    r2 = await measure_stream(client, direct_url, body, headers)
                    results_proxy.append(r1)
                    results_direct.append(r2)
                    if (idx + 1) % 50 == 0:
                        print(f"  Progress: {idx+1}/{args.n}", file=sys.stderr)
            tasks = [_one(i) for i in range(args.n)]
            await asyncio.gather(*tasks)
    
    return results_proxy, results_direct

def report(results_proxy, results_direct, label):
    """Print benchmark report comparing proxy vs direct."""
    pp = [r["ttft"] for r in results_proxy if not r["failed"]]
    pd = [r["ttft"] for r in results_direct if not r["failed"]]
    tp = [r["total"] for r in results_proxy if not r["failed"]]
    td = [r["total"] for r in results_direct if not r["failed"]]
    cp = [r["cps"] for r in results_proxy if not r["failed"]]
    cd = [r["cps"] for r in results_direct if not r["failed"]]
    
    n_fail_p = sum(1 for r in results_proxy if r["failed"])
    n_fail_d = sum(1 for r in results_direct if r["failed"])
    
    def pct(vals, p):
        if not vals:
            return 0
        s = sorted(vals)
        idx = int(len(s) * p / 100)
        idx = min(idx, len(s) - 1)
        return s[idx]
    
    ttft_overhead_p50 = pct(pp, 50) - pct(pd, 50)
    ttft_overhead_p95 = pct(pp, 95) - pct(pd, 95)
    total_overhead_p50 = pct(tp, 50) - pct(td, 50)
    total_overhead_p95 = pct(tp, 95) - pct(td, 95)
    cps_drop = (1 - pct(cp, 50) / pct(cd, 50)) * 100 if pct(cd, 50) > 0 else 0
    
    rps_p = len(pp) / (sum(tp) / len(tp)) if tp else 0
    rps_d = len(pd) / (sum(td) / len(td)) if td else 0
    
    print(f"\n=== {label} ===")
    print(f"  N={len(results_proxy)} (proxy failures: {n_fail_p}, direct failures: {n_fail_d})")
    print(f"  TTFB overhead p50: {ttft_overhead_p50*1000:.1f}ms")
    print(f"  TTFB overhead p95: {ttft_overhead_p95*1000:.1f}ms")
    print(f"  Total overhead p50: {total_overhead_p50*1000:.1f}ms")
    print(f"  Total overhead p95: {total_overhead_p95*1000:.1f}ms")
    print(f"  Chunks/sec drop: {cps_drop:.1f}%")
    print(f"  Throughput proxy: {rps_p:.2f} req/s, direct: {rps_d:.2f} req/s")
    
    return {
        "n": len(results_proxy),
        "n_fail_proxy": n_fail_p,
        "n_fail_direct": n_fail_d,
        "ttft_overhead_p50": ttft_overhead_p50,
        "ttft_overhead_p95": ttft_overhead_p95,
        "total_overhead_p50": total_overhead_p50,
        "total_overhead_p95": total_overhead_p95,
        "cps_drop_pct": cps_drop,
        "rps_proxy": rps_p,
        "rps_direct": rps_d,
    }

async def main():
    parser = argparse.ArgumentParser(description="Benchmark proxy overhead")
    parser.add_argument("--port", type=int, default=9299, help="Proxy port")
    parser.add_argument("--mock-port", type=int, default=9298, help="Mock vLLM port")
    parser.add_argument("--n", type=int, default=300, help="Number of requests")
    parser.add_argument("--concurrency", type=int, default=1, help="Concurrency level")
    parser.add_argument("--tokens", type=int, default=20000, help="Approximate token count")
    parser.add_argument("--api-key", type=str, default="", help="API key for proxy")
    parser.add_argument("--baseline-json", type=str, default="", help="Path to baseline results JSON")
    parser.add_argument("--output-json", type=str, default="", help="Path to save results JSON")
    args = parser.parse_args()
    
    print(f"Benchmarking: proxy={args.port} mock={args.mock_port} n={args.n} conc={args.concurrency} tokens~{args.tokens}")
    
    results_proxy, results_direct = await run_benchmark(args)
    results = report(results_proxy, results_direct, f"tokens~{args.tokens} conc={args.concurrency}")
    
    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump({
                "n": args.n,
                "concurrency": args.concurrency,
                "tokens": args.tokens,
                "results": results,
                "raw_proxy": results_proxy[:10],
                "raw_direct": results_direct[:10],
            }, f, indent=2)
        print(f"Results saved to {args.output_json}")

if __name__ == "__main__":
    asyncio.run(main())
