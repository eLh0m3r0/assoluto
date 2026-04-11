# SME Client Portal

Zákaznický portál pro malé a střední výrobní firmy.
Transparentní objednávkový proces mezi dodavatelem (výrobcem) a jeho klienty,
katalog produktů a evidence klientského majetku uloženého u dodavatele.

## Features (MVP)

- **Objednávky**: klient zadá položky a přílohy, dodavatel nacení,
  navrhne termín, aktualizuje stav. Komentáře, audit trail stavů.
- **Katalog produktů**: per tenant nebo per customer; výběr do objednávky
  místo free-textu.
- **Správa majetku klienta** (materiál/nástroje) u dodavatele: příjem,
  výdej, spotřeba, inventurní korekce.
- **E-mail notifikace**: nové objednávky, změny stavů, komentáře.
- **Multi-tenant** od prvního dne (shared DB + Postgres Row-Level Security).

## Stack

- Python 3.11+ · FastAPI · SQLAlchemy 2 async · PostgreSQL 16
- Jinja2 + HTMX + Tailwind + Alpine.js
- APScheduler (in-process, bez Redisu)
- S3-kompatibilní storage (MinIO dev, B2/R2 prod)
- Docker-first (`docker compose up`)

## Development

```bash
# Install deps
uv sync --all-extras

# Run dev server
uv run uvicorn app.main:app --reload

# Run tests
uv run pytest

# Lint
uv run ruff check .
```

## Docker

```bash
cp .env.example .env
docker compose up --build
```

## Status

Rozpracováno — aktuálně M0 (bootstrap).
Podrobný plán viz interní dokumentaci.
