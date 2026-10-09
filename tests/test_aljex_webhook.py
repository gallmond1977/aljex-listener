"""Aljex Live Sync webhook / record storage (app.py)."""

import base64
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["SYNC_USERNAME"] = "test-user"
os.environ["SYNC_PASSWORD"] = "test-pass"

# Importing app starts the Graph-subscription scheduler; keep tests offline.
with mock.patch("apscheduler.schedulers.background.BackgroundScheduler.start"):
    import app as app_module  # noqa: E402

AUTH = {"Authorization": "Basic " + base64.b64encode(b"test-user:test-pass").decode()}


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        conn = app_module.get_db()
        conn.execute("DELETE FROM aljex_records")
        conn.execute("DELETE FROM aljex_sync_events")
        conn.commit()
        conn.close()

    def post(self, table, action, **fields):
        data = {"web_sync_table_name": table, "web_sync_action": action}
        data.update(fields)
        return self.client.post("/aljex-webhook", data=data, headers=AUTH)

    def stored(self, table):
        r = self.client.get(f"/records/{table}?limit=all", headers=AUTH)
        return sorted(x["record_id"] for x in r.get_json())

    def test_spot_insert_and_delete(self):
        self.post("spots", "insert", spot_number="8890426", carrier_rate="3000")
        self.post("spots", "insert", spot_number="8890427", carrier_rate="2200")
        self.assertEqual(self.stored("spots"), ["8890426", "8890427"])
        self.post("spots", "delete", spot_number="8890427")
        self.assertEqual(self.stored("spots"), ["8890426"])

    def test_delete_action_is_case_insensitive(self):
        for i, action in enumerate(("Delete", "DELETE", " delete ", "deleted", "D")):
            no = f"88904{i:02d}"
            self.post("spots", "insert", spot_number=no)
            self.post("spots", action, spot_number=no)
        self.assertEqual(self.stored("spots"), [])

    def test_spot_delete_keyed_by_spot_number_even_if_id_sent(self):
        self.post("spots", "insert", spot_number="8890423")
        self.post("spots", "delete", spot_number="8890423", id="77")
        self.assertEqual(self.stored("spots"), [])

    def test_keyless_records_not_stored_under_blank_id(self):
        self.post("spots", "update", carrier_rate="100")
        self.assertEqual(self.stored("spots"), [])

    def test_loads_still_keyed_by_id(self):
        self.post("loads", "update", id="50919", status="OPEN")
        self.post("loads", "update", id="50919", status="COVERED")
        r = self.client.get("/records/loads?limit=all", headers=AUTH).get_json()
        self.assertEqual([(x["record_id"], x["data"]["status"]) for x in r], [("50919", "COVERED")])
        self.post("loads", "delete", id="50919")
        self.assertEqual(self.stored("loads"), [])

    def test_sync_events_logged_for_spots_only(self):
        self.post("spots", "Delete", spot_number="1")
        self.post("loads", "update", id="2", status="OPEN")
        events = self.client.get("/sync-events", headers=AUTH).get_json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["raw_action"], "Delete")
        self.assertEqual(events[0]["action"], "delete")
        self.assertEqual(events[0]["record_id"], "1")

    def test_manual_delete_endpoint(self):
        self.post("spots", "insert", spot_number="8890424")
        self.assertEqual(self.client.delete("/records/spots/8890424").status_code, 401)
        self.assertEqual(self.client.delete("/records/spots/8890424", headers=AUTH).status_code, 200)
        self.assertEqual(self.client.delete("/records/spots/8890424", headers=AUTH).status_code, 404)
        self.assertEqual(self.stored("spots"), [])

    def test_bulk_import_spots_and_snapshot_replace(self):
        for no in ("8890423", "8890424", "8890425", "8890426"):
            self.post("spots", "insert", spot_number=no)
        r = self.client.post(
            "/bulk-import/spots?replace=true",
            json=[{"spot_number": "8890425"}, {"spot_number": "8890426"}],
            headers=AUTH,
        )
        self.assertEqual(r.get_json()["removed"], 2)
        self.assertEqual(self.stored("spots"), ["8890425", "8890426"])

    def test_bulk_import_replace_refuses_empty_snapshot(self):
        self.post("spots", "insert", spot_number="8890426")
        r = self.client.post("/bulk-import/spots?replace=true", json=[], headers=AUTH)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.stored("spots"), ["8890426"])

    def test_bulk_import_loads_without_replace_unchanged(self):
        self.post("loads", "update", id="1", status="OPEN")
        r = self.client.post("/bulk-import/loads", json=[{"id": "2", "status": "OPEN"}, {"x": 1}], headers=AUTH)
        self.assertEqual(r.get_json(), {"status": "ok", "table": "loads", "saved": 1, "skipped": 1})
        self.assertEqual(self.stored("loads"), ["1", "2"])


if __name__ == "__main__":
    unittest.main()
