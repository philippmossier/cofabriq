import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import psycopg
import pytest
from neo4j import Driver
from neo4j.exceptions import ServiceUnavailable
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg import sql

from cofabriq import tracing
from cofabriq.config import get_settings
from cofabriq.context.store import graph, postgres
from tests.gitrepo import init_repo

# Every span goes to memory and to a throwaway run store, never to a backend
# a dev's `.env` names or to data/traces: `tracing.setup` keeps a provider
# that is already set.
SPANS = InMemorySpanExporter()
STORE = tracing.RunStore(Path(tempfile.mkdtemp(prefix="cofabriq-traces-")), "c0ffee")
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(SPANS))
tracing.use_store(_provider, STORE)
trace.set_tracer_provider(_provider)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """The spans finished during the test."""
    SPANS.clear()
    return SPANS


@pytest.fixture
def run_store(tmp_path: Path) -> tracing.RunStore:
    """The run store, writing into this test's own folder."""
    STORE.directory = tmp_path / "traces"
    return STORE


def unreachable(service: str) -> NoReturn:
    """Skip a database test locally, fail it in CI (`CI` set): a green CI
    run must have run them."""
    if os.environ.get("CI"):
        pytest.fail(f"{service} not reachable in CI (service container down?)")
    pytest.skip(f"{service} not reachable (make up)")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(
        tmp_path / "demo-repo",
        {
            "docs/adr/0001-use-sse.md": "# Use SSE\n",
            "README.md": "# Demo\n",
            "main.py": "print('hi')\n",
        },
    )


@pytest.fixture
def conn() -> Iterator[postgres.Conn]:
    """A connection whose tables live in a throwaway schema."""
    schema = f"test_{uuid.uuid4().hex[:8]}"
    ident = sql.Identifier(schema)
    dsn = get_settings().postgres_dsn
    try:
        admin = psycopg.connect(dsn, connect_timeout=2, autocommit=True)
    except psycopg.OperationalError:
        unreachable("Postgres")
    with admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(ident))
        scoped = f"{dsn}?options=-csearch_path%3D{schema},public"
        try:
            with postgres.connect(scoped) as c:
                yield c
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(ident))


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    """A DSN whose tables (the flows' checkpoints) live in a throwaway schema."""
    schema = f"test_{uuid.uuid4().hex[:8]}"
    ident = sql.Identifier(schema)
    dsn = get_settings().postgres_dsn
    try:
        admin = psycopg.connect(dsn, connect_timeout=2, autocommit=True)
    except psycopg.OperationalError:
        unreachable("Postgres")
    with admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(ident))
        try:
            yield f"{dsn}?options=-csearch_path%3D{schema}"
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(ident))


# The throwaway instance from compose (`neo4j-test`), never the dev graph.
TEST_URI = "bolt://localhost:7688"


@pytest.fixture
def driver() -> Iterator[Driver]:
    settings = get_settings()
    password = settings.neo4j_password.get_secret_value()
    try:
        d = graph.connect(TEST_URI, settings.neo4j_user, password)
    except ServiceUnavailable:
        unreachable("Neo4j test instance")
    with d:
        d.execute_query("MATCH (n) DETACH DELETE n")
        yield d
