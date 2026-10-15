# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Trainee assignment. Application code lives in `app/main.py`.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the environment, then run:

```bash
uvicorn app.main:app --port 8000
```

`GET /health` pings MongoDB. Indexes are created idempotently at startup. Pure rule checks can be run with `python -m unittest discover -s tests`; endpoint and aggregation integration tests should also be run against MongoDB 6.0+. Do not commit `.env` files, credentials, database dumps, a Dockerfile, or a virtual environment.

## Coverage

Employee CRUD/listing, race-safe punch-in and punch-out, audited regularization, monthly and department analytics, competition-ranked late leaderboard, date-gap-filled department trend, and execution-statistics explain endpoint are implemented. Before submitting, test with MongoDB 6.0+ and the edge cases in the assignment, especially concurrent requests and `IXSCAN` plans. Read `REVIEW.md` and `DECISIONS.md` carefully so you can explain the design in the live walkthrough.
