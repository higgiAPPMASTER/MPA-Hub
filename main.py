from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import uvicorn, os, secrets, hashlib
import stripe
from supabase import create_client

app = FastAPI()

# ── Config ─────────────────────────────────────────────────────────────────────
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY      = os.environ.get("SUPABASE_SERVICE_KEY", "")
STRIPE_SECRET     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID   = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SITE_URL          = os.environ.get("SITE_URL", "http://localhost:8000")
SECRET_KEY        = os.environ.get("SECRET_KEY", secrets.token_hex(32))

stripe.api_key = STRIPE_SECRET
db = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

SESSIONS: dict[str, str] = {}

def hash_pw(pw: str) -> str:
    return hashlib.sha256((pw + SECRET_KEY).encode()).hexdigest()

def get_user(request: Request) -> str:
    sid = request.cookies.get("sid")
    return SESSIONS.get(sid, "") if sid else ""

# ── HTML ───────────────────────────────────────────────────────────────────────
BASE_STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700;900&family=Source+Sans+Pro:wght@300;400;600;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f0f0f;color:#fff;font-family:'Source Sans Pro',sans-serif;min-height:100vh}
.font-display{font-family:'Playfair Display',serif}
nav{position:fixed;top:0;width:100%;background:rgba(10,10,10,.95);backdrop-filter:blur(12px);border-bottom:1px solid #1c1c1c;z-index:100;padding:0 32px;height:80px;display:flex;align-items:center;justify-content:space-between}
.logo{font-family:'Playfair Display',serif;font-size:42px;font-weight:900;color:#f59e0b;letter-spacing:.02em;line-height:1}
.logo span{color:#fff}
.nav-links{display:flex;align-items:center;gap:20px}
.nav-link{color:#9ca3af;font-size:13px;text-decoration:none;font-weight:600;transition:color .2s}
.nav-link:hover{color:#fff}
.btn{display:inline-block;background:#f59e0b;color:#000;font-weight:700;padding:10px 24px;border-radius:8px;text-decoration:none;font-size:14px;border:none;cursor:pointer;transition:all .2s;font-family:'Source Sans Pro',sans-serif}
.btn:hover{background:#fbbf24;transform:translateY(-1px);box-shadow:0 4px 20px rgba(245,158,11,.4)}
.btn-lg{font-size:18px;padding:16px 40px;border-radius:12px}
.btn-outline{background:transparent;color:#f59e0b;border:2px solid #f59e0b}
.btn-outline:hover{background:#f59e0b;color:#000}
.card{background:#161616;border:1px solid #262626;border-radius:20px;padding:32px;transition:all .2s}
.card:hover{border-color:rgba(245,158,11,.3);transform:translateY(-2px)}
.gold{color:#f59e0b}
.error-box{background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);color:#f87171;border-radius:10px;padding:14px 16px;font-size:13px;margin-bottom:16px}
.success-box{background:rgba(74,222,128,.08);border:1px solid rgba(74,222,128,.2);color:#4ade80;border-radius:10px;padding:14px 16px;font-size:13px;margin-bottom:16px}
input[type=email],input[type=password],input[type=text]{width:100%;background:#0a0a0a;border:1px solid #2a2a2a;border-radius:10px;padding:13px 16px;color:#fff;font-size:14px;font-family:'Source Sans Pro',sans-serif;outline:none;transition:border .2s;margin-bottom:4px}
input:focus{border-color:#f59e0b}
label{display:block;color:#9ca3af;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;margin-top:16px}
form button[type=submit]{width:100%;margin-top:20px}
footer{border-top:1px solid #1a1a1a;padding:28px 32px;text-align:center;color:#374151;font-size:11px;line-height:1.7}
</style>
"""

HOME_HTML = BASE_STYLE + """
<nav>
  <div class="logo">Money <span>Picks</span> Arena</div>
  <div class="nav-links">
    <a href="/login" class="nav-link">Member Login</a>
    <a href="/subscribe" class="btn">Subscribe — $50/mo</a>
  </div>
</nav>

<div style="padding-top:80px">
  <!-- HERO -->
  <section style="padding:100px 24px 80px;text-align:center;position:relative;overflow:hidden">
    <div style="position:absolute;inset:0;background:radial-gradient(ellipse at 50% 40%,rgba(245,158,11,.04),transparent 65%);pointer-events:none"></div>
    <div style="position:relative;max-width:760px;margin:0 auto">
      <div style="display:inline-flex;align-items:center;gap:8px;background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.15);border-radius:999px;padding:6px 18px;margin-bottom:28px">
        <span style="width:7px;height:7px;background:#4ade80;border-radius:50%;animation:p 2s infinite"></span>
        <span style="font-size:11px;font-weight:700;letter-spacing:.12em;color:#f59e0b">PICKS UPDATED DAILY</span>
      </div>
      <style>@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}</style>
      <h1 class="font-display" style="font-size:clamp(42px,7vw,76px);line-height:1.05;margin-bottom:20px">
        Score Big in the<br><span class="gold">Money Picks Arena</span>
      </h1>
      <p style="color:#9ca3af;font-size:18px;margin-bottom:10px;max-width:520px;margin-left:auto;margin-right:auto;line-height:1.6">
        Data-driven picks for <strong style="color:#fff">4 sports</strong> — MLB, NHL, NBA &amp; NFL — powered by real stats and sportsbook lines.
      </p>
      <p style="color:#4b5563;font-size:13px;letter-spacing:.14em;margin-bottom:40px">ONE SUBSCRIPTION. ALL 4 SPORTS.</p>
      <div style="display:flex;flex-direction:column;align-items:center;gap:12px">
        <a href="/subscribe" class="btn btn-lg" style="box-shadow:0 0 40px rgba(245,158,11,.3)">⚡ SUBSCRIBE NOW — $50/MO</a>
        <a href="/login" style="color:#4b5563;font-size:13px;text-decoration:none">Already a member? Login →</a>
      </div>
    </div>
  </section>

  <!-- SPORTS -->
  <section style="padding:60px 24px;max-width:1000px;margin:0 auto">
    <h2 class="font-display" style="text-align:center;font-size:32px;margin-bottom:10px">Choose Your Sport</h2>
    <p style="text-align:center;color:#6b7280;margin-bottom:40px">Money Picks Arena shows you the plays — you choose what to do.</p>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px">
      <div class="card" style="text-align:center">
        <div style="font-size:44px;margin-bottom:12px">⚾</div>
        <div style="font-size:10px;font-weight:700;letter-spacing:.1em;color:#fff;background:#1d4ed8;padding:3px 10px;border-radius:4px;display:inline-block;margin-bottom:10px">BASEBALL</div>
        <h3 class="font-display" style="font-size:18px;margin-bottom:8px">MLB MoneyBall</h3>
        <p style="color:#6b7280;font-size:12px;line-height:1.6">Career BA vs pitcher, H/A splits, hot streaks. Top 9 picks daily.</p>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:44px;margin-bottom:12px">🏒</div>
        <div style="font-size:10px;font-weight:700;letter-spacing:.1em;color:#fff;background:#15803d;padding:3px 10px;border-radius:4px;display:inline-block;margin-bottom:10px">HOCKEY</div>
        <h3 class="font-display" style="font-size:18px;margin-bottom:8px">NHL Money Shots</h3>
        <p style="color:#6b7280;font-size:12px;line-height:1.6">Shots on goal picks with live FanDuel sportsbook lines.</p>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:44px;margin-bottom:12px">🏀</div>
        <div style="font-size:10px;font-weight:700;letter-spacing:.1em;color:#fff;background:#7e22ce;padding:3px 10px;border-radius:4px;display:inline-block;margin-bottom:10px">BASKETBALL</div>
        <h3 class="font-display" style="font-size:18px;margin-bottom:8px">NBA Money Buckets</h3>
        <p style="color:#6b7280;font-size:12px;line-height:1.6">75%+ hit rate picks for Pts, Reb, Ast, 3PM.</p>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:44px;margin-bottom:12px">🏈</div>
        <div style="font-size:10px;font-weight:700;letter-spacing:.1em;color:#fff;background:#b45309;padding:3px 10px;border-radius:4px;display:inline-block;margin-bottom:10px">FOOTBALL</div>
        <h3 class="font-display" style="font-size:18px;margin-bottom:8px">NFL Money Bombs</h3>
        <p style="color:#6b7280;font-size:12px;line-height:1.6">Weekly NFL player prop picks with matchup analysis.</p>
      </div>
    </div>
  </section>

  <!-- PRICING -->
  <section style="padding:60px 24px;max-width:460px;margin:0 auto">
    <div class="card" style="border-color:rgba(245,158,11,.35);text-align:center;padding:44px">
      <h2 class="font-display" style="font-size:26px;margin-bottom:4px">All Access Pass</h2>
      <p style="color:#6b7280;margin-bottom:24px;font-size:14px">One subscription. Every sport.</p>
      <div style="font-size:68px;font-weight:900;color:#fff;line-height:1;font-family:'Playfair Display',serif">$50</div>
      <div style="color:#6b7280;margin-bottom:28px">per month</div>
      <div style="text-align:left;margin-bottom:28px;display:flex;flex-direction:column;gap:10px">
        <div style="color:#d1d5db;font-size:13px">⚾&nbsp; MLB MoneyBall — Daily Baseball Picks</div>
        <div style="color:#d1d5db;font-size:13px">🏒&nbsp; NHL Money Shots — Daily Hockey Picks</div>
        <div style="color:#d1d5db;font-size:13px">🏀&nbsp; NBA Money Buckets — Daily Basketball Picks</div>
        <div style="color:#d1d5db;font-size:13px">🏈&nbsp; NFL Money Bombs — Weekly Football Picks</div>
        <div style="color:#d1d5db;font-size:13px">✅&nbsp; Real sportsbook lines included</div>
        <div style="color:#d1d5db;font-size:13px">✅&nbsp; Cancel anytime</div>
      </div>
      <a href="/subscribe" class="btn btn-lg" style="display:block;width:100%;text-align:center;box-shadow:0 0 30px rgba(245,158,11,.25)">SUBSCRIBE NOW</a>
    </div>
  </section>
</div>

<footer>
  <div style="font-family:'Playfair Display',serif;font-size:16px;color:#4b5563;margin-bottom:10px">Money Picks Arena</div>
  <p>Money Picks Arena provides sports picks for entertainment purposes only. Not a sportsbook. Please gamble responsibly. Must be 21+.</p>
  <p style="margin-top:6px">© 2026 Money Picks Arena. All Rights Reserved.</p>
</footer>
"""

LOGIN_HTML = BASE_STYLE + """
<nav>
  <div class="logo">Money <span>Picks</span> Arena</div>
  <div class="nav-links">
    <a href="/subscribe" class="btn">Subscribe — $50/mo</a>
  </div>
</nav>
<div style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;padding-top:100px">
  <div style="width:100%;max-width:400px">
    <div style="text-align:center;margin-bottom:28px">
      <div class="font-display gold" style="font-size:22px;margin-bottom:4px">Money Picks Arena</div>
      <h1 style="font-size:26px;font-weight:900;margin-bottom:4px">Member Login</h1>
      <p style="color:#6b7280;font-size:13px">Access your picks dashboard</p>
    </div>
    <div class="card">
      {error}
      <form method="post" action="/login">
        <label>Email Address</label>
        <input type="email" name="email" placeholder="you@example.com" required autocomplete="email"/>
        <label>Password</label>
        <input type="password" name="password" placeholder="••••••••" required autocomplete="current-password"/>
        <button type="submit" class="btn" style="font-size:16px;padding:14px">LOGIN →</button>
      </form>
      <p style="text-align:center;margin-top:18px;font-size:13px;color:#4b5563">
        Not a member? <a href="/subscribe" style="color:#f59e0b;text-decoration:none">Subscribe for $50/mo</a>
      </p>
    </div>
    <p style="text-align:center;margin-top:16px"><a href="/" style="color:#374151;font-size:12px;text-decoration:none">← Back to home</a></p>
  </div>
</div>
"""

REGISTER_HTML = BASE_STYLE + """
<nav>
  <div class="logo">Money <span>Picks</span> Arena</div>
</nav>
<div style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;padding-top:100px">
  <div style="width:100%;max-width:420px">
    <div style="text-align:center;margin-bottom:28px">
      <div style="font-size:48px;margin-bottom:8px">🎉</div>
      <h1 class="font-display" style="font-size:26px;margin-bottom:4px">Payment Successful!</h1>
      <p style="color:#6b7280;font-size:14px">Create your account to access all 4 sports picks.</p>
    </div>
    <div class="card">
      {error}
      <form method="post" action="/register">
        <input type="hidden" name="session_id" value="{session_id}"/>
        <label>Email Address</label>
        <input type="email" name="email" value="{email}" readonly style="background:#1a1a1a;color:#9ca3af;cursor:not-allowed"/>
        <label>Create a Password</label>
        <input type="password" name="password" placeholder="Choose a strong password (min 6 chars)" required minlength="6" autocomplete="new-password"/>
        <label>Confirm Password</label>
        <input type="password" name="confirm" placeholder="Repeat your password" required minlength="6" autocomplete="new-password"/>
        <button type="submit" class="btn" style="font-size:16px;padding:14px">CREATE ACCOUNT &amp; LOGIN →</button>
      </form>
    </div>
  </div>
</div>
"""

DASHBOARD_HTML = BASE_STYLE + """
<nav>
  <div class="logo">Money <span>Picks</span> Arena</div>
  <div class="nav-links">
    <span style="color:#4b5563;font-size:12px">{email}</span>
    <span style="background:rgba(74,222,128,.08);border:1px solid rgba(74,222,128,.2);color:#4ade80;font-size:11px;font-weight:700;padding:4px 12px;border-radius:999px">✓ ACTIVE</span>
    <a href="/logout" class="nav-link">Logout</a>
  </div>
</nav>
<div style="max-width:1000px;margin:0 auto;padding:100px 24px 60px">
  <h1 class="font-display" style="font-size:36px;margin-bottom:6px">Welcome back! 🏆</h1>
  <p style="color:#6b7280;margin-bottom:44px">Choose your sport below and get today's picks.</p>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px">
    <div class="card" style="text-align:center;display:flex;flex-direction:column;gap:14px;align-items:center">
      <div style="font-size:52px">⚾</div>
      <span style="font-size:10px;font-weight:700;letter-spacing:.1em;background:#1d4ed8;color:#fff;padding:3px 10px;border-radius:4px">BASEBALL</span>
      <h3 class="font-display" style="font-size:20px">MLB MoneyBall</h3>
      <p style="color:#6b7280;font-size:12px;line-height:1.6">Career stats vs pitcher, H/A splits, hot streaks. Top 9 picks daily.</p>
      <a href="https://moneyball-1.onrender.com" target="_blank" class="btn" style="width:100%;text-align:center">🎯 OPEN PICKS</a>
    </div>
    <div class="card" style="text-align:center;display:flex;flex-direction:column;gap:14px;align-items:center">
      <div style="font-size:52px">🏒</div>
      <span style="font-size:10px;font-weight:700;letter-spacing:.1em;background:#15803d;color:#fff;padding:3px 10px;border-radius:4px">HOCKEY</span>
      <h3 class="font-display" style="font-size:20px">NHL Money Shots</h3>
      <p style="color:#6b7280;font-size:12px;line-height:1.6">Shots on goal picks with live FanDuel sportsbook lines.</p>
      <a href="https://nhl-shots.onrender.com" target="_blank" class="btn" style="width:100%;text-align:center">🎯 OPEN PICKS</a>
    </div>
    <div class="card" style="text-align:center;display:flex;flex-direction:column;gap:14px;align-items:center">
      <div style="font-size:52px">🏀</div>
      <span style="font-size:10px;font-weight:700;letter-spacing:.1em;background:#7e22ce;color:#fff;padding:3px 10px;border-radius:4px">BASKETBALL</span>
      <h3 class="font-display" style="font-size:20px">NBA Money Buckets</h3>
      <p style="color:#6b7280;font-size:12px;line-height:1.6">75%+ hit rate picks for Pts, Reb, Ast, 3PM vs today's opponent.</p>
      <a href="https://nba-money-buckets.onrender.com" target="_blank" class="btn" style="width:100%;text-align:center">🎯 OPEN PICKS</a>
    </div>
    <div class="card" style="text-align:center;display:flex;flex-direction:column;gap:14px;align-items:center">
      <div style="font-size:52px">🏈</div>
      <span style="font-size:10px;font-weight:700;letter-spacing:.1em;background:#b45309;color:#fff;padding:3px 10px;border-radius:4px">FOOTBALL</span>
      <h3 class="font-display" style="font-size:20px">NFL Money Bombs</h3>
      <p style="color:#6b7280;font-size:12px;line-height:1.6">Weekly NFL player prop picks with matchup analysis.</p>
      <a href="https://nfl-money-bombs.onrender.com" target="_blank" class="btn" style="width:100%;text-align:center">🎯 OPEN PICKS</a>
    </div>
  </div>
</div>
"""

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home():
    return HOME_HTML

# ── Stripe Checkout ────────────────────────────────────────────────────────────
@app.get("/subscribe")
async def subscribe():
    try:
        session = stripe.checkout.sessions.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{SITE_URL}/register?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{SITE_URL}/",
        )
        return RedirectResponse(url=session.url)
    except Exception as e:
        return HTMLResponse(f"<p style='color:red;font-family:sans-serif;padding:40px'>Stripe error: {e}<br><a href='/'>Go back</a></p>")

# ── Register (after Stripe payment) ───────────────────────────────────────────
@app.get("/register", response_class=HTMLResponse)
async def register_get(session_id: str = ""):
    if not session_id:
        return RedirectResponse(url="/")
    try:
        session = stripe.checkout.sessions.retrieve(session_id)
        email = session.customer_details.email if session.customer_details else ""
        return REGISTER_HTML.replace("{email}", email).replace("{session_id}", session_id).replace("{error}", "")
    except:
        return RedirectResponse(url="/")

@app.post("/register", response_class=HTMLResponse)
async def register_post(
    email: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    session_id: str = Form(...)
):
    if password != confirm:
        return REGISTER_HTML.replace("{email}", email).replace("{session_id}", session_id).replace(
            "{error}", '<div class="error-box">❌ Passwords do not match.</div>')

    if len(password) < 6:
        return REGISTER_HTML.replace("{email}", email).replace("{session_id}", session_id).replace(
            "{error}", '<div class="error-box">❌ Password must be at least 6 characters.</div>')

    # Check if account already exists
    existing = db.table("subscribers").select("id").eq("email", email).execute()
    if existing.data:
        return REGISTER_HTML.replace("{email}", email).replace("{session_id}", session_id).replace(
            "{error}", '<div class="error-box">❌ An account with this email already exists. <a href="/login" style="color:#f59e0b">Login here.</a></div>')

    # Get Stripe details
    try:
        session = stripe.checkout.sessions.retrieve(session_id)
        customer_id = session.customer
        subscription_id = session.subscription
    except:
        customer_id = ""
        subscription_id = ""

    # Create account in Supabase
    db.table("subscribers").insert({
        "email": email,
        "password_hash": hash_pw(password),
        "stripe_customer_id": customer_id,
        "stripe_subscription_id": subscription_id,
        "is_active": True
    }).execute()

    # Auto-login
    sid = secrets.token_hex(32)
    SESSIONS[sid] = email
    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp

# ── Login ──────────────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_get():
    return LOGIN_HTML.replace("{error}", "")

@app.post("/login", response_class=HTMLResponse)
async def login_post(email: str = Form(...), password: str = Form(...)):
    # ── Admin bypass ──────────────────────────────────────────────────────
    if ADMIN_EMAIL and email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
        sid = secrets.token_hex(32)
        SESSIONS[sid] = email
        resp = RedirectResponse(url="/dashboard", status_code=302)
        resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
        return resp

    result = db.table("subscribers").select("*").eq("email", email).execute()
    if not result.data:
        return LOGIN_HTML.replace("{error}", '<div class="error-box">❌ Email not found. <a href="/subscribe" style="color:#f59e0b">Subscribe here.</a></div>')

    user = result.data[0]
    if user["password_hash"] != hash_pw(password):
        return LOGIN_HTML.replace("{error}", '<div class="error-box">❌ Incorrect password.</div>')

    if not user.get("is_active"):
        return LOGIN_HTML.replace("{error}", '<div class="error-box">❌ Your subscription is inactive. <a href="/subscribe" style="color:#f59e0b">Renew here.</a></div>')

    sid = secrets.token_hex(32)
    SESSIONS[sid] = email
    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp

# ── Dashboard ──────────────────────────────────────────────────────────────────
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return DASHBOARD_HTML.replace("{email}", user)

# ── Logout ─────────────────────────────────────────────────────────────────────
@app.get("/logout")
async def logout(request: Request):
    sid = request.cookies.get("sid")
    if sid and sid in SESSIONS:
        del SESSIONS[sid]
    resp = RedirectResponse(url="/")
    resp.delete_cookie("sid")
    return resp

# ── Stripe Webhook ─────────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(body, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if event["type"] == "customer.subscription.updated":
        sub = event["data"]["object"]
        is_active = sub["status"] == "active"
        db.table("subscribers").update({"is_active": is_active}).eq("stripe_subscription_id", sub["id"]).execute()

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        db.table("subscribers").update({"is_active": False}).eq("stripe_subscription_id", sub["id"]).execute()

    return JSONResponse({"received": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
