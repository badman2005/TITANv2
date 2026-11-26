import os

os.environ["DATABASE_URL"] = "sqlite://"  # shared in-memory DB
os.environ["TITAN_AUTO_CREATE"] = "0"
os.environ["ANCHOR_BACKEND"] = "mock"

import pytest
from fastapi.testclient import TestClient

import seed
from app.database import SessionLocal, engine
from app.main import app
from app.models import Base


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        assert seed.run(s)["seeded"]
    return TestClient(app)


@pytest.fixture()
def db():
    with SessionLocal() as s:
        yield s
