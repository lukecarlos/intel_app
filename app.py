import importlib.util
import json
import os
import re
import sqlite3
import hashlib
import hmac
import ipaddress
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, Header, HTTPException, status
from fastapi.requests import Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware import Middleware
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
from fastapi.templating import Jinja2Templates

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
CAMPAIGN_SCRIPT = ROOT_DIR / "scripts" / "campaign_blueprint_generator.py"
REPORT_DIR = ROOT_DIR / "audit_reports" / "campaigns"
DB_PATH = APP_DIR / "intel_app.db"

BOOTSTRAP_OWNER_USERNAME = os.getenv("INTEL_APP_BOOTSTRAP_OWNER_USERNAME", "owner")
BOOTSTRAP_OWNER_KEY = os.getenv("INTEL_APP_BOOTSTRAP_OWNER_KEY", "owner-change-me")
BOOTSTRAP_COLLAB_USERNAME = os.getenv("INTEL_APP_BOOTSTRAP_COLLAB_USERNAME", "collaborator")
BOOTSTRAP_COLLAB_KEY = os.getenv("INTEL_APP_BOOTSTRAP_COLLAB_KEY", "collab-change-me")
ASSISTANT_INGEST_KEY = os.getenv("INTEL_APP_ASSISTANT_KEY", "assistant-ingest-change-me")
BLAND_WEBHOOK_KEY = os.getenv("INTEL_APP_BLAND_WEBHOOK_KEY", "bland-webhook-change-me")
SESSION_SECRET = os.getenv("INTEL_APP_SESSION_SECRET", "intel-app-session-secret-change-me")
HASH_ITERATIONS = 210000
TRUSTED_EASY_LOGIN = os.getenv("INTEL_APP_TRUST_NETWORK_LOGIN", "1") == "1"

app = FastAPI(
    title="Small Business Intelligence App",
    version="1.0.0",
    middleware=[Middleware(SessionMiddleware, secret_key=SESSION_SECRET)],
)
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


class ProposedLead(BaseModel):
    url: str = Field(min_length=5)
    phone: str = ""
    client_name: str = ""
    notes: str = ""


_DISTRESS_WORDS = {
    "frustrated", "angry", "annoyed", "upset", "distressed",
    "furious", "terrible", "awful", "horrible", "complaint",
}


def _phone_lead_needs_human(variables: dict, summary: str, call_ended_by: str) -> bool:
    """Return True if the phone lead should be routed to a human specialist."""
    if str(variables.get("capt_handoff", "")).lower() == "yes":
        return True
    # AI ended the call but no email was captured → incomplete conversation
    if call_ended_by == "AGENT" and not str(variables.get("capt_email", "")).strip():
        return True
    if summary and any(w in summary.lower() for w in _DISTRESS_WORDS):
        return True
    return False


def hash_access_key(access_key: str, salt: str | None = None) -> str:
    local_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        access_key.encode("utf-8"),
        local_salt.encode("utf-8"),
        HASH_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${HASH_ITERATIONS}${local_salt}${digest}"


def verify_access_key(access_key: str, stored_hash: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256",
            access_key.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        ).hex()
        return hmac.compare_digest(computed, digest)
    except Exception:
        return False


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                phone TEXT,
                client_name TEXT,
                source TEXT NOT NULL DEFAULT 'user',
                requires_approval INTEGER NOT NULL DEFAULT 0,
                approved INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                report_json_path TEXT,
                report_md_path TEXT,
                demo_url TEXT,
                email_draft TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                access_key_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        now = datetime.now().isoformat()
        bootstrap_users = [
            (BOOTSTRAP_OWNER_USERNAME.strip(), "owner", BOOTSTRAP_OWNER_KEY.strip()),
            (BOOTSTRAP_COLLAB_USERNAME.strip(), "collaborator", BOOTSTRAP_COLLAB_KEY.strip()),
        ]
        for username, role, key in bootstrap_users:
            if not username or not key:
                continue
            existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if existing:
                continue
            conn.execute(
                """
                INSERT INTO users (username, role, access_key_hash, active, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (username, role, hash_access_key(key), now, now),
            )


def load_campaign_module() -> Any:
    if not CAMPAIGN_SCRIPT.exists():
        raise RuntimeError(f"Campaign generator not found: {CAMPAIGN_SCRIPT}")

    spec = importlib.util.spec_from_file_location("campaign_blueprint_generator", CAMPAIGN_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load campaign generator module")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def build_email_draft(client_name: str, report: dict[str, Any]) -> str:
    weaknesses = report.get("weaknesses", [])[:3]
    weakness_line = "; ".join(weaknesses) if weaknesses else "a few conversion and follow-up opportunities"

    return (
        f"Subject: 3 Quick Revenue Wins for {client_name}\n\n"
        f"Hi {client_name} team,\n\n"
        f"I reviewed your customer-facing funnel and found three high-impact gaps: {weakness_line}.\n\n"
        "We can deploy a focused 30-60 day sprint covering intake, booking, and follow-up automation with clear KPI targets. "
        "If useful, I can share a 1-page implementation roadmap and a live demo flow tailored to your business.\n\n"
        "Best,\n"
        "Luke / Peace-Agent"
    )


def run_scan_for_lead(lead_id: int) -> None:
    module = load_campaign_module()

    with db_conn() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        url = lead["url"]
        phone = lead["phone"] or ""
        client_name = lead["client_name"] or ""

        html, soup, text = module.fetch_site(url)
        domain = module.urlparse(url).netloc.lower()
        industry = module.infer_industry(text, domain)
        signals = module.signal_checks(html, soup, provided_phone=phone)
        client = client_name.strip() or module.infer_client_name(domain, signals.get("title", ""))

        queries = module.build_query_set(client, industry, domain)
        visibility = module.search_visibility(queries, domain)
        competitors = module.find_competitors(client, industry, domain)

        strengths, weaknesses, score = module.build_strengths_weaknesses(signals, visibility)
        products = module.product_fit(industry)
        blueprint = module.campaign_blueprint(client, industry, products)
        solution_mapping = module.weakness_solution_mapping(weaknesses, products)

        report = {
            "generated_at": datetime.now().isoformat(),
            "url": url,
            "client": client,
            "industry": industry,
            "signals": signals,
            "visibility": visibility,
            "competitors": competitors,
            "score": score,
            "strengths": strengths,
            "weaknesses": weaknesses,
            "solution_mapping": solution_mapping,
            "call_transcript_scoring": {"status": "not_provided"},
            "workflow": blueprint,
        }

        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        slug = slugify(client) or f"lead-{lead_id}"
        base = REPORT_DIR / f"{stamp}_{slug}_lead{lead_id}"

        json_path = base.with_suffix(".json")
        md_path = base.with_suffix(".md")

        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        md_path.write_text(
            module.to_markdown(
                url,
                {
                    "signals": signals,
                    "visibility": visibility,
                    "competitors": competitors,
                    "strengths": strengths,
                    "weaknesses": weaknesses,
                    "score": score,
                    "solution_mapping": solution_mapping,
                    "call_transcript_scoring": report["call_transcript_scoring"],
                },
                blueprint,
            ),
            encoding="utf-8",
        )

        demo_slug = slugify(client)
        demo_url = f"https://sales-demo-host-production.up.railway.app/d/{demo_slug}" if demo_slug else ""
        email_draft = build_email_draft(client, report)

        now = datetime.now().isoformat()
        conn.execute(
            """
            UPDATE leads
            SET client_name = ?, status = 'completed', approved = 1,
                report_json_path = ?, report_md_path = ?, demo_url = ?, email_draft = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (client, str(json_path), str(md_path), demo_url, email_draft, now, lead_id),
        )


def get_role_from_session(request: Request) -> str:
    return str(request.session.get("role", "anonymous"))


def get_user_from_session(request: Request) -> str:
    return str(request.session.get("username", ""))


def get_user_id_from_session(request: Request) -> int | None:
    raw = request.session.get("user_id")
    if raw is None:
        return None


def is_trusted_client(request: Request) -> bool:
    client = request.client
    if not client or not client.host:
        return False
    try:
        ip = ipaddress.ip_address(client.host)
        return ip.is_loopback or ip.is_private
    except Exception:
        # Hostname clients in local environments (e.g. test client) are allowed.
        return client.host in {"testclient", "localhost"}


def set_session_user(request: Request, user_row: sqlite3.Row) -> None:
    request.session["role"] = user_row["role"]
    request.session["username"] = user_row["username"]
    request.session["user_id"] = user_row["id"]


def get_trusted_owner_user() -> sqlite3.Row | None:
    with db_conn() as conn:
        owner = conn.execute(
            "SELECT id, username, role, active FROM users WHERE role = 'owner' AND active = 1 ORDER BY id ASC LIMIT 1"
        ).fetchone()
    return owner


def get_effective_user(request: Request) -> sqlite3.Row | None:
    if is_authenticated(request):
        user_id = get_user_id_from_session(request)
        if user_id is None:
            return None
        with db_conn() as conn:
            user = conn.execute(
                "SELECT id, username, role, active FROM users WHERE id = ? AND active = 1",
                (user_id,),
            ).fetchone()
        return user

    if TRUSTED_EASY_LOGIN and is_trusted_client(request):
        return get_trusted_owner_user()

    return None


def get_effective_role(request: Request) -> str:
    user = get_effective_user(request)
    return str(user["role"]) if user else "anonymous"


def get_effective_username(request: Request) -> str:
    user = get_effective_user(request)
    return str(user["username"]) if user else ""


def get_effective_user_id(request: Request) -> int | None:
    user = get_effective_user(request)
    if not user:
        return None
    try:
        return int(user["id"])
    except Exception:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def is_authenticated(request: Request) -> bool:
    return get_role_from_session(request) in {"owner", "collaborator"}


def require_authenticated(request: Request) -> str:
    user = get_effective_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return str(user["role"])


def require_owner(request: Request) -> None:
    role = require_authenticated(request)
    if role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner permission required")


def create_user_account(username: str, role: str, access_key: str) -> None:
    now = datetime.now().isoformat()
    with db_conn() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Username already exists")
        conn.execute(
            """
            INSERT INTO users (username, role, access_key_hash, active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (username, role, hash_access_key(access_key), now, now),
        )


def admin_redirect(message: str = "") -> RedirectResponse:
    suffix = f"?msg={quote(message)}" if message else ""
    return RedirectResponse(url=f"/admin/users{suffix}", status_code=303)


def count_active_owners(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(1) AS c FROM users WHERE role = 'owner' AND active = 1").fetchone()
    return int(row["c"]) if row else 0


def insert_lead(
    *,
    url: str,
    phone: str,
    client_name: str,
    source: str,
    requires_approval: int,
    notes: str,
) -> int:
    now = datetime.now().isoformat()
    approved = 0 if requires_approval else 1
    status_value = "pending_approval" if requires_approval else "processing"

    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO leads (url, phone, client_name, source, requires_approval, approved, status, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (url.strip(), phone.strip(), client_name.strip(), source.strip(), requires_approval, approved, status_value, notes.strip(), now, now),
        )
        lead_id = cur.lastrowid
    return int(lead_id)


@app.on_event("startup")
def startup() -> None:
    init_db()


# Ensure the DB schema exists even when startup hooks are bypassed.
init_db()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login")
def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": "",
            "easy_login": TRUSTED_EASY_LOGIN,
        },
    )


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), access_key: str = Form(...)):
    input_username = username.strip()
    key = access_key.strip()

    with db_conn() as conn:
        user = conn.execute(
            "SELECT id, username, role, access_key_hash, active FROM users WHERE username = ?",
            (input_username,),
        ).fetchone()

    if not user or user["active"] != 1 or not verify_access_key(key, user["access_key_hash"]):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Invalid username or access key.",
                "easy_login": TRUSTED_EASY_LOGIN,
            },
            status_code=401,
        )

    set_session_user(request, user)
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/")
def dashboard(request: Request):
    if get_effective_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)

    with db_conn() as conn:
        leads = conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 100").fetchall()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "leads": leads,
            "role": get_effective_role(request),
            "username": get_effective_username(request),
            "easy_login": TRUSTED_EASY_LOGIN,
        },
    )


@app.get("/account")
def account_page(request: Request, msg: str = ""):
    if get_effective_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)

    with db_conn() as conn:
        user = conn.execute(
            "SELECT id, username, role, active, created_at, updated_at FROM users WHERE id = ?",
            (get_effective_user_id(request),),
        ).fetchone()
    if not user:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "user": user,
            "username": get_effective_username(request),
            "role": get_effective_role(request),
            "message": msg,
        },
    )


@app.post("/account/change-key")
def account_change_key(
    request: Request,
    current_access_key: str = Form(...),
    new_access_key: str = Form(...),
    confirm_new_access_key: str = Form(...),
):
    if get_effective_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)

    if new_access_key.strip() != confirm_new_access_key.strip():
        return RedirectResponse(url="/account?msg=New%20keys%20do%20not%20match.", status_code=303)
    if len(new_access_key.strip()) < 8:
        return RedirectResponse(url="/account?msg=Use%20at%20least%208%20characters.", status_code=303)

    user_id = get_effective_user_id(request)
    with db_conn() as conn:
        user = conn.execute(
            "SELECT id, access_key_hash FROM users WHERE id = ? AND active = 1",
            (user_id,),
        ).fetchone()
        if not user:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)

        if not verify_access_key(current_access_key.strip(), user["access_key_hash"]):
            return RedirectResponse(url="/account?msg=Current%20key%20is%20incorrect.", status_code=303)

        conn.execute(
            "UPDATE users SET access_key_hash = ?, updated_at = ? WHERE id = ?",
            (hash_access_key(new_access_key.strip()), datetime.now().isoformat(), user_id),
        )

    return RedirectResponse(url="/account?msg=Access%20key%20updated.", status_code=303)


@app.get("/admin/users")
def admin_users(request: Request, msg: str = ""):
    if get_effective_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    require_owner(request)

    with db_conn() as conn:
        users = conn.execute("SELECT id, username, role, active, created_at, updated_at FROM users ORDER BY id ASC").fetchall()

    return templates.TemplateResponse(
        "admin_users.html",
        {
            "request": request,
            "role": get_effective_role(request),
            "username": get_effective_username(request),
            "users": users,
            "message": msg,
            "easy_login": TRUSTED_EASY_LOGIN,
        },
    )


@app.post("/admin/users/create")
def create_user_admin(
    request: Request,
    username: str = Form(...),
    role: str = Form(...),
    access_key: str = Form(...),
):
    if get_effective_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    require_owner(request)

    role_value = role.strip().lower()
    if role_value not in {"owner", "collaborator"}:
        raise HTTPException(status_code=400, detail="Role must be owner or collaborator")

    create_user_account(username.strip(), role_value, access_key.strip())
    return admin_redirect("User created.")


@app.post("/admin/users/{user_id}/set-role")
def set_user_role_admin(request: Request, user_id: int, role: str = Form(...)):
    if get_effective_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    require_owner(request)
    role_value = role.strip().lower()
    if role_value not in {"owner", "collaborator"}:
        raise HTTPException(status_code=400, detail="Role must be owner or collaborator")

    with db_conn() as conn:
        target = conn.execute("SELECT id, role, active FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")

        if target["role"] == "owner" and role_value != "owner" and target["active"] == 1 and count_active_owners(conn) <= 1:
            return admin_redirect("Cannot demote the last active owner.")

        conn.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
            (role_value, datetime.now().isoformat(), user_id),
        )

    return admin_redirect("Role updated.")


@app.post("/admin/users/{user_id}/reset-key")
def reset_user_key_admin(request: Request, user_id: int, new_access_key: str = Form(...)):
    if get_effective_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    require_owner(request)
    if not new_access_key.strip():
        return admin_redirect("New key cannot be blank.")

    with db_conn() as conn:
        target = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")

        conn.execute(
            "UPDATE users SET access_key_hash = ?, updated_at = ? WHERE id = ?",
            (hash_access_key(new_access_key.strip()), datetime.now().isoformat(), user_id),
        )

    return admin_redirect("Access key reset.")


@app.post("/admin/users/{user_id}/toggle-active")
def toggle_user_active_admin(request: Request, user_id: int):
    if get_effective_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    require_owner(request)
    current_user_id = get_user_id_from_session(request)

    with db_conn() as conn:
        target = conn.execute("SELECT id, username, role, active FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")

        next_active = 0 if target["active"] == 1 else 1

        if target["id"] == current_user_id and next_active == 0:
            return admin_redirect("You cannot deactivate your own account.")

        if target["role"] == "owner" and target["active"] == 1 and next_active == 0 and count_active_owners(conn) <= 1:
            return admin_redirect("Cannot deactivate the last active owner.")

        conn.execute(
            "UPDATE users SET active = ?, updated_at = ? WHERE id = ?",
            (next_active, datetime.now().isoformat(), user_id),
        )

    state_text = "activated" if next_active == 1 else "deactivated"
    return admin_redirect(f"User {state_text}.")


@app.post("/lead")
def create_lead(
    request: Request,
    url: str = Form(...),
    phone: str = Form(""),
    client_name: str = Form(""),
    source: str = Form("user"),
    requires_approval: str = Form("0"),
    notes: str = Form(""),
):
    if get_effective_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)

    require_authenticated(request)

    approval = 1 if requires_approval == "1" else 0
    lead_id = insert_lead(
        url=url,
        phone=phone,
        client_name=client_name,
        source=source or "user",
        requires_approval=approval,
        notes=notes,
    )

    if approval == 0:
        try:
            run_scan_for_lead(lead_id)
        except Exception as exc:
            with db_conn() as conn:
                conn.execute(
                    "UPDATE leads SET status = 'failed', notes = COALESCE(notes, '') || ? , updated_at = ? WHERE id = ?",
                    (f"\nScan error: {exc}", datetime.now().isoformat(), lead_id),
                )

    return RedirectResponse(url="/", status_code=303)


@app.post("/lead/{lead_id}/approve")
def approve_lead(request: Request, lead_id: int):
    require_owner(request)

    with db_conn() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        conn.execute(
            "UPDATE leads SET approved = 1, status = 'processing', updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), lead_id),
        )

    try:
        run_scan_for_lead(lead_id)
    except Exception as exc:
        with db_conn() as conn:
            conn.execute(
                "UPDATE leads SET status = 'failed', notes = COALESCE(notes, '') || ? , updated_at = ? WHERE id = ?",
                (f"\nScan error: {exc}", datetime.now().isoformat(), lead_id),
            )

    return RedirectResponse(url="/", status_code=303)


@app.get("/lead/{lead_id}")
def lead_detail(request: Request, lead_id: int):
    if get_effective_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)

    with db_conn() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    report = None
    report_path = lead["report_json_path"]
    if report_path and Path(report_path).exists():
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))

    return templates.TemplateResponse(
        "lead_detail.html",
        {
            "request": request,
            "lead": lead,
            "report": report,
            "role": get_effective_role(request),
            "username": get_effective_username(request),
        },
    )


@app.get("/report/{lead_id}/{fmt}")
def report_file(request: Request, lead_id: int, fmt: str):
    if get_effective_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    require_authenticated(request)

    if fmt not in {"json", "md"}:
        raise HTTPException(status_code=400, detail="Format must be json or md")

    with db_conn() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    target = lead["report_json_path"] if fmt == "json" else lead["report_md_path"]
    if not target or not Path(target).exists():
        raise HTTPException(status_code=404, detail="Report file not found")

    return FileResponse(path=target)


@app.post("/api/leads/propose")
def propose_lead(
    payload: ProposedLead,
    x_assistant_key: str | None = Header(default=None),
):
    if not x_assistant_key or x_assistant_key.strip() != ASSISTANT_INGEST_KEY:
        raise HTTPException(status_code=401, detail="Invalid assistant key")

    lead_id = insert_lead(
        url=payload.url,
        phone=payload.phone,
        client_name=payload.client_name,
        source="assistant",
        requires_approval=1,
        notes=payload.notes,
    )

    return {
        "lead_id": lead_id,
        "status": "pending_approval",
        "message": "Lead proposed and queued for owner approval.",
    }


@app.post("/api/leads/phone")
async def phone_lead_webhook(
    request: Request,
    x_bland_webhook_key: str | None = Header(default=None),
):
    """
    Bland AI end-of-call webhook. Creates a lead from a completed sales-intake call.
    Automatically flags for human review if: caller asked for human, AI gave up, or
    distress sentiment detected in summary.
    """
    if not x_bland_webhook_key or x_bland_webhook_key.strip() != BLAND_WEBHOOK_KEY:
        raise HTTPException(status_code=401, detail="Invalid webhook key")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    variables: dict = body.get("variables") or {}
    summary: str = body.get("summary") or ""
    call_ended_by: str = body.get("call_ended_by") or ""
    caller_phone: str = body.get("from") or ""

    capt_name = str(variables.get("capt_name", "")).strip()
    capt_email = str(variables.get("capt_email", "")).strip()
    capt_website = str(variables.get("capt_website", "")).strip()
    capt_biz_name = str(variables.get("capt_biz_name", "")).strip()
    capt_industry = str(variables.get("capt_industry", "")).strip()
    capt_problem = str(variables.get("capt_problem", "")).strip()

    needs_human = _phone_lead_needs_human(variables, summary, call_ended_by)

    # Use captured website, fall back to a tel: URI so url NOT NULL is satisfied
    url = (
        capt_website
        if capt_website and capt_website.startswith("http")
        else f"tel:{caller_phone or 'unknown'}"
    )
    client_name = capt_biz_name or capt_name or caller_phone or "Phone Lead"

    notes_parts: list[str] = []
    if capt_email:
        notes_parts.append(f"Email: {capt_email}")
    if capt_industry:
        notes_parts.append(f"Industry: {capt_industry}")
    if capt_problem:
        notes_parts.append(f"Problem: {capt_problem}")
    if summary:
        notes_parts.append(f"AI Summary: {summary}")
    notes = "\n".join(notes_parts)

    lead_id = insert_lead(
        url=url,
        phone=caller_phone,
        client_name=client_name,
        source="phone_ai",
        requires_approval=1 if needs_human else 0,
        notes=notes,
    )

    return {
        "lead_id": lead_id,
        "status": "needs_human_review" if needs_human else "processing",
        "message": "Phone lead ingested successfully.",
    }
