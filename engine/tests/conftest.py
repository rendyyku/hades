import pytest
from sqlalchemy.orm import Session

from hades.db import init_db, make_engine


@pytest.fixture
def engine():
    eng = make_engine("sqlite:///:memory:")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s
