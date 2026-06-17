Neo 
==========

Run the API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Run the React/Tailwind frontend:

```powershell
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/api` requests to `http://127.0.0.1:8000`.
