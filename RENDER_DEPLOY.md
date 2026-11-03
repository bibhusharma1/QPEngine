# Deploying MasterDeOrca on Render (Docker + SQLite)

This project uses SQLite for both the database and BLOB storage. All uploads, artifacts, and job state are stored in the SQLite file. You must attach a persistent disk on Render and point the app to it.

## 1) Prerequisites
- A GitHub repository with this code (after pushing)
- A Render account
- A strong random value for `QUES_JWT_SECRET` (>= 32 chars)
- If using `PARSER_MODE=llm` (NVIDIA API), API keys for NVIDIA
- If using `PARSER_MODE=local` (legacy local pipeline), GPU is not available on standard Render web services; prefer `PARSER_MODE=llm`

## 2) Create a Web Service
1. In Render, go to New → Web Service → Connect your repo
2. Runtime: **Docker**
3. Render will use the `Dockerfile` in the repo root
4. Region/instance type as needed

## 3) Add a Persistent Disk
1. In the service settings → Disks → Add Disk
2. Mount Path: `/data`
3. Size: choose appropriately (depends on number of PDFs/uploads)
4. This disk must persist across deploys/restarts

## 4) Required Environment Variables
Set these in Render → Environment:

| Variable | Value | Notes |
| --- | --- | --- |
| `APP_ENV` | `local` | Required by app startup checks |
| `DATABASE_BACKEND` | `sqlite` | Must be sqlite |
| `STORAGE_BACKEND` | `sqlite` | Must be sqlite (all blobs in DB) |
| `DATABASE_PATH` | `/data/masterdeorca.sqlite` | Path on the persistent disk |
| `QUES_STORAGE_ROOT` | `/data` | Storage root on persistent disk |
| `QUES_JWT_SECRET` | `<long-random-secret>` | >= 32 chars, required |
| `QUES_ADMIN_EMAIL` | `admin@example.com` | Initial admin email (created on first start) |
| `QUES_ADMIN_PASSWORD` | `<strong-password>` | Initial admin password (created/updated on first start) |
| `PORT` | `8000` | Render sets PORT, but safe to keep |
| `PARSER_MODE` | `llm` or `local` | `llm` recommended on Render (uses NVIDIA API) |
| `CORS_ORIGINS` | e.g. `https://your-frontend.onrender.com,http://localhost:3000` | Comma-separated allowed origins |
| `QUES_RECONCILE_ENABLED` | `true` | Optional |
| `QUES_RECONCILE_INTERVAL_HOURS` | `12` | `12` or `24` only |
| `QUES_TOTP_ENCRYPTION_KEY` | `<base64/url-safe-key>` | Required if using TOTP features |

### For `PARSER_MODE=llm` (recommended)
| Variable | Value |
| --- | --- |
| `NVIDIA_API` or `NVIDIA_API_KEYS` | Your NVIDIA API key(s) |
| `NVIDIA_PARSE_MODEL` | `nvidia/nemotron-3.5-parse` (default) |
| `NVIDIA_STRUCTURE_MODEL` | `nvidia/nemotron-3.5-super-vl-120b-a12b` (default) |

### For `PARSER_MODE=local` (legacy)
Not recommended on Render without GPU. If used, you must ensure compatible deps are available; the provided Dockerfile is minimal (no OCR GPU extras). Prefer `llm` mode.

## 5) Build & Start Settings
- **Build Command:** (leave empty, Docker handles build)
- **Start Command:** (leave empty, Dockerfile CMD runs uvicorn)
- The Dockerfile binds to `0.0.0.0:$PORT` which is correct for Render

## 6) Deploy
1. Commit and push your changes
2. Create/update the Web Service on Render with the above settings
3. On first start, the app will create tables/migrations and initialize the admin user from `QUES_ADMIN_EMAIL`/`QUES_ADMIN_PASSWORD`
4. Check logs for successful startup. Hit `https://<your-service>.onrender.com/health` to verify

## 7) Notes
- **SQLite only:** The app enforces `DATABASE_BACKEND=sqlite` and `STORAGE_BACKEND=sqlite`. All files are stored as BLOBs in the DB.
- **Migrations run on startup:** `database.initialize()` applies all migrations automatically.
- **Persistent disk required:** If you don't mount `/data`, the DB will be lost on redeploy/restart.
- **Do not set legacy file paths as primary storage:** Everything goes through SQLite BLOB storage as implemented.
- **Security:** Use a strong JWT secret and admin password in production. Rotate if needed.
