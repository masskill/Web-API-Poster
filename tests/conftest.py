import os
import sys
import tempfile
from pathlib import Path

# Isolated data dir for tests; must be set before app modules are imported.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="wap-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db.init_db("sqlite://")
    yield
