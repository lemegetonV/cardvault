from alembic import context

from shared.migrate import run_env

run_env(context)
