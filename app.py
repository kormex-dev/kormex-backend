"""
ProGrowth AI — Backend Server
================================
Python Flask backend handling:
  - User registration & login (JWT auth)
  - Stripe checkout sessions & webhooks
  - Subscription gating (free trial, active, cancelled)
  - Protected API routes for all AI tools

Run locally:  python app.py
Deploy:       See DEPLOY.md for Railway / Render / VPS instructions
"""

import os, json, sqlite3, hashlib, secrets, datetime, time
from functools import wraps
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
import stripe

# ──────────────────────────────────────────────
# CONFIG  (set real values in environment variables)
# ──────────────────────────────────────────────
app = Flask(__name__, static_folder='../static')
CORS(app, origins=["https://kormex.net", "https://app.kormex.net", "http://localhost:3000"])

SECRET_KEY         = os.environ.get("SECRET_KEY", "change-this-in-production-use-a-long-random-string")
STRIPE_SECRET_KEY  = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SEC = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DATABASE           = os.environ.get("DATABASE_PATH", "progrowth.db")
FRONTEND_URL       = os.environ.get("FRONTEND_URL", "https://app.kormex.net")

# Stripe price IDs — paste yours from Stripe dashboard
PRICE_IDS = {
    "starter": os.environ.get("STRIPE_PRICE_STARTER", "price_1T90Qr09xjTDj24ntlF1u2qF"),   # $49/mo
    "growth":  os.environ.get("STRIPE_PRICE_GROWTH",  "price_1T90UD09xjTDj24n6gcZ6Zzs"),   # $99/mo
    "agency":  os.environ.get("STRIPE_PRICE_AGENCY",  "price_1T90Ue09xjTDj24nc2rL2PG4"),   # $249/mo
}

stripe.api_key = STRIPE_SECRET_KEY


# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                email           TEXT    UNIQUE NOT NULL,
                name            TEXT    NOT NULL,
                password_hash   TEXT    NOT NULL,
                plan            TEXT    DEFAULT 'trial',
                trial_ends_at   INTEGER,
                stripe_customer TEXT,
                stripe_sub      TEXT,
                sub_status      TEXT    DEFAULT 'trialing',
                created_at      INTEGER DEFAULT (strftime('%s','now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id         TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS audits (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                url        TEXT,
                results    TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """)
        db.commit()


# ──────────────────────────────────────────────
# AUTH HELPERS
# ──────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password + SECRET_KEY).encode()).hexdigest()
    return f"{salt}:{h}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":")
        return h == hashlib.sha256((salt + password + SECRET_KEY).encode()).hexdigest()
    except Exception:
        return False

def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(48)
    expires = int(time.time()) + 60 * 60 * 24 * 30  # 30 days
    get_db().execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires)
    )
    get_db().commit()
    return token

def require_auth(f):
    """Decorator: verifies Bearer token, attaches user to request context."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401
        token = auth[7:]
        db = get_db()
        row = db.execute(
            "SELECT s.user_id, s.expires_at, u.* FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.id = ?",
            (token,)
        ).fetchone()
        if not row or row["expires_at"] < int(time.time()):
            return jsonify({"error": "Session expired"}), 401
        g.user = dict(row)
        return f(*args, **kwargs)
    return decorated

def require_active_sub(f):
    """Decorator: blocks access if subscription is cancelled/past_due."""
    @wraps(f)
    def decorated(*args, **kwargs):
        status = g.user.get("sub_status", "")
        trial_ok = status == "trialing" and g.user.get("trial_ends_at", 0) > int(time.time())
        if status == "active" or trial_ok:
            return f(*args, **kwargs)
        return jsonify({"error": "Subscription required", "upgrade_url": f"{FRONTEND_URL}/#pricing"}), 402
    return decorated


# ──────────────────────────────────────────────
# AUTH ROUTES
# ──────────────────────────────────────────────
@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json()
    name     = (data.get("name") or "").strip()
    email    = (data.get("email") or "").lower().strip()
    password = (data.get("password") or "")
    plan     = (data.get("plan") or "starter")

    if not name or not email or len(password) < 8:
        return jsonify({"error": "Name, email, and password (8+ chars) required"}), 400

    db = get_db()
    if db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
        return jsonify({"error": "Email already registered"}), 409

    trial_ends = int(time.time()) + 60 * 60 * 24 * 14  # 14-day trial

    # Create Stripe customer
    try:
        customer = stripe.Customer.create(email=email, name=name,
                                          metadata={"plan": plan})
        stripe_id = customer["id"]
    except Exception as e:
        stripe_id = None  # Don't fail registration if Stripe is unavailable

    db.execute(
        "INSERT INTO users (email, name, password_hash, plan, trial_ends_at, stripe_customer, sub_status) VALUES (?,?,?,?,?,?,?)",
        (email, name, hash_password(password), plan, trial_ends, stripe_id, "trialing")
    )
    db.commit()
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    token = create_session(user["id"])

    return jsonify({
        "token": token,
        "user": {
            "id": user["id"], "name": user["name"], "email": user["email"],
            "plan": user["plan"], "sub_status": user["sub_status"],
            "trial_ends_at": trial_ends
        }
    }), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    data  = request.get_json()
    email = (data.get("email") or "").lower().strip()
    pw    = (data.get("password") or "")

    db  = get_db()
    row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    if not row or not verify_password(pw, row["password_hash"]):
        return jsonify({"error": "Invalid email or password"}), 401

    token = create_session(row["id"])
    return jsonify({
        "token": token,
        "user": {
            "id": row["id"], "name": row["name"], "email": row["email"],
            "plan": row["plan"], "sub_status": row["sub_status"]
        }
    })


@app.route("/api/auth/me", methods=["GET"])
@require_auth
def me():
    u = g.user
    return jsonify({
        "id": u["id"], "name": u["name"], "email": u["email"],
        "plan": u["plan"], "sub_status": u["sub_status"],
        "trial_ends_at": u["trial_ends_at"]
    })


@app.route("/api/auth/logout", methods=["POST"])
@require_auth
def logout():
    auth  = request.headers.get("Authorization", "")[7:]
    get_db().execute("DELETE FROM sessions WHERE id = ?", (auth,))
    get_db().commit()
    return jsonify({"ok": True})


# ──────────────────────────────────────────────
# STRIPE ROUTES
# ──────────────────────────────────────────────
@app.route("/api/billing/checkout", methods=["POST"])
@require_auth
def create_checkout():
    plan    = request.get_json().get("plan", "starter")
    user    = g.user
    price   = PRICE_IDS.get(plan)

    if not price:
        return jsonify({"error": "Invalid plan"}), 400

    session = stripe.checkout.Session.create(
        customer=user["stripe_customer"],
        payment_method_types=["card"],
        line_items=[{"price": price, "quantity": 1}],
        mode="subscription",
        subscription_data={"trial_period_days": 14},
        success_url=f"{FRONTEND_URL}/app.html?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{FRONTEND_URL}/#pricing",
        metadata={"user_id": str(user["id"]), "plan": plan}
    )
    return jsonify({"checkout_url": session.url})


@app.route("/api/billing/portal", methods=["POST"])
@require_auth
def billing_portal():
    """Lets users manage their subscription (cancel, upgrade, update card)."""
    session = stripe.billing_portal.Session.create(
        customer=g.user["stripe_customer"],
        return_url=f"{FRONTEND_URL}/app.html"
    )
    return jsonify({"portal_url": session.url})


@app.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SEC)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    db = get_db()
    et = event["type"]
    obj = event["data"]["object"]

    if et in ("customer.subscription.created", "customer.subscription.updated"):
        status     = obj["status"]
        stripe_sub = obj["id"]
        plan_name  = "starter"
        # Detect plan from price ID
        for item in obj.get("items", {}).get("data", []):
            pid = item["price"]["id"]
            for name, p in PRICE_IDS.items():
                if pid == p: plan_name = name

        db.execute(
            "UPDATE users SET sub_status=?, stripe_sub=?, plan=? WHERE stripe_customer=?",
            (status, stripe_sub, plan_name, obj["customer"])
        )
        db.commit()

    elif et == "customer.subscription.deleted":
        db.execute(
            "UPDATE users SET sub_status='cancelled', plan='free' WHERE stripe_customer=?",
            (obj["customer"],)
        )
        db.commit()

    elif et == "invoice.payment_failed":
        db.execute(
            "UPDATE users SET sub_status='past_due' WHERE stripe_customer=?",
            (obj["customer"],)
        )
        db.commit()

    return jsonify({"received": True})


# ──────────────────────────────────────────────
# TOOL ROUTES  (protected — require active sub)
# ──────────────────────────────────────────────
@app.route("/api/tools/seo-audit", methods=["POST"])
@require_auth
@require_active_sub
def seo_audit():
    """
    In production: connect to a real SEO data API (e.g. DataForSEO, Semrush API).
    For now, returns a realistic demo analysis based on the URL.
    """
    url = (request.get_json().get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400

    domain = url.replace("https://", "").replace("http://", "").split("/")[0]

    # Save audit record
    get_db().execute(
        "INSERT INTO audits (user_id, url, results) VALUES (?, ?, ?)",
        (g.user["id"], url, json.dumps({"domain": domain, "status": "complete"}))
    )
    get_db().commit()

    return jsonify({
        "domain": domain,
        "overall_score": 68,
        "scores": {
            "page_speed": 55, "on_page": 74, "technical": 61,
            "content": 78, "mobile": 82, "backlinks": 44
        },
        "issues": [
            {"priority": "high",   "category": "Speed",       "fix": f"Page load time is slow — compress images and enable browser caching on {domain}"},
            {"priority": "high",   "category": "On-Page",     "fix": "Multiple pages missing meta descriptions — add unique descriptions for each page"},
            {"priority": "medium", "category": "Technical",   "fix": "3 broken internal links found — update or remove them"},
            {"priority": "medium", "category": "Schema",      "fix": "No schema markup detected — add LocalBusiness or Organization schema"},
            {"priority": "low",    "category": "Mobile",      "fix": "Some tap targets too small on mobile — increase button sizes"},
        ],
        "opportunities": [
            "Competitor gap: 'AI marketing tools' keyword — 3,200 searches/month, low competition",
            "Local SEO: No city-specific pages found — add location pages to capture local traffic",
            "Content: No blog section detected — publishing 2 posts/month could 3x organic traffic in 6 months"
        ]
    })


@app.route("/api/tools/content", methods=["POST"])
@require_auth
@require_active_sub
def generate_content():
    """
    In production: call OpenAI / Anthropic API here.
    Replace the placeholder with:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        message = client.messages.create(model="claude-opus-4-6", max_tokens=2000, messages=[...])
    """
    data    = request.get_json()
    ctype   = data.get("type", "blog")
    topic   = data.get("topic", "")
    keyword = data.get("keyword", "")

    if not topic:
        return jsonify({"error": "Topic required"}), 400

    # ── PLACEHOLDER: replace with real AI call ──
    # import anthropic
    # client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    # prompt = build_content_prompt(ctype, topic, keyword)
    # result = client.messages.create(
    #     model="claude-opus-4-6",
    #     max_tokens=2000,
    #     messages=[{"role": "user", "content": prompt}]
    # )
    # content = result.content[0].text

    content = f"[AI-generated {ctype} content about: {topic}]\n\nKeyword targeted: {keyword}\n\nTo activate real AI content generation, add your ANTHROPIC_API_KEY to the server environment and uncomment the API call in app.py."

    return jsonify({"content": content, "type": ctype, "topic": topic})


@app.route("/api/tools/keywords", methods=["POST"])
@require_auth
@require_active_sub
def keyword_research():
    """
    In production: integrate DataForSEO, Ahrefs, or Semrush API.
    API call example in DEPLOY.md.
    """
    seed = request.get_json().get("seed", "")
    if not seed:
        return jsonify({"error": "Seed keyword required"}), 400

    return jsonify({
        "seed": seed,
        "keywords": [
            {"keyword": seed,                      "volume": 22000, "difficulty": "High",   "cpc": 4.20, "opportunity": "build"},
            {"keyword": f"{seed} for small business", "volume": 3600,  "difficulty": "Low",    "cpc": 2.80, "opportunity": "target"},
            {"keyword": f"best {seed} tools",      "volume": 1900,  "difficulty": "Low",    "cpc": 5.40, "opportunity": "target"},
            {"keyword": f"affordable {seed}",      "volume": 880,   "difficulty": "Low",    "cpc": 3.10, "opportunity": "target"},
            {"keyword": f"how to do {seed}",       "volume": 4400,  "difficulty": "Medium", "cpc": 2.40, "opportunity": "content"},
            {"keyword": f"{seed} agency",          "volume": 9900,  "difficulty": "High",   "cpc": 8.70, "opportunity": "build"},
            {"keyword": f"free {seed} tools",      "volume": 3200,  "difficulty": "Low",    "cpc": 1.60, "opportunity": "target"},
        ]
    })


@app.route("/api/tools/competitor", methods=["POST"])
@require_auth
@require_active_sub
def competitor_analysis():
    url    = (request.get_json().get("url") or "").strip()
    domain = url.replace("https://", "").replace("http://", "").split("/")[0]
    return jsonify({
        "domain": domain,
        "authority": 42, "monthly_traffic": 18400, "backlinks": 2847, "keywords": 1204,
        "top_keywords": [
            {"keyword": "best marketing tools for small business", "position": 3,  "volume": 2400},
            {"keyword": "affordable seo services",                 "position": 7,  "volume": 1800},
            {"keyword": "how to rank on google",                   "position": 12, "volume": 5400},
        ],
        "gaps": [
            {"opportunity": "AI marketing tools", "volume": 3200, "difficulty": "Low",  "why": "Competitor has no content on this topic"},
            {"opportunity": "Local SEO tips",     "volume": 1800, "difficulty": "Low",  "why": "Competitor targeting national only"},
        ]
    })


# ──────────────────────────────────────────────
# HEALTH CHECK
# ──────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "service": "ProGrowth AI", "version": "1.0.0"})


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"
    print(f"✅ ProGrowth AI backend running on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
