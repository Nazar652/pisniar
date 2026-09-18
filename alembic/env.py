import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import models so they register on Base.metadata before autogenerate runs.
from pisniar.db.base import Base
from pisniar.db.models import Post, Track  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Alembic intentionally does not go through pisniar.config.Settings here:
# migrations only need a database URL, not the bot token / admin ids / etc.
# that the rest of the app requires. DATABASE_URL from the environment always
# wins over the placeholder in alembic.ini — and is required, not optional:
# failing fast here beats a confusing NoSuchModuleError from trying to
# connect to the literal "driver://user:pass@localhost/dbname" placeholder.
database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is not set. alembic/env.py reads it directly from the "
        "environment (not pisniar.config.Settings) and has no real default."
    )
# `configparser` (which backs alembic.ini) interpolates "%" in option values;
# a DSN with a literal "%" (e.g. a URL-encoded password) would otherwise
# raise a confusing InterpolationSyntaxError deep inside ConfigParser.
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # `compare_type` is Alembic's own default already; `compare_server_default`
        # defaults to False and would silently never catch a `server_default`
        # drift between the models and the database — `alembic check` (V11) is
        # supposed to be a real guarantee, not a partial one.
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # `context.configure()` must run — and the migration transaction must be
    # opened via `context.begin_transaction()` — before any `execute()` on
    # this connection. SQLAlchemy 2.0 autobegins a transaction on the first
    # `execute()`; if that happens before `configure()`, Alembic's
    # `_in_external_transaction` check (it inspects `connection.in_transaction()`
    # in its constructor) concludes some *external* caller owns the
    # transaction and quietly no-ops its own commit. The DDL still runs, but
    # `async with connectable.connect()` then closes without ever committing
    # it — a silent rollback of the whole migration, `alembic_version` row
    # included, while `alembic upgrade head` itself still exits 0.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        # `SET LOCAL`, not `SET`: scoped to this transaction, not leaked into
        # the rest of the session (irrelevant here with a fresh NullPool
        # connection, but `SET LOCAL` is the correct tool for "this
        # migration's timeout" regardless). A migration that can't take its
        # lock should fail fast and say so, rather than hang and eventually
        # take the whole service down with it.
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("SET LOCAL statement_timeout = '60s'"))
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
