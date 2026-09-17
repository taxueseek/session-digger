"""Redirect every test away from the live session-digger data directory.

``index_builder._schema`` resolves ``DB_PATH`` from ``SESSION_DIGGER_DATA_DIR``
at *import* time, and a dozen scripts then bind that value into their own module
namespace with ``from index_builder._schema import DB_PATH``. A test that only
patches ``_schema.DB_PATH`` therefore cannot redirect those copies — which is
how ``test_deep_analyze`` reached ``build_index`` through
``deep_analyze.ensure_fresh_index`` and rebuilt the user's real
``~/.claude/.session-digger/index.db`` from inside a test run (observed as
spurious ``database is locked`` when another process held the index, and as
incremental page churn in the production file).

Setting the env var here — conftest is imported before any test module — makes
the suite hermetic by construction, including for any module added later. Tests
that want their own database still patch the module binding they exercise; they
now patch a throwaway path instead of production.
"""
import atexit
import os
import shutil
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="session-digger-tests-")
os.environ["SESSION_DIGGER_DATA_DIR"] = _TEST_DATA_DIR
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)
