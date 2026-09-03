# SIH26122 — Infrastructure Schedule Intelligence

Foundation for the SIH 2026 prototype. It currently contains independent React and FastAPI applications only; no product features have been implemented.

## Project structure

```
frontend/    React + Vite client
backend/     FastAPI service
```

## Prerequisites

- Node.js 20.19+ (or 22.12+)
- Python 3.11+

## Run the backend

From the repository root:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

If PowerShell blocks activation, run the final command with the virtual environment interpreter instead:

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

The API runs at `http://127.0.0.1:8000`. Check `GET /health` or open `http://127.0.0.1:8000/docs`.

## Database configuration

The backend reads its PostgreSQL connection from `DATABASE_URL`. Copy
`backend/.env.example` to `backend/.env` and set this value to the Supabase
PostgreSQL connection string. Do not commit `.env` or expose this value to the
frontend.

With `DATABASE_URL` configured, check `GET /health/database` to verify that the
API can connect to PostgreSQL. The existing `GET /health` endpoint remains a
database-independent service check.

## Run the frontend

In a second terminal, from the repository root:

```powershell
cd frontend
npm install
npm run dev
```

Open the local URL printed by Vite (normally `http://localhost:5173`).

## Current scope

The foundation deliberately does not yet include PostgreSQL configuration, LLM integration, schedule ingestion, authentication, Docker, or application workflows. Add those only when their concrete requirements are defined.
