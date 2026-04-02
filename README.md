# Small Business Intelligence App

This app provides a dashboard workflow for lead intake, approvals, scanning, and campaign output.

## Features

- URL and phone lead intake
- Approval gate for assistant-proposed leads
- Scan execution using the existing campaign generator
- Lead detail dashboard with summaries and recommended fixes
- Links to JSON/Markdown reports
- Demo URL field generation
- Sales outreach email draft generation
- Role-based access (owner/collaborator)
- Assistant proposal API that always queues `pending_approval`
- Salted hashed access keys stored per user account in SQLite
- Dedicated owner-only user admin area (`/admin/users`)

## Easy Local Network Mode (Temporary Convenience)

- Env var: `INTEL_APP_TRUST_NETWORK_LOGIN`
- Default: `1` (enabled)
- Behavior: trusted local/private network clients are treated as owner for browsing and admin tasks when no session is present.

Use this for local exploration. Before production, set:

- `INTEL_APP_TRUST_NETWORK_LOGIN=0`

Then require explicit login for all protected routes.

## Change Your Key After Login

- URL: `/account`
- Use the "Change Access Key" form (current key + new key confirmation)
- New key is hashed and stored in DB.

## Run

Use:

powershell -ExecutionPolicy Bypass -File C:\Users\Luke\projects\Business_Workbench\run_intel_app.ps1

Then open:

http://127.0.0.1:8090

Default bootstrap credentials (change for production):

- Owner username/key: `owner` / `owner-change-me`
- Collaborator username/key: `collaborator` / `collab-change-me`
- Assistant ingest API key: `assistant-ingest-change-me`

Env vars for bootstrap users:

- `INTEL_APP_BOOTSTRAP_OWNER_USERNAME`
- `INTEL_APP_BOOTSTRAP_OWNER_KEY`
- `INTEL_APP_BOOTSTRAP_COLLAB_USERNAME`
- `INTEL_APP_BOOTSTRAP_COLLAB_KEY`

## Data Storage

- SQLite DB: Business_Workbench/intel_app/intel_app.db
- Reports: Business_Workbench/audit_reports/campaigns/

## Approval Workflow

- Set `Source = assistant` and `Approval Required = Yes` when adding a lead.
- Lead stays in `pending_approval` until approved.
- On approval, scan executes and report artifacts are generated.

## Assistant Proposal API

Endpoint:

- `POST /api/leads/propose`

Header:

- `X-Assistant-Key: <INTEL_APP_ASSISTANT_KEY>`

Body:

{
	"url": "https://example.com",
	"phone": "+1 954 555 1234",
	"client_name": "Example Co",
	"notes": "Proposed by assistant"
}

Behavior:

- Lead is created as `pending_approval`.
- Owner approval in UI is required before scan can run.

## Dedicated User Admin Area

- URL: `/admin/users` (owner only)
- Features:
	- Create user account
	- Change user role
	- Reset user access key (re-hashed)
	- Activate/deactivate user account

Safety rules:

- Collaborators cannot access `/admin/users`
- Last active owner cannot be demoted/deactivated
- Owner cannot deactivate their own account
