# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

## Run
```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or copy .env.example to .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). `GET /health` returns 200 once MongoDB answers.

## Notes
- Everything in the contract is implemented, including `/admin/explain/{endpoint}`.
- Department trend builds its day series inside MongoDB with `$range` (no Python loop); `moving_avg_7d` uses `$setWindowFields`; the late leaderboard uses `$rank`.
- `moving_avg_7d` averages the exact (unrounded) daily rates and rounds the result half-up to 4 decimals.
- `tools/verify_analytics.py` is an optional self-check that compares the analytics with a plain-Python oracle on a real MongoDB; it is not needed to run the service.
