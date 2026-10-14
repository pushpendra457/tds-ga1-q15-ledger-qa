"""
Offline test harness for the Acme ledger Q&A service.

Builds the dataset from the local /tmp/data.ndjson snapshot (no network) using
the SAME preparation logic as main.py, injects it into the cache, then drives
the real FastAPI endpoint via TestClient and checks each answer against a
hand-computed reference value.

Run:  python3 test_local.py
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

DATA_FILE = "/tmp/data.ndjson"
IST = timezone(timedelta(hours=5, minutes=30))
RATES = {"USD": 1.0, "EUR": 1.13, "INR": 0.01231}


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def build_local_dataset():
    rows = [json.loads(l) for l in open(DATA_FILE) if l.strip()]
    latest = {}
    for row in rows:
        oid = row["id"]
        cur = latest.get(oid)
        if cur is None or parse_ts(row["updated_at"]) > parse_ts(cur["updated_at"]):
            latest[oid] = row
    orders = []
    for row in latest.values():
        cur = row["currency"]
        rate = RATES[cur]
        amount = float(row["amount"])
        ci = parse_ts(row["created_at"]).astimezone(IST)
        orders.append({
            "id": row["id"], "customer": row["customer"], "region": row["region"],
            "product": row["product"], "qty": int(row["qty"]),
            "unit_price": float(row["unit_price"]), "amount": amount,
            "currency": cur, "status": row["status"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "usd": amount * rate,
            "created_ist": ci, "year": ci.year, "month": ci.month,
        })
    return {
        "orders": orders, "rates": RATES,
        "regions": sorted({o["region"] for o in orders}),
        "products": sorted({o["product"] for o in orders}),
        "statuses": sorted({o["status"] for o in orders}),
        "currencies": sorted({o["currency"] for o in orders}),
        "built_at": __import__("time").time(),
    }


# (question, expected_answer)
CASES = [
    # --- the canonical example ---
    ("What was the total revenue in USD from the North region in March 2026?", 37382.56),
    ("Total revenue from the North region in March 2026", 37382.56),
    ("How much revenue did the North region make in Mar 2026?", 37382.56),
    # --- all-time / broad revenue ---
    ("What is the total revenue for all time?", 1066390.12),
    ("Total revenue across all regions", 1066390.12),
    ("How much revenue did we make in 2026?", 1066390.12),
    ("Total sales in 2026", 1066390.12),
    # --- quarters ---
    ("What was the revenue in Q1 2026?", 528461.18),
    ("Revenue in the first quarter of 2026", 528461.18),
    ("How much revenue in Q2 2026?", 537928.95),
    # --- single months ---
    ("What was the total revenue in June 2026?", 183564.68),
    ("Revenue for April 2026", None),  # computed below dynamically
    # --- refunds ---
    ("How much was refunded in the West region?", 30937.53),
    ("Total refunds in the West", 30937.53),
    ("What were the refunds in North in March 2026?", 5968.84),
    ("How many refunds were there?", 451),
    # --- counts ---
    ("How many orders were placed in April 2026?", 648),
    ("How many distinct customers made purchases?", 250),
    # --- units ---
    ("How many units were sold in total?", 9695),
    # --- products ---
    ("What was the top selling product by revenue?", "Blender"),
    ("Which product generated the most revenue?", "Blender"),
    ("What is our best-selling product by units sold?", "Air Fryer"),
    # --- product as a filter (NOT a ranking) ---
    ("What was the revenue from Blender?", 231610.05),
    # --- average ---
    ("What was the average order value?", 331.80),
    # --- net revenue (paid - refunded) ---
    ("What was the net revenue overall?", 916613.52),
    # --- customer-specific ---
    ("How much has customer C1141 spent in total?", 10053.82),
]



def expected_april_revenue(ds):
    paid = [o for o in ds["orders"] if o["status"] == "paid" and o["year"] == 2026 and o["month"] == 4]
    return round(sum(o["usd"] for o in paid), 2)


def main_test():
    ds = build_local_dataset()
    # Inject into the cache so the endpoint serves this snapshot.
    main.CACHE._data = ds

    client = TestClient(main.app)

    # health check
    h = client.get("/")
    print("HEALTH:", h.status_code, h.json())

    passed = 0
    failed = 0
    for question, expected in CASES:
        if expected is None:
            expected = expected_april_revenue(ds)
        r = client.post("/", json={"question": question})
        assert r.status_code == 200, (question, r.status_code, r.text)
        got = r.json()["answer"]
        ok = (abs(got - expected) < 0.005) if isinstance(expected, float) else (got == expected)
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{status}] {question!r}\n         got={got!r} expected={expected!r}")

    print(f"\n{passed} passed, {failed} failed out of {len(CASES)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main_test())
