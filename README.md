# Todoist Reference Transfer

Transfers active Todoist tasks labelled `@reference` to Workflowy through a SQLite-backed queue. The collector only enqueues; the worker creates/updates Workflowy and safely deletes Todoist after confirmation.

## Setup

Inside WSL, install [uv](https://docs.astral.sh/uv/), copy `.env.example` to `.env`, and fill in a Todoist API token plus Workflowy API key. Do not commit `.env`.

```bash
mkdir -p data logs
uv sync --locked
uv run reference-transfer collect
uv run reference-transfer work
uv run --with pytest pytest -q
```

## Cron

Use one non-blocking lock shared by both schedules. Replace paths with absolute WSL paths.

```cron
0 * * * * flock -n /tmp/reference-transfer.lock bash -lc 'cd /path/to/reference-transfer && /path/to/uv run reference-transfer collect >> logs/transfer.log 2>&1'
* * * * * flock -n /tmp/reference-transfer.lock bash -lc 'cd /path/to/reference-transfer && /path/to/uv run reference-transfer work >> logs/transfer.log 2>&1'
```

The worker claims the 10 oldest ready items, works sequentially, limits Todoist and Workflowy independently to 20 requests/minute by default, honors server retry instructions, and uses full-jitter exponential backoff for transient failures.

## Optional Docker Compose Deployment

Use Docker Compose later when you want long-running, restartable collector and worker services. Both containers share a named volume for the SQLite queue and mappings.

```bash
cp .env.example .env
# Fill in TODOIST_API_TOKEN and WORKFLOWY_API_KEY in .env.
docker compose up -d --build
docker compose logs -f collector worker
```

If a one-shot worker is intentionally stopped, return its claimed jobs to the queue before restarting it:

```bash
docker compose run --rm worker reference-transfer recover
```

Run the containerized test suite with:

```bash
docker compose --profile test run --rm test
```

Stop the services with `docker compose down`. Queue state is retained in the `transfer-data` volume; use `docker compose down -v` only when intentionally discarding that state.
