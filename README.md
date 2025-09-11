## Telematics Integration in Auto Insurance (Prototype)

This repository contains a prototype implementation for a telematics-enabled, usage-based auto insurance platform. It includes (in progress) backend/data/model scaffolding and a newly added TypeScript + React dashboard frontend (simulation) that demonstrates how driver behavior can feed into dynamic premium pricing.

### Key Goals

- Fairer pricing via behavior-based risk assessment
- Transparency for policyholders (see how driving impacts premiums)
- Extensible architecture for real telematics ingestion, ML scoring, and pricing engines

### Frontend Dashboard (Simulation)

Folder: `frontend/`

Implements a lightweight Vite + React + TypeScript single-page dashboard with:

- Current premium & month-over-month delta
- Composite Safety Score (toy calculation) & raw factor metrics:
	- Hard braking
	- Aggressive turning
	- Following distance
	- Excessive speeding
	- Late-night driving
- Historical chart (premium vs. safety score)
- Live (simulated) driving events feed streaming every few seconds
- Simple gamification badges (placeholder logic)

All telematics and scoring data in the frontend are randomly simulated for demonstration. Replace the functions in `frontend/src/services/api.ts` with authenticated API calls to your backend when ready.

### Quick Start (Frontend)

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173/ in your browser.

Production build:

```bash
npm run build
npm run preview
```

### Architecture Notes

| Layer | Purpose | Status |
|-------|---------|--------|
| Data Ingestion | Collect telematics (GPS, accelerometer, speed) | Simulated only (frontend) |
| Processing / Feature Store | Clean & aggregate trip metrics | Not yet implemented |
| Risk Scoring | ML / heuristic safety scoring | Toy scoring in `scoring.ts` |
| Pricing Engine | Adjust premium based on score & base rate | Simple proportional adjustment |
| User Dashboard | Visual transparency & engagement | Implemented (simulation) |

### Scoring (Prototype Logic)

`safetyScore = 100 - Σ(weight_i * normalized_factor_i)` with heuristic weights. Real implementation should:

1. Normalize features per driver cohort.
2. Use statistically validated / ML model (e.g., Gradient Boosted Trees, GLM, or hybrid deep feature extractor feeding a calibrated regressor).
3. Include confidence intervals or explainability artifacts.

### Extending This Prototype

1. Backend API (REST / GraphQL) endpoint examples:
	 - `GET /driver/{id}/dashboard` → current premium, safety factors, history
	 - `GET /driver/{id}/events?since=...` → incremental event polling (or WebSocket stream)
	 - `POST /telematics/ingest` → secure ingestion of raw events
2. Replace `startEventStream` with real WebSocket.
3. Persist historical monthly aggregates in a warehouse (e.g., Snowflake, BigQuery) and expose via a caching layer.
4. Swap toy scoring with a model service (FastAPI / gRPC) returning versioned score + reason codes.
5. Add authentication (OIDC) & per-policy RBAC.
6. Harden privacy: pseudonymize driver identifiers & consider differential privacy where appropriate.

### Data & Models

`/models` can store model weights or pointers. None committed yet.

`/data` may contain small anonymized samples (currently empty) for local experimentation. Avoid committing raw PII / VIN / GPS traces.

### Repository Structure

```
frontend/              # React + TS dashboard (simulation)
src/                   # (Future) Python backend / processing code
models/                # Model artifacts or references
data/                  # Sample or synthetic datasets
docs/                  # Design notes, diagrams, research
bin/                   # Utility scripts
```

### Evaluation Guidance

Focus areas for future completeness:

- Model selection transparency & statistical validation
- Latency & freshness of risk updates
- Cost efficiency of data pipeline
- User behavior change (score uplift over time)

### Next Steps (Suggested Roadmap)

1. Define backend domain models & schemas (Driver, Trip, Event, MonthlyAggregate, PolicyPricingRevision).
2. Implement ingestion API & streaming pipeline (Kafka / Kinesis) → feature store.
3. Train baseline risk model (e.g., LightGBM) with synthetic dataset; track in MLflow.
4. Add WebSocket events -> live frontend updates.
5. Implement badge logic from real KPIs.
6. Add unit/integration tests & CI pipeline.

---

This README will evolve as backend components are added.

