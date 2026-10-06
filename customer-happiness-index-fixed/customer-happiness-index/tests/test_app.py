import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app import create_app, get_db
from database import DatabaseConnection


class CustomerPulseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(Path(self.temp_dir.name) / "test.sqlite3"),
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "test-password",
        })
        self.client = self.app.test_client()
        login_page = self.client.get("/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.get_data(as_text=True)).group(1)
        self.client.post("/login", data={"csrf_token": csrf, "username": "admin", "password": "test-password"})

    def tearDown(self):
        self.temp_dir.cleanup()

    def csrf_token(self):
        page = self.client.get("/dashboard")
        return re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)

    def review_count(self):
        with self.app.app_context():
            return get_db().execute("SELECT COUNT(*) FROM customer_reviews").fetchone()[0]

    def test_seeded_sample_and_protected_pages(self):
        self.assertEqual(self.review_count(), 46)
        for path in ("/dashboard", "/reviews", "/analyze", "/insights"):
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_manual_analysis_saves_vader_result(self):
        response = self.client.post("/analyze", data={
            "csrf_token": self.csrf_token(),
            "review": "The mobile app is excellent and easy to use.",
            "source": "Mobile app",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"positive", response.data)
        self.assertEqual(self.review_count(), 47)

    def test_csv_import_bulk_analyzes_reviews(self):
        payload = b'review,source,date\n"The mobile app is excellent and fast",Website,2026-10-06\n"The loan service was terrible",Email,2026-10-05\n'
        response = self.client.post("/analyze", data={
            "csrf_token": self.csrf_token(),
            "file": (io.BytesIO(payload), "batch.csv"),
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Analyzed and saved 2 reviews", response.data)
        self.assertEqual(self.review_count(), 48)

    def test_filters_and_csv_export(self):
        response = self.client.get("/reviews?q=mobile&sentiment=positive")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"matching reviews", response.data)
        export = self.client.get("/export/reviews.csv?q=mobile&sentiment=positive")
        self.assertEqual(export.status_code, 200)
        self.assertIn(b"text/csv", export.content_type.encode())
        self.assertIn(b"vader_compound", export.data)
        self.assertIn(b"mobile app", export.data.lower())

    def test_sample_reload_and_database_clear(self):
        token = self.csrf_token()
        self.client.post("/database/clear", data={"csrf_token": token})
        self.assertEqual(self.review_count(), 0)
        token = self.csrf_token()
        response = self.client.post("/database/reload-samples", data={"csrf_token": token}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.review_count(), 46)

    def test_csrf_is_required_for_mutations(self):
        response = self.client.post("/database/clear", data={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.review_count(), 46)

    def test_demo_password_is_hidden_when_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            production_app = create_app({
                "TESTING": True,
                "DATABASE": str(Path(folder) / "production.sqlite3"),
                "SHOW_DEMO_CREDENTIALS": False,
            })
            response = production_app.test_client().get("/login")
        self.assertNotIn(b"admin123", response.data)

    def test_new_account_must_register_then_sign_in(self):
        client = self.app.test_client()
        page = client.get("/register")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        response = client.post("/register", data={
            "csrf_token": token,
            "username": "new-analyst",
            "password": "secure-pass-123",
            "confirm_password": "secure-pass-123",
        }, follow_redirects=True)
        self.assertIn(b"Account created", response.data)
        self.assertIn(b"Welcome back", response.data)
        token = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)
        signed_in = client.post("/login", data={
            "csrf_token": token,
            "username": "new-analyst",
            "password": "secure-pass-123",
        }, follow_redirects=True)
        self.assertIn(b"Customer pulse", signed_in.data)
        self.assertIn(b"Analyst", signed_in.data)
        analyst_page = client.get("/dashboard")
        analyst_token = re.search(r'name="csrf_token" value="([^"]+)"', analyst_page.get_data(as_text=True)).group(1)
        denied = client.post("/database/clear", data={"csrf_token": analyst_token})
        self.assertEqual(denied.status_code, 403)

    def test_registration_rejects_duplicate_usernames(self):
        client = self.app.test_client()
        page = client.get("/register")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        data = {"csrf_token": token, "username": "same-user", "password": "secure-pass-123", "confirm_password": "secure-pass-123"}
        client.post("/register", data=data)
        page = client.get("/register")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        data["csrf_token"] = token
        response = client.post("/register", data=data)
        self.assertIn(b"already registered", response.data)

    def test_postgres_adapter_translates_parameters_and_insert_ids(self):
        cursor = Mock()
        cursor.fetchone.return_value = {"id": 73}
        connection = Mock()
        connection.cursor.return_value = cursor
        database = DatabaseConnection(connection, is_postgres=True)

        result = database.execute(
            "INSERT INTO customer_reviews (review, sentiment) VALUES (?, ?)",
            ("Helpful service", "positive"),
        )

        query = cursor.execute.call_args.args[0]
        self.assertIn("VALUES (%s, %s)", query)
        self.assertTrue(query.endswith("RETURNING id"))
        self.assertEqual(result.lastrowid, 73)


if __name__ == "__main__":
    unittest.main()
