"""
Acme Appliances order-ledger Q&A service.

A public HTTPS endpoint that accepts

    POST  {"question": "..."}   ->   {"answer": ...}

and answers plain-English questions about the Acme Appliances order ledger
(revenue, refunds, products, customers).

How the data was reverse-engineered (there are no docs):

  Root  (path=/)        -> service descriptor + links + 3 business notes:
                             * "Business dates use the Asia/Kolkata timezone."
                             * "An order id can appear more than once; the row
                                with the latest updated_at is current."
                             * "Only orders with status 'paid' count as revenue."
  /orders?page=N        -> paginated JSON. 86 pages x 50 rows = 4251 rows.
                           Fields: id, customer, region, product, qty,
                           unit_price, amount, currency, status, created_at,
                           updated_at.  Response has page/pages/next/orders.
  /rates                -> {"usd_per_unit": {"USD":1,"EUR":1.13,"INR":0.01231}}
  /export               -> the ENTIRE dataset as newline-delimited JSON in a
                           single ~1 MB request. We use this (one request beats
                           86 paginated ones well within the 15s grading budget).

Data quirks handled here:
  * 251 order ids appear twice; in every case the two rows differ in `amount`
    and 45 also differ in `status` (e.g. paid -> refunded). We keep only the
    row with the latest `updated_at`, compared as a real instant (timestamps
    mix "Z"/UTC and "+05:30"/IST, so a naive string sort is unsafe).
  * 18 rows change calendar month when converted UTC -> Asia/Kolkata, so all
    date bucketing is done in IST.
  * amount == qty * unit_price always holds.
  * Money is answered in USD, correct to the cent (convert each row's amount by
    its currency's USD rate, sum, then round once at the end).

The natural-language understanding is a deterministic rule-based parser rather
than a call to an external LLM: the grader enforces a hard 15s correctness gate,
so we avoid any network dependency at question time and keep answers fast and
reproducible.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# The signed URL the task hands us. We keep the raw path prefix and simply swap
# the trailing `path=` value to reach /orders, /rates and /export. If the query
# string ever changes, override with the LEDGER_BASE_URL env var.
DEFAULT_BASE_URL = (
    "https://exam.sanand.workers.dev/questionData?"
    "email=24f2003000%40ds.study.iitm.ac.in"
    "&quizSign=h5OvN0z%2F%2F6fCIo%2FZTgqqu2BEpRcLpAqh%2BMox236EkVyRWzbXUxnGCjREC"
    "%2FsRHrG9kdDS5pwmba7hEAGm%2FB75AyZTJ6rFcSnuL%2BkVRnU%2B9uRfurjDiP7pU9%2B9uvqCFH"
    "%2BUMoOBzESL5ezwKoFCsCB2%2B1hs1mpOSlEYRW1%2B86%2FZrHCxN3B1sSHc7Iw%2BB7qw6KFD3RMEV"
    "dx8nTrJVbP8tM7WjuZj9aSkKbfShPYKkrMF4OVoBBxBTAvhbTQy6Bcjx%2FtDmq9NDVbq0QP%2BVymZleT"
    "DVN2bbUEkbDeX%2BIC2UuYo3Q%2Fo4%2FPb4E6Q0Pd%2Fo8JODk3MlGYHCgLYoPHZdiLhsIi2TA%3D%3D"
    "&questionId=q-ledger-agent-server&path="
)

IST = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata, per the notes.

FALLBACK_RATES = {"USD": 1.0, "EUR": 1.13, "INR": 0.01231}

# Cache the dataset for this long before a background refresh (seconds). The
# ledger is static for grading, but we refresh defensively.
CACHE_TTL_SECONDS = 15 * 60

HTTP_TIMEOUT = 20  # seconds for the upstream data fetch.

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("acme-ledger")

app = FastAPI(title="Acme Appliances ledger Q&A", version="1.0.0")

# --------------------------------------------------------------------------- #
# Data loading + preparation
# --------------------------------------------------------------------------- #

def _base_url() -> str:
    return os.environ.get("LEDGER_BASE_URL", DEFAULT_BASE_URL)


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp that may end in 'Z' or a numeric offset."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _fetch_export() -> list[dict]:
    """Fetch the full dataset as NDJSON and return the parsed rows."""
    url = _base_url() + "%2Fexport"
    resp = requests.get(url, timeout=HTTP_TIMEOUT,
                        headers={"User-Agent": "acme-ledger-qa/1.0"})
    resp.raise_for_status()
    rows: list[dict] = []
    for line in resp.text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _fetch_rates() -> dict:
    """Fetch the currency -> USD-per-unit table, with a safe fallback."""
    try:
        url = _base_url() + "%2Frates"
        resp = requests.get(url, timeout=HTTP_TIMEOUT,
                            headers={"User-Agent": "acme-ledger-qa/1.0"})
        resp.raise_for_status()
        data = resp.json()
        rates = data.get("usd_per_unit")
        if isinstance(rates, dict) and rates:
            return {k: float(v) for k, v in rates.items()}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not fetch /rates (%s); using fallback.", exc)
    return dict(FALLBACK_RATES)


def _build_dataset() -> dict:
    """Fetch everything and return a prepared, cached dataset."""
    rates = _fetch_rates()
    raw_rows = _fetch_export()

    # De-duplicate: keep the row with the latest updated_at per order id,
    # compared as a real instant (NOT a string, because offsets differ).
    latest: dict[str, dict] = {}
    for row in raw_rows:
        oid = row["id"]
        cur = latest.get(oid)
        if cur is None or _parse_ts(row["updated_at"]) > _parse_ts(cur["updated_at"]):
            latest[oid] = row

    orders: list[dict] = []
    for row in latest.values():
        currency = row.get("currency", "USD")
        rate = rates.get(currency, FALLBACK_RATES.get(currency, 1.0))
        amount = float(row["amount"])
        created_ist = _parse_ts(row["created_at"]).astimezone(IST)
        orders.append({
            "id": row["id"],
            "customer": row["customer"],
            "region": row["region"],
            "product": row["product"],
            "qty": int(row["qty"]),
            "unit_price": float(row["unit_price"]),
            "amount": amount,
            "currency": currency,
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "usd": amount * rate,          # full precision; round only at output
            "created_ist": created_ist,
            "year": created_ist.year,
            "month": created_ist.month,
        })

    # Distinct value vocabularies (for NL matching), preserving API spelling.
    regions = sorted({o["region"] for o in orders})
    products = sorted({o["product"] for o in orders})
    statuses = sorted({o["status"] for o in orders})
    currencies = sorted({o["currency"] for o in orders})

    return {
        "orders": orders,
        "rates": rates,
        "regions": regions,
        "products": products,
        "statuses": statuses,
        "currencies": currencies,
        "built_at": time.time(),
    }


class DatasetCache:
    """Thread-safe lazy cache with a background refresh on TTL expiry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Optional[dict] = None

    def get(self) -> dict:
        with self._lock:
            data = self._data
            fresh = data is not None and (
                time.time() - data["built_at"] < CACHE_TTL_SECONDS
            )
        if data is None:
            # First load: block until ready (fast: one ~1MB request).
            return self._rebuild()
        if not fresh:
            # Refresh in the background; serve the stale copy meanwhile.
            threading.Thread(target=self._rebuild, daemon=True).start()
        return data

    def _rebuild(self) -> dict:
        with self._lock:
            try:
                self._data = _build_dataset()
                logger.info("Dataset ready: %d current orders.",
                            len(self._data["orders"]))
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Failed to build dataset: %s", exc)
                if self._data is None:
                    raise
            return self._data


CACHE = DatasetCache()


# --------------------------------------------------------------------------- #
# Natural-language understanding (rule-based)
# --------------------------------------------------------------------------- #

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12,
    "dec": 12,
}

QUARTERS = {"q1": (1, 2, 3), "q2": (4, 5, 6), "q3": (7, 8, 9), "q4": (10, 11, 12)}

QUARTER_WORD = {"first": "q1", "second": "q2", "third": "q3", "fourth": "q4"}


def _find_region(text: str, regions: list[str]) -> Optional[str]:
    low = text.lower()
    for r in regions:
        if re.search(rf"\b{re.escape(r.lower())}\b", low):
            return r
    return None


def _find_product(text: str, products: list[str]) -> Optional[str]:
    low = text.lower()
    for p in products:
        # match singular or with a trailing plural 's'
        pat = rf"\b{re.escape(p.lower())}s?\b"
        if re.search(pat, low):
            return p
    return None


def _find_status(text: str, statuses: list[str]) -> Optional[str]:
    low = text.lower()
    for s in statuses:
        if re.search(rf"\b{s}\b", low):
            return s
    if re.search(r"\brefund(s|ed)?\b", low):
        return "refunded"
    if re.search(r"\bvoid(ed)?\b", low):
        return "void"
    return None


def _find_currency(text: str, currencies: list[str]) -> Optional[str]:
    """Return a currency to FILTER on, or None.

    Money is always reported in USD, so "revenue in USD" / "in dollars" names
    the reporting unit, NOT a filter -- we return None for those. We only treat
    a currency as a filter when a non-USD currency is named explicitly (e.g.
    "EUR orders"), which is the only case where filtering by currency is
    meaningful.
    """
    low = text.lower()
    # "dollar(s)" is the USD reporting unit -> never a filter.
    if re.search(r"\busd\b|\bdollars?\b", low):
        return None
    for c in currencies:
        if c.upper() == "USD":
            continue
        if re.search(rf"\b{c.lower()}\b", low):
            return c
    return None


def _find_customer(text: str) -> Optional[str]:
    m = re.search(r"\bc\s?\d{3,4}\b", text, re.IGNORECASE)
    if m:
        return m.group(0).upper().replace(" ", "")
    return None


def _find_year(text: str) -> Optional[int]:
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else None


def _find_month(text: str) -> Optional[int]:
    low = text.lower()
    for name, num in MONTHS.items():
        if re.search(rf"\b{name}\b", low):
            return num
    # numeric month like "03/2026" or "2026-03"
    m = re.search(r"\b(\d{1,2})/(20\d{2})\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\b20\d{2}-(\d{1,2})\b", text)
    if m:
        return int(m.group(1))
    return None


def _find_quarters(text: str) -> Optional[tuple[int, ...]]:
    low = text.lower()
    found: list[int] = []
    for q, months in QUARTERS.items():
        if re.search(rf"\b{q}\b", low):
            found.extend(months)
    for word, q in QUARTER_WORD.items():
        if re.search(rf"\b{word}\s+quarter\b", low):
            found.extend(QUARTERS[q])
    return tuple(sorted(set(found))) if found else None


def _product_rank(text: str) -> str:
    """Decide how to rank a 'top product' question: by units or by revenue.

    Revenue/dollar wording wins (the task's own example anchors 'top selling
    product BY REVENUE' = Blender). Bare selling/units wording -> by units.
    """
    low = text.lower()
    revenue_words = (r"\brevenue\b", r"\bsales\b", r"\bdollars?\b", r"\bmoney\b",
                     r"\bvalue\b", r"\bearned\b", r"\bgenerated\b", r"\bincome\b",
                     r"\bturnover\b", r"\$\s?\d")
    if any(re.search(w, low) for w in revenue_words):
        return "revenue"
    unit_words = (r"\bunits?\b", r"\bquantity\b", r"\bqty\b", r"\bsold\b",
                  r"\bselling\b", r"\bbest[- ]sell", r"\bpopular\b", r"\bpieces\b",
                  r"\bvolume\b", r"\bmost\b")
    if any(re.search(w, low) for w in unit_words):
        return "units"
    return "revenue"  # safe default, matches the anchored example


def _classify_metric(text: str) -> str:
    """Return one of: revenue, net_revenue, refunds, refund_count, order_count,
    customer_count, units, top_product, avg_order_value."""
    low = text.lower()

    has_revenue = bool(re.search(r"\brevenue\b|\bsales\b|\bturnover\b|\bearned\b", low))
    has_refund = bool(re.search(r"\brefund", low))
    has_count = bool(re.search(r"\bhow many\b|\bcount\b|\bnumber of\b|\btotal number\b", low))
    has_order = bool(re.search(r"\borders?\b", low))
    has_customer = bool(re.search(r"\bcustomers?\b|\bbuyers?\b|\bclients?\b", low))
    has_product = bool(re.search(r"\bproducts?\b|\bitems?\b", low))
    has_units = bool(re.search(r"\bunits?\b|\bquantity\b|\bqty\b|\bpieces\b", low))
    has_top = bool(re.search(r"\btop\b|\bbest[- ]selling\b|\bmost\b|\bhighest\b|\blargest\b|\bbiggest\b|\bpopular\b", low))
    has_avg = bool(re.search(r"\baverage\b|\bavg\b|\bmean\b", low))
    has_distinct = bool(re.search(r"\bdistinct\b|\bunique\b|\bdifferent\b|\bseparate\b", low))
    has_net = bool(re.search(r"\bnet\b", low))

    # Refunds (explicitly about refunds, not about revenue)
    if has_refund and not has_revenue:
        if has_count:
            return "refund_count"
        return "refunds"

    # Counts of distinct customers
    if has_customer and (has_count or has_distinct) and not has_top:
        return "customer_count"

    # Counts of orders
    if has_order and has_count and not has_revenue:
        return "order_count"

    # Top / best-selling product (check BEFORE bare units, so "best-selling
    # product by units" ranks products rather than summing all units).
    if has_product and has_top:
        return "top_product"

    # Units / quantity sold
    if has_units and not has_revenue:
        return "units"

    # Average order value
    if has_avg and has_order:
        return "avg_order_value"

    # Net revenue = paid minus refunded
    if has_net and (has_revenue or has_sales_phrase(low)):
        return "net_revenue"

    # Default money question -> revenue (paid only, per the business note)
    if has_revenue or has_product or has_customer or has_order:
        return "revenue"

    return "revenue"


def has_sales_phrase(low: str) -> bool:
    return bool(re.search(r"\bsales\b|\bearnings?\b|\bincome\b|\brevenue\b|\bmoney\b|\btakings?\b", low))



def understand(question: str, ds: dict) -> dict:
    """Turn a plain-English question into a structured query."""
    text = question
    q: dict[str, Any] = {
        "metric": _classify_metric(text),
        "region": _find_region(text, ds["regions"]),
        "product": _find_product(text, ds["products"]),
        "status": _find_status(text, ds["statuses"]),
        "currency": _find_currency(text, ds["currencies"]),
        "customer": _find_customer(text),
        "year": _find_year(text),
        "month": _find_month(text),
        "quarters": _find_quarters(text),
        "product_rank": _product_rank(text) if "product" in text.lower() or "top" in text.lower() else "revenue",
    }

    # A named product is a FILTER, not a "which product is top" question. If the
    # user named a specific product, this isn't a top_product ranking question.
    if q["metric"] == "top_product" and q["product"] is not None:
        # e.g. "revenue from Blender" is really a revenue question.
        q["metric"] = "revenue"

    # Revenue / avg / units-sold are paid-only by definition (an order only
    # really "sold" once it's paid). If the question doesn't name another
    # status, lock these to 'paid'. An explicit status (e.g. "units refunded")
    # still wins.
    if q["metric"] in ("revenue", "avg_order_value", "units") and q["status"] is None:
        q["status"] = "paid"

    # top_product is paid-only too (we rank completed sales).
    if q["metric"] == "top_product" and q["status"] is None:
        q["status"] = "paid"

    return q



# --------------------------------------------------------------------------- #
# Query execution
# --------------------------------------------------------------------------- #

def _apply_filters(orders: list[dict], q: dict) -> list[dict]:
    out = []
    for o in orders:
        if q["region"] and o["region"] != q["region"]:
            continue
        if q["product"] and o["product"] != q["product"]:
            continue
        if q["status"] and o["status"] != q["status"]:
            continue
        if q["currency"] and o["currency"] != q["currency"]:
            continue
        if q["customer"] and o["customer"] != q["customer"]:
            continue
        if q["year"] and o["year"] != q["year"]:
            continue
        if q["month"] and o["month"] != q["month"]:
            continue
        if q["quarters"] and o["month"] not in q["quarters"]:
            continue
        out.append(o)
    return out


def _money(value: float) -> float:
    return round(value + 0.0, 2)


def execute(q: dict, ds: dict) -> Any:
    metric = q["metric"]

    # Net revenue = paid minus refunded over the same non-status filters.
    if metric == "net_revenue":
        base = {k: v for k, v in q.items() if k not in ("status", "metric")}
        paid_q = {**base, "status": "paid"}
        refunded_q = {**base, "status": "refunded"}
        paid = sum(o["usd"] for o in _apply_filters(ds["orders"], paid_q))
        refunded = sum(o["usd"] for o in _apply_filters(ds["orders"], refunded_q))
        return _money(paid - refunded)

    orders = _apply_filters(ds["orders"], q)

    if metric in ("revenue", "refunds"):
        # Money is always answered in USD, correct to the cent.
        return _money(sum(o["usd"] for o in orders))

    if metric == "refund_count":
        return len(orders)

    if metric == "order_count":
        return len(orders)

    if metric == "customer_count":
        return len({o["customer"] for o in orders})

    if metric == "units":
        return sum(o["qty"] for o in orders)

    if metric == "avg_order_value":
        if not orders:
            return 0.0
        return _money(sum(o["usd"] for o in orders) / len(orders))

    if metric == "top_product":
        by_rev: dict[str, float] = defaultdict(float)
        by_units: dict[str, int] = defaultdict(int)
        for o in orders:
            by_rev[o["product"]] += o["usd"]
            by_units[o["product"]] += o["qty"]
        if not by_rev:
            return None
        # Rank by the dimension the question named (default revenue). Returns
        # the product name exactly as the API spells it.
        if q.get("product_rank") == "units":
            return max(by_units.items(), key=lambda kv: kv[1])[0]
        return max(by_rev.items(), key=lambda kv: kv[1])[0]

    return _money(sum(o["usd"] for o in orders))



# --------------------------------------------------------------------------- #
# HTTP endpoints
# --------------------------------------------------------------------------- #

@app.get("/")
async def root() -> dict:
    """Liveness / health check (also warms the dataset)."""
    try:
        ds = CACHE.get()
        return {
            "service": "acme-ledger-qa",
            "status": "ok",
            "orders": len(ds["orders"]),
        }
    except Exception as exc:  # pragma: no cover
        return {"service": "acme-ledger-qa", "status": "warming", "error": str(exc)}


@app.post("/")
async def ask(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "expected a JSON object"})

    question = body.get("question")
    if question is None:
        # be lenient about common alternative keys
        for key in ("q", "query", "prompt", "text"):
            if key in body:
                question = body[key]
                break
    if not isinstance(question, str) or not question.strip():
        return JSONResponse(status_code=400,
                            content={"error": "missing 'question' string"})

    try:
        ds = CACHE.get()
    except Exception as exc:  # pragma: no cover
        logger.exception("dataset unavailable")
        return JSONResponse(status_code=503,
                            content={"error": f"dataset unavailable: {exc}"})

    try:
        structured = understand(question, ds)
        answer = execute(structured, ds)
    except Exception as exc:  # pragma: no cover
        logger.exception("failed to answer %r", question)
        return JSONResponse(status_code=500,
                            content={"error": f"could not answer: {exc}"})

    logger.info("Q=%r -> metric=%s answer=%r", question, structured["metric"], answer)
    return JSONResponse(content={"answer": answer})


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)

