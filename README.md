# Employee Attendance API

A small attendance and analytics API built with FastAPI and MongoDB. The API code is in `app/main.py`.

## Run it on Windows

Start MongoDB first. If you use Docker, this starts MongoDB 7 in the background:

```powershell
docker run -d --name mongo-verify -p 27017:27017 mongo:7
```

Then, from the project folder, install the packages and start the API:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

Open `http://localhost:8000/health`. A healthy response is `{"status":"ok"}`. The interactive API page is at `http://localhost:8000/docs`.

If port 8000 is already being used, the API may already be running. Check `/health` in your browser, or stop the other server before starting another one.

## Run the checks

In a second PowerShell window, go to the project folder and activate the virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
pip install mongomock pytest httpx
python -m pytest tools/test_local.py -q
```

To compare analytics against the included Python oracle on a real MongoDB, set the verification database and run:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "verify_db"
python tools/verify_analytics.py --big
```

The verifier prints an `OK` for each explain plan and ends with `ALL ANALYTICS MATCH THE ORACLE` when the results match.

## Project files

- `app/main.py` — API routes, attendance rules, and MongoDB analytics.
- `tools/test_local.py` — quick tests that use `mongomock`.
- `tools/verify_analytics.py` — real-MongoDB analytics check and Python oracle.
- `DECISIONS.md` — notes on implementation choices and indexes.
- `REVIEW.md` — review checklist.

Business-rule labels such as R1–R10 refer to the assignment requirements.

