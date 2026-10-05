# Craftarr development

## Requirements

- Python 3.10 or newer
- SQLite (included with Python on most systems)
- Java installed locally only when running a managed Minecraft server
- Node.js and npm only when rebuilding the bundled CodeMirror editor

Craftarr targets PaperMC on Minecraft Java Edition. macOS is supported for local
development. Production installation currently targets Ubuntu 22.04+ and Oracle
Linux 8+; Windows is not supported.

## Local setup

Clone the current repository:

```bash
git clone https://github.com/STEMMechanics/Craftarr.git
cd Craftarr
```

Create and activate a virtual environment, then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Apply database migrations and create the first local administrator:

```bash
alembic upgrade head
python -m app.admin_cli ensure-admin --username admin
```

The command prints a labeled temporary administrator password once. Copy it
to sign in; Craftarr requires you to choose a new password at first login. If
you need to recover an account later, run
`python -m app.admin_cli reset-password admin` and use the temporary password
it prints. Start the development server:

```bash
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>.

Set a persistent, randomly generated secret before first use (at least 32
characters). Without one, the development server generates a temporary secret
and sessions are invalidated on restart:

```bash
export CRAFTARR_CONSOLE_SECRET="replace-with-a-long-random-value"
```

For HTTPS, set `CRAFTARR_CONSOLE_COOKIE_SECURE=true`. File uploads default to
512 MiB; set `CRAFTARR_CONSOLE_MAX_UPLOAD_BYTES` to change the limit.

## Frontend assets

The bundled CodeMirror editor is committed under `app/static`, so production
installs do not require Node.js. When changing `editor-source.js`, rebuild the
asset with:

```bash
npm install
npm run build:editor
```

## Database changes

When a change modifies SQLAlchemy models, create and review a migration:

```bash
alembic revision --autogenerate -m "Description of change"
alembic upgrade head
```

Do not edit an already released migration to introduce a new schema change.

## Checks and releases

The GitHub Actions test workflow runs pytest on Python 3.13 and 3.14 for pull
requests and pushes to `main`. Run the same suite locally with:

```bash
pytest --cov=app --cov-report=term-missing
```

For release tagging and archive details, see
[Installation and deployment](installation.md#release-publishing). Contribution
scope and pull request guidance are in [CONTRIBUTING.md](../CONTRIBUTING.md).
