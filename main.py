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
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_PRICE_ID_SINGLE = os.environ.get("STRIPE_PRICE_ID_SINGLE", "")
STRIPE_PRICE_ID_YEARLY = os.environ.get("STRIPE_PRICE_ID_YEARLY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SITE_URL          = os.environ.get("SITE_URL", "http://localhost:8000")
SECRET_KEY        = os.environ.get("SECRET_KEY", secrets.token_hex(32))

stripe.api_key = STRIPE_SECRET
JWT_SECRET = os.environ.get("JWT_SECRET", "")

def make_app_token(email):
    from jose import jwt as _jwt
    from datetime import datetime, timedelta
    key = JWT_SECRET or SECRET_KEY
    return _jwt.encode({"sub": email, "exp": datetime.utcnow() + timedelta(days=30)}, key, algorithm="HS256")

# ── Cross-sport parlay (admin only) ──────────────────────────────────────────
# Each sport app exposes a read-only cached-picks JSON endpoint that accepts the
# hub's JWT. We mint a token for the admin, pull the latest cached picks from all
# four server-side (no fresh runs), normalize every market into one common leg
# shape, and feed them to the admin-only combined parlay builder. Admin runs each
# sport app first; this just reads whatever each app last cached.
SPORT_APPS = {
    "MLB": os.environ.get("MLB_URL", "https://moneyball-1.onrender.com"),
    "NHL": os.environ.get("NHL_URL", "https://nhl-shots.onrender.com"),
    "NBA": os.environ.get("NBA_URL", "https://nba-money-buckets.onrender.com"),
    "NFL": os.environ.get("NFL_URL", "https://nfl-money-bombs.onrender.com"),
}

def _am_to_dec(odds):
    """American odds -> decimal multiplier (mirror of each app's _amToDec)."""
    try:
        n = float(str(odds).replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    if not n:
        return None
    return 1 + n / 100.0 if n > 0 else 1 + 100.0 / abs(n)

def _floor_ok(odds, floor=-500):
    """A priced leg only qualifies at -500 or better; None/empty is rejected."""
    if odds is None or odds == "":
        return False
    try:
        a = float(str(odds).replace("+", "").strip())
    except (TypeError, ValueError):
        return False
    if a == 0:
        return False
    return a >= floor

def _frac(h, t, rate=None):
    """Render a hit fraction like '6/10 (60%)' from whatever pieces exist."""
    if h is not None and t:
        return "%s/%s (%s%%)" % (h, t, rate) if rate not in (None, "") else "%s/%s" % (h, t)
    if rate not in (None, ""):
        return "%s%%" % rate
    return None

def _mklog(rows, vkeys, n=8):
    """Normalize a sport's recent-game log into [{d,v,opp}] for the 'why' ladder.
    vkeys is a tuple of candidate value keys (first present, non-None wins)."""
    out = []
    if isinstance(vkeys, str):
        vkeys = (vkeys,)
    for g in (rows or [])[:n]:
        if not isinstance(g, dict):
            continue
        v = None
        for k in vkeys:
            if g.get(k) is not None:
                v = g.get(k)
                break
        if v is None:
            continue
        out.append({"d": g.get("d") or g.get("date") or "",
                    "v": v, "opp": g.get("opp") or g.get("o") or ""})
    return out

def _why(head=None, stats=None, log=None, log_label=None):
    """Bundle the reasoning behind a pick: a headline, key/value stats, and a
    recent-game ladder. Empty pieces are dropped so the modal only shows real data."""
    w = {}
    if head:
        w["head"] = head
    if stats:
        s = [[a, b] for a, b in stats if b not in (None, "", "None")]
        if s:
            w["stats"] = s
    if log:
        w["log"] = log
        if log_label:
            w["logLabel"] = log_label
    return w or None

def _mk_leg(sport, player, team, opp, market, line, side, odds, rate=0, why=None):
    dec = _am_to_dec(odds)
    if not dec:
        return None
    pair = [x for x in [team, opp] if x]
    game = " vs ".join(sorted(pair)) if len(pair) == 2 else (("vs " + opp) if opp else (team or ""))
    leg = {"sport": sport, "player": player or "", "team": team or "", "opp": opp or "",
           "market": market or "", "line": line, "side": side or "OVER",
           "odds": str(odds), "dec": round(dec, 4), "rate": int(rate or 0), "game": game}
    if why:
        leg["why"] = why
    return leg

def _dedup_best(legs, by="player_market"):
    """Keep the single best leg per key (priced, then rate, then odds). `by` mirrors
    each app's own pool granularity:
      "player"             -> one best leg per player            (NFL pool)
      "player_market"      -> one best leg per player+market     (MLB type|stat, NBA player|stat)
      "player_market_side" -> one best leg per player+market+side (NHL: keeps the OVER
                              AND the UNDER side of the same market so both are draftable).
    For "player"/"player_market" side is NOT in the key, so a conflicting OVER and UNDER
    for the same player/market collapse to the better one."""
    best = {}
    for lg in legs:
        if not lg:
            continue
        if by == "player":
            key = (lg["sport"], lg["player"])
        elif by == "player_market_side":
            key = (lg["sport"], lg["player"], lg["market"], lg["side"])
        else:
            key = (lg["sport"], lg["player"], lg["market"])
        score = (1 if lg["dec"] else 0, lg["rate"], min(lg["dec"] or 0, 11))
        cur = best.get(key)
        if cur is None or score > cur[0]:
            best[key] = (score, lg)
    return [v[1] for v in best.values()]

def _legs_mlb(r):
    out = []
    if not isinstance(r, dict):
        return out
    for p in (r.get("top9") or []) + (r.get("also_ran") or []):
        s4 = p.get("s4_pct")
        why = _why(
            head=("Records a hit in %s%% of head-to-head games" % s4) if s4 not in (None, "") else "Top hitter to record a hit",
            stats=[["Hit rate vs opp", ("%s%%" % s4) if s4 not in (None, "") else None],
                   ["Line", "0.5 hits"]],
            log=_mklog(p.get("recent_hit_log"), "h"),
            log_label="Recent games \u2014 hits")
        out.append(_mk_leg("MLB", p.get("full_name") or p.get("name"), p.get("team"),
                           p.get("opp"), "Hits", 0.5, "OVER", p.get("hit_odds"),
                           s4 or 0, why))
    for p in (r.get("under_picks") or []):
        u_stats = [["Lifetime vs pitcher", p.get("s1_disp")],
                   ["Under score", p.get("under_score")]]
        if _floor_ok(p.get("under_odds")):
            out.append(_mk_leg("MLB", p.get("name"), p.get("team"), p.get("opp"),
                               "Under Hits", 1.5, "UNDER", p.get("under_odds"), 0,
                               _why(head="Cold matchup / recent form \u2014 model leans under",
                                    stats=u_stats, log=_mklog(p.get("recent_hit_log"), "h"),
                                    log_label="Recent games \u2014 hits")))
        if _floor_ok(p.get("tb_under_odds")):
            out.append(_mk_leg("MLB", p.get("name"), p.get("team"), p.get("opp"),
                               "Under Total Bases", 1.5, "UNDER", p.get("tb_under_odds"), 0,
                               _why(head="Cold matchup / recent form \u2014 model leans under",
                                    stats=u_stats, log=_mklog(p.get("recent_hit_log"), "tb"),
                                    log_label="Recent games \u2014 total bases")))
    for p in ((r.get("pitcher_k") or {}).get("all") or []):
        if not (p.get("pick") and (p.get("starts") or 0) > 0):
            continue
        has_sugg = p.get("sugg_line") is not None
        side = "OVER" if has_sugg else p.get("pick")
        line = p.get("sugg_line") if has_sugg else p.get("line")
        odds = p.get("sugg_odds") if has_sugg else (
            p.get("over_odds") if p.get("pick") == "OVER" else p.get("under_odds"))
        why = _why(
            head=("Avg %s K vs a %s line" % (p.get("avg_k"), line)) if p.get("avg_k") not in (None, "") else None,
            stats=[["Avg K", p.get("avg_k")],
                   ["Blended", p.get("blended_avg_k") if p.get("blended_avg_k") is not None else p.get("blended")],
                   ["Hit rate", p.get("k_hit_rate") if p.get("k_hit_rate") not in (None, "") else p.get("hit_rate")],
                   ["Line", line]],
            log=_mklog(p.get("recent_k_log"), "v"),
            log_label="Recent starts \u2014 Ks")
        out.append(_mk_leg("MLB", p.get("name"), "", p.get("opp"), "Strikeouts", line, side, odds, 0, why))
    for p in (r.get("runs_picks") or []):
        od = p.get("over_odds") if p.get("pick") == "OVER" else p.get("under_odds")
        ln = p.get("line") if p.get("line") is not None else 0.5
        why = _why(
            head=("Scores a run in %s of games (%s)" % (p.get("rate_disp"), p.get("basis"))) if p.get("rate_disp") not in (None, "") else None,
            stats=[["Runs rate", p.get("rate_disp")], ["Basis", p.get("basis")],
                   ["Wilson LB", p.get("wilson")], ["Games", p.get("games")]],
            log=_mklog(p.get("recent_runs_log"), "r"),
            log_label="Recent games \u2014 runs")
        out.append(_mk_leg("MLB", p.get("name"), p.get("team"), p.get("opp"), "Runs", ln, p.get("pick"), od, 0, why))
    _prop_lbl = {"pitcher_hits_allowed": "Hits Allowed", "pitcher_outs": "Outs",
                 "pitcher_earned_runs": "Earned Runs", "pitcher_walks": "Walks Allowed"}
    for mkt, bucket in (r.get("pitcher_props") or {}).items():
        for p in ((bucket or {}).get("picks") or []):
            od = p.get("over_odds") if p.get("pick") == "OVER" else p.get("under_odds")
            why = _why(
                head=("Blend %s vs a %s line" % (p.get("blended"), p.get("line"))) if p.get("blended") not in (None, "") else None,
                stats=[["Career avg", p.get("career_avg")], ["Recent avg", p.get("recent_avg")],
                       ["Blended", p.get("blended")], ["Hit rate", p.get("hit_rate")],
                       ["Line", p.get("line")]],
                log=_mklog(p.get("recent_log"), "v"),
                log_label="Recent starts")
            out.append(_mk_leg("MLB", p.get("name"), p.get("team"), p.get("opp"),
                               _prop_lbl.get(mkt, mkt), p.get("line"), p.get("pick"), od, 0, why))
    return _dedup_best(out)

def _legs_nhl(r):
    out = []
    if not isinstance(r, dict):
        return out
    plays = []
    for k in ("picks", "rest", "ptsPicks", "ptsRest", "astPicks", "astRest", "savesPicks", "savesRest"):
        plays += (r.get(k) or [])
    for p in plays:
        if not p or not p.get("name"):
            continue
        name = p.get("name"); team = p.get("team"); opp = p.get("opponent")
        mkt = p.get("mkt") or "Shots on Goal"
        glog = _mklog(p.get("glog"), "v")
        line = p.get("realLine")
        if line is None:
            line = p.get("dispLine")
        if line is None:
            line = 1.5
        # OVER leg — the app's headline lean. Skip when the model tags the pick FADE
        # (it is telling you to bet the under, not force an over on that player).
        if p.get("tag") != "FADE" and _floor_ok(p.get("realOdds")):
            o_rate = p.get("vsLineRate") or p.get("rateB") or p.get("rateA") or 0
            why_o = _why(
                head=p.get("head"),
                stats=[["Avg", p.get("avg")],
                       ["Career vs opp", _frac(p.get("hitsA"), p.get("totA"), p.get("rateA"))],
                       ["L10 home/road", _frac(p.get("hitsB"), p.get("totB"), p.get("rateB"))],
                       ["Cleared line", _frac(p.get("vsLineHits"), p.get("vsLineTotal"), p.get("vsLineRate"))],
                       ["Projected", p.get("proj")], ["Line", line]],
                log=glog, log_label="Recent games")
            out.append(_mk_leg("NHL", name, team, opp, mkt, line, "OVER",
                               p.get("realOdds"), o_rate, why_o))
        # UNDER leg — every pick with a posted, priced under side. Mirrors the NHL
        # app's full UNDER tracks (shots/points/assists/saves) so all of them are
        # draftable in the hub, not just FADE-tagged players.
        if _floor_ok(p.get("realUnderOdds")):
            uline = p.get("underLine")
            if uline is None:
                uline = line
            u_rate = p.get("underRate") or 0
            why_u = _why(
                head=("Stays under %s in %s%% of recent games" % (uline, u_rate)) if u_rate else p.get("head"),
                stats=[["Avg", p.get("avg")],
                       ["Under line L10", _frac(p.get("underHits"), p.get("underTotal"), p.get("underRate"))],
                       ["Cleared line", _frac(p.get("vsLineHits"), p.get("vsLineTotal"), p.get("vsLineRate"))],
                       ["Projected", p.get("proj")], ["Line", uline]],
                log=glog, log_label="Recent games")
            out.append(_mk_leg("NHL", name, team, opp, mkt, uline, "UNDER",
                               p.get("realUnderOdds"), u_rate, why_u))
    return _dedup_best(out, "player_market_side")

def _legs_nfl(r):
    out = []
    if not isinstance(r, dict):
        return out
    for p in (r.get("all") or []):
        if not p or not p.get("name") or not p.get("pick"):
            continue
        if p.get("score") is None or (p.get("score") or 0) < 55:  # NFL pool gate (matches _parlayPool)
            continue
        pick = p.get("pick")
        side = "OVER" if pick in ("O", "OVER") else "UNDER" if pick in ("U", "UNDER") else pick
        odds = p.get("realOdds") if side == "OVER" else p.get("realUnderOdds") if side == "UNDER" else None
        if not _floor_ok(odds):
            continue
        line = p.get("realLine")
        if line is None:
            line = p.get("dispLine")
        rate = p.get("vsLineRate") or p.get("rateB") or p.get("rateA") or 0
        why = _why(
            head=p.get("head"),
            stats=[["Avg", p.get("avg")],
                   ["Career vs opp", _frac(p.get("hitsA"), p.get("totA"), p.get("rateA"))],
                   ["Recent home/road", _frac(p.get("hitsB"), p.get("totB"), p.get("rateB"))],
                   ["Cleared line", _frac(p.get("vsLineHits"), p.get("vsLineTotal"), p.get("vsLineRate"))],
                   ["Score", p.get("score")], ["Line", line]],
            log=_mklog(p.get("glog"), "v"),
            log_label="Recent games")
        out.append(_mk_leg("NFL", p.get("name"), p.get("team"), p.get("opponent"),
                           p.get("mkt") or p.get("label") or "", line, side, odds, rate, why))
    return _dedup_best(out, "player")

def _legs_nba(r):
    out = []
    if not isinstance(r, dict):
        return out
    for p in (r.get("all_picks") or []):
        if not p:
            continue
        mpg = p.get("mpg")
        if mpg is not None and mpg < 18:
            continue
        line = p.get("dk_line")
        if line is None:
            line = p.get("fd_line")
        stat = p.get("stat_label") or p.get("stat") or ""
        pat = bool(p.get("has_consistency"))
        cands = []
        if pat:
            cands.append(("OVER", p.get("pct") or 0))
        lr = p.get("line_rec")
        if lr and not (pat and lr != "OVER"):
            cands.append((lr, p.get("line_rec_pct") or 0))
        sr = p.get("streak_rec")
        if sr and not (pat and sr != "OVER"):
            cands.append((sr, min(99, 85 + (p.get("streak_n") or 0))))
        ar = p.get("alt_rec")
        if ar and not (pat and ar != "OVER"):
            cands.append((ar, 0))
        why = _why(
            head=("Recent avg %s vs a %s line" % (p.get("recent_avg"), line)) if p.get("recent_avg") not in (None, "") else None,
            stats=[["Recent avg", p.get("recent_avg")], ["Gap vs line", p.get("gap")],
                   ["Consistency %", p.get("pct")], ["Line rec", p.get("line_rec")],
                   ["Streak rec", p.get("streak_rec")], ["Min/game", p.get("mpg")]],
            log=_mklog(p.get("glog"), "v"),
            log_label="Recent games vs opp")
        for side, conf in cands:
            odds = (p.get("dk_over_odds") or p.get("fd_odds")) if side == "OVER" else p.get("dk_under_odds")
            if not _floor_ok(odds):
                continue
            out.append(_mk_leg("NBA", p.get("player"), p.get("team"), p.get("opp"),
                               stat, line, side, odds, conf, why))
    return _dedup_best(out)

async def _fetch_sport_legs(token, dates):
    import asyncio
    import httpx
    # `dates` is a per-sport map {MLB,NHL,NBA,NFL: "YYYY-MM-DD"} so playoff slates on
    # different days (e.g. NHL tomorrow, NBA in 2 days) can be combined in one pool.
    endpoints = {
        "MLB": (SPORT_APPS["MLB"] + "/api/results/" + dates["MLB"], None),
        "NHL": (SPORT_APPS["NHL"] + "/api/cached", {"target_date": dates["NHL"]}),
        "NBA": (SPORT_APPS["NBA"] + "/api/cached", {"target_date": dates["NBA"]}),
        "NFL": (SPORT_APPS["NFL"] + "/api/cached", {"target_date": dates["NFL"]}),
    }
    normalizers = {"MLB": _legs_mlb, "NHL": _legs_nhl, "NBA": _legs_nba, "NFL": _legs_nfl}
    headers = {"Authorization": "Bearer " + token}

    async def one(client, sport):
        url, params = endpoints[sport]
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                return sport, {"ok": False, "error": "HTTP " + str(resp.status_code), "legs": []}
            legs = [l for l in normalizers[sport](resp.json()) if l]
            return sport, {"ok": True, "error": "", "legs": legs}
        except Exception as e:
            return sport, {"ok": False, "error": str(e)[:160], "legs": []}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        pairs = await asyncio.gather(*[one(client, s) for s in endpoints])
    return {sport: info for sport, info in pairs}


async def _fetch_sport_bets(token):
    """Fan out to every sport app's GET /api/bets (admin JWT), pull the {summary}
    block, so the hub can show one combined My Record across all four apps."""
    import asyncio
    import httpx
    headers = {"Authorization": "Bearer " + token}

    async def one(client, sport):
        url = SPORT_APPS[sport] + "/api/bets"
        try:
            resp = await client.get(url, params={"token": token, "settle": "true"}, headers=headers)
            if resp.status_code != 200:
                # Settlement runs ESPN calls per game date and can transiently rate-limit
                # (HTTP 429). Fall back to a read-only fetch so a settle hiccup never blanks
                # a sport on the combined My Record card; each app settles itself elsewhere.
                resp = await client.get(url, params={"token": token, "settle": "false"}, headers=headers)
            if resp.status_code != 200:
                return sport, {"ok": False, "error": "HTTP " + str(resp.status_code), "summary": None}
            return sport, {"ok": True, "error": "", "summary": (resp.json() or {}).get("summary")}
        except Exception as e:
            return sport, {"ok": False, "error": str(e)[:160], "summary": None}

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        pairs = await asyncio.gather(*[one(client, s) for s in SPORT_APPS])
    return {sport: info for sport, info in pairs}

db = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

SESSIONS: dict[str, str] = {}

def hash_pw(pw: str) -> str:
    return hashlib.sha256((pw + SECRET_KEY).encode()).hexdigest()

def get_user(request: Request) -> str:
    sid = request.cookies.get("sid")
    return SESSIONS.get(sid, "") if sid else ""

def _norm_email(e: str) -> str:
    return (e or "").strip().lower()

def _find_subscriber(email: str):
    """Case-insensitive lookup of a subscriber row by email."""
    if not db:
        return None
    try:
        res = db.table("subscribers").select("*").ilike("email", _norm_email(email)).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None

def _find_stripe_active(email: str):
    """(customer_id, subscription_id) for an active Stripe sub for this email, else ('','')."""
    email = _norm_email(email)
    if not email or not STRIPE_SECRET:
        return ("", "")
    try:
        custs = stripe.Customer.list(email=email, limit=20)
    except Exception:
        return ("", "")
    for c in custs.data:
        try:
            subs = stripe.Subscription.list(customer=c.id, status="all", limit=20)
        except Exception:
            continue
        for s in subs.data:
            if getattr(s, "status", "") in ("active", "trialing", "past_due"):
                return (c.id, s.id)
    return ("", "")

# ── HTML ───────────────────────────────────────────────────────────────────────
BASE_STYLE = """
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700;900&family=Source+Sans+Pro:wght@300;400;600;700&display=swap');
/* responsive: phones & tablets (mobile fit) */
html,body{max-width:100%;overflow-x:hidden}
img{max-width:100%;height:auto}
@media (max-width:1200px){table{display:block;width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;white-space:nowrap}}
@media (max-width:560px){table{font-size:12px}table th,table td{padding:6px 8px}}
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#fff;font-family:'Source Sans Pro',sans-serif;min-height:100vh}
.font-display{font-family:'Playfair Display',serif}
nav{position:fixed;top:0;width:100%;background:rgba(10,10,10,.9);backdrop-filter:blur(12px);border-bottom:1px solid #1c1c1c;z-index:100;padding:0 24px;height:80px;display:flex;align-items:center;justify-content:space-between}
.brand{display:flex;align-items:center;gap:12px;text-decoration:none}
.brand img{height:48px;width:48px;object-fit:contain;mix-blend-mode:lighten}
.brand-text{font-family:'Playfair Display',serif;font-size:24px;font-weight:700;letter-spacing:.01em;line-height:1}
.brand-text .m,.brand-text .a{color:#fff}
.brand-text .p{color:#f59e0b}
@media(max-width:640px){.brand-text{display:none}}
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
  <a href="/" class="brand">
    <img src="https://moneypicksarena.com/logo.png" alt="Money Picks Arena"/>
    <span class="brand-text"><span class="m">Money </span><span class="p">Picks </span><span class="a">Arena</span></span>
  </a>
  <div class="nav-links">
    <a href="/login" class="nav-link">Login</a>
    <a href="/pricing" class="btn">Plans</a>
  </div>
</nav>

<div style="padding-top:80px">
  <!-- HERO -->
  <section style="padding:100px 24px 80px;text-align:center;position:relative;overflow:hidden">
    <div style="position:absolute;inset:0;background:radial-gradient(ellipse at 50% 40%,rgba(245,158,11,.04),transparent 65%);pointer-events:none"></div>
    <div style="position:relative;max-width:760px;margin:0 auto">
      <h1 class="font-display" style="font-size:clamp(42px,7vw,76px);line-height:1.05;margin-bottom:20px">
        Score Big in the<br><span class="gold">Money Picks Arena</span>
      </h1>
      <p style="color:#9ca3af;font-size:18px;margin-bottom:10px;max-width:520px;margin-left:auto;margin-right:auto;line-height:1.6">
        Data-driven picks for <strong style="color:#fff">4 sports</strong> — MLB, NHL, NBA &amp; NFL — powered by real stats and sportsbook lines.
      </p>
      <p style="color:#4b5563;font-size:13px;letter-spacing:.14em;margin-bottom:40px">ONE SUBSCRIPTION. ALL 4 SPORTS.</p>
      <div style="display:flex;flex-direction:column;align-items:center;gap:12px">
        <a href="/pricing" class="btn btn-lg" style="box-shadow:0 0 40px rgba(245,158,11,.3)">View Plans</a>
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
        
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:44px;margin-bottom:12px">🏒</div>
        <div style="font-size:10px;font-weight:700;letter-spacing:.1em;color:#fff;background:#15803d;padding:3px 10px;border-radius:4px;display:inline-block;margin-bottom:10px">HOCKEY</div>
        <h3 class="font-display" style="font-size:18px;margin-bottom:8px">NHL Money Shots</h3>
        
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:44px;margin-bottom:12px">🏀</div>
        <div style="font-size:10px;font-weight:700;letter-spacing:.1em;color:#fff;background:#7e22ce;padding:3px 10px;border-radius:4px;display:inline-block;margin-bottom:10px">BASKETBALL</div>
        <h3 class="font-display" style="font-size:18px;margin-bottom:8px">NBA Money Buckets</h3>
        
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:44px;margin-bottom:12px">🏈</div>
        <div style="font-size:10px;font-weight:700;letter-spacing:.1em;color:#fff;background:#b45309;padding:3px 10px;border-radius:4px;display:inline-block;margin-bottom:10px">FOOTBALL</div>
        <h3 class="font-display" style="font-size:18px;margin-bottom:8px">NFL Money Bombs</h3>
        
      </div>
    </div>
  </section>

  
</div>

<footer>
  <div style="font-family:'Playfair Display',serif;font-size:16px;color:#4b5563;margin-bottom:10px">Money Picks Arena</div>
  <p style="color:#4b5563;font-size:12px;max-width:600px;margin:0 auto 8px;line-height:1.8">
    For entertainment and informational purposes only. We do not accept bets or guarantee results. 
    Please gamble responsibly. Must be 18+ (21+ in some states).
  </p>
  <p style="margin-top:4px;color:#374151">
    <a href="https://www.ncpgambling.org" target="_blank" style="color:#4b5563;text-decoration:underline">Problem Gambling Help</a>
    &nbsp;·&nbsp; 1-800-522-4700
  </p>
  <p style="margin-top:8px">© 2026 Money Picks Arena. All Rights Reserved.</p>
</footer>
"""


PRICING_HTML = BASE_STYLE + """
<nav>
  <a href="/" class="brand">
    <img src="https://moneypicksarena.com/logo.png" alt="Money Picks Arena"/>
    <span class="brand-text"><span class="m">Money </span><span class="p">Picks </span><span class="a">Arena</span></span>
  </a>
  <div class="nav-links">
    <a href="/login" class="nav-link">Login</a>
    <a href="/" class="btn">Home</a>
  </div>
</nav>
<div style="padding-top:100px;padding-bottom:60px;min-height:100vh">
  <div style="text-align:center;margin-bottom:44px;padding:0 24px">
    <h1 class="font-display" style="font-size:42px;margin-bottom:10px">Choose Your Plan</h1>
    <p style="color:#6b7280;font-size:15px">Pick the plan that works for you. Cancel anytime.</p>
  </div>
  <div style="max-width:1000px;margin:0 auto;padding:0 24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px;align-items:start">
    <div class="card" style="border-color:rgba(245,158,11,.25);padding:36px;text-align:center">
      <div style="font-size:11px;font-weight:700;letter-spacing:.15em;color:#9ca3af;text-transform:uppercase;margin-bottom:16px">Single Sport</div>
      <div style="font-size:64px;font-weight:900;color:#fff;line-height:1;font-family:'Playfair Display',serif">$20</div>
      <div style="color:#6b7280;font-size:13px;margin-bottom:28px">per month</div>
      <div style="text-align:left;margin-bottom:28px;display:flex;flex-direction:column;gap:10px">
        <div style="color:#9ca3af;font-size:13px">&#10003; &nbsp;1 sport of your choice</div>
        <div style="color:#9ca3af;font-size:13px">&#10003; &nbsp;Daily picks at 10 AM ET</div>
        <div style="color:#9ca3af;font-size:13px">&#10003; &nbsp;Real sportsbook lines</div>
        <div style="color:#9ca3af;font-size:13px">&#10003; &nbsp;Cancel anytime</div>
      </div>
      <a href="/subscribe/single" class="btn" style="display:block;width:100%;text-align:center;font-size:14px">Get Started</a>
    </div>
    <div class="card" style="border-color:rgba(245,158,11,.5);padding:36px;text-align:center;position:relative">
      <div style="position:absolute;top:-14px;left:50%;transform:translateX(-50%);background:#f59e0b;color:#000;font-size:10px;font-weight:900;letter-spacing:.15em;padding:4px 16px;border-radius:999px;white-space:nowrap">MOST POPULAR</div>
      <div style="font-size:11px;font-weight:700;letter-spacing:.15em;color:#f59e0b;text-transform:uppercase;margin-bottom:16px">All Sports</div>
      <div style="font-size:64px;font-weight:900;color:#fff;line-height:1;font-family:'Playfair Display',serif">$50</div>
      <div style="color:#6b7280;font-size:13px;margin-bottom:28px">per month</div>
      <div style="text-align:left;margin-bottom:28px;display:flex;flex-direction:column;gap:10px">
        <div style="display:flex;align-items:center;gap:8px;color:#d1d5db;font-size:13px"><span style="background:#1d4ed8;border-radius:3px;padding:1px 6px;font-size:9px;font-weight:700;color:#fff">MLB</span> MoneyBall</div>
        <div style="display:flex;align-items:center;gap:8px;color:#d1d5db;font-size:13px"><span style="background:#15803d;border-radius:3px;padding:1px 6px;font-size:9px;font-weight:700;color:#fff">NHL</span> Money Shots</div>
        <div style="display:flex;align-items:center;gap:8px;color:#d1d5db;font-size:13px"><span style="background:#7e22ce;border-radius:3px;padding:1px 6px;font-size:9px;font-weight:700;color:#fff">NBA</span> Money Buckets</div>
        <div style="display:flex;align-items:center;gap:8px;color:#d1d5db;font-size:13px"><span style="background:#b45309;border-radius:3px;padding:1px 6px;font-size:9px;font-weight:700;color:#fff">NFL</span> Money Bombs</div>
        <div style="height:1px;background:#262626;margin:2px 0"></div>
        <div style="color:#9ca3af;font-size:13px">&#10003; &nbsp;Real sportsbook lines</div>
        <div style="color:#9ca3af;font-size:13px">&#10003; &nbsp;Cancel anytime</div>
      </div>
      <a href="/subscribe" class="btn btn-lg" style="display:block;width:100%;text-align:center;box-shadow:0 0 30px rgba(245,158,11,.3);font-size:15px">Subscribe Now</a>
    </div>
    <div class="card" style="border-color:rgba(245,158,11,.25);padding:36px;text-align:center">
      <div style="font-size:11px;font-weight:700;letter-spacing:.15em;color:#9ca3af;text-transform:uppercase;margin-bottom:16px">Yearly Pass</div>
      <div style="font-size:64px;font-weight:900;color:#fff;line-height:1;font-family:'Playfair Display',serif">$500</div>
      <div style="color:#6b7280;font-size:13px;margin-bottom:8px">per year</div>
      <div style="background:rgba(74,222,128,.08);border:1px solid rgba(74,222,128,.2);color:#4ade80;border-radius:6px;padding:4px 12px;font-size:11px;font-weight:700;display:inline-block;margin-bottom:20px">Save $100 vs monthly</div>
      <div style="text-align:left;margin-bottom:28px;display:flex;flex-direction:column;gap:10px">
        <div style="color:#9ca3af;font-size:13px">&#10003; &nbsp;All 4 sports included</div>
        <div style="color:#9ca3af;font-size:13px">&#10003; &nbsp;Daily picks at 10 AM ET</div>
        <div style="color:#9ca3af;font-size:13px">&#10003; &nbsp;Best value &mdash; 2 months free</div>
        <div style="color:#9ca3af;font-size:13px">&#10003; &nbsp;Real sportsbook lines</div>
      </div>
      <a href="/subscribe/yearly" class="btn" style="display:block;width:100%;text-align:center;font-size:14px">Get Annual Pass</a>
    </div>
  </div>
  <p style="text-align:center;margin-top:32px;color:#374151;font-size:11px;line-height:1.8;padding:0 24px">
    Already a member? <a href="/login" style="color:#f59e0b;text-decoration:none">Login here</a>
    &nbsp;&middot;&nbsp; For entertainment only. Must be 18+. Please gamble responsibly.
  </p>
</div>
"""

LOGIN_HTML = BASE_STYLE + """
<nav>
  <a href="/" class="brand">
    <img src="https://moneypicksarena.com/logo.png" alt="Money Picks Arena"/>
    <span class="brand-text"><span class="m">Money </span><span class="p">Picks </span><span class="a">Arena</span></span>
  </a>
  <div class="nav-links">
    <a href="/pricing" class="btn">Plans</a>
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
        Not a member? <a href="/pricing" style="color:#f59e0b;text-decoration:none">View our Plans</a>
      </p>
      <p style="text-align:center;margin-top:8px;font-size:13px;color:#4b5563">
        Paid but can't log in? <a href="/setup" style="color:#f59e0b;text-decoration:none">Set your password</a>
      </p>
    </div>
    <p style="text-align:center;margin-top:16px"><a href="/" style="color:#374151;font-size:12px;text-decoration:none">← Back to home</a></p>
  </div>
</div>
"""

REGISTER_HTML = BASE_STYLE + """
<nav>
  <a href="/" class="brand">
    <img src="https://moneypicksarena.com/logo.png" alt="Money Picks Arena"/>
    <span class="brand-text"><span class="m">Money </span><span class="p">Picks </span><span class="a">Arena</span></span>
  </a>
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

SETUP_HTML = BASE_STYLE + """
<nav>
  <a href="/" class="brand">
    <img src="https://moneypicksarena.com/logo.png" alt="Money Picks Arena"/>
    <span class="brand-text"><span class="m">Money </span><span class="p">Picks </span><span class="a">Arena</span></span>
  </a>
  <div class="nav-links"><a href="/login" class="nav-link">Login</a></div>
</nav>
<div style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;padding-top:100px">
  <div style="width:100%;max-width:420px">
    <div style="text-align:center;margin-bottom:28px">
      <div class="font-display gold" style="font-size:22px;margin-bottom:4px">Money Picks Arena</div>
      <h1 style="font-size:26px;font-weight:900;margin-bottom:4px">Set Your Password</h1>
      <p style="color:#6b7280;font-size:13px">Already paid but never set a password? Set it here using the email you used at checkout.</p>
    </div>
    <div class="card">
      {error}
      <form method="post" action="/setup">
        <label>Email Address</label>
        <input type="email" name="email" value="{email}" placeholder="you@example.com" required autocomplete="email"/>
        <label>Create a Password</label>
        <input type="password" name="password" placeholder="Choose a strong password (min 6 chars)" required minlength="6" autocomplete="new-password"/>
        <label>Confirm Password</label>
        <input type="password" name="confirm" placeholder="Repeat your password" required minlength="6" autocomplete="new-password"/>
        <button type="submit" class="btn" style="font-size:16px;padding:14px">SET PASSWORD &amp; LOGIN →</button>
      </form>
      <p style="text-align:center;margin-top:18px;font-size:13px;color:#4b5563">
        Already have a password? <a href="/login" style="color:#f59e0b;text-decoration:none">Login here</a>
      </p>
    </div>
  </div>
</div>
"""

DASHBOARD_HTML = BASE_STYLE + """
<nav>
  <div style="display:flex;align-items:center;gap:16px;min-width:0;flex:1">
    <a href="/dashboard" class="brand">
      <img src="https://moneypicksarena.com/logo.png" alt="Money Picks Arena"/>
      <span class="brand-text"><span class="m">Money </span><span class="p">Picks </span><span class="a">Arena</span></span>
    </a>
    <div id="hub-rec-chip" onclick="hubRecChipClick()" style="display:none;cursor:pointer;background:#0f172a;border:1px solid #1f2937;border-radius:10px;padding:7px 14px;font-size:12px;font-weight:700;color:#9ca3af;white-space:nowrap">📊 <span id="hub-rec-chip-txt">My Record</span></div>
  </div>
  <div class="nav-links">
    <span style="color:#4b5563;font-size:12px" class="hide-sm">{email}</span>
    <span style="background:rgba(74,222,128,.08);border:1px solid rgba(74,222,128,.2);color:#4ade80;font-size:11px;font-weight:700;padding:4px 12px;border-radius:999px;white-space:nowrap">✓ ACTIVE</span>
    {admin_link}
    <a href="/logout" class="nav-link">Logout</a>
  </div>
</nav>
<div style="max-width:1000px;margin:0 auto;padding:100px 24px 60px">
  <h1 class="font-display" style="font-size:36px;margin-bottom:6px">Welcome back! 🏆</h1>
  <p style="color:#6b7280;margin-bottom:16px">Choose your sport below and get today's picks.</p>
  <div style="background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.2);border-radius:10px;padding:12px 18px;margin-bottom:32px;display:flex;align-items:center;gap:12px;font-size:12px">
    <span style="font-size:20px">🔐</span>
    <span style="color:#6b7280">These picks are exclusively for <strong style="color:#f59e0b">{email}</strong> — sharing your account or picks violates our terms and will result in immediate cancellation.</span>
  </div>
  <style>
  .hub-rec-tbl{width:100%;border-collapse:collapse;font-size:13px}
  .hub-rec-tbl th{padding:7px 10px;text-align:left;font-size:11px;color:#6b7280;font-weight:700;text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid #1f2937;white-space:nowrap}
  .hub-rec-tbl td{padding:8px 10px;border-bottom:1px solid #111827;color:#e5e7eb}
  .hub-rec-tbl tr:last-child td{border-bottom:none}
  </style>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px">
    <div class="card" style="text-align:center;display:flex;flex-direction:column;gap:14px;align-items:center">
      <div style="font-size:52px">⚾</div>
      <span style="font-size:10px;font-weight:700;letter-spacing:.1em;background:#1d4ed8;color:#fff;padding:3px 10px;border-radius:4px">BASEBALL</span>
      <h3 class="font-display" style="font-size:20px">MLB MoneyBall</h3>
        <a href="#" onclick="openApp('https://moneyball-1.onrender.com');return false;" class="btn" style="width:100%;text-align:center;margin-top:auto">🎯 OPEN PICKS</a>
    </div>
    <div class="card" style="text-align:center;display:flex;flex-direction:column;gap:14px;align-items:center">
      <div style="font-size:52px">🏒</div>
      <span style="font-size:10px;font-weight:700;letter-spacing:.1em;background:#15803d;color:#fff;padding:3px 10px;border-radius:4px">HOCKEY</span>
      <h3 class="font-display" style="font-size:20px">NHL Money Shots</h3>
      <a href="#" onclick="openApp('https://nhl-shots.onrender.com');return false;" class="btn" style="width:100%;text-align:center;margin-top:auto">🎯 OPEN PICKS</a>
    </div>
    <div class="card" style="text-align:center;display:flex;flex-direction:column;gap:14px;align-items:center">
      <div style="font-size:52px">🏀</div>
      <span style="font-size:10px;font-weight:700;letter-spacing:.1em;background:#7e22ce;color:#fff;padding:3px 10px;border-radius:4px">BASKETBALL</span>
      <h3 class="font-display" style="font-size:20px">NBA Money Buckets</h3>
      <a href="#" onclick="openApp('https://nba-money-buckets.onrender.com');return false;" class="btn" style="width:100%;text-align:center;margin-top:auto">🎯 OPEN PICKS</a>
    </div>
    <div class="card" style="text-align:center;display:flex;flex-direction:column;gap:14px;align-items:center">
      <div style="font-size:52px">🏈</div>
      <span style="font-size:10px;font-weight:700;letter-spacing:.1em;background:#b45309;color:#fff;padding:3px 10px;border-radius:4px">FOOTBALL</span>
      <h3 class="font-display" style="font-size:20px">NFL Money Bombs</h3>
      <a href="#" onclick="openApp('https://nfl-money-bombs.onrender.com');return false;" class="btn" style="width:100%;text-align:center;margin-top:auto">🎯 OPEN PICKS</a>
    </div>
  </div>
  <div id="hub-record-card" style="display:none;margin-top:32px">
    <div class="card" style="padding:22px 24px">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:14px">
        <h2 class="font-display" style="font-size:22px;margin:0">📊 My Record <span style="font-size:12px;color:#6b7280;font-weight:400">· all sports combined</span></h2>
        <button onclick="loadHubRecord()" style="background:#1f2937;color:#9ca3af;border:none;border-radius:8px;padding:7px 13px;font-size:12px;font-weight:700;cursor:pointer">&#8635; Refresh</button>
      </div>
      <div id="hub-record-body"><p style="color:#6b7280;font-size:13px">Loading&#8230;</p></div>
    </div>
  </div>
</div>
<script>
var _hubTok='__HUB_TOKEN__';
function openApp(url){window.open(url+'?token='+encodeURIComponent(_hubTok),'_blank');}
var _hubIsAdmin=__IS_ADMIN__;
function _hrMoney(v){var n=Number(v)||0;return(n>=0?'$':'\u2212$')+Math.abs(n).toFixed(2);}
function _hrStat(lbl,val,clr){return '<div style="background:#0e0e0e;border-radius:10px;padding:12px 16px;min-width:104px"><div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.08em">'+lbl+'</div><div style="font-size:1.3rem;font-weight:800;color:'+(clr||'#e5e7eb')+'">'+val+'</div></div>';}
function renderHubRecord(d){
  var c=d.combined||{};
  var roiTxt=c.roi!=null?((c.roi>0?'+':'')+c.roi+'%'):'\u2014';
  var roiClr=c.roi==null?'#9ca3af':(c.roi>0?'#4ade80':(c.roi<0?'#f87171':'#facc15'));
  var netClr=(c.profit||0)>0?'#4ade80':((c.profit||0)<0?'#f87171':'#cbd5e1');
  var rec=(c.wins||0)+'-'+(c.losses||0)+(c.push?('-'+c.push+'P'):'');
  var wp=c.win_pct!=null?c.win_pct+'%':'\u2014';
  var head='<div style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:16px">'
    +_hrStat('Record',rec,'#e5e7eb')+_hrStat('Win %',wp,'#e5e7eb')+_hrStat('Pending',(c.pending||0),'#9ca3af')
    +_hrStat('Staked',_hrMoney(c.staked||0),'#cbd5e1')+_hrStat('Net',_hrMoney(c.profit||0),netClr)+_hrStat('ROI',roiTxt,roiClr)+'</div>';
  var _chip=document.getElementById('hub-rec-chip');var _chipTxt=document.getElementById('hub-rec-chip-txt');
  if(_chip)_chip.style.display='block';
  if(_chipTxt)_chipTxt.textContent='My Record \xb7 '+rec+(c.win_pct!=null?' \xb7 '+c.win_pct+'%':'');
  var EMO={MLB:'⚾',NHL:'🏒',NBA:'🏀',NFL:'🏈'};
  var rows=(d.by_sport||[]).map(function(s){
    var nm=(EMO[s.sport]||'')+' '+s.sport;
    if(!s.ok) return '<tr><td style="font-weight:700">'+nm+'</td><td colspan="5" style="color:#6b7280;font-size:12px">'+(s.error||'unavailable')+'</td></tr>';
    var sroi=s.roi!=null?((s.roi>0?'+':'')+s.roi+'%'):'\u2014';
    var sclr=s.roi==null?'#9ca3af':(s.roi>0?'#4ade80':(s.roi<0?'#f87171':'#facc15'));
    return '<tr><td style="font-weight:700">'+nm+'</td>'
      +'<td style="font-family:monospace">'+(s.wins||0)+'-'+(s.losses||0)+(s.push?('-'+s.push+'P'):'')+'</td>'
      +'<td style="font-family:monospace;color:#9ca3af">'+(s.pending||0)+'</td>'
      +'<td style="font-family:monospace">'+_hrMoney(s.staked||0)+'</td>'
      +'<td style="font-family:monospace;color:'+((s.profit||0)>=0?'#4ade80':'#f87171')+'">'+_hrMoney(s.profit||0)+'</td>'
      +'<td style="font-family:monospace;font-weight:700;color:'+sclr+'">'+sroi+'</td></tr>';
  }).join('');
  var tbl='<div style="overflow-x:auto"><table class="hub-rec-tbl"><thead><tr><th>Sport</th><th>W-L</th><th>Pend</th><th>Staked</th><th>Net</th><th>ROI</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
  document.getElementById('hub-record-body').innerHTML=head+tbl;
}
function hubRecChipClick(){
  var card=document.getElementById('hub-record-card');
  if(!card) return;
  card.style.display='block';
  setTimeout(function(){card.scrollIntoView({behavior:'smooth',block:'start'});},50);
}
function loadHubRecord(){
  if(!_hubIsAdmin) return;
  var card=document.getElementById('hub-record-card');if(!card) return;
  card.style.display='block';
  document.getElementById('hub-record-body').innerHTML='<p style="color:#6b7280;font-size:13px">Loading\u2026</p>';
  fetch('/api/my-record',{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(d){
    if(d.error){document.getElementById('hub-record-body').innerHTML='<p style="color:#f87171">'+d.error+'</p>';return;}
    renderHubRecord(d);
  }).catch(function(){document.getElementById('hub-record-body').innerHTML='<p style="color:#f87171">Error loading record</p>';});
}
if(_hubIsAdmin){if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',loadHubRecord);}else{loadHubRecord();}}
</script>
"""

PARLAY_HTML = BASE_STYLE + """
<style>
  .pl-wrap{max-width:1200px;margin:0 auto;padding:90px 18px 60px}
  .pl-grid{display:grid;grid-template-columns:1fr 360px;gap:20px}
  @media(max-width:880px){.pl-grid{grid-template-columns:1fr}}
  .pl-card{background:#0f172a;border:1px solid #1f2937;border-radius:12px;padding:16px}
  .pl-btn{background:#f59e0b;color:#111;border:none;border-radius:8px;padding:9px 16px;font-weight:800;cursor:pointer;font-size:13px}
  .pl-btn.sec{background:#1f2937;color:#e5e7eb}
  .pl-in{background:#0b1220;border:1px solid #334155;border-radius:8px;color:#e5e7eb;padding:8px 10px;font-size:13px}
  .pl-chip{display:inline-block;padding:3px 9px;border-radius:999px;font-size:10px;font-weight:800;letter-spacing:.04em}
  .pl-mlb{background:#1e3a8a;color:#bfdbfe}.pl-nhl{background:#0e7490;color:#a5f3fc}
  .pl-nba{background:#7e22ce;color:#e9d5ff}.pl-nfl{background:#b45309;color:#fde68a}
  .pl-leg{display:flex;align-items:center;gap:10px;border-bottom:1px solid #1f2937;padding:9px 4px}
  .pl-leg:hover{background:#111827}
  .pl-over{color:#4ade80;font-weight:800}.pl-under{color:#fb7185;font-weight:800}
  .pl-add{background:#14532d;color:#86efac;border:1px solid #166534;border-radius:7px;padding:4px 10px;font-size:12px;cursor:pointer;font-weight:800}
  .pl-rm{background:#7f1d1d;color:#fca5a5;border:1px solid #991b1b;border-radius:7px;padding:3px 9px;font-size:12px;cursor:pointer;font-weight:800}
  .pl-swap{background:#1e3a8a;color:#bfdbfe;border:1px solid #1d4ed8;border-radius:7px;padding:3px 8px;font-size:13px;cursor:pointer;font-weight:800;line-height:1}
  .pl-swap.none{background:#374151;color:#9ca3af;border-color:#4b5563}
  .pl-fbtn{background:#1f2937;color:#9ca3af;border:1px solid #334155;border-radius:999px;padding:5px 12px;font-size:12px;cursor:pointer;font-weight:700}
  .pl-fbtn.on{background:#f59e0b;color:#111;border-color:#f59e0b}
  .pl-st{font-size:11px;font-weight:700;padding:3px 9px;border-radius:6px;margin-right:6px;display:inline-block;margin-top:6px}
</style>
<nav>
  <a href="/dashboard" class="brand">
    <img src="https://moneypicksarena.com/logo.png" alt="Money Picks Arena"/>
    <span class="brand-text"><span class="m">Money </span><span class="p">Picks </span><span class="a">Arena</span></span>
  </a>
  <div class="nav-links">
    <a href="/dashboard" class="nav-link">&#8592; Dashboard</a>
    <a href="/admin" class="nav-link">&#9881; Admin</a>
    <a href="/logout" class="nav-link">Logout</a>
  </div>
</nav>
<div id="plWhyModal" onclick="if(event.target===this)plCloseWhy()" style="display:none;position:fixed;inset:0;z-index:300;background:rgba(0,0,0,.72);align-items:flex-start;justify-content:center;padding:48px 16px;overflow:auto">
  <div style="background:#0b0b0b;border:1px solid #262626;border-radius:12px;max-width:520px;width:100%;padding:18px;box-shadow:0 20px 60px rgba(0,0,0,.6)">
    <div id="plWhyBody"></div>
  </div>
</div>
<div class="pl-wrap">
  <a href="/dashboard" style="display:inline-block;margin-bottom:10px;color:#9ca3af;font-size:13px;text-decoration:none">&#8592; Back to Dashboard</a>
  <h1 class="font-display" style="font-size:30px;margin-bottom:4px">&#127919; Cross-Sport Parlay Lab</h1>
  <p style="color:#6b7280;font-size:13px;margin-bottom:14px">Admin only. Combines the latest <strong>cached</strong> picks from all four apps &mdash; run each sport first, then load. Same-game / same-day legs are correlated; mix games for true diversification.</p>
  <div class="pl-card" style="margin-bottom:16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center">
    <span style="font-size:12px;color:#9ca3af">Slate dates &mdash;</span>
    <label style="font-size:12px;color:#9ca3af">MLB <input type="date" id="plDateMLB" class="pl-in" style="margin-left:3px"></label>
    <label style="font-size:12px;color:#9ca3af">NHL <input type="date" id="plDateNHL" class="pl-in" style="margin-left:3px"></label>
    <label style="font-size:12px;color:#9ca3af">NBA <input type="date" id="plDateNBA" class="pl-in" style="margin-left:3px"></label>
    <label style="font-size:12px;color:#9ca3af">NFL <input type="date" id="plDateNFL" class="pl-in" style="margin-left:3px"></label>
    <button class="pl-btn" onclick="plSyncDates()" title="Set all sports to the MLB date">All =</button>
    <button class="pl-btn" onclick="plLoad()">&#8635; Load Picks</button>
    <span id="plStatus" style="font-size:12px;color:#6b7280"></span>
    <div id="plSports" style="width:100%"></div>
  </div>
  <div class="pl-grid">
    <div class="pl-card">
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px" id="plSportFilters"></div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:10px">
        <button class="pl-fbtn on" data-side="ALL" onclick="plSetSide(this)">All</button>
        <button class="pl-fbtn" data-side="OVER" onclick="plSetSide(this)">&#11014; Overs</button>
        <button class="pl-fbtn" data-side="UNDER" onclick="plSetSide(this)">&#11015; Unders</button>
        <button class="pl-fbtn" id="plMinusBtn" onclick="plToggleMinus()">&minus; Odds Only</button>
        <button class="pl-fbtn" id="plPlusBtn" onclick="plTogglePlus()">&plus; Odds Only</button>
        <div style="position:relative">
          <button class="pl-fbtn" id="plCatBtn" onclick="plToggleCatMenu(event)">&#9776; Categories &#9662;</button>
          <div id="plCatMenu" style="display:none;position:absolute;z-index:50;top:calc(100% + 4px);left:0;background:#0e0e0e;border:1px solid #1f2937;border-radius:8px;padding:8px;min-width:190px;max-height:280px;overflow:auto;box-shadow:0 8px 24px rgba(0,0,0,.55)">
            <div style="display:flex;gap:6px;margin-bottom:6px"><button class="pl-fbtn" style="font-size:10px;padding:2px 8px" onclick="plCatSetAll(true)">All</button><button class="pl-fbtn" style="font-size:10px;padding:2px 8px" onclick="plCatSetAll(false)">None</button></div>
            <div id="plCatList"></div>
          </div>
        </div>
        <div style="position:relative">
          <button class="pl-fbtn" id="plGameBtn" onclick="plToggleGameMenu(event)">&#9776; Games &#9662;</button>
          <div id="plGameMenu" style="display:none;position:absolute;z-index:50;top:calc(100% + 4px);left:0;background:#0e0e0e;border:1px solid #1f2937;border-radius:8px;padding:8px;min-width:210px;max-height:280px;overflow:auto;box-shadow:0 8px 24px rgba(0,0,0,.55)">
            <div style="display:flex;gap:6px;margin-bottom:6px"><button class="pl-fbtn" style="font-size:10px;padding:2px 8px" onclick="plGameSetAll(true)">All</button><button class="pl-fbtn" style="font-size:10px;padding:2px 8px" onclick="plGameSetAll(false)">None</button></div>
            <div id="plGameList"></div>
          </div>
        </div>
        <input id="plSearch" class="pl-in" placeholder="Search player..." oninput="plRender()" style="flex:1;min-width:120px">
      </div>
      <div id="plCount" style="font-size:11px;color:#6b7280;margin-bottom:6px"></div>
      <div id="plList" style="max-height:60vh;overflow:auto"></div>
    </div>
    <div class="pl-card" style="align-self:start;position:sticky;top:80px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <strong class="font-display" style="font-size:16px">Your Ticket</strong>
        <button class="pl-rm" onclick="plClear()">Clear</button>
      </div>
      <div id="plTicket" style="max-height:36vh;overflow:auto"></div>
      <div style="border-top:1px solid #1f2937;margin-top:10px;padding-top:10px">
        <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px"><span style="color:#9ca3af">Legs</span><span id="plLegs" style="font-weight:800">0</span></div>
        <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px"><span style="color:#9ca3af">Combined odds</span><span id="plOdds" style="font-weight:800;color:#f59e0b">&mdash;</span></div>
        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:8px"><span style="color:#9ca3af">Decimal</span><span id="plDec" style="font-weight:700">&mdash;</span></div>
        <label style="font-size:12px;color:#9ca3af">Stake $ <input id="plStake" class="pl-in" type="number" value="10" min="1" step="1" oninput="plMath()" style="width:80px;margin-left:6px"></label>
        <div style="display:flex;justify-content:space-between;font-size:14px;margin-top:8px"><span style="color:#9ca3af">Payout</span><span id="plPay" style="font-weight:900;color:#4ade80">&mdash;</span></div>
        <div style="display:flex;justify-content:space-between;font-size:12px"><span style="color:#9ca3af">Profit</span><span id="plProfit" style="font-weight:700;color:#86efac">&mdash;</span></div>
      </div>
      <div style="border-top:1px solid #1f2937;margin-top:10px;padding-top:10px;display:flex;flex-direction:column;gap:8px">
        <div style="display:flex;gap:8px;align-items:center">
          <select id="plMix" class="pl-in" onchange="plMixToggle()" style="flex:1">
            <option value="even">Even mix (round-robin)</option>
            <option value="custom">Custom per-sport</option>
          </select>
          <label style="font-size:11px;color:#9ca3af">Legs <select id="plGen" class="pl-in" style="margin-left:3px"><option>2</option><option>3</option><option>4</option><option>5</option><option>6</option><option>7</option><option>8</option><option>9</option><option>10</option><option>11</option><option>12</option><option>13</option><option>14</option><option>15</option><option>16</option><option>17</option><option>18</option><option>19</option><option>20</option></select></label>
        </div>
        <div id="plCustomMix" style="display:none;gap:8px;flex-wrap:wrap;align-items:center;font-size:11px;color:#9ca3af">
          <span>MLB <input id="plMixMLB" class="pl-in" type="number" min="0" value="0" style="width:48px"></span>
          <span>NHL <input id="plMixNHL" class="pl-in" type="number" min="0" value="0" style="width:48px"></span>
          <span>NBA <input id="plMixNBA" class="pl-in" type="number" min="0" value="0" style="width:48px"></span>
          <span>NFL <input id="plMixNFL" class="pl-in" type="number" min="0" value="0" style="width:48px"></span>
        </div>
        <div style="display:flex;gap:8px">
          <button class="pl-btn sec" style="flex:1" onclick="plBuild(false)">Top legs</button>
          <button class="pl-btn sec" style="flex:1" onclick="plBuild(true)">Surprise</button>
        </div>
      </div>
    </div>
  </div>
</div>
<script>/*PARLAY_JS_START*/
var PL_ALL=[], PL_TICKET=[], PL_SPORT="ALL", PL_SIDE="ALL", PL_MINUS=false, PL_PLUS=false, PL_CATS={}, PL_GAMES={};
var PL_COLORS={MLB:"pl-mlb",NHL:"pl-nhl",NBA:"pl-nba",NFL:"pl-nfl"};
function plToday(){var d=new Date();return d.toISOString().slice(0,10);}
function _esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function plAmToDec(a){var n=parseFloat(String(a==null?"":a).replace("+","").trim());if(!n||isNaN(n))return null;return n>0?1+n/100:1+100/Math.abs(n);}
function plDecToAm(d){if(!d||d<=1)return "";var v=d>=2?(d-1)*100:-100/(d-1);v=Math.round(v);return (v>0?"+":"")+v;}
function plAmFmt(o){if(o==null||o==="")return "\\u2014";var a=parseFloat(o);if(isNaN(a))return _esc(o);return (a>0?"+":"")+a;}
function plUid(l){return l.sport+"|"+l.player+"|"+l.market+"|"+l.side+"|"+l.line;}
var PL_SPORTS=["MLB","NHL","NBA","NFL"];
function plSportDate(s){return document.getElementById("plDate"+s).value||plToday();}
function plSyncDates(){var v=plSportDate("MLB");PL_SPORTS.forEach(function(s){document.getElementById("plDate"+s).value=v;});}
function plLoad(){
  var qs=[], lbl=[];
  PL_SPORTS.forEach(function(s){var v=plSportDate(s);qs.push("date_"+s.toLowerCase()+"="+encodeURIComponent(v));lbl.push(s+" "+v);});
  document.getElementById("plStatus").textContent="Loading "+lbl.join(" \\u00b7 ")+" ...";
  fetch("/admin/parlay/data?"+qs.join("&"),{credentials:"same-origin"})
    .then(function(r){if(!r.ok)throw new Error("HTTP "+r.status);return r.json();})
    .then(function(d){
      PL_ALL=d.legs||[]; PL_TICKET=[]; PL_ALL.forEach(function(l,i){l._i=i;});
      var s=d.summary||{}, html="";
      PL_SPORTS.forEach(function(k){
        var info=s[k]||{}; var ok=info.ok;
        var col=ok?(info.count>0?"background:#14532d;color:#86efac":"background:#374151;color:#9ca3af"):"background:#7f1d1d;color:#fca5a5";
        var txt=ok?(info.count+" legs "+(info.date||"")):("error: "+(info.error||"")); 
        html+='<span class="pl-st" style="'+col+'">'+k+": "+_esc(txt)+"</span>";
      });
      document.getElementById("plSports").innerHTML=html;
      document.getElementById("plStatus").textContent=PL_ALL.length+" total legs loaded";
      plBuildFilters(); plRender(); plMath();
    })
    .catch(function(e){document.getElementById("plStatus").textContent="Load failed: "+e.message;});
}
function plBuildFilters(){
  var sports={}, games={}, cats={}, scat={};
  PL_ALL.forEach(function(l){sports[l.sport]=1; if(l.game)games[l.game]=1; if(l.market){cats[l.market]=1; if(PL_SPORT==="ALL"||l.sport===PL_SPORT)scat[l.market]=1;}});
  var sf=document.getElementById("plSportFilters");
  var order=["ALL","MLB","NHL","NBA","NFL"].filter(function(s){return s==="ALL"||sports[s];});
  sf.innerHTML=order.map(function(s){
    return '<button class="pl-fbtn'+(s===PL_SPORT?" on":"")+'" onclick="plSetSport(\\''+s+'\\')">'+s+"</button>";
  }).join("");
  var rowS='display:flex;align-items:center;gap:6px;font-size:12px;color:#cbd5e1;padding:3px 2px;cursor:pointer;white-space:nowrap';
  Object.keys(cats).forEach(function(c){if(PL_CATS[c]===undefined)PL_CATS[c]=true;});
  var ck=Object.keys(scat).sort();
  var noLoad=Object.keys(cats).length===0;
  document.getElementById("plCatList").innerHTML=ck.length?ck.map(function(c){
    return '<label style="'+rowS+'"><input type="checkbox" class="pl-cat-cb" value="'+_esc(c)+'"'+(PL_CATS[c]?" checked":"")+' onchange="plCatChanged()"> '+_esc(c)+"</label>";
  }).join(""):(noLoad?'<div style="font-size:11px;color:#666">Load picks first.</div>':'<div style="font-size:11px;color:#666">No categories for this sport.</div>');
  var gk=Object.keys(games).sort();
  var ng={}; gk.forEach(function(g){ng[g]=(PL_GAMES[g]!==false);}); PL_GAMES=ng;
  document.getElementById("plGameList").innerHTML=gk.length?gk.map(function(g){
    return '<label style="'+rowS+'"><input type="checkbox" class="pl-game-cb" value="'+_esc(g)+'"'+(PL_GAMES[g]?" checked":"")+' onchange="plGameChanged()"> '+_esc(g)+"</label>";
  }).join(""):'<div style="font-size:11px;color:#666">Load picks first.</div>';
  plPaintCatBtn(); plPaintGameBtn();
}
function plSetSport(s){PL_SPORT=s;plBuildFilters();plRender();}
function plSetSide(btn){PL_SIDE=btn.getAttribute("data-side");var ps=[btn.parentNode.querySelector('[data-side="ALL"]'),btn.parentNode.querySelector('[data-side="OVER"]'),btn.parentNode.querySelector('[data-side="UNDER"]')];for(var i=0;i<ps.length;i++){if(ps[i])ps[i].classList.remove("on");}btn.classList.add("on");plRender();}
function plToggleMinus(){PL_MINUS=!PL_MINUS;if(PL_MINUS)PL_PLUS=false;plPaintOddsBtns();plRender();}
function plTogglePlus(){PL_PLUS=!PL_PLUS;if(PL_PLUS)PL_MINUS=false;plPaintOddsBtns();plRender();}
function plPaintOddsBtns(){var m=document.getElementById("plMinusBtn");if(m)m.classList.toggle("on",PL_MINUS);var p=document.getElementById("plPlusBtn");if(p)p.classList.toggle("on",PL_PLUS);}
function plPaintCatBtn(){var cb=document.querySelectorAll(".pl-cat-cb");var t=cb.length,n=0;for(var i=0;i<cb.length;i++)if(cb[i].checked)n++;var b=document.getElementById("plCatBtn");if(b)b.innerHTML="\\u2630 Categories ("+n+"/"+t+") \\u25be";}
function plPaintGameBtn(){var n=0,t=0;for(var k in PL_GAMES){t++;if(PL_GAMES[k])n++;}var b=document.getElementById("plGameBtn");if(b)b.innerHTML="\\u2630 Games ("+n+"/"+t+") \\u25be";}
function plToggleCatMenu(e){if(e)e.stopPropagation();var m=document.getElementById("plCatMenu");m.style.display=(m.style.display==="block")?"none":"block";}
function plToggleGameMenu(e){if(e)e.stopPropagation();var m=document.getElementById("plGameMenu");m.style.display=(m.style.display==="block")?"none":"block";}
function plCatChanged(){var cb=document.querySelectorAll(".pl-cat-cb");for(var i=0;i<cb.length;i++)PL_CATS[cb[i].value]=cb[i].checked;plPaintCatBtn();plRender();}
function plGameChanged(){var cb=document.querySelectorAll(".pl-game-cb");for(var i=0;i<cb.length;i++)PL_GAMES[cb[i].value]=cb[i].checked;plPaintGameBtn();plRender();}
function plCatSetAll(v){var cb=document.querySelectorAll(".pl-cat-cb");for(var i=0;i<cb.length;i++)cb[i].checked=v;plCatChanged();}
function plGameSetAll(v){var cb=document.querySelectorAll(".pl-game-cb");for(var i=0;i<cb.length;i++)cb[i].checked=v;plGameChanged();}
document.addEventListener("click",function(e){
  [["plCatMenu","plCatBtn"],["plGameMenu","plGameBtn"]].forEach(function(p){
    var m=document.getElementById(p[0]);if(!m||m.style.display!=="block")return;
    var b=document.getElementById(p[1]);
    if(m.contains(e.target)||(b&&b.contains(e.target)))return;
    m.style.display="none";
  });
});
function plFiltered(){
  var q=(document.getElementById("plSearch").value||"").toLowerCase();
  return PL_ALL.filter(function(l){
    if(PL_SPORT!=="ALL"&&l.sport!==PL_SPORT)return false;
    if(PL_SIDE!=="ALL"&&l.side!==PL_SIDE)return false;
    if(PL_MINUS&&!(parseFloat(l.odds)<0))return false;
    if(PL_PLUS&&!(parseFloat(l.odds)>0))return false;
    if(l.market&&PL_CATS[l.market]===false)return false;
    if(l.game&&PL_GAMES[l.game]===false)return false;
    if(q&&l.player.toLowerCase().indexOf(q)<0)return false;
    return true;
  }).sort(function(a,b){return (b.rate-a.rate)||(a.dec-b.dec);});
}
function plRender(){
  var list=plFiltered();
  var inTicket={}; PL_TICKET.forEach(function(l){inTicket[l._i]=1;});
  document.getElementById("plCount").textContent=list.length+" available legs";
  if(!list.length){document.getElementById("plList").innerHTML='<div style="color:#6b7280;padding:14px;font-size:13px">No legs. Run the sport apps, pick a date, and Load Picks.</div>';plTicket();return;}
  var h=list.map(function(l){
    var added=inTicket[l._i];
    var sc=l.side==="OVER"?"pl-over":"pl-under";
    return '<div class="pl-leg">'
      +'<span class="pl-chip '+PL_COLORS[l.sport]+'">'+l.sport+"</span>"
      +'<div style="flex:1;min-width:0"><div onclick="plWhy('+l._i+')" title="Why this pick?" style="font-weight:700;font-size:13px;cursor:pointer;border-bottom:1px dotted #6b7280;display:inline-block">'+_esc(l.player)+'</div>'
      +'<div style="font-size:11px;color:#9ca3af">'+_esc(l.market)+' &middot; <span class="'+sc+'">'+l.side+" "+(l.line==null?"":l.line)+'</span> &middot; '+_esc(l.game)+"</div></div>"
      +'<span style="font-weight:800;font-size:13px;color:#f59e0b;min-width:48px;text-align:right">'+plAmFmt(l.odds)+"</span>"
      +(added?'<button class="pl-rm" onclick="plRemoveIdx('+l._i+')">&minus;</button>':'<button class="pl-add" onclick="plAddIdx('+l._i+')">+ Add</button>')
      +"</div>";
  }).join("");
  document.getElementById("plList").innerHTML=h;
  plTicket();
}
function plPMKey(l){return l.sport+"|"+(l.player||"")+"|"+(l.market||"");}
function plAddIdx(i){var l=PL_ALL[i];if(!l)return;for(var j=0;j<PL_TICKET.length;j++){if(PL_TICKET[j]._i===i)return;}
  var k=plPMKey(l);PL_TICKET=PL_TICKET.filter(function(t){return plPMKey(t)!==k;});
  PL_TICKET.push(l);plRender();plMath();}
function plRemoveIdx(i){PL_TICKET=PL_TICKET.filter(function(l){return l._i!==i;});plRender();plMath();}
function plReplaceIdx(i){
  var pos=-1; for(var j=0;j<PL_TICKET.length;j++){if(PL_TICKET[j]._i===i){pos=j;break;}}
  if(pos<0)return;
  var cur=PL_TICKET[pos];
  var inT={}, pmT={};
  PL_TICKET.forEach(function(t){if(t._i!==i){inT[t._i]=1;pmT[plPMKey(t)]=1;}});
  var cands=plFiltered().filter(function(l){
    return l.sport===cur.sport && l._i!==cur._i && !inT[l._i] && !pmT[plPMKey(l)];
  });
  if(!cands.length){plFlashNoSwap(i);return;}
  var pick=cands[Math.floor(Math.random()*cands.length)];
  PL_TICKET[pos]=pick;
  plRender();plMath();
}
function plFlashNoSwap(i){
  var b=document.getElementById("plrep"+i);if(!b)return;
  var orig=b.innerHTML;b.classList.add("none");b.innerHTML="none";
  setTimeout(function(){b.classList.remove("none");b.innerHTML=orig;},1000);
}
function plClear(){PL_TICKET=[];plRender();plMath();}
function plWhy(i){
  var l=PL_ALL[i]; if(!l)return;
  var w=l.why||{};
  var sc=l.side==="OVER"?"#34d399":"#f87171";
  var html='<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px">'
    +'<div><div style="font-weight:800;font-size:17px">'+_esc(l.player)+'</div>'
    +'<div style="font-size:12px;color:#9ca3af;margin-top:3px"><span class="pl-chip '+(PL_COLORS[l.sport]||"")+'">'+_esc(l.sport)+'</span> '+_esc(l.market)+' &middot; <span style="color:'+sc+';font-weight:700">'+l.side+" "+(l.line==null?"":l.line)+'</span> &middot; <span style="color:#f59e0b;font-weight:700">'+plAmFmt(l.odds)+'</span></div>'
    +'<div style="font-size:11px;color:#6b7280;margin-top:2px">'+_esc(l.game)+'</div></div>'
    +'<span onclick="plCloseWhy()" style="cursor:pointer;color:#9ca3af;font-size:26px;line-height:1">&times;</span></div>';
  if(w.head) html+='<div style="font-size:13px;color:#cbd5e1;background:#161616;border:1px solid #232323;border-radius:6px;padding:8px 10px;margin-bottom:12px">'+_esc(w.head)+'</div>';
  if(w.stats&&w.stats.length){
    html+='<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:12px">';
    w.stats.forEach(function(s){html+='<div style="background:#0e0e0e;border:1px solid #232323;border-radius:6px;padding:6px 9px"><div style="font-size:9px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em">'+_esc(s[0])+'</div><div style="font-size:13px;font-weight:700;margin-top:1px">'+_esc(s[1])+'</div></div>';});
    html+='</div>';
  }
  if(w.log&&w.log.length){
    if(w.logLabel) html+='<div style="font-size:11px;color:#9ca3af;margin-bottom:5px">'+_esc(w.logLabel)+'</div>';
    var ln=parseFloat(l.line);
    html+='<div style="display:flex;flex-wrap:wrap;gap:5px">';
    w.log.forEach(function(g){
      var v=parseFloat(g.v),hit=null;
      if(!isNaN(ln)&&!isNaN(v)) hit=(l.side==="UNDER")?(v<ln):(v>ln);
      var bg=hit===null?"#1f2937":(hit?"#14532d":"#7f1d1d");
      var fg=hit===null?"#9ca3af":(hit?"#86efac":"#fca5a5");
      html+='<div title="'+_esc(g.d)+(g.opp?(" vs "+_esc(g.opp)):"")+'" style="min-width:44px;text-align:center;background:'+bg+';color:'+fg+';border-radius:5px;padding:4px 6px"><div style="font-size:14px;font-weight:800">'+_esc(g.v)+'</div><div style="font-size:9px;opacity:.85">'+_esc(g.d)+'</div></div>';
    });
    html+='</div><div style="font-size:10px;color:#6b7280;margin-top:6px">Green = cleared the pick ('+(l.side==="UNDER"?"under":"over")+' the line).</div>';
  }
  if(!w.head&&!(w.stats&&w.stats.length)&&!(w.log&&w.log.length)) html+='<div style="font-size:12px;color:#6b7280">No extra detail available for this pick.</div>';
  document.getElementById("plWhyBody").innerHTML=html;
  document.getElementById("plWhyModal").style.display="flex";
}
function plCloseWhy(){document.getElementById("plWhyModal").style.display="none";}
function plTicket(){
  var t=document.getElementById("plTicket");
  if(!PL_TICKET.length){t.innerHTML='<div style="color:#6b7280;font-size:12px;padding:8px 0">No legs yet. Add from the left.</div>';return;}
  t.innerHTML=PL_TICKET.map(function(l){
    var sc=l.side==="OVER"?"pl-over":"pl-under";
    return '<div class="pl-leg" style="padding:7px 2px">'
      +'<span class="pl-chip '+PL_COLORS[l.sport]+'">'+l.sport+"</span>"
      +'<div style="flex:1;min-width:0"><div onclick="plWhy('+l._i+')" title="Why this pick?" style="font-weight:700;font-size:12px;cursor:pointer;border-bottom:1px dotted #6b7280;display:inline-block">'+_esc(l.player)+'</div>'
      +'<div style="font-size:10px;color:#9ca3af">'+_esc(l.market)+' <span class="'+sc+'">'+l.side+" "+(l.line==null?"":l.line)+"</span></div></div>"
      +'<span style="font-weight:800;font-size:12px;color:#f59e0b">'+plAmFmt(l.odds)+"</span>"
      +'<button class="pl-swap" id="plrep'+l._i+'" onclick="plReplaceIdx('+l._i+')" title="Swap this leg for another '+_esc(l.sport)+' pick">&#8635;</button>'
      +'<button class="pl-rm" onclick="plRemoveIdx('+l._i+')">&minus;</button></div>';
  }).join("");
}
function plMath(){
  var dec=1, ok=true;
  PL_TICKET.forEach(function(l){var d=plAmToDec(l.odds);if(!d){ok=false;}else{dec*=d;}});
  document.getElementById("plLegs").textContent=PL_TICKET.length;
  if(!PL_TICKET.length||!ok){document.getElementById("plOdds").textContent="\\u2014";document.getElementById("plDec").textContent="\\u2014";document.getElementById("plPay").textContent="\\u2014";document.getElementById("plProfit").textContent="\\u2014";return;}
  var stake=parseFloat(document.getElementById("plStake").value)||0;
  var pay=stake*dec;
  document.getElementById("plOdds").textContent=plDecToAm(dec);
  document.getElementById("plDec").textContent=dec.toFixed(2);
  document.getElementById("plPay").textContent="$"+pay.toFixed(2);
  document.getElementById("plProfit").textContent="$"+(pay-stake).toFixed(2);
}
function plMixToggle(){document.getElementById("plCustomMix").style.display=document.getElementById("plMix").value==="custom"?"flex":"none";}
function plShuffle(a){for(var i=a.length-1;i>0;i--){var j=Math.floor(Math.random()*(i+1));var t=a[i];a[i]=a[j];a[j]=t;}return a;}
// Players used in the most recent Surprise parlay, so the next Surprise draws a
// different set instead of repeating the same faces.
var PL_LAST_PLAYERS=[];
function plBuild(rand){
  var pool=plFiltered();                 // already sorted rate desc (tie: shorter odds)
  // How many legs this build needs (custom mix sums the per-sport inputs).
  var want;
  if(document.getElementById("plMix").value==="custom"){
    want=0;PL_SPORTS.forEach(function(s){want+=parseInt(document.getElementById("plMix"+s).value,10)||0;});
  }else{
    want=parseInt(document.getElementById("plGen").value,10)||2;
  }
  // FRESH LIST: on Surprise, drop players from the previous Surprise so back-to-back
  // presses don't repeat faces. Falls back to the full pool if excluding would leave
  // too few legs to fill the parlay.
  if(rand&&PL_LAST_PLAYERS.length){
    var avoid={};PL_LAST_PLAYERS.forEach(function(p){avoid[p]=1;});
    var fresh=pool.filter(function(l){return !avoid[l.player];});
    if(fresh.length>=want)pool=fresh;
  }
  var buckets={}; PL_SPORTS.forEach(function(s){buckets[s]=[];});
  pool.forEach(function(l){if(buckets[l.sport])buckets[l.sport].push(l);});
  if(rand)PL_SPORTS.forEach(function(s){plShuffle(buckets[s]);});
  PL_TICKET=[]; var seen={}, seenPM={};
  function take(s,cnt){var b=buckets[s]||[],got=0;for(var k=0;k<b.length&&got<cnt;k++){var lg=b[k],u=lg._i;if(seen[u])continue;var pk=plPMKey(lg);if(seenPM[pk])continue;seen[u]=1;seenPM[pk]=1;PL_TICKET.push(lg);got++;}return got;}
  if(document.getElementById("plMix").value==="custom"){
    PL_SPORTS.forEach(function(s){take(s,parseInt(document.getElementById("plMix"+s).value,10)||0);});
  }else{
    var n=parseInt(document.getElementById("plGen").value,10)||2;
    var active=PL_SPORTS.filter(function(s){return buckets[s].length;});
    if(rand)plShuffle(active);
    var guard=0;
    while(PL_TICKET.length<n&&active.length){
      for(var i=0;i<active.length&&PL_TICKET.length<n;i++){take(active[i],1);}
      active=active.filter(function(s){return buckets[s].some(function(l){return !seen[l._i];});});
      if(++guard>500)break;
    }
  }
  if(rand)PL_LAST_PLAYERS=PL_TICKET.map(function(l){return l.player;});
  plRender();plMath();
}
PL_SPORTS.forEach(function(s){document.getElementById("plDate"+s).value=plToday();});
plLoad();
/*PARLAY_JS_END*/</script>
"""

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home():
    return HOME_HTML

# ── Stripe Checkout ────────────────────────────────────────────────────────────
@app.get("/pricing", response_class=HTMLResponse)
async def pricing():
    return PRICING_HTML

@app.get("/subscribe/single")
async def subscribe_single():
    try:
        session = stripe.checkout.sessions.create(
            payment_method_types=["card"], mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID_SINGLE, "quantity": 1}],
            success_url=f"{SITE_URL}/register?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{SITE_URL}/pricing",
        )
        return RedirectResponse(url=session.url)
    except Exception as e:
        return HTMLResponse(f"<p style='color:red;font-family:sans-serif;padding:40px'>Stripe error: {e}<br><a href='/pricing'>Go back</a></p>")

@app.get("/subscribe/yearly")
async def subscribe_yearly():
    try:
        session = stripe.checkout.sessions.create(
            payment_method_types=["card"], mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID_YEARLY, "quantity": 1}],
            success_url=f"{SITE_URL}/register?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{SITE_URL}/pricing",
        )
        return RedirectResponse(url=session.url)
    except Exception as e:
        return HTMLResponse(f"<p style='color:red;font-family:sans-serif;padding:40px'>Stripe error: {e}<br><a href='/pricing'>Go back</a></p>")

@app.get("/subscribe")
async def subscribe():
    try:
        if not STRIPE_SECRET_KEY:
            return HTMLResponse("<pre style='color:red;padding:40px'>ERROR: STRIPE_SECRET_KEY not set in Render env vars</pre>")
        if not STRIPE_PRICE_ID:
            return HTMLResponse("<pre style='color:red;padding:40px'>ERROR: STRIPE_PRICE_ID not set in Render env vars</pre>")
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{SITE_URL}/register?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{SITE_URL}/",
        )
        return RedirectResponse(url=session.url)
    except Exception as e:
        import traceback
        return HTMLResponse(f"<pre style='color:red;font-family:monospace;padding:40px;background:#111'>STRIPE ERROR: {repr(e)}\n\n{traceback.format_exc()}\n\nKey starts with: {STRIPE_SECRET_KEY[:12]}...\nPrice ID: {STRIPE_PRICE_ID}\nSite URL: {SITE_URL}</pre><a href='/' style='color:#f59e0b;padding:40px;display:block'>Go back</a>")

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

    # Verify the email matches the Stripe checkout session (ownership proof:
    # only the person who completed checkout has this session_id).
    try:
        session = stripe.checkout.sessions.retrieve(session_id)
        sess_email = _norm_email((session.customer_details.email if session.customer_details else "") or "")
        customer_id = session.customer or ""
        subscription_id = session.subscription or ""
    except Exception:
        sess_email = ""
        customer_id = ""
        subscription_id = ""
    if not sess_email or _norm_email(email) != sess_email:
        return REGISTER_HTML.replace("{email}", email).replace("{session_id}", session_id).replace(
            "{error}", '<div class="error-box">❌ This email does not match your payment. Use the email you checked out with.</div>')
    email = sess_email

    # Account may already exist (e.g. created by the Stripe webhook). Be idempotent.
    existing = _find_subscriber(email)
    if existing and existing.get("password_hash"):
        return REGISTER_HTML.replace("{email}", email).replace("{session_id}", session_id).replace(
            "{error}", '<div class="error-box">❌ An account with this email already exists. <a href="/login" style="color:#f59e0b">Login here.</a></div>')

    if existing:
        # Row exists without a password yet — set it and activate.
        db.table("subscribers").update({
            "password_hash": hash_pw(password),
            "stripe_customer_id": customer_id or existing.get("stripe_customer_id") or "",
            "stripe_subscription_id": subscription_id or existing.get("stripe_subscription_id") or "",
            "is_active": True
        }).eq("email", existing["email"]).execute()
    else:
        try:
            db.table("subscribers").insert({
                "email": email,
                "password_hash": hash_pw(password),
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "is_active": True
            }).execute()
        except Exception:
            # Race: webhook created the row a moment ago — update it instead.
            db.table("subscribers").update({
                "password_hash": hash_pw(password),
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "is_active": True
            }).eq("email", email).execute()

    # Auto-login
    sid = secrets.token_hex(32)
    SESSIONS[sid] = email
    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp

# ── Set / recover password (paid but no password yet) ─────────────────────────
@app.get("/setup", response_class=HTMLResponse)
async def setup_get(email: str = ""):
    return SETUP_HTML.replace("{email}", email).replace("{error}", "")

@app.post("/setup", response_class=HTMLResponse)
async def setup_post(
    email: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...)
):
    email = _norm_email(email)
    if password != confirm:
        return SETUP_HTML.replace("{email}", email).replace(
            "{error}", '<div class="error-box">❌ Passwords do not match.</div>')
    if len(password) < 6:
        return SETUP_HTML.replace("{email}", email).replace(
            "{error}", '<div class="error-box">❌ Password must be at least 6 characters.</div>')

    user = _find_subscriber(email)

    if user and user.get("password_hash"):
        return SETUP_HTML.replace("{email}", email).replace(
            "{error}", '<div class="error-box">❌ This account already has a password. <a href="/login" style="color:#f59e0b">Login here.</a></div>')

    if user:
        # Paid account exists without a password — set it.
        db.table("subscribers").update({
            "password_hash": hash_pw(password),
            "is_active": True
        }).eq("email", user["email"]).execute()
    else:
        # No row yet — verify they actually paid via Stripe before creating one.
        customer_id, subscription_id = _find_stripe_active(email)
        if not customer_id:
            return SETUP_HTML.replace("{email}", email).replace(
                "{error}", '<div class="error-box">❌ No active subscription found for this email. <a href="/subscribe" style="color:#f59e0b">Subscribe here.</a></div>')
        try:
            db.table("subscribers").insert({
                "email": email,
                "password_hash": hash_pw(password),
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "is_active": True
            }).execute()
        except Exception:
            # Race: row created concurrently — update it instead.
            db.table("subscribers").update({
                "password_hash": hash_pw(password),
                "is_active": True
            }).eq("email", email).execute()

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
async def login_post(request: Request, email: str = Form(...), password: str = Form(...)):
    # ── Admin bypass ──────────────────────────────────────────────────────
    if ADMIN_EMAIL and email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
        sid = secrets.token_hex(32)
        SESSIONS[sid] = email
        resp = RedirectResponse(url="/dashboard", status_code=302)
        resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 365)  # 1 year
        return resp

    user = _find_subscriber(email)
    if not user:
        return LOGIN_HTML.replace("{error}", '<div class="error-box">❌ Email not found. <a href="/setup" style="color:#f59e0b">Set your password</a> if you already paid, or <a href="/subscribe" style="color:#f59e0b">subscribe here.</a></div>')

    email = user["email"]
    if not user.get("password_hash"):
        return LOGIN_HTML.replace("{error}", '<div class="error-box">❌ You have not set a password yet. <a href="/setup" style="color:#f59e0b">Set it here.</a></div>')

    if user["password_hash"] != hash_pw(password):
        return LOGIN_HTML.replace("{error}", '<div class="error-box">❌ Incorrect password.</div>')

    if not user.get("is_active"):
        return LOGIN_HTML.replace("{error}", '<div class="error-box">❌ Your subscription is inactive. <a href="/subscribe" style="color:#f59e0b">Renew here.</a></div>')

    # Log this login attempt for IP tracking (skip for admin)
    if email != ADMIN_EMAIL:
        try:
            ip = request.headers.get("X-Forwarded-For", request.client.host or "unknown").split(",")[0].strip()
            ua = request.headers.get("User-Agent", "")[:200]
            db.table("login_log").insert({"email": email, "ip": ip, "user_agent": ua}).execute()
            # Check for suspicious activity (5+ unique IPs in last 24h)
            from datetime import datetime, timedelta, timezone
            since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            logs = db.table("login_log").select("ip").eq("email", email).gte("logged_at", since).execute()
            unique_ips = len(set(l["ip"] for l in logs.data))
            if unique_ips >= 5:
                db.table("subscribers").update({"notes": f"⚠️ SUSPICIOUS: {unique_ips} IPs in 24h"}).eq("email", email).execute()
        except Exception:
            pass

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
    # Only the admin sees the ⚙️ Admin link. Regular members get an empty string here
    # (the /admin route is still server-side protected by is_admin regardless).
    admin_link = ('<a href="/admin/parlay" style="color:#f59e0b;font-size:12px;font-weight:700;'
                  'text-decoration:none" class="nav-link">🎯 Parlay Lab</a>'
                  '<a href="/admin" style="color:#f59e0b;font-size:12px;font-weight:700;'
                  'text-decoration:none" class="nav-link">⚙️ Admin</a>') if is_admin(request) else ""
    return (DASHBOARD_HTML
            .replace("{admin_link}", admin_link)
            .replace("{email}", user)
            .replace("__IS_ADMIN__", "true" if is_admin(request) else "false")
            .replace("__HUB_TOKEN__", make_app_token(user)))

# ── Logout ─────────────────────────────────────────────────────────────────────
@app.get("/logout")
async def logout(request: Request):
    sid = request.cookies.get("sid")
    if sid and sid in SESSIONS:
        del SESSIONS[sid]
    # Send users to the branded landing page (custom domain) to log in again,
    # not the raw onrender.com URL. NOTE: the apex domain (no "www") is the one
    # that resolves; the "www." subdomain is not configured.
    resp = RedirectResponse(url="https://moneypicksarena.com")
    resp.delete_cookie("sid")
    return resp


# ── Admin Dashboard ────────────────────────────────────────────────────────────
def is_admin(request: Request) -> bool:
    sid = request.cookies.get("sid")
    email = SESSIONS.get(sid, "") if sid else ""
    return email == ADMIN_EMAIL and bool(ADMIN_EMAIL)

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not is_admin(request):
        return RedirectResponse(url="/login")

    from datetime import datetime, timedelta, timezone
    from collections import defaultdict

    # Get all subscribers (wrapped in try/except)
    try:
        subs = db.table("subscribers").select("*").execute().data or []
        subs.sort(key=lambda x: x.get("created_at",""), reverse=True)
    except Exception as e:
        return HTMLResponse(f"<h2>DB Error fetching subscribers: {e}</h2>")

    # Get login logs (may not exist yet — handled gracefully)
    all_logs = []
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        all_logs = db.table("login_log").select("email,ip,logged_at").gte("logged_at", since).execute().data or []
    except Exception:
        pass  # login_log table may not exist yet — that's OK

    # Build per-user stats
    from collections import defaultdict
    ip_map = defaultdict(set)
    last_login_map = {}
    for log in all_logs:
        ip_map[log["email"]].add(log["ip"])
        ts = log.get("logged_at","")
        if ts > last_login_map.get(log["email"],""):
            last_login_map[log["email"]] = ts

    rows = ""
    for s in subs:
        em = s["email"]
        active = s.get("is_active", False)
        ips = ip_map.get(em, set())
        ip_count = len(ips)
        last_ip = list(ips)[-1] if ips else "—"
        last_seen = last_login_map.get(em, "—")[:16].replace("T"," ") if last_login_map.get(em) else "—"
        notes = s.get("notes","") or ""
        suspicious = "⚠️" in notes
        status_badge = '<span style="color:#4ade80;font-weight:700">✅ Active</span>' if active else '<span style="color:#f87171;font-weight:700">❌ Inactive</span>'
        sus_badge = '<span style="background:#7f1d1d;color:#fca5a5;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">⚠️ SUSPICIOUS</span>' if suspicious else ""
        ip_color = "#fca5a5" if ip_count >= 5 else "#f59e0b" if ip_count >= 3 else "#4ade80"
        cancel_btn = f'<form method="post" action="/admin/cancel" style="display:inline"><input type="hidden" name="email" value="{em}"><button style="background:#7f1d1d;color:#fca5a5;border:1px solid #991b1b;border-radius:6px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:700">❌ Cancel</button></form>' if active else f'<form method="post" action="/admin/reinstate" style="display:inline"><input type="hidden" name="email" value="{em}"><button style="background:#14532d;color:#86efac;border:1px solid #166534;border-radius:6px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:700">✅ Reinstate</button></form>'
        rows += f"""<tr style="border-bottom:1px solid #1f2937">
          <td style="padding:12px 14px;color:#e5e7eb;font-size:13px">{em}</td>
          <td style="padding:12px 14px">{status_badge}</td>
          <td style="padding:12px 14px;color:{ip_color};font-weight:700;font-size:13px">{ip_count} IPs {sus_badge}</td>
          <td style="padding:12px 14px;color:#9ca3af;font-size:12px;font-family:monospace">{last_ip}</td>
          <td style="padding:12px 14px;color:#9ca3af;font-size:12px">{last_seen}</td>
          <td style="padding:12px 14px;color:#9ca3af;font-size:11px;max-width:180px">{notes}</td>
          <td style="padding:12px 14px">{cancel_btn}</td>
        </tr>"""

    total = len(subs)
    active_count = sum(1 for s in subs if s.get("is_active"))
    suspicious_count = sum(1 for s in subs if "⚠️" in (s.get("notes","") or ""))

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MPA Admin</title>
<style>
  body{{background:#0a0a0a;color:#e5e7eb;font-family:'Segoe UI',sans-serif;padding:32px}}
  h1{{color:#f59e0b;font-size:28px;margin-bottom:4px}}
  .stats{{display:flex;gap:20px;margin:20px 0}}
  .stat{{background:#111;border:1px solid #1f2937;border-radius:10px;padding:16px 24px;text-align:center}}
  .stat .n{{font-size:28px;font-weight:900;color:#f59e0b}}
  .stat .l{{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:1px;margin-top:4px}}
  table{{width:100%;border-collapse:collapse;background:#111;border-radius:10px;overflow:hidden;border:1px solid #1f2937}}
  th{{background:#0a0a0a;padding:10px 14px;text-align:left;color:#f59e0b;font-size:11px;text-transform:uppercase;letter-spacing:1px;white-space:nowrap}}
  tr:hover td{{background:#1a1a1a}}
  .back{{color:#f59e0b;text-decoration:none;font-size:13px;display:inline-block;margin-bottom:20px}}
</style></head><body>
  <a href="/dashboard" class="back">← Back to Dashboard</a>
  <h1>🔐 Money Picks Arena — Admin</h1>
  <p style="color:#6b7280;margin-bottom:20px">Manage subscribers, detect sharing, cancel accounts.</p>
  <div class="stats">
    <div class="stat"><div class="n">{total}</div><div class="l">Total Members</div></div>
    <div class="stat"><div class="n" style="color:#4ade80">{active_count}</div><div class="l">Active</div></div>
    <div class="stat"><div class="n" style="color:#fca5a5">{total-active_count}</div><div class="l">Inactive</div></div>
    <div class="stat"><div class="n" style="color:#fca5a5">{suspicious_count}</div><div class="l">Suspicious ⚠️</div></div>
  </div>
  <table>
    <thead><tr>
      <th>Email</th><th>Status</th><th>Unique IPs (30d)</th>
      <th>Last IP</th><th>Last Seen</th><th>Notes</th><th>Action</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#374151;font-size:11px;margin-top:16px">⚠️ = 5+ unique IPs in 24h &nbsp;|&nbsp; 🟡 = 3-4 IPs &nbsp;|&nbsp; 🟢 = 1-2 IPs</p>

  <div style="background:#111;border:1px solid #1f2937;border-radius:10px;padding:24px;margin-top:28px;max-width:480px">
    <h3 style="color:#f59e0b;font-size:16px;margin-bottom:4px">➕ Create User (No Stripe needed)</h3>
    <p style="color:#6b7280;font-size:12px;margin-bottom:16px">Use for test accounts, comped users, or friends.</p>
    <form method="post" action="/admin/create-user">
      <div style="margin-bottom:12px">
        <label style="display:block;color:#9ca3af;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Email</label>
        <input name="email" type="email" required placeholder="user@example.com"
               style="width:100%;background:#0a0a0a;border:1px solid #374151;border-radius:8px;padding:10px 14px;color:#fff;font-size:13px;outline:none;box-sizing:border-box">
      </div>
      <div style="margin-bottom:12px">
        <label style="display:block;color:#9ca3af;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Password</label>
        <input name="password" type="text" required placeholder="Choose a password for them"
               style="width:100%;background:#0a0a0a;border:1px solid #374151;border-radius:8px;padding:10px 14px;color:#fff;font-size:13px;outline:none;box-sizing:border-box">
      </div>
      <div style="margin-bottom:16px">
        <label style="display:block;color:#9ca3af;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Notes (optional)</label>
        <input name="notes" type="text" placeholder="e.g. Test user - John"
               style="width:100%;background:#0a0a0a;border:1px solid #374151;border-radius:8px;padding:10px 14px;color:#fff;font-size:13px;outline:none;box-sizing:border-box">
      </div>
      <button type="submit"
              style="background:linear-gradient(135deg,#f59e0b,#d97706);color:#000;border:none;border-radius:8px;padding:12px 28px;font-size:13px;font-weight:900;cursor:pointer;width:100%">
        ➕ Create User
      </button>
    </form>
  </div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/admin/parlay", response_class=HTMLResponse)
async def admin_parlay(request: Request):
    if not is_admin(request):
        return RedirectResponse(url="/login")
    return HTMLResponse(PARLAY_HTML)


@app.get("/admin/parlay/data")
async def admin_parlay_data(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    from datetime import datetime, timezone
    qp = request.query_params
    default = qp.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dates = {s: (qp.get("date_" + s.lower()) or default) for s in ("MLB", "NHL", "NBA", "NFL")}
    email = get_user(request) or ADMIN_EMAIL
    token = make_app_token(email)
    sports = await _fetch_sport_legs(token, dates)
    legs = []
    for info in sports.values():
        legs.extend(info.get("legs", []))
    summary = {s: {"ok": i["ok"], "error": i["error"], "count": len(i["legs"]), "date": dates[s]}
               for s, i in sports.items()}
    return JSONResponse({"date": default, "dates": dates, "summary": summary, "legs": legs})


@app.get("/api/my-record")
async def my_record(request: Request):
    """Combined cross-sport bet record (admin only). Fans out to all four apps'
    /api/bets, aggregates Record / Win% / ROI, and returns a per-sport breakdown."""
    if not is_admin(request):
        return JSONResponse({"error": "admin only"}, status_code=403)
    email = get_user(request) or ADMIN_EMAIL
    token = make_app_token(email)
    sports = await _fetch_sport_bets(token)
    agg = {"wins": 0, "losses": 0, "push": 0, "pending": 0, "staked": 0.0, "profit": 0.0}
    by_sport = []
    for s in ("MLB", "NHL", "NBA", "NFL"):
        info = sports.get(s, {})
        summ = info.get("summary")
        row = {"sport": s, "ok": bool(info.get("ok")), "error": info.get("error", "")}
        if summ:
            agg["wins"] += summ.get("wins", 0) or 0
            agg["losses"] += summ.get("losses", 0) or 0
            agg["push"] += summ.get("push", 0) or 0
            agg["pending"] += summ.get("pending", 0) or 0
            agg["staked"] += summ.get("staked", 0) or 0
            agg["profit"] += summ.get("profit", 0) or 0
            for k in ("wins", "losses", "push", "pending", "staked", "profit", "returned", "roi"):
                row[k] = summ.get(k)
        by_sport.append(row)
    staked = agg["staked"]
    profit = agg["profit"]
    decided = agg["wins"] + agg["losses"]
    combined = {
        "wins": agg["wins"], "losses": agg["losses"], "push": agg["push"],
        "pending": agg["pending"], "staked": round(staked, 2), "profit": round(profit, 2),
        "returned": round(staked + profit, 2),
        "roi": round(profit / staked * 100, 1) if staked > 0 else None,
        "win_pct": round(agg["wins"] / decided * 100, 1) if decided > 0 else None,
    }
    return JSONResponse({"combined": combined, "by_sport": by_sport})


@app.post("/admin/cancel")
async def admin_cancel(request: Request, email: str = Form(...)):
    if not is_admin(request):
        return RedirectResponse(url="/login")
    if email == ADMIN_EMAIL:  # Never cancel the master account
        return RedirectResponse(url="/admin", status_code=302)
    try:
        existing = db.table("subscribers").select("notes,stripe_subscription_id").eq("email", email).execute().data or [{}]
        old_notes = existing[0].get("notes", "") or ""
        db.table("subscribers").update({
            "is_active": False,
            "notes": old_notes + " | CANCELLED BY ADMIN"
        }).eq("email", email).execute()
        sid = existing[0].get("stripe_subscription_id")
        if sid:
            try:
                stripe.Subscription.cancel(sid)
            except Exception:
                pass
    except Exception:
        pass
    return RedirectResponse(url="/admin", status_code=302)


@app.post("/admin/reinstate")
async def admin_reinstate(request: Request, email: str = Form(...)):
    if not is_admin(request):
        return RedirectResponse(url="/login")
    try:
        db.table("subscribers").update({"is_active": True}).eq("email", email).execute()
    except Exception:
        pass
    return RedirectResponse(url="/admin", status_code=302)


@app.post("/admin/create-user")
async def admin_create_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    notes: str = Form("")
):
    if not is_admin(request):
        return RedirectResponse(url="/login")
    try:
        # Check if user already exists
        existing = db.table("subscribers").select("id").eq("email", email).execute().data
        if existing:
            return HTMLResponse(f"""<html><body style="background:#0a0a0a;color:#f87171;font-family:sans-serif;padding:40px">
                <h2>❌ User already exists: {email}</h2>
                <a href="/admin" style="color:#f59e0b">← Back to Admin</a>
            </body></html>""")
        # Create the user
        db.table("subscribers").insert({
            "email":         email,
            "password_hash": hash_pw(password),
            "is_active":     True,
            "notes":         notes or "Created by admin (no Stripe)"
        }).execute()
        return HTMLResponse(f"""<html><body style="background:#0a0a0a;color:#4ade80;font-family:sans-serif;padding:40px">
            <h2>✅ User created successfully!</h2>
            <p style="color:#9ca3af;margin:12px 0">Email: <strong style="color:#fff">{email}</strong></p>
            <p style="color:#9ca3af;margin:12px 0">Password: <strong style="color:#fff">{password}</strong></p>
            <p style="color:#6b7280;font-size:13px;margin-top:20px">Share these credentials with your test user. They can log in at your hub URL.</p>
            <a href="/admin" style="color:#f59e0b;display:inline-block;margin-top:20px">← Back to Admin</a>
        </body></html>""")
    except Exception as e:
        return HTMLResponse(f"""<html><body style="background:#0a0a0a;color:#f87171;font-family:sans-serif;padding:40px">
            <h2>❌ Error: {e}</h2>
            <a href="/admin" style="color:#f59e0b">← Back to Admin</a>
        </body></html>""")

# ── Stripe Webhook ─────────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(body, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        details = sess.get("customer_details") or {}
        email = _norm_email(details.get("email") or sess.get("customer_email") or "")
        customer_id = sess.get("customer") or ""
        subscription_id = sess.get("subscription") or ""
        if email and db:
            existing = _find_subscriber(email)
            if existing:
                db.table("subscribers").update({
                    "stripe_customer_id": customer_id or existing.get("stripe_customer_id") or "",
                    "stripe_subscription_id": subscription_id or existing.get("stripe_subscription_id") or "",
                    "is_active": True
                }).eq("email", existing["email"]).execute()
            else:
                db.table("subscribers").insert({
                    "email": email,
                    "password_hash": "",
                    "stripe_customer_id": customer_id,
                    "stripe_subscription_id": subscription_id,
                    "is_active": True
                }).execute()

    elif event["type"] == "customer.subscription.updated":
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
