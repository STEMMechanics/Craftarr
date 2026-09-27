# Contributing to Craftarr

Thanks for your interest in contributing to Craftarr.

Bug reports, documentation improvements, suggestions and pull requests are welcome.

For larger changes or new features, please open an issue first so the proposed change can be discussed before significant development work begins.

## Scope

Craftarr is a general-purpose, open-source web management panel developed by
STEMMechanics. It is also used internally to power STEMCraft.

The project primarily targets:

- PaperMC servers
- Minecraft Java Edition
- Linux/systemd production environments
- macOS development environments

The project focuses on practical tools for PaperMC server operators and
communities. Contributions that make server operations safer, clearer or easier
to automate are welcome.

## Before You Write Code

### Bug fixes

Open a bug report describing the problem and how to reproduce it.

For small, well-contained fixes, you are welcome to submit a pull request at the same time.

### Improvements

Small improvements to existing behaviour, interface elements, defaults or documentation are welcome.

If the change significantly alters existing behaviour, please open an issue first.

### New features

Please open a feature request before starting substantial work.

Describe the problem or use case you are trying to address, not only the proposed implementation.

This helps make sure the feature fits the direction and requirements of Craftarr before you spend time building it.

### Structural changes

Please open an issue before making substantial changes to:

- Project structure
- Database architecture
- Authentication
- Server process management
- Installation or deployment
- Build or release tooling
- Major dependencies

These areas can affect existing installations and upgrade paths beyond what may be obvious from an individual change.

### Documentation

Documentation fixes can generally be submitted directly as a pull request.

Typos, incorrect commands, outdated instructions and clearer explanations are always welcome.

## Security Vulnerabilities

Please do not report security vulnerabilities through a public GitHub issue or pull request.

Instead, [report the vulnerability privately through GitHub](https://github.com/STEMMechanics/Craftarr/security/advisories/new) or email [hello@stemmechanics.com.au](mailto:hello@stemmechanics.com.au).

This includes vulnerabilities involving authentication, account access, file management, command execution or access to managed Minecraft servers.

## Pull Requests

Please keep pull requests focused.

- Keep one main concern or feature per pull request.
- Avoid unrelated formatting or refactoring.
- Explain what problem the change solves.
- Link the relevant issue where applicable.
- Test the behaviour you have changed.
- Include screenshots for visible interface changes where useful.
- Include database migrations when changing database models.
- Do not modify an existing released migration to introduce a new schema change.

Changes should preserve compatibility with existing Craftarr installations wherever practical.

## Project Layout

The project is primarily organised as:

```text
app/
    FastAPI application, routes, models and server management

app/templates/
    Jinja templates and page partials

app/static/
    JavaScript, CSS, images and other static assets

migrations/
    Alembic database migrations

migrations/versions/
    Individual database schema revisions

alembic.ini
    Alembic configuration

requirements.txt
    Python dependencies
```

The exact structure may evolve as the project develops.

## Development Setup

Clone the repository:

```bash
git clone https://github.com/STEMMechanics/Craftarr.git
cd Craftarr
```

Create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Apply database migrations:

```bash
alembic upgrade head
```

Create a local administrator if the development database does not have one:

```bash
python -m app.admin_cli ensure-admin --username admin
```

Start the development server:

```bash
uvicorn app.main:app --reload
```

The development interface will normally be available at:

```text
http://127.0.0.1:8000
```

## Database Changes

Craftarr uses Alembic for database migrations.

If your change modifies SQLAlchemy models, create a migration:

```bash
alembic revision --autogenerate -m "Description of change"
```

Review the generated migration before applying it.

Then run:

```bash
alembic upgrade head
```

Migrations must take existing installations into account. In particular, adding non-nullable columns may require a server default or staged migration so existing databases can be upgraded safely.

Do not require users to delete and recreate their database to upgrade Craftarr.

## Testing

Please test your changes before submitting a pull request.

At minimum, verify the affected functionality manually and confirm that an existing database can still start and migrate successfully.

Changes involving server management should be tested against PaperMC where practical.

Bug fixes should ideally include a regression test where the affected behaviour can reasonably be tested automatically.

As the project's automated test suite develops, new functionality should include appropriate tests.

## User Interface Changes

Craftarr aims to maintain a consistent and straightforward interface.

When changing the UI:

- Follow existing interface patterns.
- Keep server administration tasks clear and predictable.
- Avoid unnecessary complexity.
- Consider users who may not be experienced Linux or Minecraft server administrators.
- Ensure destructive actions have appropriate confirmation.
- Show useful feedback for operations that take time.
- Include screenshots with your pull request where they help demonstrate the change.

## PaperMC

PaperMC is the primary Minecraft server platform targeted by Craftarr.

Changes involving server startup, configuration, plugins, console handling, backups or updates should be designed and tested with PaperMC in mind.

Support for other server implementations may be considered, but should not compromise PaperMC support or unnecessarily complicate the project.

## Licensing Contributions

Craftarr is licensed under the GNU General Public License version 3 or any later version (`GPL-3.0-or-later`).

By submitting a contribution to this repository, you agree that your contribution may be distributed under the same `GPL-3.0-or-later` licence.

You must have the right to submit any code, documentation, images or other material included in your contribution.

Do not submit code or assets copied from another project unless their licence is compatible and the required attribution and licensing obligations have been satisfied.

## Code of Conduct

Contributors are expected to communicate respectfully and constructively.

Craftarr is part of a project that supports young people, educators and community organisations. Contributions and community interactions should reflect that environment.

## Questions

If you're unsure whether a proposed change fits the project, open an issue and describe what you'd like to achieve.

Discussing an idea before implementation is encouraged, particularly for larger features.
