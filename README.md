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

### Quick Start (Backend)

NOTE: Ensure that you have a provisioned AWS IAM User that is allowed to provision the following services (AWS Kinesis, AWS Lambda, AWS S3 Bucket, AWS DynamoDB, AWS Sagemaker)

0. Ensure Docker (building os-specific dependencies) and Terraform is Installed
1. build tf script...
2. run tf script...



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


### Repository Structure

```
frontend/              # React + TS dashboard (simulation)
src/                   # (Future) Python backend / processing code
models/                # Model artifacts or references
data/                  # Sample or synthetic datasets
docs/                  # Design notes, diagrams, research
bin/                   # Utility scripts
```


---

This README will evolve as backend components are added.