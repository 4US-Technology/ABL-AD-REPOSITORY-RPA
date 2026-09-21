import tempfile
import unittest
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from types import SimpleNamespace

from ad_rpa.flows import vpn
from ad_rpa.integrations.glpi import GlpiClient, GlpiConfig, TransientGlpiError
from ad_rpa.storage import database


class Response:
    status = 200
    headers = {"Content-Type": "application/json"}
    def __init__(self, body=b"{}"):
        self.body = body
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *args): return False


class VpnResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(str(Path(self.tmp.name) / "state.db"))
        database.initialize(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_all_vpn_labels_and_inline_fallback(self):
        for label in vpn.VPN_LOGIN_LABELS:
            self.assertEqual(vpn.extract_login_from_ticket_content(f"{label}\njoao.silva"), "joao.silva")
        self.assertEqual(
            vpn.extract_login_from_ticket_content("Usuário(Login) da VPN / Internet joao.silva"),
            "joao.silva",
        )

    def test_state_is_persisted_for_ad_and_glpi_steps(self):
        expiry = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        database.save_vpn_state(self.conn, 12, "ad_pending", "joao", expiry, "solução")
        state = database.get_vpn_state(self.conn, 12)
        self.assertEqual((state["stage"], state["planned_expiry"], state["glpi_message"]),
                         ("ad_pending", expiry, "solução"))
        database.save_vpn_state(self.conn, 12, "close_pending", "joao", expiry, "solução")
        self.assertEqual(database.get_vpn_state(self.conn, 12)["stage"], "close_pending")

    def test_incomplete_read_is_identifiable_for_json_and_multipart(self):
        client = GlpiClient(GlpiConfig("https://glpi", None, "token", None, None, True))
        with patch("ad_rpa.integrations.glpi.urlopen", side_effect=IncompleteRead(b"{", 2)):
            with self.assertRaises(TransientGlpiError): client.request("GET", "Ticket/1")
        file_path = Path(self.tmp.name) / "a.txt"
        file_path.write_text("x")
        with patch("ad_rpa.integrations.glpi.urlopen", side_effect=IncompleteRead(b"{", 2)):
            with self.assertRaises(TransientGlpiError):
                client.request_multipart("POST", "Document", fields={}, files={"file": file_path})

    def test_401_refreshes_session_once(self):
        client = GlpiClient(GlpiConfig("https://glpi", None, "token", None, None, True))
        client.session_token = "expired"
        error = HTTPError("https://glpi/Ticket/1", 401, "expired", {}, None)
        with patch("ad_rpa.integrations.glpi.urlopen", side_effect=[error, Response(b'{"session_token":"new"}'), Response(b'{"id":1}')]):
            self.assertEqual(client.request("GET", "Ticket/1"), {"id": 1})
        self.assertEqual(client.session_token, "new")

    def test_resume_does_not_duplicate_solution_and_marks_only_after_close(self):
        ticket = SimpleNamespace(id=99, login="joao")
        database.save_vpn_state(self.conn, 99, "message_pending", "joao", "date", "solução")
        glpi = SimpleNamespace(
            ticket_has_message=lambda *args, **kwargs: True,
            add_solution=lambda *args: self.fail("não deve duplicar"),
            add_followup=lambda *args: self.fail("não deve duplicar"),
            update_ticket=lambda *args: None,
            get_item=lambda *args: {"status": 6},
        )
        vpn._finish_ticket(ticket, glpi, self.conn, solution=True, action="renewed")
        self.assertEqual(database.get_vpn_state(self.conn, 99)["stage"], "completed")
        self.assertTrue(database.was_ticket_processed(self.conn, 99))

    def test_close_failure_keeps_pending_state_and_does_not_mark_action(self):
        ticket = SimpleNamespace(id=100, login="joao")
        database.save_vpn_state(self.conn, 100, "close_pending", "joao", "date", "solução")
        glpi = SimpleNamespace(
            update_ticket=lambda *args: (_ for _ in ()).throw(TransientGlpiError("rede")),
            get_item=lambda *args: {"status": 6},
        )
        with self.assertRaises(TransientGlpiError):
            vpn._finish_ticket(ticket, glpi, self.conn, solution=True, action="renewed")
        self.assertEqual(database.get_vpn_state(self.conn, 100)["stage"], "close_pending")
        self.assertFalse(database.was_ticket_processed(self.conn, 100))


if __name__ == "__main__":
    unittest.main()
