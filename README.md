Neo 
==========

LLM providers
-------------

Neo can use any number of configured models. Native Ollama and OpenAI-compatible APIs are
supported; the latter includes hosted APIs and local servers such as LM Studio, vLLM, and
LocalAI. The active model can be changed from the chat composer.

On first start, Neo exposes the existing `NEO_CHAT_MODEL` / `NEO_OLLAMA_URL` settings as
`ollama-default`. Add providers with `PUT /api/llms/{id}`. Configurations are stored locally in
`neo_llms.json` (override with `NEO_LLM_CONFIG_PATH`). API keys are never returned by the API;
prefer `api_key_env` so only the environment variable name is stored.

Example OpenAI-compatible configuration:

```json
{
  "id": "my-api-model",
  "name": "My API model",
  "provider": "openai_compatible",
  "model": "provider-model-name",
  "base_url": "https://provider.example/v1",
  "api_key_env": "MY_PROVIDER_API_KEY",
  "enabled": true,
  "timeout_seconds": 240,
  "num_predict": 512
}
```

Run everything:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-dev.ps1
```

Keep that terminal open; it runs the backend server.

Run the API:

```powershell
& "C:\Program Files\Python313\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Run the React/Tailwind frontend:

```powershell
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/api` requests to `http://127.0.0.1:8000`.

For the single-container deployment, see [docs/deployment.md](docs/deployment.md).
