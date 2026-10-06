from __future__ import annotations

import csv
import hmac
import io
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, abort, current_app, flash, g, make_response, redirect, render_template, request, send_file, session, url_for
import psycopg
from markupsafe import escape
from werkzeug.security import check_password_hash, generate_password_hash
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from database import connect_database, initialize_database, load_sample_data, normalize_date

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_FILE = BASE_DIR / "sample_reviews.csv"
SENTIMENT_ANALYZER = SentimentIntensityAnalyzer()

CATEGORY_PATTERNS = [
    ("Loans", re.compile(r"\b(loan|loans|mortgage|emi|installment|lending|borrow|credit limit)\b", re.I)),
    ("Fees", re.compile(r"\b(fee|fees|charge|charges|penalty|overdraft|commission|cost|rate)\b", re.I)),
    ("App", re.compile(r"\b(app|mobile|login|log in|logged out|crash|screen|online banking|website|digital)\b", re.I)),
    ("Cards", re.compile(r"\b(card|cards|debit|credit card|atm|cash machine|pin)\b", re.I)),
    ("Payments", re.compile(r"\b(payment|payments|transfer|transaction|deposit|withdraw|refund|bill pay)\b", re.I)),
    ("Branch", re.compile(r"\b(branch|queue|teller|cashier|office)\b", re.I)),
    ("Service", re.compile(r"\b(service|support|staff|agent|call|representative|response|help|customer care)\b", re.I)),
    ("Accounts", re.compile(r"\b(account|accounts|balance|statement|savings|saving|account opening)\b", re.I)),
]


def detect_category(text: str) -> str:
    for category, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return category
    return "General"


def analyze_review(text: str) -> dict[str, Any]:
    scores = SENTIMENT_ANALYZER.polarity_scores(text)
    compound = float(scores["compound"])
    sentiment = "positive" if compound >= 0.05 else "negative" if compound <= -0.05 else "neutral"
    return {"sentiment": sentiment, "score": compound, "category": detect_category(text), **scores}


def calculate_chi(reviews: list[Any]) -> int:
    if not reviews:
        return 0
    happy_points = sum(
        1.0 if review["sentiment"] == "positive" else 0.5 if review["sentiment"] == "neutral" else 0.0
        for review in reviews
    )
    return round(happy_points / len(reviews) * 100)


def get_db():
    if "db" not in g:
        g.db = connect_database(current_app.config.get("DATABASE_URL"), current_app.config["DATABASE"])
    return g.db


def close_db(_error: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin") and not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(24)
    return session["csrf_token"]


def build_filters(args) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    sentiment = args.get("sentiment", "").strip().lower()
    if sentiment in {"positive", "neutral", "negative"}:
        clauses.append("sentiment = ?")
        values.append(sentiment)
    category = args.get("category", "").strip()
    if category and category != "all":
        clauses.append("category = ?")
        values.append(category)
    source = args.get("source", "").strip()
    if source and source != "all":
        clauses.append("source = ?")
        values.append(source)
    query = args.get("q", "").strip()[:120]
    if query:
        clauses.append("review LIKE ? ESCAPE '\\'")
        escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        values.append(f"%{escaped_query}%")
    period = args.get("period", "all")
    if period in {"7", "30", "90"}:
        clauses.append("review_date >= ?")
        values.append((date.today() - timedelta(days=int(period) - 1)).isoformat())
    return clauses, values


def fetch_reviews(args=None) -> list[Any]:
    clauses, values = build_filters(args if args is not None else request.args)
    query = "SELECT * FROM customer_reviews"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY review_date DESC, id DESC"
    return get_db().execute(query, values).fetchall()


def sentiment_counts(reviews: list[Any]) -> dict[str, int]:
    result = {"positive": 0, "neutral": 0, "negative": 0}
    for review in reviews:
        result[review["sentiment"]] += 1
    return result


def build_trend(reviews: list[Any], days: int = 7) -> dict[str, list[Any]]:
    dates = [(date.today() - timedelta(days=days - index - 1)).isoformat() for index in range(days)]
    counts = {day: {"positive": 0, "neutral": 0, "negative": 0} for day in dates}
    for review in reviews:
        if review["review_date"] in counts:
            counts[review["review_date"]][review["sentiment"]] += 1
    return {
        "labels": [datetime.fromisoformat(day).strftime("%b %d") for day in dates],
        "positive": [counts[day]["positive"] for day in dates],
        "neutral": [counts[day]["neutral"] for day in dates],
        "negative": [counts[day]["negative"] for day in dates],
    }


def category_report(reviews: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = {}
    for review in reviews:
        grouped.setdefault(review["category"], []).append(review)
    report = []
    for category, rows in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        counts = sentiment_counts(rows)
        report.append({"category": category, "total": len(rows), **counts, "chi": calculate_chi(rows)})
    return report


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True, static_folder="public", static_url_path="")
    is_vercel = os.environ.get("VERCEL") == "1"
    is_production = is_vercel or os.environ.get("FLASK_ENV") == "production"
    database_url = os.environ.get("DATABASE_URL", "").strip() or None
    if database_url and database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "" if is_vercel else "dev-only-change-this-secret"),
        DATABASE=os.environ.get("DATABASE_PATH", str(Path(app.instance_path) / "customer_happiness.sqlite3")),
        DATABASE_URL=database_url,
        ADMIN_USERNAME=os.environ.get("ADMIN_USERNAME", "admin"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", "" if is_vercel else "admin123"),
        IS_VERCEL=is_vercel,
        IS_PRODUCTION=is_production,
        SHOW_DEMO_CREDENTIALS=os.environ.get("SHOW_DEMO_CREDENTIALS", "false" if is_production else "true").lower() == "true",
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=is_production,
    )
    if test_config:
        app.config.update(test_config)
        if "DATABASE" in test_config and "DATABASE_URL" not in test_config:
            app.config["DATABASE_URL"] = None
    # Never crash at import time: a crash shows Vercel's useless "FUNCTION_INVOCATION_FAILED".
    # Instead, remember what is wrong and show it on a readable page (and on /health).
    app.config["STARTUP_ERRORS"] = []

    def collect_config_problems() -> list[str]:
        problems: list[str] = []
        if app.config["IS_VERCEL"] and not app.config["DATABASE_URL"]:
            problems.append("DATABASE_URL is not set in Vercel -> Settings -> Environment Variables (use the Supabase Transaction Pooler URL, port 6543).")
        if app.config["IS_VERCEL"] and not app.config["SECRET_KEY"]:
            problems.append("SECRET_KEY is not set in Vercel -> Settings -> Environment Variables.")
        if app.config["IS_VERCEL"] and not app.config["ADMIN_PASSWORD"]:
            problems.append("ADMIN_PASSWORD is not set in Vercel -> Settings -> Environment Variables.")
        return problems

    def start_database() -> list[str]:
        problems = collect_config_problems()
        if problems:
            return problems
        app.config["ADMIN_PASSWORD_HASH"] = generate_password_hash(app.config["ADMIN_PASSWORD"])
        try:
            if not app.config["DATABASE_URL"]:
                os.makedirs(app.instance_path, exist_ok=True)
            initialize_database(app.config.get("DATABASE_URL"), app.config["DATABASE"], SAMPLE_FILE, analyze_review)
        except Exception as error:  # noqa: BLE001 - report any startup failure on the page instead of crashing
            return [f"Database connection/setup failed: {type(error).__name__}: {str(error)[:300]}"]
        return []

    app.config["STARTUP_ERRORS"] = start_database()
    app.teardown_appcontext(close_db)

    @app.before_request
    def startup_guard():
        # Retry once per request in case the first attempt failed (e.g. a slow database wake-up).
        if app.config["STARTUP_ERRORS"]:
            app.config["STARTUP_ERRORS"] = start_database()
        if request.path == "/health":
            return None
        if app.config["STARTUP_ERRORS"]:
            items = "".join(f"<li>{escape(message)}</li>" for message in app.config["STARTUP_ERRORS"])
            page = (
                "<!doctype html><meta charset='utf-8'><title>Setup needed</title>"
                "<body style='font-family:system-ui;max-width:680px;margin:48px auto;padding:0 16px;line-height:1.5'>"
                "<h1>App setup is incomplete</h1><p>The app started, but could not finish setup:</p>"
                f"<ul>{items}</ul><p>Fix the item(s) above, then redeploy on Vercel.</p></body>"
            )
            return make_response(page, 503)
        return None

    @app.get("/health")
    def health():
        errors = app.config["STARTUP_ERRORS"]
        return {"ok": not errors, "problems": errors}, (503 if errors else 200)

    @app.before_request
    def verify_csrf():
        if request.method == "POST":
            expected = session.get("csrf_token", "")
            provided = request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(expected, provided):
                abort(400, description="The form expired. Refresh and try again.")

    @app.context_processor
    def inject_template_helpers():
        return {
            "csrf_token": csrf_token,
            "admin_username": session.get("username", "Administrator"),
            "show_demo_credentials": current_app.config["SHOW_DEMO_CREDENTIALS"],
            "account_role": "Administrator" if session.get("is_admin") else "Analyst",
            "can_manage_database": bool(session.get("is_admin")),
            "database_label": "Supabase Postgres" if current_app.config.get("DATABASE_URL") else "SQLite local database",
            "database_location": "Supabase project" if current_app.config.get("DATABASE_URL") else "instance/customer_happiness.sqlite3",
        }

    @app.route("/")
    def index():
        is_authenticated = session.get("is_admin") or session.get("user_id")
        return redirect(url_for("dashboard" if is_authenticated else "login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("is_admin") or session.get("user_id"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            account = get_db().execute(
                "SELECT id, username, password_hash FROM app_users WHERE LOWER(username) = LOWER(?)",
                (username,),
            ).fetchone()
            valid_demo_admin = (
                hmac.compare_digest(username.lower(), current_app.config["ADMIN_USERNAME"].lower())
                and check_password_hash(current_app.config["ADMIN_PASSWORD_HASH"], password)
            )
            valid_account = account is not None and check_password_hash(account["password_hash"], password)
            if valid_demo_admin or valid_account:
                session.clear()
                if valid_demo_admin:
                    session["is_admin"] = True
                    session["username"] = current_app.config["ADMIN_USERNAME"]
                else:
                    session["user_id"] = account["id"]
                    session["username"] = account["username"]
                flash("Welcome back. Your analytics workspace is ready.", "success")
                next_url = request.args.get("next", "")
                if next_url.startswith("/") and not next_url.startswith("//"):
                    return redirect(next_url)
                return redirect(url_for("dashboard"))
            flash("Username or password was not recognized.", "error")
        return render_template("login.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if session.get("is_admin") or session.get("user_id"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
                flash("Username must be 3–32 characters using letters, numbers, dots, dashes, or underscores.", "error")
            elif hmac.compare_digest(username.lower(), current_app.config["ADMIN_USERNAME"].lower()):
                flash("That username is reserved for the administrator.", "error")
            elif len(password) < 8:
                flash("Password must be at least 8 characters.", "error")
            elif password != confirm_password:
                flash("The passwords do not match.", "error")
            else:
                try:
                    get_db().execute(
                        "INSERT INTO app_users (username, password_hash) VALUES (?, ?)",
                        (username, generate_password_hash(password)),
                    )
                    get_db().commit()
                    flash("Account created. Sign in with your new username and password.", "success")
                    return redirect(url_for("login"))
                except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation):
                    get_db().rollback()
                    flash("That username is already registered.", "error")
        return render_template("register.html")

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        flash("You have been signed out.", "success")
        return redirect(url_for("login"))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        selected_period = request.args.get("period", "30")
        filters = {"period": selected_period}
        reviews = fetch_reviews(filters)
        counts = sentiment_counts(reviews)
        total = len(reviews)
        return render_template(
            "dashboard.html",
            page="dashboard",
            reviews=reviews[:6],
            total=total,
            counts=counts,
            chi=calculate_chi(reviews),
            trend=build_trend(reviews),
            category_report=category_report(reviews),
            selected_period=selected_period,
        )

    @app.get("/reviews")
    @login_required
    def reviews_page():
        reviews = fetch_reviews()
        db = get_db()
        categories = [row[0] for row in db.execute("SELECT DISTINCT category FROM customer_reviews ORDER BY category")]
        sources = [row[0] for row in db.execute("SELECT DISTINCT source FROM customer_reviews ORDER BY source")]
        return render_template(
            "reviews.html", page="reviews", reviews=reviews, categories=categories, sources=sources,
            filters={key: request.args.get(key, "") for key in ("q", "sentiment", "category", "source", "period")},
        )

    @app.route("/analyze", methods=["GET", "POST"])
    @login_required
    def analyze_page():
        analyzed: list[dict[str, Any]] = []
        if request.method == "POST":
            upload = request.files.get("file")
            if upload and upload.filename:
                if not upload.filename.lower().endswith(".csv"):
                    flash("Please upload a .csv file.", "error")
                    return render_template("analyze.html", page="analyze", analyzed=analyzed)
                try:
                    content = upload.stream.read().decode("utf-8-sig")
                    reader = csv.DictReader(io.StringIO(content))
                    if not reader.fieldnames:
                        raise ValueError("The CSV file is empty.")
                    normalized_headers = {header.strip().lower(): header for header in reader.fieldnames if header}
                    review_column = next((normalized_headers[key] for key in ("review", "text", "feedback", "comment") if key in normalized_headers), None)
                    if not review_column:
                        raise ValueError("CSV needs a review, text, feedback, or comment column.")
                    source_column = next((normalized_headers[key] for key in ("source", "channel") if key in normalized_headers), None)
                    date_column = next((normalized_headers[key] for key in ("date", "review_date") if key in normalized_headers), None)
                    for row in reader:
                        text = (row.get(review_column) or "").strip()[:2000]
                        if not text:
                            continue
                        result = analyze_review(text)
                        source = (row.get(source_column) if source_column else "CSV upload") or "CSV upload"
                        review_date = normalize_date(row.get(date_column) if date_column else None)
                        cursor = get_db().execute(
                            "INSERT INTO customer_reviews (review, review_date, source, sentiment, score, category) VALUES (?, ?, ?, ?, ?, ?)",
                            (text, review_date, source.strip()[:80], result["sentiment"], result["score"], result["category"]),
                        )
                        analyzed.append({"id": cursor.lastrowid, "review": text, "source": source.strip(), "review_date": review_date, **result})
                    get_db().commit()
                    flash(f"Analyzed and saved {len(analyzed)} reviews from the CSV.", "success")
                except (UnicodeDecodeError, csv.Error, ValueError) as error:
                    get_db().rollback()
                    flash(str(error), "error")
            else:
                text = request.form.get("review", "").strip()[:2000]
                source = request.form.get("source", "Manual entry").strip()[:80] or "Manual entry"
                if not text:
                    flash("Enter a customer review before analyzing.", "error")
                else:
                    result = analyze_review(text)
                    review_date = date.today().isoformat()
                    cursor = get_db().execute(
                        "INSERT INTO customer_reviews (review, review_date, source, sentiment, score, category) VALUES (?, ?, ?, ?, ?, ?)",
                        (text, review_date, source, result["sentiment"], result["score"], result["category"]),
                    )
                    get_db().commit()
                    analyzed = [{"id": cursor.lastrowid, "review": text, "source": source, "review_date": review_date, **result}]
                    flash(f"Review analyzed and saved to {current_app.config['DATABASE_URL'] and 'Supabase Postgres' or 'SQLite'}.", "success")
        return render_template("analyze.html", page="analyze", analyzed=analyzed)

    @app.get("/insights")
    @login_required
    def insights():
        reviews = fetch_reviews()
        counts = sentiment_counts(reviews)
        report = category_report(reviews)
        positive_themes = sorted((row for row in report if row["positive"]), key=lambda row: (-row["positive"], row["category"]))[:5]
        negative_themes = sorted((row for row in report if row["negative"]), key=lambda row: (-row["negative"], row["category"]))[:5]
        db_count = get_db().execute("SELECT COUNT(*) FROM customer_reviews").fetchone()[0]
        return render_template(
            "insights.html", page="insights", reviews=reviews, counts=counts, total=len(reviews),
            chi=calculate_chi(reviews), report=report, positive_themes=positive_themes,
            negative_themes=negative_themes, trend=build_trend(reviews), database_count=db_count,
        )

    @app.get("/export/reviews.csv")
    @login_required
    def export_reviews():
        reviews = fetch_reviews()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["id", "review", "date", "source", "sentiment", "vader_compound", "category"])
        writer.writerows([[row["id"], row["review"], row["review_date"], row["source"], row["sentiment"], row["score"], row["category"]] for row in reviews])
        response = make_response("\ufeff" + output.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = "attachment; filename=customer-reviews.csv"
        return response

    @app.get("/export/report.csv")
    @login_required
    def export_report():
        reviews = fetch_reviews()
        counts = sentiment_counts(reviews)
        report = category_report(reviews)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Customer Happiness Index", calculate_chi(reviews)])
        writer.writerow(["Total reviews", len(reviews)])
        writer.writerow(["Positive", counts["positive"], "Neutral", counts["neutral"], "Negative", counts["negative"]])
        writer.writerow([])
        writer.writerow(["Category", "Reviews", "Positive", "Neutral", "Negative", "Category CHI"])
        for row in report:
            writer.writerow([row["category"], row["total"], row["positive"], row["neutral"], row["negative"], row["chi"]])
        response = make_response("\ufeff" + output.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = "attachment; filename=customer-happiness-report.csv"
        return response

    @app.get("/sample-reviews.csv")
    @login_required
    def download_sample():
        return send_file(SAMPLE_FILE, as_attachment=True, download_name="sample_reviews.csv", mimetype="text/csv")

    @app.post("/database/reload-samples")
    @admin_required
    def reload_samples():
        get_db().execute("DELETE FROM customer_reviews")
        get_db().commit()
        inserted = load_sample_data(current_app.config.get("DATABASE_URL"), current_app.config["DATABASE"], SAMPLE_FILE, analyze_review)
        flash(f"Sample dataset reloaded: {inserted} reviews analyzed with VADER.", "success")
        return redirect(url_for("insights"))

    @app.post("/database/clear")
    @admin_required
    def clear_database():
        get_db().execute("DELETE FROM customer_reviews")
        get_db().commit()
        flash("All review records were deleted from the database.", "success")
        return redirect(url_for("insights"))

    @app.errorhandler(413)
    def upload_too_large(_error):
        flash("Upload is too large. Keep CSV files under 2 MB.", "error")
        return redirect(url_for("analyze_page")), 413

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("FLASK_DEBUG") == "1")
