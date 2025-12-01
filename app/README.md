# AdGen AI Platform — Internal Next.js App

This folder contains a Next.js (App Router) + TypeScript + Tailwind CSS scaffold for the AdGen AI Platform. The app is intentionally UI-only at the moment — no business logic or external integrations exist yet.

What was added:

- App Router structure in `app/` with the full campaign flow: `campaigns/create`, `campaigns/[id]/formats`, `variations`, `select-variation`, `final`, and `view`.
- Placeholder API route handlers under `app/api` (empty for now).
- UI components in `components/` and types and DB schema placeholders in `lib/`.

API endpoints (local file-backed, placeholders for real integrations):

- POST /api/campaigns — create a campaign record. Body: { name, client }
- POST /api/upload/logo — multipart/form-data: campaignId, file (single)
- POST /api/upload/image — multipart/form-data: campaignId, file (single)
- DELETE /api/upload/logo?id=ASSET_ID — delete logo asset and file
- DELETE /api/upload/image?id=ASSET_ID — delete image asset and file

Files are saved under app/public/uploads/campaigns/{campaignId}/logo and /images.

Example (create campaign):

```
curl -X POST -H "Content-Type: application/json" -d '{"name":"My Campaign","client":"ACME"}' localhost:3000/api/campaigns
```

Example (upload):

```
curl -X POST -F "campaignId=<id>" -F "file=@./logo.png" localhost:3000/api/upload/logo
```

How to run locally (from inside this folder):

```bash
# install dependencies
npm install

# run dev
npm run dev
```

Note: Depending on your host repo you might want to run this app in its own container or inside a monorepo workspace. This is an internal scaffold and intentionally contains no secrets or integrations.
