"""Spot rows in loads/open-loads.json (sync_open_loads.py)."""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_open_loads as s  # noqa: E402

TODAY = date(2026, 10, 9)


def spot(no, entered="2026-10-09", exp="2026-10-10", action="update", record_id=None, received="2026-10-09T14:00:00+00:00", **extra):
    data = {
        "spot_number": no,
        "date_entered": entered,
        "expiration_date": exp,
        "pickup_city": "TROY",
        "pickup_state": "TN",
        "consignee_city": "OMAHA",
        "consignee_state": "NE",
        "type": "F",
        "weight": "47500",
        "carrier_rate": "3000",
    }
    data.update(extra)
    return {"record_id": no if record_id is None else record_id, "action": action, "data": data, "received_at": received}


def load(pro, status="OPEN", pu="2026-10-09", origin=("TROY", "TN"), dest=("OMAHA", "NE"), received="2026-10-09T15:00:00+00:00", **extra):
    data = {
        "id": pro,
        "status": status,
        "pickup_date": pu,
        "origin_city": origin[0],
        "origin_state": origin[1],
        "dest_city": dest[0],
        "dest_state": dest[1],
        "equipment": "V",
        "weight": "40000",
        "carrier_line_haul": "1500",
    }
    data.update(extra)
    return {"record_id": pro, "action": "update", "data": data, "received_at": received}


def pros(rows):
    return [r["pro"] for r in rows]


class SpotRemovalTests(unittest.TestCase):
    def test_live_spot_is_shown(self):
        rows = s.select_spot_rows([spot("8890426")], [], TODAY)
        self.assertEqual(pros(rows), ["8890426"])
        self.assertTrue(rows[0]["spot"])
        self.assertEqual(rows[0]["status"], "OPEN")

    def test_stored_delete_action_is_removed(self):
        for action in ("delete", "Delete", "DELETE", " deleted "):
            self.assertEqual(s.select_spot_rows([spot("8890423", action=action)], [], TODAY), [], action)

    def test_expired_spot_is_removed_but_expiring_today_still_shown(self):
        self.assertEqual(s.select_spot_rows([spot("8890420", exp="2026-10-08")], [], TODAY), [])
        self.assertEqual(pros(s.select_spot_rows([spot("8890423", exp="2026-10-09")], [], TODAY)), ["8890423"])
        self.assertEqual(s.select_spot_rows([spot("8890423", exp="10/08/2026")], [], TODAY), [])

    def test_spot_without_expiration_ages_out(self):
        self.assertEqual(pros(s.select_spot_rows([spot("1", exp="", entered="2026-10-08")], [], TODAY)), ["1"])
        self.assertEqual(s.select_spot_rows([spot("1", exp="", entered="2026-10-01")], [], TODAY), [])

    def test_deleted_or_converted_flags_sent_as_update(self):
        self.assertEqual(s.select_spot_rows([spot("1", status="Deleted")], [], TODAY), [])
        self.assertEqual(s.select_spot_rows([spot("1", deleted="Y")], [], TODAY), [])
        self.assertEqual(s.select_spot_rows([spot("1", converted_pro="50919")], [], TODAY), [])

    def test_absent_from_snapshot_means_absent_from_feed(self):
        # Nothing is remembered between runs: only spots the listener still
        # holds are output.
        rows = s.select_spot_rows([spot("8890425"), spot("8890426")], [], TODAY)
        self.assertEqual(pros(rows), ["8890425", "8890426"])

    def test_blank_keyed_record_ignored(self):
        self.assertEqual(s.select_spot_rows([spot("8890421", record_id="")], [], TODAY), [])

    def test_duplicate_spot_uses_newest(self):
        old = spot("5", action="update", received="2026-10-09T10:00:00+00:00")
        new = spot("5", action="delete", received="2026-10-09T11:00:00+00:00")
        self.assertEqual(s.select_spot_rows([old, new], [], TODAY), [])

    def test_converted_to_load_by_spot_number_reference(self):
        loads = [load("50919", origin=("X", "GA"), dest=("Y", "NC"), spot_number="8890428")]
        self.assertEqual(s.select_spot_rows([spot("8890428")], loads, TODAY), [])

    def test_converted_to_load_by_same_lane(self):
        loads = [load("50919", status="COVERED")]
        self.assertEqual(s.select_spot_rows([spot("8890426")], loads, TODAY), [])

    def test_same_lane_old_load_does_not_remove_spot(self):
        # Load entered before the spot and picking up before it - not a conversion.
        loads = [load("50700", pu="2026-10-02", received="2026-10-02T15:00:00+00:00")]
        self.assertEqual(pros(s.select_spot_rows([spot("8890426")], loads, TODAY)), ["8890426"])

    def test_different_lane_load_does_not_remove_spot(self):
        loads = [load("50900", origin=("GRAND PRAIRIE", "TX"), dest=("COLUMBUS", "OH"))]
        self.assertEqual(pros(s.select_spot_rows([spot("8890426")], loads, TODAY)), ["8890426"])

    def test_lane_match_can_be_disabled(self):
        loads = [load("50919")]
        orig = s.SPOT_LANE_MATCH_REMOVES
        s.SPOT_LANE_MATCH_REMOVES = False
        try:
            self.assertEqual(pros(s.select_spot_rows([spot("8890426")], loads, TODAY)), ["8890426"])
        finally:
            s.SPOT_LANE_MATCH_REMOVES = orig


class RealLoadsUnchangedTests(unittest.TestCase):
    def test_open_loads_unchanged_by_spot_logic(self):
        future = "2099-01-01"
        loads = [
            load("50919", pu=future),
            load("50800", pu=future, origin=("ENTERPRISE", "AL"), dest=("SALISBURY", "NC")),
            load("50801", status="HOLD", pu=future),
        ]
        without = s.select_rows(loads, [])
        with_spots = s.select_rows(loads, [spot("8890426", entered="2099-01-01", exp="2099-01-02")])
        real = [r for r in with_spots if not r.get("spot")]
        self.assertEqual(real, without)
        self.assertEqual(sorted(pros(without)), ["50800", "50919"])


if __name__ == "__main__":
    unittest.main()
