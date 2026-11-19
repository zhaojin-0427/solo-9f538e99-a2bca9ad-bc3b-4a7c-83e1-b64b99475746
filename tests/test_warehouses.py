"""Multi-warehouse behavior and in-place upgrade of legacy SQLite volumes."""

from __future__ import annotations

import io
import sqlite3
import threading
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from coldchain import facts, knowledge
from coldchain.app import create_app
from coldchain.db import connect, init_db
from coldchain.warehouses import DEFAULT_WAREHOUSE


def csv_bytes(rows, header="流水号,SKU,出库时刻,箱数"):
    lines = [header]
    lines.extend(",".join(str(cell) for cell in row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


WH_A_ROWS = [
    ("OUT-001", "SKU-A", "2026-03-01T01:00:00Z", 12),
    ("OUT-002", "SKU-B", "2026-03-05T23:30:00Z", 7),
]
WH_B_ROWS = [
    ("OUT-001", "SKU-A", "2026-03-02T03:00:00Z", 3),
    ("OUT-009", "SKU-C", "2026-03-20T00:00:00Z", 4),
]


class WarehouseAppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.sqlite3")
        self.app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, rows, warehouse=None, filename="m.csv", month=None):
        data = {"file": (io.BytesIO(csv_bytes(rows)), filename)}
        if warehouse is not None:
            data["warehouse"] = warehouse
        if month:
            data["month"] = month
        return self.client.post(
            "/api/months", data=data, content_type="multipart/form-data"
        )

    def report(self, warehouse=None, month="2026-03", version=None):
        query = f"/api/report?month={month}"
        if warehouse is not None:
            query += f"&warehouse={warehouse}"
        if version is not None:
            query += f"&version={version}"
        return self.client.get(query)


class MultiWarehouseTests(WarehouseAppTestCase):
    def test_two_warehouses_freeze_same_month_with_same_sequence(self):
        first = self.upload(WH_A_ROWS, warehouse="WH-A")
        second = self.upload(WH_B_ROWS, warehouse="WH-B")
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(second.status_code, 201, second.get_data(as_text=True))
        self.assertEqual(first.get_json()["warehouse"], "WH-A")
        self.assertEqual(second.get_json()["warehouse"], "WH-B")

        listing = self.client.get("/api/months").get_json()
        self.assertEqual(listing["warehouse"], DEFAULT_WAREHOUSE)
        self.assertEqual(listing["months"], [])
        for code in ("WH-A", "WH-B"):
            rows = self.client.get(f"/api/months?warehouse={code}").get_json()["months"]
            self.assertEqual([row["month"] for row in rows], ["2026-03"])

    def test_report_boxes_and_details_only_count_selected_warehouse(self):
        self.upload(WH_A_ROWS, warehouse="WH-A")
        self.upload(WH_B_ROWS, warehouse="WH-B")

        report_a = self.report(warehouse="WH-A").get_json()
        report_b = self.report(warehouse="WH-B").get_json()
        self.assertEqual(report_a["warehouse"], "WH-A")
        self.assertEqual(report_a["total_boxes"], 19)
        self.assertEqual(report_a["total_shipments"], 2)
        self.assertEqual(report_b["total_boxes"], 7)
        self.assertEqual(report_b["total_shipments"], 2)

        for report, code in ((report_a, "WH-A"), (report_b, "WH-B")):
            for bucket in report["zones"] + [report["unclassified"]]:
                for item in bucket["shipments"]:
                    self.assertEqual(item["warehouse"], code)
                    self.assertIn("sequence_no", item)
        # Both warehouses legitimately keep their own OUT-001.
        seq_a = {
            item["sequence_no"]
            for bucket in report_a["zones"] + [report_a["unclassified"]]
            for item in bucket["shipments"]
        }
        self.assertIn("OUT-001", seq_a)

    def test_same_warehouse_same_month_only_one_freeze_succeeds(self):
        self.assertEqual(self.upload(WH_A_ROWS, warehouse="WH-A").status_code, 201)
        conflict = self.upload(
            [("OUT-777", "SKU-Z", "2026-03-11T00:00:00Z", 1)], warehouse="WH-A"
        )
        self.assertEqual(conflict.status_code, 409)
        # The losing upload must not have landed any fact.
        report = self.report(warehouse="WH-A").get_json()
        self.assertEqual(report["total_shipments"], 2)
        # The other warehouse still freezes its own copy.
        self.assertEqual(self.upload(WH_B_ROWS, warehouse="WH-B").status_code, 201)

    def test_same_warehouse_sequence_unique_across_months(self):
        self.assertEqual(
            self.upload(
                [("OUT-001", "SKU-A", "2026-03-01T00:00:00Z", 1)], warehouse="WH-A"
            ).status_code,
            201,
        )
        clash = self.upload(
            [("OUT-001", "SKU-A", "2026-04-01T00:00:00Z", 1)], warehouse="WH-A"
        )
        self.assertEqual(clash.status_code, 409)
        # Same sequence in another warehouse is fine.
        ok = self.upload(
            [("OUT-001", "SKU-A", "2026-04-01T00:00:00Z", 1)], warehouse="WH-B"
        )
        self.assertEqual(ok.status_code, 201)

    def test_failed_validation_writes_nothing_for_any_warehouse(self):
        bad = self.upload(
            [("", "SKU-A", "2026-03-01T00:00:00Z", 1)], warehouse="WH-A"
        )
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(
            self.client.get("/api/warehouses").get_json()["warehouses"],
            [{"warehouse": DEFAULT_WAREHOUSE, "created_at": unittest.mock.ANY, "month_count": 0}],
        )
        # A clean retry of the same warehouse/month still works.
        self.assertEqual(self.upload(WH_A_ROWS, warehouse="WH-A").status_code, 201)

    def test_legacy_links_without_warehouse_use_default_warehouse(self):
        self.upload(WH_A_ROWS)  # no warehouse field -> default
        body = self.client.get("/api/months").get_json()
        self.assertEqual(body["warehouse"], DEFAULT_WAREHOUSE)
        self.assertEqual(len(body["months"]), 1)
        # Every historical response field is still present.
        month_row = body["months"][0]
        for field in ("month", "row_count", "total_boxes", "frozen_at", "source_filename"):
            self.assertIn(field, month_row)

        report = self.client.get("/api/report?month=2026-03").get_json()
        self.assertEqual(report["warehouse"], DEFAULT_WAREHOUSE)
        self.assertEqual(report["total_boxes"], 19)
        self.assertEqual(
            report["month_fact"]["source_filename"], "m.csv"
        )

    def test_invalid_warehouse_code_rejected(self):
        response = self.upload(WH_A_ROWS, warehouse="bad/code!")
        self.assertEqual(response.status_code, 400)
        # Blank warehouse means the default warehouse; it has no facts yet,
        # so the month lookup returns 404 rather than another warehouse's data.
        self.assertEqual(self.client.get("/api/report?month=2026-03").status_code, 404)
        self.upload(WH_A_ROWS)
        self.assertEqual(self.client.get("/api/report?month=2026-03").status_code, 200)

    def test_report_details_carry_warehouse_and_sequence_in_json(self):
        self.upload(WH_B_ROWS, warehouse="WH-B")
        report = self.report(warehouse="WH-B").get_json()
        all_items = [
            item
            for bucket in report["zones"] + [report["unclassified"]]
            for item in bucket["shipments"]
        ]
        self.assertTrue(all_items)
        for item in all_items:
            self.assertEqual(item["warehouse"], "WH-B")
            self.assertTrue(item["sequence_no"])
            self.assertIn("occurred_at_display", item)

    def test_knowledge_versions_are_shared_across_warehouses(self):
        self.upload(WH_A_ROWS, warehouse="WH-A")
        self.upload(WH_B_ROWS, warehouse="WH-B")
        response = self.client.post(
            "/api/versions",
            json={
                "sku": "SKU-A",
                "reason": "shared v1",
                "base_version": None,
                "intervals": [
                    {"zone": "冷藏", "valid_from": "2026-03-01T00:00:00Z"}
                ],
            },
        )
        self.assertEqual(response.status_code, 201)
        for code in ("WH-A", "WH-B"):
            report = self.report(warehouse=code).get_json()
            self.assertEqual(report["version"]["version_no"], 1)
            classified = report["classified_boxes"]
            self.assertGreater(classified, 0)


class SameWarehouseConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "concurrent.sqlite3")
        init_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_same_warehouse_same_month_freezes_only_one(self):
        barrier = threading.Barrier(8)
        outcomes = []

        def freeze(index):
            conn = connect(self.db_path)
            barrier.wait()
            try:
                facts.ingest_month(
                    conn,
                    csv_bytes(
                        [
                            (
                                f"OUT-{index:03d}",
                                "SKU-A",
                                "2026-03-10T00:00:00Z",
                                index + 1,
                            )
                        ]
                    ),
                    filename=f"wh-a-{index}.csv",
                    warehouse="WH-A",
                )
                outcomes.append(("ok", index))
            except Exception as exc:  # noqa: BLE001 - record every rejection
                outcomes.append(("rejected", str(exc)))
            finally:
                conn.close()

        threads = [threading.Thread(target=freeze, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        accepted = [item for item in outcomes if item[0] == "ok"]
        self.assertEqual(len(accepted), 1, outcomes)
        conn = connect(self.db_path)
        try:
            month_rows = conn.execute(
                "SELECT * FROM months WHERE warehouse = 'WH-A'"
            ).fetchall()
            shipment_rows = conn.execute(
                "SELECT * FROM shipments WHERE warehouse = 'WH-A'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(month_rows), 1)
        self.assertEqual(len(shipment_rows), 1)

    def test_concurrent_different_warehouses_both_succeed(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def freeze(code):
            conn = connect(self.db_path)
            barrier.wait()
            try:
                facts.ingest_month(
                    conn,
                    csv_bytes([("OUT-001", "SKU-A", "2026-03-10T00:00:00Z", 1)]),
                    filename=f"{code}.csv",
                    warehouse=code,
                )
                outcomes.append(("ok", code))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("rejected", str(exc)))
            finally:
                conn.close()

        threads = [
            threading.Thread(target=freeze, args=(code,)) for code in ("WH-A", "WH-B")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(code for status, code in outcomes), ["WH-A", "WH-B"])
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT warehouse FROM shipments WHERE sequence_no = 'OUT-001'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(sorted(row["warehouse"] for row in rows), ["WH-A", "WH-B"])


# --------------------------------------------------------------------------
# Legacy (v1, no warehouse column) database upgrade
# --------------------------------------------------------------------------

LEGACY_SCHEMA = """
CREATE TABLE months (
    month                TEXT PRIMARY KEY,
    source_filename      TEXT NOT NULL,
    source_sha256        TEXT NOT NULL,
    row_count            INTEGER NOT NULL,
    total_boxes          INTEGER NOT NULL,
    frozen_at            TEXT NOT NULL
);
CREATE TABLE shipments (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    month                TEXT NOT NULL REFERENCES months(month),
    sequence_no          TEXT NOT NULL,
    sku                  TEXT NOT NULL,
    occurred_at          TEXT NOT NULL,
    occurred_at_us       INTEGER NOT NULL,
    boxes                INTEGER NOT NULL CHECK (boxes > 0),
    source_row           INTEGER NOT NULL,
    UNIQUE (sequence_no)
);
CREATE TABLE knowledge_versions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no           INTEGER NOT NULL UNIQUE,
    based_on_version     INTEGER REFERENCES knowledge_versions(version_no),
    reason               TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE TABLE version_ranges (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no           INTEGER NOT NULL REFERENCES knowledge_versions(version_no),
    sku                  TEXT NOT NULL,
    zone                 TEXT NOT NULL,
    valid_from           TEXT NOT NULL,
    valid_from_us        INTEGER NOT NULL,
    valid_to             TEXT,
    valid_to_us          INTEGER,
    UNIQUE (version_no, sku, valid_from_us),
    CHECK (valid_to_us IS NULL OR valid_to_us > valid_from_us)
);
CREATE TABLE audit_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    at                   TEXT NOT NULL,
    action               TEXT NOT NULL,
    month                TEXT,
    version_no           INTEGER,
    summary              TEXT NOT NULL
);
"""


def build_legacy_database(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO months(month, source_filename, source_sha256, row_count,"
            " total_boxes, frozen_at) VALUES ('2026-03', 'legacy.csv',"
            " 'abc123', 2, 19, '2026-04-01T00:00:00.000000Z')"
        )
        conn.executemany(
            "INSERT INTO shipments(id, month, sequence_no, sku, occurred_at,"
            " occurred_at_us, boxes, source_row) VALUES (?,?,?,?,?,?,?,?)",
            [
                (1, "2026-03", "OUT-001", "SKU-A", "2026-03-01T01:00:00.000000Z",
                 1_772_326_800_000_000, 12, 2),
                (2, "2026-03", "OUT-002", "SKU-B", "2026-03-05T23:30:00.000000Z",
                 1_772_753_400_000_000, 7, 3),
            ],
        )
        conn.execute(
            "INSERT INTO knowledge_versions(version_no, based_on_version, reason,"
            " created_at) VALUES (1, NULL, 'legacy v1', '2026-04-01T00:00:00.000000Z')"
        )
        conn.execute(
            "INSERT INTO version_ranges(version_no, sku, zone, valid_from,"
            " valid_from_us, valid_to, valid_to_us) VALUES (1, 'SKU-A', '冷藏',"
            " '2026-03-01T00:00:00.000000Z', 1772323200000000, NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO audit_log(at, action, month, version_no, summary)"
            " VALUES ('2026-04-01T00:00:00.000000Z', 'freeze_month', '2026-03', NULL,"
            " 'legacy audit')"
        )
        conn.commit()
    finally:
        conn.close()


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "legacy.sqlite3")
        build_legacy_database(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_rows_move_into_default_warehouse_without_loss(self):
        init_db(self.db_path)
        conn = connect(self.db_path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            # No leftover rename tables.
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertNotIn("months_legacy", names)
            self.assertNotIn("shipments_legacy", names)

            months = conn.execute("SELECT * FROM months").fetchall()
            shipments = conn.execute("SELECT * FROM shipments ORDER BY id").fetchall()
            self.assertEqual(len(months), 1)
            self.assertEqual(months[0]["warehouse"], DEFAULT_WAREHOUSE)
            self.assertEqual(months[0]["source_filename"], "legacy.csv")
            self.assertEqual(len(shipments), 2)
            for row in shipments:
                self.assertEqual(row["warehouse"], DEFAULT_WAREHOUSE)
            # Source row numbers and identities survive.
            self.assertEqual(
                [(row["sequence_no"], row["source_row"]) for row in shipments],
                [("OUT-001", 2), ("OUT-002", 3)],
            )
            self.assertEqual([row["id"] for row in shipments], [1, 2])
            # Knowledge ledger and audit history are untouched.
            self.assertEqual(
                conn.execute("SELECT version_no FROM knowledge_versions").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM version_ranges").fetchone()["c"],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"],
                1,
            )
        finally:
            conn.close()

        # Old links (no warehouse parameter) read the migrated default warehouse
        # with the original response fields and historical temperature basis.
        app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        client = app.test_client()
        report = client.get("/api/report?month=2026-03&version=1").get_json()
        self.assertEqual(report["warehouse"], DEFAULT_WAREHOUSE)
        self.assertEqual(report["total_boxes"], 19)
        self.assertEqual(report["total_shipments"], 2)
        self.assertEqual(
            {z["zone"]: z["boxes"] for z in report["zones"]}, {"冷藏": 12}
        )
        self.assertEqual(report["unclassified"]["boxes"], 7)
        self.assertEqual(report["month_fact"]["source_filename"], "legacy.csv")
        for bucket in report["zones"] + [report["unclassified"]]:
            for item in bucket["shipments"]:
                self.assertEqual(item["warehouse"], DEFAULT_WAREHOUSE)

    def test_migration_is_idempotent_on_restart(self):
        init_db(self.db_path)
        init_db(self.db_path)  # restart with the same volume
        conn = connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM months").fetchone()["c"], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM shipments").fetchone()["c"], 2
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM warehouses").fetchone()["c"], 1
            )
        finally:
            conn.close()

    def test_interrupted_migration_rolls_back_and_restart_recovers(self):
        import coldchain.db as db_module

        triggered = {"count": 0}

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if str(sql).strip().upper().startswith("DROP TABLE SHIPMENTS_LEGACY"):
                    triggered["count"] += 1
                    raise sqlite3.OperationalError("simulated crash during upgrade")
                return super().execute(sql, parameters)

        real_connect = sqlite3.connect

        def failing_connect(path, *args, **kwargs):
            kwargs.pop("factory", None)
            return real_connect(str(path), *args, factory=FailingConnection, **kwargs)

        with unittest.mock.patch.object(db_module.sqlite3, "connect", side_effect=failing_connect):
            with self.assertRaises(sqlite3.OperationalError):
                init_db(self.db_path)
        self.assertEqual(triggered["count"], 1)

        # After rollback the legacy tables are intact and no v2 tables exist.
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM months").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0], 2
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(months)")}
            self.assertNotIn("warehouse", columns)
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("warehouses", names)
        finally:
            conn.close()

        # Restart with the same volume: migration retries and succeeds,
        # old records are neither lost nor duplicated.
        init_db(self.db_path)
        conn = connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM months").fetchone()["c"], 1
            )
            shipments = conn.execute("SELECT * FROM shipments ORDER BY id").fetchall()
            self.assertEqual(len(shipments), 2)
            self.assertEqual(
                [row["sequence_no"] for row in shipments], ["OUT-001", "OUT-002"]
            )
            self.assertTrue(
                all(row["warehouse"] == DEFAULT_WAREHOUSE for row in shipments)
            )
        finally:
            conn.close()

    def test_immutable_triggers_survive_migration(self):
        init_db(self.db_path)
        conn = connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE shipments SET boxes = 1")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM months")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM warehouses")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
