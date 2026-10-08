from __future__ import annotations

from functools import partial

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.server import create_api_app
from app.server.performance import (
    format_http_percentile_report,
    read_performance_samples,
    InstrumentedJSONResponse,
    install_performance_middleware,
    install_sql_performance_instrumentation,
    performance_phase,
    record_external_call,
    record_external_items,
)


from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class ServerPerformanceTests(unittest.TestCase):
    def test_request_metrics_use_route_template_and_detect_repeated_queries(self) -> None:
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "performance.jsonl"
            engine = create_engine(
                "sqlite+pysqlite:///:memory:",
                connect_args={"check_same_thread": False},
            )
            install_sql_performance_instrumentation(engine)
            app = FastAPI(default_response_class=InstrumentedJSONResponse)
            install_performance_middleware(app, log_path=log_path)

            @app.get("/api/v1/items/{item_id}")
            def read_item(item_id: str) -> dict[str, str]:
                with performance_phase("compute"):
                    with engine.connect() as connection:
                        for _ in range(5):
                            connection.execute(text("SELECT 1")).all()
                    record_external_call()
                    with performance_phase("external"):
                        external_rows = ("a", "b", "c")
                    record_external_items(len(external_rows))
                return {"item_id": item_id}

            try:
                with TestClient(app) as client:
                    response = client.get("/api/v1/items/opaque-id-123")
            finally:
                engine.dispose()

            self.assertEqual(response.status_code, 200)
            self.assertIn("db;dur=", response.headers["Server-Timing"])
            samples = read_performance_samples(path=log_path, limit=10)
            self.assertEqual(len(samples), 1)
            sample = samples[0]
            self.assertEqual(sample["operation"], "http GET /api/v1/items/{item_id}")
            self.assertNotIn("opaque-id-123", log_path.read_text(encoding="utf-8"))
            self.assertEqual(sample["db_query_count"], 5)
            self.assertEqual(sample["db_select_count"], 5)
            self.assertEqual(sample["db_repeated_query_max"], 5)
            self.assertTrue(sample["db_n_plus_one_suspected"])
            self.assertEqual(sample["external_call_count"], 1)
            self.assertEqual(sample["external_item_count"], 3)
            self.assertEqual(sample["engine"], "fastapi-v2")

    def test_create_api_app_records_health_without_database_dependency(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "health.db"
            log_path = Path(directory) / "performance.jsonl"
            app = create_api_app(
                f"sqlite:///{database_path.as_posix()}",
                performance_log_path=log_path,
            )

            with TestClient(app) as client:
                response = client.get("/health")

            self.assertEqual(response.status_code, 200)
            self.assertIn("total;dur=", response.headers["Server-Timing"])
            samples = read_performance_samples(path=log_path, limit=10)
            self.assertEqual(len(samples), 1)
            sample = samples[0]
            self.assertEqual(sample["operation"], "http GET /health")
            self.assertEqual(sample["db_query_count"], 0)
            self.assertEqual(sample["db_seconds"], 0.0)

    def test_percentile_report_aggregates_http_samples(self) -> None:
        rows = [
            {
                "operation": "http GET /api/v1/projects",
                "total_seconds": total,
                "auth_seconds": 0.01,
                "api_seconds": 0.02,
                "db_seconds": total - 0.03,
                "db_query_count": index,
                "db_n_plus_one_suspected": index == 3,
            }
            for index, total in enumerate((1.0, 2.0, 3.0), start=1)
        ]

        report = format_http_percentile_report(rows)

        self.assertIn("p50", report)
        self.assertIn("p95", report)
        self.assertIn("p99", report)
        self.assertIn("2.000s", report)
        self.assertIn("2.900s", report)
        self.assertIn("2.980s", report)
        self.assertIn("http GET /api/v1/projects", report)


if __name__ == "__main__":
    unittest.main()
