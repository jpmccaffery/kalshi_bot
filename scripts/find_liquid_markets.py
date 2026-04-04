"""
Quick script to find open markets with non-zero bid/ask spreads on the demo API.
Run with: docker exec kalshi_bot python scripts/find_liquid_markets.py
"""
import os
import sys
sys.path.insert(0, "/app")

from dotenv import load_dotenv
load_dotenv()

from src.kalshi.client import KalshiClient

client = KalshiClient(
    base_url="https://demo-api.kalshi.co/trade-api/v2",
    api_key_id=os.environ.get("KALSHI_API_KEY_ID", ""),
    private_key_path=os.environ.get("KALSHI_API_PRIVATE_KEY_PATH", ""),
)

liquid = []
cursor = None

print("Fetching open markets...")
while True:
    resp = client.list_markets(status="open", limit=200, cursor=cursor)
    markets = resp.get("markets", [])
    for m in markets:
        bid_raw = m.get("yes_bid") or m.get("yes_bid_dollars")
        ask_raw = m.get("yes_ask") or m.get("yes_ask_dollars")
        try:
            bid = round(float(bid_raw) * 100) if bid_raw else 0
            ask = round(float(ask_raw) * 100) if ask_raw else 100
        except (TypeError, ValueError):
            bid, ask = 0, 100

        spread = ask - bid
        vol = m.get("volume", 0) or 0
        oi = m.get("open_interest", 0) or 0

        if bid > 0 and ask < 100 and spread < 30:
            liquid.append({
                "ticker": m["ticker"],
                "title": m.get("title", "")[:60],
                "bid": bid,
                "ask": ask,
                "spread": spread,
                "volume": vol,
                "oi": oi,
            })

    cursor = resp.get("cursor")
    if not cursor:
        break

liquid.sort(key=lambda x: (-x["volume"], x["spread"]))

print(f"\nFound {len(liquid)} liquid markets (bid>0, ask<100, spread<30):\n")
print(f"{'Ticker':<50} {'Bid':>4} {'Ask':>4} {'Sprd':>5} {'Vol':>7} {'OI':>6}")
print("-" * 90)
for m in liquid[:40]:
    print(f"{m['ticker']:<50} {m['bid']:>4} {m['ask']:>4} {m['spread']:>5} {m['volume']:>7} {m['oi']:>6}")
    print(f"  {m['title']}")
