import os
import tempfile
import unittest
from pathlib import Path

from ad_rpa.config import Settings
from ad_rpa.health import ServiceState
from ad_rpa.storage.database import connect, integrity_check, migrate


class PlatformTests(unittest.TestCase):
    def test_secret_file_overrides_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret"
            path.write_text("value\n")
            old = os.environ.get("EXAMPLE_FILE")
            os.environ["EXAMPLE_FILE"] = str(path)
            Settings.load(load_dotenv=False)
            self.assertEqual(os.environ["EXAMPLE"], "value")
            if old is None: os.environ.pop("EXAMPLE_FILE")
            else: os.environ["EXAMPLE_FILE"] = old

    def test_migration_and_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "state.db")
            conn = connect(db)
            try: self.assertEqual(migrate(conn), 2)
            finally: conn.close()
            self.assertEqual(integrity_check(db).lower(), "ok")
        state = ServiceState()
        self.assertFalse(state.ready)
        state.db_ok = True; state.cycles["email"] = state.cycles["vpn"] = 1
        self.assertTrue(state.ready)
