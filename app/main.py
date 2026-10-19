"""Web application for entering and reviewing M&E input data.

Roles / who enters what
  AGENCY   user agency supervisor at a posting site: monthly attendance returns, supervisor evaluations,
           service-outcome proposals (CSV upload or grid)
  DISTRICT district M&E officer: verifies agency submissions, enters district monitoring visits
  REGION   regional M&E officer: approves verified submissions and district visits, enters regional visits
  HQ       headquarters: certifies service outcomes, enters HQ visits, manages periods, refreshes analytics
"""
import contextlib, csv, io, os, secrets, sys, threading, traceback, datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWERBI_DIR = ROOT / "outputs" / "powerbi"
POWERBI_REPORT = POWERBI_DIR / "National_Service_ME_Dashboard.pbix"
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("NSME_DB_NAME", "ns_me_v2")

from fastapi import FastAPI, HTTPException, Request, Form, UploadFile, File
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from psycopg2.extras import RealDictCursor

from db import connect
import ingest

ALLOW_FUTURE_DATES = os.environ.get("NSME_ALLOW_FUTURE_DATES", "1") == "1"   # demo cycle lies in the future
CREDENTIALS_FILE = ROOT / "outputs" / "credentials_v2.csv"
SECRET_FILE = ROOT / ".secret"
if not SECRET_FILE.exists():
    SECRET_FILE.write_text(secrets.token_hex(32))

app = FastAPI(title="National Service M&E")
app.add_middleware(SessionMiddleware, secret_key=SECRET_FILE.read_text(), same_site="lax", max_age=8 * 3600)
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
templates.env.filters["mon"] = lambda d: d.strftime("%b %Y") if d else ""


# ------------------------------------------------------------------ db helpers
def q(sql, params=(), one=False):
    con = connect()
    try:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows
    finally:
        con.close()


@contextlib.contextmanager
def tx():
    con = connect()
    try:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def audit(cur, uid, action, table, detail):
    cur.execute("INSERT INTO audit_log(user_id,action,table_name,detail) VALUES (%s,%s,%s,%s)", (uid, action, table, detail))


# ------------------------------------------------------------------ session / scope
class NeedLogin(Exception):
    pass


@app.exception_handler(NeedLogin)
async def _login_redirect(request, exc):
    return RedirectResponse("/login", 303)


def user_of(request):
    uid = request.session.get("uid")
    if not uid:
        raise NeedLogin()
    u = q("SELECT u.*, rd.district_name, ua.agency_name FROM app_users u "
          "LEFT JOIN regions_districts rd USING (district_id) LEFT JOIN user_agencies ua ON ua.agency_id=u.agency_id "
          "WHERE u.user_id=%s AND u.is_active", (uid,), one=True)
    if not u:
        request.session.clear()
        raise NeedLogin()
    return u


def need(u, *roles):
    if u["role"] not in roles:
        raise PermissionError("Your role cannot do this.")


def scope(u, po="po", rd="rd"):
    """SQL predicate limiting rows to the user's remit (alias po = postings, rd = regions_districts)."""
    r = u["role"]
    if r == "AGENCY":
        return f"{po}.agency_id = %s AND {po}.district_id = %s", [u["agency_id"], u["district_id"]]
    if r == "DISTRICT":
        return f"{po}.district_id = %s", [u["district_id"]]
    if r == "REGION":
        return f"{rd}.region_name = %s", [u["region_name"]]
    return "TRUE", []


def csrf(request):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_hex(16)
    return request.session["csrf"]


def check_csrf(request, token):
    if not token or token != request.session.get("csrf"):
        raise PermissionError("Form expired - reload the page and try again.")


def render(request, name, u=None, **ctx):
    ctx.update(u=u, csrf=csrf(request), msg=request.session.pop("msg", None), err=request.session.pop("err", None))
    ctx.setdefault("hide_err", False)
    return templates.TemplateResponse(request, name, ctx)


def flash(request, msg=None, err=None):
    if msg:
        request.session["msg"] = msg
    if err:
        request.session["err"] = err


@app.exception_handler(PermissionError)
async def _perm(request, exc):
    flash(request, err=str(exc))
    return RedirectResponse(request.headers.get("referer") or "/", 303)


# ------------------------------------------------------------------ periods
def all_periods():
    return q("SELECT * FROM reporting_periods ORDER BY period_no")


def pick_period(request, open_only=False):
    ps = all_periods()
    want = request.query_params.get("period")
    if want:
        for p in ps:
            if str(p["reporting_period"]) == want:
                return p, ps
    today = dt.date.today()
    cur = [p for p in ps if p["reporting_period"] <= today]
    return (cur[-1] if cur else ps[0]), ps


def period_by_date(d):
    return q("SELECT * FROM reporting_periods WHERE %s >= reporting_period "
             "AND %s < reporting_period + interval '1 month'", (d, d), one=True)


# ------------------------------------------------------------------ auth
def credentials_match(username, password):
    with CREDENTIALS_FILE.open(newline="", encoding="utf-8-sig") as credentials:
        reader = csv.DictReader(credentials)
        if not reader.fieldnames or not {"username", "password"}.issubset(reader.fieldnames):
            raise RuntimeError(f"{CREDENTIALS_FILE} must contain username and password columns.")
        for row in reader:
            stored_password = row.get("password")
            if (
                row.get("username") == username
                and isinstance(stored_password, str)
                and secrets.compare_digest(password, stored_password)
            ):
                return True
    return False


@app.get("/login")
def login_form(request: Request):
    return render(request, "login.html", hide_err=True)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    if not credentials_match(username, password):
        flash(request, err="Wrong username or password.")
        return RedirectResponse("/login", 303)
    u = q("SELECT * FROM app_users WHERE username=%s AND is_active", (username,), one=True)
    if not u:
        flash(request, err="Wrong username or password.")
        return RedirectResponse("/login", 303)
    request.session.clear()
    request.session["uid"] = u["user_id"]
    with tx() as cur:
        audit(cur, u["user_id"], "LOGIN", "app_users", username)
    return RedirectResponse("/", 303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


# ------------------------------------------------------------------ dashboard
@app.get("/")
def dashboard(request: Request):
    u = user_of(request)
    p, ps = pick_period(request)
    sc, sp = scope(u)
    ctx = dict(period=p, periods=ps)
    base = (f"FROM {{t}} x JOIN postings po USING (personnel_id) JOIN regions_districts rd USING (district_id) "
            f"WHERE {sc} ")
    if u["role"] == "AGENCY":
        ctx["headcount"] = q(f"SELECT count(*) n FROM postings po JOIN regions_districts rd USING (district_id) WHERE {sc}", sp, one=True)["n"]
        ctx["by_status"] = q("SELECT x.status, count(*) n " + base.format(t="attendance_returns") +
                             "AND x.reporting_period=%s GROUP BY 1 ORDER BY 1", sp + [p["reporting_period"]])
        ctx["rejected"] = q("SELECT x.personnel_id, x.reject_reason " + base.format(t="attendance_returns") +
                            "AND x.status='REJECTED' AND x.reporting_period=%s", sp + [p["reporting_period"]])
        ctx["comp_status"] = q("SELECT x.status, count(*) n " + base.format(t="service_completion") + "GROUP BY 1", sp)
    else:
        ctx["pending"] = pending_counts(u)
        flt, fp = {"DISTRICT": ("district_id = %s", [u["district_id"]]), "REGION": ("region_name = %s", [u["region_name"]]),
                   "HQ": ("TRUE", [])}[u["role"]]
        ctx["score"] = q(f"SELECT * FROM vw_district_scorecard WHERE reporting_period=%s AND {flt} "
                         "ORDER BY n_red DESC, composite_achievement LIMIT 40", [p["reporting_period"]] + fp)
    if u["role"] == "HQ":
        ctx["kpis"] = q("SELECT * FROM vw_national_kpis ORDER BY reporting_period")
    return render(request, "dashboard.html", u, **ctx)


# ------------------------------------------------------------------ personnel directory
@app.get("/personnel")
def personnel_directory(request: Request):
    u = user_of(request)
    sc, sp = scope(u)
    metrics = q(f"""SELECT count(*) AS total,
                           count(*) FILTER (WHERE sc.personnel_id IS NULL) AS active,
                           count(*) FILTER (WHERE sc.outcome='COMPLETED') AS completed,
                           count(*) FILTER (WHERE pe.qualification='Degree') AS degree,
                           count(*) FILTER (WHERE pe.qualification='HND') AS hnd,
                           count(*) FILTER (WHERE pe.qualification='Diploma') AS diploma,
                           count(*) FILTER (WHERE pe.qualification='Masters') AS masters
                    FROM postings po
                    JOIN personnel pe USING (personnel_id)
                    JOIN regions_districts rd USING (district_id)
                    LEFT JOIN service_completion sc ON sc.personnel_id=pe.personnel_id
                         AND sc.status='CERTIFIED'
                    WHERE {sc}""", sp, one=True)
    regions = q("SELECT DISTINCT region_name FROM regions_districts ORDER BY region_name")
    agencies = q(f"""SELECT DISTINCT ua.agency_id, ua.agency_name
                     FROM postings po
                     JOIN regions_districts rd USING (district_id)
                     JOIN user_agencies ua USING (agency_id)
                     WHERE {sc}
                     ORDER BY ua.agency_name""", sp)
    return render(request, "personnel.html", u, metrics=metrics, regions=regions, agencies=agencies)


@app.get("/agencies")
def agencies_directory(request: Request):
    u = user_of(request)
    where, params = agency_scope(u)
    regions = q(f"""SELECT DISTINCT rd.region_name
                    FROM user_agencies ua
                    JOIN postings po USING (agency_id)
                    JOIN regions_districts rd USING (district_id)
                    WHERE {where}
                    ORDER BY rd.region_name""", params)
    return render(request, "agencies.html", u, regions=regions)


def agency_scope(u):
    if u["role"] == "AGENCY":
        return "po.agency_id=%s AND po.district_id=%s", [u["agency_id"], u["district_id"]]
    if u["role"] == "DISTRICT":
        return "po.district_id=%s", [u["district_id"]]
    if u["role"] == "REGION":
        return "rd.region_name=%s", [u["region_name"]]
    return "TRUE", []


@app.get("/agencies/search")
def agencies_search(request: Request):
    u = user_of(request)
    params = request.query_params
    search = params.get("q", "").strip()[:120]
    category = params.get("category", "").strip()
    region = params.get("region", "").strip()
    try:
        offset = max(0, int(params.get("offset", "0")))
        limit = min(100, max(1, int(params.get("limit", "50"))))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid directory pagination.")
    if category and category not in ("Public", "Private", "Quasi"):
        raise HTTPException(status_code=422, detail="Invalid agency category.")

    scoped_where, scoped_params = agency_scope(u)
    filters = [scoped_where]
    values = list(scoped_params)
    if category:
        filters.append("ua.category=%s")
        values.append(category)
    if region:
        filters.append("rd.region_name=%s")
        values.append(region)
    if search:
        pattern = f"%{search}%"
        filters.append("(ua.agency_name ILIKE %s OR ua.category ILIKE %s "
                       "OR rd.region_name ILIKE %s OR rd.district_name ILIKE %s)")
        values.extend([pattern] * 4)

    joins = """FROM user_agencies ua
               JOIN postings po USING (agency_id)
               JOIN regions_districts rd USING (district_id)"""
    where_sql = " AND ".join(filters)
    total = q(f"SELECT count(DISTINCT ua.agency_id) AS total {joins} WHERE {where_sql}",
              values, one=True)["total"]
    agencies = q(f"""SELECT ua.agency_id, ua.agency_name, ua.category,
                            count(DISTINCT po.personnel_id) AS headcount,
                            count(DISTINCT po.district_id) AS active_sites,
                            count(DISTINCT rd.region_name) AS region_count,
                            string_agg(DISTINCT rd.region_name, ', ' ORDER BY rd.region_name) AS operating_regions
                     {joins}
                     WHERE {where_sql}
                     GROUP BY ua.agency_id, ua.agency_name, ua.category
                     ORDER BY ua.agency_name
                     LIMIT %s OFFSET %s""", values + [limit, offset])
    return JSONResponse(jsonable_encoder({"agencies": agencies, "total": total,
                                          "offset": offset, "limit": limit}))


@app.get("/personnel/districts")
def personnel_districts(request: Request, region: str = ""):
    u = user_of(request)
    sc, sp = scope(u)
    rows = q(f"""SELECT DISTINCT rd.district_id, rd.district_name
                 FROM postings po
                 JOIN regions_districts rd USING (district_id)
                 WHERE {sc} AND (%s='' OR rd.region_name=%s)
                 ORDER BY rd.district_name""", sp + [region, region])
    return JSONResponse(jsonable_encoder(rows))


@app.get("/personnel/search")
def personnel_search(request: Request):
    u = user_of(request)
    params = request.query_params
    query = params.get("q", "").strip()[:120]
    region = params.get("region", "").strip()
    district_id = params.get("district_id", "").strip()
    agency_id = params.get("agency_id", "").strip()
    qualification = params.get("qualification", "").strip()
    gender = params.get("gender", "").strip()
    status = params.get("status", "").strip().upper()
    try:
        offset = max(0, int(params.get("offset", "0")))
        limit = min(100, max(1, int(params.get("limit", "50"))))
        district_id = int(district_id) if district_id else None
        agency_id = int(agency_id) if agency_id else None
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid directory filter.")
    if qualification and qualification not in ("Degree", "HND", "Diploma", "Masters"):
        raise HTTPException(status_code=422, detail="Invalid qualification filter.")
    if gender and gender not in ("Male", "Female"):
        raise HTTPException(status_code=422, detail="Invalid gender filter.")
    if status and status not in ("ACTIVE", "COMPLETED", "DISCONTINUED", "ABSCONDED"):
        raise HTTPException(status_code=422, detail="Invalid service outcome filter.")

    sc, sp = scope(u)
    where = [sc]
    values = list(sp)
    if query:
        where.append("(pe.personnel_id ILIKE %s OR (pe.surname || ' ' || pe.other_names) ILIKE %s "
                     "OR pe.institution_attended ILIKE %s OR pe.course_of_study ILIKE %s "
                     "OR ua.agency_name ILIKE %s)")
        pattern = f"%{query}%"
        values.extend([pattern] * 5)
    for expression, value in (
        ("rd.region_name=%s", region),
        ("rd.district_id=%s", district_id),
        ("ua.agency_id=%s", agency_id),
        ("pe.qualification=%s", qualification),
        ("pe.gender=%s", gender),
        ("COALESCE(sc.outcome, 'ACTIVE')=%s", status),
    ):
        if value:
            where.append(expression)
            values.append(value)
    where_sql = " AND ".join(where)
    joins = """FROM postings po
               JOIN personnel pe USING (personnel_id)
               JOIN regions_districts rd USING (district_id)
               JOIN user_agencies ua USING (agency_id)
               LEFT JOIN service_completion sc ON sc.personnel_id=pe.personnel_id
                    AND sc.status='CERTIFIED'"""
    rows = q(f"""SELECT pe.personnel_id, pe.surname, pe.other_names, pe.institution_attended,
                        pe.course_of_study, pe.qualification, pe.gender, ua.agency_name,
                        rd.region_name, rd.district_name,
                        COALESCE(sc.outcome, 'ACTIVE') AS service_status,
                        count(*) OVER() AS total_count
                 {joins}
                 WHERE {where_sql}
                 ORDER BY pe.surname, pe.other_names, pe.personnel_id
                 LIMIT %s OFFSET %s""", values + [limit, offset])
    total = rows[0]["total_count"] if rows else 0
    for row in rows:
        row.pop("total_count")
    return JSONResponse(jsonable_encoder({"rows": rows, "total": total, "offset": offset, "limit": limit}))


@app.get("/personnel/{personnel_id}")
def personnel_profile(request: Request, personnel_id: str):
    u = user_of(request)
    sc, sp = scope(u)
    person = q(f"""SELECT pe.personnel_id, pe.surname, pe.other_names, pe.gender,
                          pe.date_of_birth, pe.age_at_posting, pe.residential_region,
                          pe.residential_district, pe.institution_attended, pe.course_of_study,
                          pe.qualification, po.choice_1_region, po.choice_2_region, po.choice_3_region,
                          po.choice_rank_met, po.service_cycle, ua.agency_name, ua.category,
                          rd.region_name AS posting_region, rd.district_name AS posting_district,
                          COALESCE(c.outcome, 'ACTIVE') AS service_status, c.outcome_date, c.reason
                   FROM postings po
                   JOIN personnel pe USING (personnel_id)
                   JOIN regions_districts rd USING (district_id)
                   JOIN user_agencies ua USING (agency_id)
                   LEFT JOIN service_completion c ON c.personnel_id=pe.personnel_id
                        AND c.status='CERTIFIED'
                   WHERE {sc} AND pe.personnel_id=%s""", sp + [personnel_id], one=True)
    if not person:
        raise HTTPException(status_code=404, detail="Personnel record not found.")
    monthly = q("""SELECT COALESCE(ar.reporting_period, ev.reporting_period) AS reporting_period,
                          ar.reported_for_duty, ar.days_expected, ar.days_present, ar.status AS attendance_status,
                          ev.rating, ev.comments, ev.status AS evaluation_status
                   FROM (SELECT personnel_id, reporting_period, reported_for_duty, days_expected, days_present, status
                         FROM attendance_returns
                         WHERE personnel_id=%s AND status IN ('DISTRICT_VERIFIED','APPROVED')) ar
                   FULL OUTER JOIN
                        (SELECT personnel_id, reporting_period, rating, comments, status
                         FROM supervisor_evaluations
                         WHERE personnel_id=%s AND status IN ('DISTRICT_VERIFIED','APPROVED')) ev
                   USING (personnel_id, reporting_period)
                   ORDER BY reporting_period DESC""", [personnel_id, personnel_id])
    return JSONResponse(jsonable_encoder({"person": person, "monthly": monthly}))


def pending_counts(u):
    """Items waiting for this user's action."""
    sc, sp = scope(u)
    step = {"DISTRICT": "SUBMITTED", "REGION": "DISTRICT_VERIFIED", "HQ": "DISTRICT_VERIFIED"}[u["role"]]
    j = "JOIN postings po USING (personnel_id) JOIN regions_districts rd USING (district_id)"
    out = {"returns": q(f"SELECT count(*) n FROM attendance_returns x {j} WHERE x.status=%s AND {sc}", [step] + sp, one=True)["n"],
           "completion": q(f"SELECT count(*) n FROM service_completion x {j} WHERE x.status=%s AND {sc}",
                           [{"DISTRICT": "SUBMITTED", "REGION": "DISTRICT_VERIFIED", "HQ": "APPROVED"}[u["role"]]] + sp, one=True)["n"]}
    if u["role"] in ("REGION", "HQ"):
        out["visits"] = q("SELECT count(*) n FROM monitoring_visits mv JOIN regions_districts rd USING (district_id) "
                          "WHERE mv.status='SUBMITTED' AND (%s OR rd.region_name=%s)",
                          (u["role"] == "HQ", u["region_name"]), one=True)["n"]
    return out


# ------------------------------------------------------------------ AGENCY: attendance + evaluation grid
def parse_row(pid, rep, exp, pre, rat, errs):
    """Validate one grid / CSV row. Returns tuple or None if the row is blank."""
    exp, pre, rat = (exp or "").strip(), (pre or "").strip(), (rat or "").strip()
    if not exp and not pre and not rat:      # untouched row (the "reported" box is pre-ticked, so ignore it)
        return None
    try:
        e = int(exp or 0)
        pr = int(pre or 0)
    except ValueError:
        errs.append(f"{pid}: days must be whole numbers")
        return None
    if not (0 <= e <= 31) or not (0 <= pr <= 31):
        errs.append(f"{pid}: days must be between 0 and 31")
    if pr > e:
        errs.append(f"{pid}: days present ({pr}) exceeds days expected ({e})")
    if not rep and pr > 0:
        errs.append(f"{pid}: cannot have days present if not reported for duty")
    if rep and e == 0:
        errs.append(f"{pid}: reported for duty but days expected is 0")
    rating = None
    if rat:
        try:
            rating = Decimal(rat).quantize(Decimal("0.01"))
        except InvalidOperation:
            errs.append(f"{pid}: rating must be a number")
        else:
            if not Decimal(1) <= rating <= Decimal(5):
                errs.append(f"{pid}: rating must be between 1 and 5")
            if not rep:
                errs.append(f"{pid}: rating given but person has not reported for duty")
    return (bool(rep), e, pr, rating)


def save_returns(cur, u, period, rows, submit, errs):
    """rows: {pid: (reported, expected, present, rating)}. Upsert, respecting the edit lock."""
    sc, sp = scope(u)
    cur.execute(f"SELECT po.personnel_id FROM postings po JOIN regions_districts rd USING (district_id) WHERE {sc}", sp)
    mine = {r["personnel_id"] for r in cur.fetchall()}
    cur.execute("SELECT personnel_id, status FROM attendance_returns WHERE reporting_period=%s AND personnel_id = ANY(%s)",
                (period["reporting_period"], list(rows)))
    st = {r["personnel_id"]: r["status"] for r in cur.fetchall()}
    status = "SUBMITTED" if submit else "DRAFT"
    n = 0
    for pid, (rep, e, pr, rating) in rows.items():
        if pid not in mine:
            errs.append(f"{pid}: not posted to your site")
            continue
        if st.get(pid) not in (None, "DRAFT", "REJECTED"):
            errs.append(f"{pid}: already {st[pid]} - cannot be changed")
            continue
        if errs:
            continue
        stamp = ("now()", u["user_id"]) if submit else (None, None)
        cur.execute("""INSERT INTO attendance_returns(personnel_id,reporting_period,reported_for_duty,days_expected,days_present,
                         status,submitted_by,submitted_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN now() END)
                       ON CONFLICT (personnel_id,reporting_period) DO UPDATE SET reported_for_duty=EXCLUDED.reported_for_duty,
                         days_expected=EXCLUDED.days_expected, days_present=EXCLUDED.days_present, status=EXCLUDED.status,
                         submitted_by=EXCLUDED.submitted_by, submitted_at=EXCLUDED.submitted_at, reject_reason=NULL""",
                    (pid, period["reporting_period"], rep, e, pr, status, u["user_id"] if submit else None, submit))
        if rating is not None:
            cur.execute("""INSERT INTO supervisor_evaluations(personnel_id,reporting_period,rating,status,submitted_by,submitted_at)
                           VALUES (%s,%s,%s,%s,%s,CASE WHEN %s THEN now() END)
                           ON CONFLICT (personnel_id,reporting_period) DO UPDATE SET rating=EXCLUDED.rating, status=EXCLUDED.status,
                             submitted_by=EXCLUDED.submitted_by, submitted_at=EXCLUDED.submitted_at, reject_reason=NULL
                           WHERE supervisor_evaluations.status IN ('DRAFT','REJECTED')""",
                        (pid, period["reporting_period"], rating, status, u["user_id"] if submit else None, submit))
        else:      # blank rating on an editable row removes an unsubmitted evaluation
            cur.execute("DELETE FROM supervisor_evaluations WHERE personnel_id=%s AND reporting_period=%s "
                        "AND status IN ('DRAFT','REJECTED')", (pid, period["reporting_period"]))
        n += 1
    return n


def require_open(period):
    if not period["is_open"]:
        raise PermissionError(f"{period['reporting_period']:%B %Y} is closed for new entries.")


@app.get("/agency/returns")
def returns_grid(request: Request):
    u = user_of(request); need(u, "AGENCY")
    p, ps = pick_period(request)
    sc, sp = scope(u)
    rows = q(f"""SELECT pe.personnel_id, pe.surname, pe.other_names, ar.reported_for_duty, ar.days_expected, ar.days_present,
                        ar.status AS ar_status, ar.reject_reason, se.rating, se.status AS ev_status
                 FROM postings po JOIN regions_districts rd USING (district_id) JOIN personnel pe USING (personnel_id)
                 LEFT JOIN attendance_returns ar ON ar.personnel_id=pe.personnel_id AND ar.reporting_period=%s
                 LEFT JOIN supervisor_evaluations se ON se.personnel_id=pe.personnel_id AND se.reporting_period=%s
                 LEFT JOIN service_completion sc ON sc.personnel_id=pe.personnel_id AND sc.status IN ('APPROVED','CERTIFIED')
                      AND sc.exit_period_no <= %s
                 WHERE {sc} AND sc.personnel_id IS NULL ORDER BY pe.surname, pe.other_names""",
             [p["reporting_period"], p["reporting_period"], p["period_no"]] + sp)
    return render(request, "returns_grid.html", u, rows=rows, period=p, periods=ps,
                  editable_states=(None, "DRAFT", "REJECTED"))


@app.post("/agency/returns")
async def returns_save(request: Request):
    u = user_of(request); need(u, "AGENCY")
    f = await request.form()
    check_csrf(request, f.get("csrf"))
    period = q("SELECT * FROM reporting_periods WHERE reporting_period=%s", (f.get("period"),), one=True)
    if not period:
        raise PermissionError("Unknown period.")
    require_open(period)
    submit = f.get("action") == "submit"
    errs, rows = [], {}
    for pid in f.getlist("pid"):
        r = parse_row(pid, f.get(f"rep_{pid}"), f.get(f"exp_{pid}"), f.get(f"pre_{pid}"), f.get(f"rat_{pid}"), errs)
        if r:
            rows[pid] = r
    if not rows and not errs:
        errs.append("Nothing to save - no row has data.")
    if errs:
        flash(request, err="Nothing saved. " + " | ".join(errs[:8]) + (f" (+{len(errs)-8} more)" if len(errs) > 8 else ""))
    else:
        with tx() as cur:
            n = save_returns(cur, u, period, rows, submit, errs)
            if errs:
                cur.connection.rollback()
            else:
                audit(cur, u["user_id"], "SUBMIT_RETURNS" if submit else "SAVE_RETURNS", "attendance_returns",
                      f"{n} rows, period {period['reporting_period']}")
        flash(request, **({"err": "Nothing saved. " + " | ".join(errs[:8])} if errs else
                          {"msg": f"{n} returns {'submitted for district verification' if submit else 'saved as draft'}."}))
    return RedirectResponse(f"/agency/returns?period={period['reporting_period']}", 303)


@app.get("/agency/template.csv")
def template_csv(request: Request):
    u = user_of(request); need(u, "AGENCY")
    p, _ = pick_period(request)
    sc, sp = scope(u)
    rows = q(f"SELECT pe.personnel_id, pe.surname, pe.other_names FROM postings po JOIN regions_districts rd USING (district_id) "
             f"JOIN personnel pe USING (personnel_id) WHERE {sc} ORDER BY 2,3", sp)
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["personnel_id", "name", "reporting_period", "reported_for_duty", "days_expected", "days_present", "rating"])
    for r in rows:
        w.writerow([r["personnel_id"], f"{r['surname']} {r['other_names']}", p["reporting_period"], "", "", "", ""])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=returns_{p['reporting_period']}.csv"})


@app.get("/agency/upload")
def upload_form(request: Request):
    u = user_of(request); need(u, "AGENCY")
    p, ps = pick_period(request)
    return render(request, "upload.html", u, period=p, periods=ps)


@app.post("/agency/upload")
async def upload(request: Request, csrf_token: str = Form(""), file: UploadFile = File(...), mode: str = Form("submit")):
    u = user_of(request); need(u, "AGENCY")
    check_csrf(request, csrf_token)
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    errs, byperiod = [], {}
    for i, r in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        pid = (r.get("personnel_id") or "").strip()
        rp = (r.get("reporting_period") or "").strip()
        if not pid or not rp:
            errs.append(f"line {i}: personnel_id and reporting_period are required")
            continue
        rep = (r.get("reported_for_duty") or "").strip().lower() in ("1", "yes", "y", "true", "t")
        row = parse_row(pid, rep, r.get("days_expected"), r.get("days_present"), r.get("rating"), errs)
        if row:
            byperiod.setdefault(rp, {})[pid] = row
    n = 0
    if not errs and not byperiod:
        errs.append("The file has no data rows.")
    if not errs:
        with tx() as cur:
            for rp, rows in byperiod.items():
                cur.execute("SELECT * FROM reporting_periods WHERE reporting_period=%s", (rp,))
                period = cur.fetchone()
                if not period:
                    errs.append(f"unknown reporting_period {rp}"); break
                if not period["is_open"]:
                    errs.append(f"{rp} is closed"); break
                n += save_returns(cur, u, period, rows, mode == "submit", errs)
            if errs:
                cur.connection.rollback()
            else:
                audit(cur, u["user_id"], "UPLOAD_RETURNS", "attendance_returns", f"{n} rows from {file.filename}")
    if errs:
        flash(request, err="Upload rejected - nothing saved. " + " | ".join(errs[:10]) + (f" (+{len(errs)-10} more)" if len(errs) > 10 else ""))
    else:
        flash(request, msg=f"{n} rows {'submitted' if mode == 'submit' else 'saved as draft'}.")
    return RedirectResponse("/agency/upload", 303)


# ------------------------------------------------------------------ AGENCY: service outcome proposals
@app.get("/agency/completion")
def completion_form(request: Request):
    u = user_of(request); need(u, "AGENCY")
    sc, sp = scope(u)
    rows = q(f"""SELECT pe.personnel_id, pe.surname, pe.other_names, c.outcome, c.status, c.reject_reason, c.outcome_date, c.reason
                 FROM postings po JOIN regions_districts rd USING (district_id) JOIN personnel pe USING (personnel_id)
                 LEFT JOIN service_completion c ON c.personnel_id=pe.personnel_id
                 WHERE {sc} ORDER BY pe.surname, pe.other_names""", sp)
    return render(request, "completion_form.html", u, rows=rows)


@app.post("/agency/completion")
async def completion_save(request: Request):
    u = user_of(request); need(u, "AGENCY")
    f = await request.form(); check_csrf(request, f.get("csrf"))
    sc, sp = scope(u)
    mine = {r["personnel_id"] for r in q(f"SELECT po.personnel_id FROM postings po JOIN regions_districts rd USING (district_id) WHERE {sc}", sp)}
    errs, n = [], 0
    with tx() as cur:
        for pid in f.getlist("pid"):
            oc = (f.get(f"oc_{pid}") or "").strip()
            if not oc:
                continue
            if pid not in mine:
                errs.append(f"{pid}: not at your site"); continue
            try:
                d = dt.date.fromisoformat(f.get(f"dt_{pid}") or "")
            except ValueError:
                errs.append(f"{pid}: a valid outcome date is required"); continue
            per = period_by_date(d)
            if not per:
                errs.append(f"{pid}: date {d} is outside the service cycle"); continue
            if not ALLOW_FUTURE_DATES and d > dt.date.today():
                errs.append(f"{pid}: date is in the future"); continue
            if oc not in ("COMPLETED", "DISCONTINUED", "ABSCONDED", "DECEASED"):
                errs.append(f"{pid}: invalid outcome"); continue
            exit_no = None if oc == "COMPLETED" else per["period_no"]
            cur.execute("""INSERT INTO service_completion(personnel_id,outcome,exit_period_no,outcome_date,reason,status,submitted_by)
                           VALUES (%s,%s,%s,%s,%s,'SUBMITTED',%s)
                           ON CONFLICT (personnel_id) DO UPDATE SET outcome=EXCLUDED.outcome, exit_period_no=EXCLUDED.exit_period_no,
                              outcome_date=EXCLUDED.outcome_date, reason=EXCLUDED.reason, status='SUBMITTED',
                              submitted_by=EXCLUDED.submitted_by, submitted_at=now(), reject_reason=NULL
                           WHERE service_completion.status IN ('REJECTED','SUBMITTED')""",
                        (pid, oc, exit_no, d, (f.get(f"rs_{pid}") or None), u["user_id"]))
            n += cur.rowcount
        if errs:
            cur.connection.rollback()
        else:
            audit(cur, u["user_id"], "PROPOSE_OUTCOMES", "service_completion", f"{n} rows")
    flash(request, **({"err": "Nothing saved. " + " | ".join(errs[:8])} if errs else {"msg": f"{n} outcome proposals submitted."}))
    return RedirectResponse("/agency/completion", 303)


# ------------------------------------------------------------------ REVIEW chain
RET_STEP = {"DISTRICT": ("SUBMITTED", "DISTRICT_VERIFIED", "verify"), "REGION": ("DISTRICT_VERIFIED", "APPROVED", "approve"),
            "HQ": ("DISTRICT_VERIFIED", "APPROVED", "approve")}
COMP_STEP = {"DISTRICT": ("SUBMITTED", "DISTRICT_VERIFIED", "verify"), "REGION": ("DISTRICT_VERIFIED", "APPROVED", "approve"),
             "HQ": ("APPROVED", "CERTIFIED", "certify")}


@app.get("/review")
def review(request: Request):
    u = user_of(request); need(u, "DISTRICT", "REGION", "HQ")
    sc, sp = scope(u)
    frm, to, _ = RET_STEP[u["role"]]
    j = "JOIN postings po USING (personnel_id) JOIN user_agencies ua USING (agency_id) JOIN regions_districts rd USING (district_id)"
    returns = q(f"""SELECT po.agency_id, po.district_id, ua.agency_name, rd.district_name, rd.region_name, x.reporting_period, count(*) n,
                    round(avg(CASE WHEN x.days_expected>0 THEN 100.0*x.days_present/x.days_expected END),1) avg_att,
                    count(*) FILTER (WHERE NOT x.reported_for_duty) not_reported,
                    (SELECT count(*) FROM supervisor_evaluations se JOIN postings p2 USING (personnel_id)
                      WHERE se.status=%s AND p2.agency_id=po.agency_id AND p2.district_id=po.district_id
                        AND se.reporting_period=x.reporting_period) n_evals,
                    (SELECT count(*) FROM postings p3 WHERE p3.agency_id=po.agency_id AND p3.district_id=po.district_id) headcount
                    FROM attendance_returns x {j} WHERE x.status=%s AND {sc}
                    GROUP BY 1,2,3,4,5,6 ORDER BY x.reporting_period, rd.region_name, rd.district_name, ua.agency_name LIMIT 300""",
                [frm, frm] + sp)
    cf, ct, _ = COMP_STEP[u["role"]]
    comps = q(f"""SELECT po.agency_id, po.district_id, ua.agency_name, rd.district_name, rd.region_name, count(*) n,
                  string_agg(DISTINCT x.outcome, ', ') outcomes
                  FROM service_completion x {j} WHERE x.status=%s AND {sc}
                  GROUP BY 1,2,3,4,5 ORDER BY 5,4,3 LIMIT 300""", [cf] + sp)
    visits = []
    if u["role"] in ("REGION", "HQ"):
        visits = q("""SELECT mv.*, ua.agency_name, rd.district_name, rd.region_name,
                      (SELECT count(*) FROM monitoring_visit_personnel vp WHERE vp.visit_id=mv.visit_id) n_people
                      FROM monitoring_visits mv JOIN user_agencies ua USING (agency_id) JOIN regions_districts rd USING (district_id)
                      WHERE mv.status='SUBMITTED' AND (%s OR rd.region_name=%s) ORDER BY mv.visit_date LIMIT 200""",
                   (u["role"] == "HQ", u["region_name"]))
    return render(request, "review.html", u, returns=returns, comps=comps, visits=visits,
                  ret_verb=RET_STEP[u["role"]][2], comp_verb=COMP_STEP[u["role"]][2])


@app.post("/review/act")
async def review_act(request: Request, csrf_token: str = Form(""), kind: str = Form(...), action: str = Form(...),
                     agency_id: int = Form(0), district_id: int = Form(0), period: str = Form(""),
                     visit_id: int = Form(0), reason: str = Form("")):
    u = user_of(request); need(u, "DISTRICT", "REGION", "HQ")
    check_csrf(request, csrf_token)
    reject = action == "reject"
    if reject and len(reason.strip()) < 5:
        raise PermissionError("Give a reason (at least 5 characters) when sending back.")
    d = q("SELECT * FROM regions_districts WHERE district_id=%s", (district_id,), one=True) if district_id else None
    if kind != "visit":
        if not d:
            raise PermissionError("Unknown batch.")
        if (u["role"] == "DISTRICT" and u["district_id"] != d["district_id"]) or \
           (u["role"] == "REGION" and u["region_name"] != d["region_name"]):
            raise PermissionError("That batch is outside your remit.")
    uid = u["user_id"]
    role_col = {"DISTRICT": ("verified_by", "verified_at"), "REGION": ("approved_by", "approved_at"), "HQ": ("approved_by", "approved_at")}
    with tx() as cur:
        if kind == "returns":
            frm, to, verb = RET_STEP[u["role"]]
            by, at = role_col[u["role"]]
            total = 0
            for tbl in ("attendance_returns", "supervisor_evaluations"):
                new = "REJECTED" if reject else to
                cur.execute(f"""UPDATE {tbl} x SET status=%s, {by}=%s, {at}=now(), reject_reason=%s
                                FROM postings po WHERE po.personnel_id=x.personnel_id AND po.agency_id=%s AND po.district_id=%s
                                AND x.reporting_period=%s AND x.status=%s""",
                            (new, uid, reason.strip() if reject else None, agency_id, district_id, period, frm))
                total += cur.rowcount if tbl == "attendance_returns" else 0
            audit(cur, uid, ("REJECT" if reject else verb.upper()) + "_RETURNS", "attendance_returns",
                  f"agency {agency_id} district {district_id} period {period}: {total} returns. {reason}")
        elif kind == "completion":
            frm, to, verb = COMP_STEP[u["role"]]
            col = {"DISTRICT": "verified", "REGION": "approved", "HQ": "certified"}[u["role"]]
            cur.execute(f"""UPDATE service_completion x SET status=%s, {col}_by=%s, {col}_at=now(), reject_reason=%s
                            FROM postings po WHERE po.personnel_id=x.personnel_id AND po.agency_id=%s AND po.district_id=%s
                            AND x.status=%s""",
                        ("REJECTED" if reject else to, uid, reason.strip() if reject else None, agency_id, district_id, frm))
            total = cur.rowcount
            audit(cur, uid, ("REJECT" if reject else verb.upper()) + "_OUTCOMES", "service_completion",
                  f"agency {agency_id} district {district_id}: {total} rows. {reason}")
        elif kind == "visit":
            need(u, "REGION", "HQ")
            cur.execute("""UPDATE monitoring_visits mv SET status=%s, approved_by=%s, approved_at=now(), reject_reason=%s
                           FROM regions_districts rd WHERE rd.district_id=mv.district_id AND mv.visit_id=%s AND mv.status='SUBMITTED'
                           AND (%s OR rd.region_name=%s)""",
                        ("REJECTED" if reject else "APPROVED", uid, reason.strip() if reject else None, visit_id,
                         u["role"] == "HQ", u["region_name"]))
            total = cur.rowcount
            audit(cur, uid, "REJECT_VISIT" if reject else "APPROVE_VISIT", "monitoring_visits", f"visit {visit_id}. {reason}")
        else:
            raise PermissionError("Unknown kind.")
    flash(request, msg=f"{total} record(s) {'sent back' if reject else 'moved forward'}." if total else "Nothing to update (already actioned?).")
    return RedirectResponse("/review", 303)


# ------------------------------------------------------------------ monitoring visits (DISTRICT / REGION / HQ)
@app.get("/visits/new")
def visit_form(request: Request):
    u = user_of(request); need(u, "DISTRICT", "REGION", "HQ")
    dists = q("SELECT * FROM regions_districts WHERE (%s OR district_id=%s) AND (%s OR region_name=%s) ORDER BY region_name, district_name",
              (u["role"] != "DISTRICT", u["district_id"], u["role"] != "REGION", u["region_name"]))
    did = request.query_params.get("district_id") or (dists[0]["district_id"] if len(dists) == 1 else None)
    sites = people = []
    aid = request.query_params.get("agency_id")
    if did:
        sites = q("""SELECT ua.agency_id, ua.agency_name, count(*) n FROM postings po JOIN user_agencies ua USING (agency_id)
                     WHERE po.district_id=%s GROUP BY 1,2 ORDER BY 2""", (did,))
    if did and aid:
        people = q("""SELECT pe.personnel_id, pe.surname, pe.other_names FROM postings po JOIN personnel pe USING (personnel_id)
                      WHERE po.district_id=%s AND po.agency_id=%s ORDER BY 2,3""", (did, aid))
    recent = q("""SELECT mv.visit_date, ua.agency_name, rd.district_name, mv.visit_level, mv.status,
                  (SELECT count(*) FROM monitoring_visit_personnel vp WHERE vp.visit_id=mv.visit_id) n_people
                  FROM monitoring_visits mv JOIN user_agencies ua USING (agency_id) JOIN regions_districts rd USING (district_id)
                  WHERE mv.submitted_by=%s ORDER BY mv.visit_id DESC LIMIT 10""", (u["user_id"],))
    return render(request, "visit_form.html", u, dists=dists, did=int(did) if did else None,
                  aid=int(aid) if aid else None, sites=sites, people=people, recent=recent)


@app.post("/visits/new")
async def visit_save(request: Request):
    u = user_of(request); need(u, "DISTRICT", "REGION", "HQ")
    f = await request.form(); check_csrf(request, f.get("csrf"))
    did, aid = int(f.get("district_id") or 0), int(f.get("agency_id") or 0)
    d = q("SELECT * FROM regions_districts WHERE district_id=%s", (did,), one=True)
    if not d or (u["role"] == "DISTRICT" and u["district_id"] != did) or (u["role"] == "REGION" and u["region_name"] != d["region_name"]):
        raise PermissionError("That district is outside your remit.")
    try:
        vdate = dt.date.fromisoformat(f.get("visit_date") or "")
    except ValueError:
        raise PermissionError("A valid visit date is required.")
    if not ALLOW_FUTURE_DATES and vdate > dt.date.today():
        raise PermissionError("Visit date cannot be in the future.")
    per = period_by_date(vdate)
    if not per:
        raise PermissionError("Visit date is outside the service cycle.")
    require_open(per)
    pids = f.getlist("met")
    if not pids:
        raise PermissionError("Select at least one personnel met during the visit.")
    ok = {r["personnel_id"] for r in q("SELECT personnel_id FROM postings WHERE district_id=%s AND agency_id=%s", (did, aid))}
    if not set(pids) <= ok:
        raise PermissionError("Some personnel are not posted to that site.")
    status = "SUBMITTED" if u["role"] == "DISTRICT" else "APPROVED"
    with tx() as cur:
        cur.execute("SELECT 1 FROM monitoring_visits WHERE agency_id=%s AND district_id=%s AND visit_date=%s AND visit_level=%s",
                    (aid, did, vdate, u["role"]))
        if cur.fetchone():
            raise PermissionError("A visit by your level to this site on that date is already recorded.")
        cur.execute("""INSERT INTO monitoring_visits(agency_id,district_id,reporting_period,visit_date,visit_level,officer_name,findings,
                         status,submitted_by,approved_by,approved_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='APPROVED' THEN now() END) RETURNING visit_id""",
                    (aid, did, per["reporting_period"], vdate, u["role"], u["full_name"], (f.get("findings") or None), status,
                     u["user_id"], u["user_id"] if status == "APPROVED" else None, status))
        vid = cur.fetchone()["visit_id"]
        cur.executemany("INSERT INTO monitoring_visit_personnel VALUES (%s,%s)", [(vid, p) for p in pids])
        audit(cur, u["user_id"], "RECORD_VISIT", "monitoring_visits", f"visit {vid}, {len(pids)} personnel")
    flash(request, msg=f"Visit recorded ({len(pids)} personnel) - "
                       + ("awaiting regional approval." if status == "SUBMITTED" else "approved."))
    return RedirectResponse(f"/visits/new?district_id={did}", 303)


# ------------------------------------------------------------------ HQ: periods, analytics, results
REFRESH = {"running": False, "log": "", "started": None, "finished": None, "ok": None}


def _refresh_job():
    import run_pipeline
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            run_pipeline.refresh()
        REFRESH["ok"] = True
    except Exception:
        buf.write(traceback.format_exc()); REFRESH["ok"] = False
    REFRESH.update(running=False, log=buf.getvalue()[-6000:], finished=dt.datetime.now())


@app.get("/hq")
def hq(request: Request):
    u = user_of(request); need(u, "HQ")
    risk = q("SELECT * FROM vw_district_risk_latest ORDER BY risk_probability DESC LIMIT 30")
    logit = q("SELECT * FROM eval_logistic_results WHERE term IN ('visits_q1','evals_q1','rating_q1') ORDER BY model_name, term")
    counts = q("""SELECT (SELECT count(*) FROM attendance_returns WHERE status='APPROVED') approved_returns,
                         (SELECT count(*) FROM service_completion WHERE status='CERTIFIED') certified,
                         (SELECT count(*) FROM monitoring_visits WHERE status='APPROVED') visits""", one=True)
    return render(request, "hq.html", u, periods=all_periods(), risk=risk, logit=logit, refresh=REFRESH, counts=counts)


@app.get("/powerbi")
def powerbi_alias(request: Request):
    return hq_powerbi(request)


@app.get("/analytics")
def analytics_alias(request: Request):
    return RedirectResponse("/hq", 303)


@app.get("/hq/powerbi")
def hq_powerbi(request: Request):
    u = user_of(request); need(u, "HQ")
    if not POWERBI_REPORT.is_file():
        raise HTTPException(status_code=404, detail="The Power BI report file is not available.")

    periods = all_periods()
    selected_period = request.query_params.get("period")
    period = next((p for p in periods if str(p["reporting_period"]) == selected_period), periods[-1] if periods else None)
    if period is None:
        period = dt.date.today()
        period_record = period_by_date(period)
        if period_record:
            period = period_record
        elif periods:
            period = periods[-1]

    selected_region = request.query_params.get("region", "").strip()
    regions = [r["region_name"] for r in q("SELECT DISTINCT region_name FROM regions_districts ORDER BY region_name")]

    region_sql = " AND rd.region_name = %s" if selected_region else ""
    region_params = [selected_region] if selected_region else []
    posted = q(
        """SELECT count(DISTINCT po.personnel_id) AS n_posted
           FROM postings po
           JOIN regions_districts rd USING (district_id)
           WHERE TRUE""" + region_sql,
        region_params,
        one=True,
    )
    latest_period_kpi = q(
        """SELECT count(*) FILTER (WHERE pm.reported_for_duty) AS n_active,
                   count(*) FILTER (WHERE pm.reported_for_duty AND pm.monitoring_visits > 0) AS n_monitored,
                   count(*) FILTER (WHERE pm.reported_for_duty AND pm.evaluation_done) AS n_evaluated,
                   COALESCE(sum(pm.monitoring_visits), 0) AS n_visits
                FROM personnel_monthly pm
                JOIN postings po USING (personnel_id)
                JOIN regions_districts rd USING (district_id)
                WHERE pm.reporting_period = %s""" + region_sql,
        [period["reporting_period"], *region_params],
        one=True,
    )
    if latest_period_kpi:
        latest_period_kpi["n_posted"] = posted["n_posted"]
        latest_period_kpi["deployment_rate"] = round((latest_period_kpi["n_active"] / latest_period_kpi["n_posted"] * 100) if latest_period_kpi["n_posted"] else 0, 2)
        latest_period_kpi["monitoring_coverage_pct"] = round((latest_period_kpi["n_monitored"] / latest_period_kpi["n_active"] * 100) if latest_period_kpi["n_active"] else 0, 2)
        latest_period_kpi["evaluation_completion_pct"] = round((latest_period_kpi["n_evaluated"] / latest_period_kpi["n_active"] * 100) if latest_period_kpi["n_active"] else 0, 2)

    outcome_summary = q(
        """SELECT count(*) FILTER (WHERE sc.outcome = 'COMPLETED') AS completed_n,
                   count(*) AS total_certified,
                   round(100.0 * count(*) FILTER (WHERE sc.outcome = 'COMPLETED') / NULLIF(count(*), 0), 2) AS completed_share
                FROM service_completion sc
                JOIN postings po USING (personnel_id)
                JOIN regions_districts rd USING (district_id)
                WHERE sc.status = 'CERTIFIED'""" + region_sql,
        [*region_params],
        one=True,
    )

    district_region_sql = " AND region_name = %s" if selected_region else ""
    district_region_params = [selected_region] if selected_region else []
    score_region_sql = " AND ds.region_name = %s" if selected_region else ""
    trend_region_sql = " AND region_name = %s" if selected_region else ""
    district_rag = q(
        """SELECT count(*) FILTER (WHERE composite_achievement >= 95) AS green,
          count(*) FILTER (WHERE composite_achievement >= 80 AND composite_achievement < 95) AS yellow,
          count(*) FILTER (WHERE composite_achievement < 80) AS red
           FROM vw_district_scorecard
           WHERE reporting_period = %s""" + district_region_sql,
        [period["reporting_period"], *district_region_params],
        one=True,
    )
    regional_performance = q(
        """WITH outcomes AS (
               SELECT rd.region_name,
              count(*) FILTER (WHERE sc.outcome = 'COMPLETED') AS completed_n,
              count(*) AS total_certified
               FROM service_completion sc
               JOIN postings po USING (personnel_id)
               JOIN regions_districts rd USING (district_id)
               WHERE sc.status = 'CERTIFIED'
               GROUP BY rd.region_name
           )
           SELECT ds.region_name,
          count(DISTINCT ds.district_id) AS district_count,
          round(avg(ds.composite_achievement), 2) AS achievement_rate,
          round(avg(ia.actual_val) FILTER (WHERE ia.indicator_id = 'IND02'), 2) AS monitoring_per_100,
          round(100.0 * max(outcomes.completed_n) / NULLIF(max(outcomes.total_certified), 0), 2) AS service_completion_rate,
          round(avg(ia.actual_val) FILTER (WHERE ia.indicator_id = 'IND03'), 2) AS evaluation_compliance,
          count(DISTINCT ds.district_id) FILTER (WHERE ds.composite_achievement >= 95) AS green_districts,
          count(DISTINCT ds.district_id) FILTER (WHERE ds.composite_achievement >= 80 AND ds.composite_achievement < 95) AS yellow_districts,
          count(DISTINCT ds.district_id) FILTER (WHERE ds.composite_achievement < 80) AS red_districts
           FROM vw_district_scorecard ds
           JOIN vw_indicator_achievement ia USING (district_id, reporting_period)
           LEFT JOIN outcomes ON outcomes.region_name = ds.region_name
           WHERE ds.reporting_period = %s""" + score_region_sql + """
           GROUP BY ds.region_name
           ORDER BY ds.region_name""",
        [period["reporting_period"], *district_region_params],
    )
    district_indicators = q(
        """SELECT district_id, region_name, district_name, indicator_id, indicator_name,
                  result_level, actual_val, target_val, achievement_rate, status_flag
           FROM vw_indicator_achievement
           WHERE reporting_period = %s""" + district_region_sql + """
           ORDER BY region_name, district_name, indicator_id""",
        [period["reporting_period"], *district_region_params],
    )
    district_performance = q(
        """WITH activity AS (
               SELECT po.district_id,
              count(*) FILTER (WHERE pm.reported_for_duty) AS active_personnel,
              COALESCE(sum(pm.monitoring_visits) FILTER (WHERE pm.reported_for_duty), 0) AS monitoring_visits,
              count(*) FILTER (WHERE pm.reported_for_duty AND NOT pm.evaluation_done) AS outstanding_evaluations
               FROM personnel_monthly pm
               JOIN postings po USING (personnel_id)
               WHERE pm.reporting_period = %s
               GROUP BY po.district_id
           ), outcomes AS (
               SELECT po.district_id,
              count(*) FILTER (WHERE sc.outcome = 'COMPLETED') AS completed_n,
              count(*) AS total_certified
               FROM service_completion sc
               JOIN postings po USING (personnel_id)
               WHERE sc.status = 'CERTIFIED'
               GROUP BY po.district_id
           )
           SELECT ds.district_id, ds.region_name, ds.district_name,
          ds.composite_achievement, ds.n_green, ds.n_yellow, ds.n_red,
          max(ia.actual_val) FILTER (WHERE ia.indicator_id = 'IND02') AS monitoring_per_100,
          max(ia.actual_val) FILTER (WHERE ia.indicator_id = 'IND03') AS evaluation_compliance,
          round(100.0 * max(o.completed_n) / NULLIF(max(o.total_certified), 0), 2) AS service_completion_rate,
          COALESCE(a.active_personnel, 0) AS active_personnel,
          COALESCE(a.monitoring_visits, 0) AS monitoring_visits,
          COALESCE(a.outstanding_evaluations, 0) AS outstanding_evaluations,
          r.risk_probability, r.risk_class
           FROM vw_district_scorecard ds
           JOIN vw_indicator_achievement ia USING (district_id, reporting_period)
           LEFT JOIN activity a USING (district_id)
           LEFT JOIN outcomes o USING (district_id)
           LEFT JOIN vw_district_risk_latest r USING (district_id)
           WHERE ds.reporting_period = %s""" + score_region_sql + """
           GROUP BY ds.district_id, ds.region_name, ds.district_name,
            ds.composite_achievement, ds.n_green, ds.n_yellow, ds.n_red,
            a.active_personnel, a.monitoring_visits, a.outstanding_evaluations,
            o.completed_n, o.total_certified,
            r.risk_probability, r.risk_class
           ORDER BY ds.composite_achievement ASC NULLS LAST, ds.district_name""",
        [period["reporting_period"], period["reporting_period"], *district_region_params],
    )
    performance_trend = q(
        """SELECT reporting_period,
          round(avg(composite_achievement), 2) AS achievement_rate,
          count(DISTINCT district_id) AS districts_reporting
           FROM vw_district_scorecard
           WHERE TRUE""" + trend_region_sql + """
           GROUP BY reporting_period
           ORDER BY reporting_period""",
        district_region_params,
    )

    if selected_region:
        risk = q("SELECT * FROM vw_district_risk_latest WHERE region_name = %s ORDER BY risk_probability DESC LIMIT 30", (selected_region,))
    else:
        risk = q("SELECT * FROM vw_district_risk_latest ORDER BY risk_probability DESC LIMIT 30")

    exports = sorted(POWERBI_DIR.glob("*.csv"), key=lambda path: path.name.casefold())
    latest_export = max((path.stat().st_mtime for path in exports), default=None)
    report_mtime = POWERBI_REPORT.stat().st_mtime
    export_files = [
        {
            "name": path.name,
            "modified": dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone(),
            "size": path.stat().st_size,
        }
        for path in exports
    ]
    return render(
        request,
        "powerbi.html",
        u,
        periods=periods,
        period=period,
        regions=regions,
        selectedRegion=selected_region,
        latestPeriodKpi=latest_period_kpi,
        outcomeSummary=outcome_summary,
        districtRag=district_rag,
        regionalPerformance=regional_performance,
        districtIndicators=district_indicators,
        districtPerformance=district_performance,
        performanceTrend=performance_trend,
        risk=risk,
        report_size=POWERBI_REPORT.stat().st_size,
        report_modified=dt.datetime.fromtimestamp(report_mtime).astimezone(),
        export_modified=dt.datetime.fromtimestamp(latest_export).astimezone() if latest_export else None,
        exports=export_files,
        report_older_than_exports=latest_export is not None and report_mtime < latest_export,
        report_pages=(
            "Executive Overview",
            "Early Warning and Risk Matrix",
            "District Scorecards",
            "Policy Insights",
        ),
    )


@app.get("/hq/powerbi/report")
def hq_powerbi_report(request: Request):
    u = user_of(request); need(u, "HQ")
    if not POWERBI_REPORT.is_file():
        raise HTTPException(status_code=404, detail="The Power BI report file is not available.")
    return FileResponse(
        POWERBI_REPORT,
        media_type="application/octet-stream",
        filename=POWERBI_REPORT.name,
    )


@app.get("/hq/powerbi/exports/{filename}")
def hq_powerbi_export(request: Request, filename: str):
    u = user_of(request); need(u, "HQ")
    if Path(filename).name != filename or Path(filename).suffix.lower() != ".csv":
        raise HTTPException(status_code=404, detail="Power BI export not found.")
    export_path = POWERBI_DIR / filename
    if not export_path.is_file():
        raise HTTPException(status_code=404, detail="Power BI export not found.")
    return FileResponse(export_path, media_type="text/csv", filename=filename)


@app.post("/hq/period")
def hq_period(request: Request, csrf_token: str = Form(""), period: str = Form(...), open_: str = Form(...)):
    u = user_of(request); need(u, "HQ"); check_csrf(request, csrf_token)
    with tx() as cur:
        cur.execute("UPDATE reporting_periods SET is_open=%s WHERE reporting_period=%s", (open_ == "1", period))
        audit(cur, u["user_id"], "PERIOD_OPEN" if open_ == "1" else "PERIOD_CLOSE", "reporting_periods", period)
    return RedirectResponse("/hq", 303)


@app.post("/hq/refresh")
def hq_refresh(request: Request, csrf_token: str = Form("")):
    u = user_of(request); need(u, "HQ"); check_csrf(request, csrf_token)
    if REFRESH["running"]:
        raise PermissionError("A refresh is already running.")
    REFRESH.update(running=True, log="", started=dt.datetime.now(), finished=None, ok=None)
    threading.Thread(target=_refresh_job, daemon=True).start()
    with tx() as cur:
        audit(cur, u["user_id"], "REFRESH_ANALYTICS", None, "started")
    return RedirectResponse("/hq", 303)


# ------------------------------------------------------------------ HQ: data upload (agencies, personnel + postings)
@app.get("/hq/data")
def hq_data(request: Request):
    u = user_of(request); need(u, "HQ")
    counts = q("""SELECT (SELECT count(*) FROM personnel) personnel, (SELECT count(*) FROM postings) postings,
                         (SELECT count(*) FROM user_agencies) agencies, (SELECT count(*) FROM regions_districts) districts""", one=True)
    recent = q("SELECT a.at, u.username, a.action, a.detail FROM audit_log a LEFT JOIN app_users u USING (user_id) "
               "WHERE a.action IN ('UPLOAD_AGENCIES','UPLOAD_POSTINGS') ORDER BY audit_id DESC LIMIT 8")
    return render(request, "data_upload.html", u, counts=counts, recent=recent)


@app.get("/hq/data/template/{kind}.csv")
def hq_data_template(request: Request, kind: str):
    u = user_of(request); need(u, "HQ")
    if kind not in ("agencies", "postings"):
        raise PermissionError("Unknown template.")
    return Response(ingest.template_csv(kind), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={kind}_template.csv"})


@app.post("/hq/data/preview")
def hq_data_preview(request: Request, kind: str = Form(...), csrf_token: str = Form(""), file: UploadFile = File(...)):
    u = user_of(request); need(u, "HQ"); check_csrf(request, csrf_token)
    if kind not in ("agencies", "postings"):
        raise PermissionError("Unknown upload type.")
    try:
        path = ingest.save_upload(file.file, file.filename)
        res = ingest.preview(kind, path, file.filename, u["user_id"])
    except ingest.IngestError as exc:
        flash(request, err=str(exc))
        return RedirectResponse("/hq/data", 303)
    return render(request, "data_preview.html", u, res=res)


@app.post("/hq/data/commit")
def hq_data_commit(request: Request, csrf_token: str = Form(""), token: str = Form(...),
                   update_existing: str = Form(""), on_errors: str = Form("reject_all"), create_accounts: str = Form("")):
    u = user_of(request); need(u, "HQ"); check_csrf(request, csrf_token)
    try:
        r = ingest.commit(token, u["user_id"], update_existing == "1", on_errors,
                          create_accounts == "1", CREDENTIALS_FILE)
    except ingest.IngestError as exc:
        flash(request, err=str(exc))
        return RedirectResponse("/hq/data", 303)
    if r["kind"] == "agencies":
        flash(request, msg=f"Agencies loaded from {r['filename']}: {r['added']} added, {r['updated']} updated, "
                           f"{r['unchanged']} already there, {r['skipped']} skipped.")
    else:
        flash(request, msg=f"Personnel and postings loaded from {r['filename']}: {r['added']} added, {r['updated']} updated, "
                           f"{r['unchanged']} already there, {r['skipped']} skipped. New: {r['new_agencies']} agencies, "
                           f"{r['new_districts']} districts, {r['new_sites']} brand-new posting sites. "
                           + (f"{r['new_accounts']} supervisor account(s) created; usernames and passwords were added to "
                              f"{r['credentials_file']}. " if r["new_accounts"] else "")
                           + "Press 'Refresh indicators & models' on the HQ page so indicators use the new headcounts.")
    return RedirectResponse("/hq/data", 303)


@app.get("/hq/data/errors/{token}.csv")
def hq_data_errors(request: Request, token: str):
    u = user_of(request); need(u, "HQ")
    try:
        text = ingest.error_report(token, u["user_id"])
    except ingest.IngestError as exc:
        raise PermissionError(str(exc))
    return Response(text, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=upload_problems.csv"})


@app.get("/audit")
def audit_view(request: Request):
    u = user_of(request); need(u, "HQ")
    rows = q("SELECT a.at, u.username, a.action, a.table_name, a.detail FROM audit_log a LEFT JOIN app_users u USING (user_id) "
             "ORDER BY audit_id DESC LIMIT 200")
    return render(request, "audit.html", u, rows=rows)
