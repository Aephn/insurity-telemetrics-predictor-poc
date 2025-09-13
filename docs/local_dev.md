# Local Development Guide

This document explains how to run the pricing/dashboard experience completely locally without deploying to AWS.

## Overview
Components you can run locally:
- Mock Dashboard Server (`scripts/local/mock_dashboard_server.py`) – synthetic evolving snapshot + recent events.
- Frontend (Vite/React) – consumes the mock server or real API.
- Telemetry Event Injection Script (`scripts/test_api_endpoint.py`) – generate or replay events to the cloud ingestion API (optional when focusing purely local).

## 1. Environment Setup
Create & activate a virtual environment (Python 3.11+ recommended):
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .  # if editable install needed for shared packages
```
(If dependencies are already vendored in Lambda bundles you may not need `pip install -e .`.)

Optional: Set base premium and other overrides:
```bash
export BASE_PREMIUM=190
```

## 2. Run the Mock Dashboard Server
The server exposes two endpoints:
- `GET /api/dashboard` – JSON dashboard snapshot with history, currentFactors, recentEvents.
- `GET /healthz` – simple health check.

Launch (default random mode):
```bash
python scripts/local/mock_dashboard_server.py
```

### Driver Behavior Modes
Mutually exclusive flags influence how risk & events evolve each refresh:
- `--bad-driver`      – Gradually worsens factors (risk & premium trend up).
- `--good-driver`     – Gradually improves factors (risk & premium trend down).
- `--random-driver`   – Random walk (default if no flag supplied).
- `--port <PORT>`     – Custom listening port (default 8787).

Examples:
```bash
python scripts/local/mock_dashboard_server.py --bad-driver
python scripts/local/mock_dashboard_server.py --good-driver --port 9090
python scripts/local/mock_dashboard_server.py --random-driver
```

State is **persistent in-memory** for the life of the process:
- First request seeds a synthetic dataset.
- Subsequent requests mutate latest month metrics and append one new event.
- Rolling recentEvents buffer (max 200; response shows last 50) accumulates over time.

Returned JSON adds:
- `mode`: `bad` | `good` | `random`
- `recentEventsCount`: number of retained events
- `lastMutationTs`: epoch seconds of last mutation

## 3. View in Browser / Frontend Integration
Start the frontend dev server (in a separate terminal):
```bash
cd src/frontend
npm install   # first time only
npm run dev
```
By default the frontend is likely pointing at the deployed API. To point it at the local mock server, either:
1. Set an environment variable / config (if supported) OR
2. Temporarily modify the API base URL in `src/frontend/src/services/api.ts` to `http://localhost:8787/api`.

Open the frontend in the browser (Vite usually prints the local URL, e.g. `http://localhost:5173`).
Refresh the Dashboard page to see evolving events; each refresh triggers one mutation on the server side.

## 4. Telemetry Event Injection (Optional)
If you still want to push events into the live cloud ingestion (Kinesis → feature extraction → DynamoDB path ):
Edit config at top of `scripts/test_api_endpoint.py` or set env vars (e.g., `API_ROOT`, `API_STAGE`). Run:
```bash
python scripts/test_api_endpoint.py
```
### Deterministic / Direct Event Controls
Inside `test_api_endpoint.py`:
- `RANDOM_SEED` – fixed seed for reproducibility.
- `FIXED_DRIVER_ID` – single driver id for all events.
- `FIXED_EVENT_TYPE` – lock event type.
- `USE_VARIANTS=False` – stable IDs (driver/trip/event).
- `DIRECT_EVENTS_FILE` – path to a JSON array of explicit events.

Example direct file run:
```bash
# Put events in scripts/local/events_sample.json
python scripts/test_api_endpoint.py  # with DIRECT_EVENTS_FILE set inside script
```

## 5. Adjusting Base Premium Locally
Pricing & snapshot code use `BASE_PREMIUM` (default 190). Override:
```bash
export BASE_PREMIUM=200
python scripts/local/mock_dashboard_server.py --random-driver
```

## 6. Resetting Local State
Currently the mock server does not expose a reset endpoint. To clear accumulated events & regenerate the base snapshot, just restart the process (`Ctrl+C` then re-run). If you want a `/api/reset` route, it can be added later.

## 7. Troubleshooting
| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: src` | Running server without repo root on PYTHONPATH | The script auto-injects repo root; ensure you didn't move it. |
| No events accumulating | Hitting a cached frontend without reloading | Manual refresh; each request triggers one mutation. |
| Frontend still shows old base premium | Cached build / not using env | Restart frontend after exporting `BASE_PREMIUM`. |
| Port already in use | Another process bound to 8787 | Use `--port 9090` (or another). |

## 8. JSON Shape (Excerpt)
```json
{
  "profile": {"id":"D1234","basePremium":190,...},
  "history": [ {"month":"2025-09","premium":205.4,"factors":{...}} ],
  "recentEvents": [ {"id":"evt_hardBraking_...","timestamp":"2025-09-13T01:21:00Z","type":"hardBraking","value":7} ],
  "currentFactors": {"hardBraking":8,"aggressiveTurning":2,...},
  "mode":"bad",
  "recentEventsCount": 42,
  "lastMutationTs": 1694568061
}
```

## 9. Summary
You can iterate rapidly without AWS by:
1. Starting mock server with desired mode.
2. Running frontend dev server pointing to local API.
3. (Optionally) Injecting real telemetry to cloud separately.

That’s it—happy local hacking.


