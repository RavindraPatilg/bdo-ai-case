# Deployment: how the cloud environment is set up

The application is deployed on **Render.com** as a Docker container (free tier, Frankfurt region).
Render was chosen over Azure Container Apps because it deploys directly from the Git repository
with a free tier, no credit card, and a managed Docker build; the application itself is
cloud-native and platform-agnostic (standard FastAPI + Docker image).

## Live environment

* URL: https://bdo-ai-case.onrender.com
* Health check: `/health` returns `{"status": "ok", ...}`
* Model: `openai/gpt-oss-20b` served by Groq (OpenAI-compatible API, free tier)
* LLM base URL: `https://api.groq.com/openai/v1`

## Setup steps (as performed)

1. Push the repository to GitHub (`RavindraPatilg/bdo-ai-case`).
2. On Render: New + -> Web Service -> connect the repo.
3. Runtime: Docker (detected from `Dockerfile`), Region: Frankfurt, Instance type: Free.
4. Environment variables set in the Render dashboard (secrets never in Git):

   * `LLM\_PROVIDER=openai` (means: any OpenAI-compatible provider)
   * `OPENAI\_API\_KEY=<Groq key>` (stored as a Render environment secret)
   * `OPENAI\_MODEL=openai/gpt-oss-20b`
   * `OPENAI\_BASE\_URL=https://api.groq.com/openai/v1`
5. Render builds the image from `Dockerfile` and starts the container. The Dockerfile binds to
the platform-provided `PORT` (fallback 8000 for local runs), so the same image works locally
and in the cloud.

Infrastructure as code: `render.yaml` in the repository root describes the service, region,
plan, health check and environment variables (the API key is declared with `sync: false` so it
is only entered in the dashboard).

## Local run

```bash
pip install -r requirements.txt
python -m pytest tests/ -q        # 5 offline tests, no external calls
uvicorn app.main:app --port 8000  # .env decides mock vs real provider
```

## Costs

Free tier only: Render free instance (sleeps after 15 minutes idle, cold start \~1 minute),
Groq free tier for the model. No paid resources are used. For a production setup the README
lists what should be added (persistent database, identity, telemetry).

