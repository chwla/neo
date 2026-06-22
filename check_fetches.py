import requests
r = requests.get("http://127.0.0.1:8000/api/research/list")
jobs = r.json().get("jobs", [])
if jobs:
    job_id = jobs[0]["id"]
    job = requests.get(f"http://127.0.0.1:8000/api/research/{job_id}").json()
    sources = job.get("sources", [])
    print(f"Job: {job.get('user_query', '')} [{job.get('depth', '')}]")
    print(f"Total sources: {len(sources)}")
    for s in sources[:8]:
        url = s.get("url", "")
        fs = s.get("fetch_status", "")
        fe = s.get("fetch_error", "")
        hs = s.get("http_status", "")
        print(f"\n  URL: {url[:80]}")
        print(f"  fetch_status={fs}, http_status={hs}")
        print(f"  fetch_error: {fe[:120]}")
else:
    print("No jobs found")
