import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.repositories.sql import Base, create_tables


@pytest.fixture(scope="session")
def sql_engine():
    """Engine for a throwaway Postgres database; SQL tests skip without one.

    Example: TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    create_tables(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sql_sessions(sql_engine):
    """Session factory on empty tables with ids restarting at 1."""
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with sql_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return sessionmaker(sql_engine)
