# Acme Appliances — Order Ledger Q&A Service

A public HTTPS endpoint that answers plain-English questions about the Acme
Appliances order ledger (revenue, refunds, products, customers).

```
POST {"question": "What was the total revenue in USD from the North region in March 2026?"}
->   {"answer": 37382.56}
```

Money is in USD, correct to the cent. Counts are integers. Product names are
returned exactly as the upstream API spells them.

## The undocumented API (reverse-engineered)

| Endpoint                | What it returns |
| ----------------------- | --------------- |
| `…&path=/`              | Descriptor + links + **3 business notes** (below) |
| `…&path=/orders?page=N` | Paginated JSON: 86 pages × 50 rows = 4251 rows |
| `…&path=/rates`         | `{"usd_per_unit": {"USD":1,"EUR":1.13,"INR":0.01231}}` |
| `…&path=/export`        | The **entire dataset as NDJSON in one ~1 MB request** |

The three business notes (from the root descriptor) drive every calculation:

1. **Business dates use the Asia/Kolkata timezone.**
2. **An order id can appear more than once; the row with the latest
   `updated_at` is current.**
3. **Only orders with status `paid` count as revenue.**

### Data quirks handled

* **De-duplication.** 251 order ids appear twice; every pair differs in
  `amount`, and 45 also differ in `status` (e.g. `paid` → `refunded`). We keep
  only the row with the latest `updated_at`, compared as a **real instant** —
  timestamps mix `Z` (UTC) and `+05:30` (IST), so a naive string sort is unsafe.
* **Timezone.** 18 rows change calendar month when converted UTC → IST, so all
  date bucketing is done in Asia/Kolkata.
* **Money.** `amount == qty × unit_price` always. Each row is converted to USD
  by its currency's rate; the sum is rounded to the cent **once** at the end.
* **Performance.** We fetch `/export` once at startup (~1 MB) rather than 86
  paginated calls, so the whole dataset is in memory and each question is
  answered in ~15 ms — far inside the 15 s grading budget.

## How questions are understood

A deterministic, rule-based NL parser (no external LLM call at question time,
so answers are fast and reproducible). It extracts:

* **metric** — revenue, refunds, refund count, order count, distinct customers,
  units sold, top product, average order value, net revenue.
* **filters** — region, product (fuzzy; exact spelling on output), customer
  `C####`, status, non-USD currency, and date (month+year, year, `Q1`…`Q4`,
  "first quarter"…).

Default status rules (from the business notes): revenue, average order value,
units-sold and top-product are **paid-only** unless the question names another
status (e.g. "units refunded"). "in USD" is treated as the reporting unit, never
a filter.

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000

# offline regression suite (uses a local /tmp/data.ndjson snapshot, no network)
python3 test_local.py
```

## Deploy to Render

This repo includes a Render Blueprint (`render.yaml`) and a `Dockerfile`
(optional). Two ways:

### 1) One-command push + deploy (recommended)

```bash
export GITHUB_TOKEN=ghp_xxx     # classic PAT with `repo` scope
python deploy_and_verify.py push tds-ga1-q15-ledger-qa
```

Then in the [Render dashboard](https://dashboard.render.com):
**New + → Blueprint**, select the repo, **Apply**. Render reads `render.yaml`
and creates a web service (build `pip install -r requirements.txt`, start
`uvicorn main:app --host 0.0.0.0 --port $PORT`, health check `/`).

### 2) Manual

Create a **Web Service** (Python runtime) from the repo and set:

* **Build command:** `pip install -r requirements.txt`
* **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
* **Health check path:** `/`

Render provisions a public HTTPS URL like `https://<service>.onrender.com`.
Submit that URL.

### Verify the live endpoint like the grader

```bash
python deploy_and_verify.py verify https://<service>.onrender.com
```

This POSTs a battery of phrasings and checks each answer + latency.

> **Free-tier note:** Render free web services spin down after ~15 min of
> inactivity and take a few seconds to cold-start (plus ~1 s for the first
> dataset fetch). The service warms its dataset on the first request; keep it
> awake through the grading deadline.

## Files

| File | Purpose |
| ---- | ------- |
| `main.py` | FastAPI service: fetch + prepare data, NL parser, query execution |
| `requirements.txt` | fastapi, uvicorn, requests |
| `render.yaml` | Render Blueprint |
| `Dockerfile` | Optional Docker deploy |
| `deploy_and_verify.py` | GitHub push helper + live verifier |
| `test_local.py` | Offline regression suite |
| `.gitignore` | Ignore caches/venv |
