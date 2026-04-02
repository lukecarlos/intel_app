# Deployment Guide - Small Business Intelligence App

## Required Environment Variables

- `INTEL_APP_BOOTSTRAP_OWNER_USERNAME`
- `INTEL_APP_BOOTSTRAP_OWNER_KEY`
- `INTEL_APP_BOOTSTRAP_COLLAB_USERNAME`
- `INTEL_APP_BOOTSTRAP_COLLAB_KEY`
- `INTEL_APP_ASSISTANT_KEY` (API key for assistant lead proposals)
- `INTEL_APP_SESSION_SECRET` (session signing secret)
- `INTEL_APP_TRUST_NETWORK_LOGIN` (`0` for production hardening)

## Railway

1. Create a new Railway service from this repo.
2. Set start command:

python -m uvicorn Business_Workbench.intel_app.app:app --host 0.0.0.0 --port $PORT

3. Set required environment variables.
4. Deploy and verify `/health`.

## Render

1. Create a new Web Service from this repo.
2. Build command:

pip install -r Business_Workbench/intel_app/requirements.txt

3. Start command:

python -m uvicorn Business_Workbench.intel_app.app:app --host 0.0.0.0 --port $PORT

4. Add environment variables and deploy.

## Fly.io

1. Launch app from repo root.
2. Build using Dockerfile: `Business_Workbench/intel_app/Dockerfile`.
3. Set env vars using `fly secrets set`.
4. Expose service and deploy.

## Assistant Lead Proposal API

POST `/api/leads/propose`

Headers:
- `X-Assistant-Key: <INTEL_APP_ASSISTANT_KEY>`

Body JSON:

{
  "url": "https://example.com",
  "phone": "+1 954 555 1234",
  "client_name": "Example Co",
  "notes": "Proposed by assistant"
}

Behavior:
- Lead is always created as `pending_approval`.
- Owner must approve through UI before scan runs.
