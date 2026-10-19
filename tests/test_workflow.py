"""End-to-end workflow test against the running app (http://127.0.0.1:8000) and the database.
Prepares one posting site (DVLA - Tano North, period Oct 2027) by clearing its demo data, then drives
every role through the real HTTP forms. Run:  python tests/test_workflow.py"""
import csv, re, sys, time, io
import httpx
sys.path.insert(0, "src")
from db import connect

BASE = "http://127.0.0.1:8000"
PER = "2027-10-01"
creds = {r["username"]: r["password"] for r in csv.DictReader(open("outputs/credentials.csv"))}
db = connect(); db.autocommit = True
cur = db.cursor()
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def sql(q, p=()):
    cur.execute(q, p)
    return cur.fetchall() if cur.description else None


def login(user):
    c = httpx.Client(base_url=BASE, follow_redirects=True, timeout=120)
    c.post("/login", data={"username": user, "password": creds[user]})
    return c


def token(c, path):
    html = c.get(path).text
    return re.search(r'name="csrf" content="([0-9a-f]+)"', html).group(1)


def flash(r):
    m = re.search(r'<div class="(msg|err)"[^>]*>(.*?)</div>', r.text, re.S)
    return (m.group(1), m.group(2)) if m else (None, "")


# ---------- prepare site
site = sql("""SELECT po.agency_id, po.district_id FROM postings po JOIN user_agencies ua USING (agency_id)
              JOIN regions_districts rd USING (district_id) WHERE ua.agency_name='DVLA' AND rd.district_name='Tano North'""")[0]
aid, did = site
people = [r[0] for r in sql("SELECT personnel_id FROM postings WHERE agency_id=%s AND district_id=%s ORDER BY 1", (aid, did))]
sql("DELETE FROM attendance_returns WHERE reporting_period=%s AND personnel_id=ANY(%s)", (PER, people))
sql("DELETE FROM supervisor_evaluations WHERE reporting_period=%s AND personnel_id=ANY(%s)", (PER, people))
sql("DELETE FROM monitoring_visits WHERE reporting_period=%s AND agency_id=%s AND district_id=%s", (PER, aid, did))
sql("DELETE FROM service_completion WHERE personnel_id=ANY(%s)", (people,))
sql("UPDATE reporting_periods SET is_open=TRUE")
# one person is not expected any more in Oct (already certified early exit) - leave none: all expected
print(f"Test site: DVLA - Tano North, {len(people)} personnel")

agency = login("agency.dvla.tano.north")
grid = agency.get(f"/agency/returns?period={PER}")
check("agency grid loads with site personnel", all(p in grid.text for p in people[:3]))
tok = token(agency, f"/agency/returns?period={PER}")

# invalid: present > expected
form = {"csrf": tok, "period": PER, "action": "submit"}
form["pid"] = people
for p in people:
    form.update({f"rep_{p}": "1", f"exp_{p}": "22", f"pre_{p}": "20", f"rat_{p}": "4"})
bad = dict(form); bad[f"pre_{people[0]}"] = "25"
r = agency.post("/agency/returns", data=bad)
check("rejects days present > expected (nothing saved)", flash(r)[0] == "err" and
      sql("SELECT count(*) FROM attendance_returns WHERE reporting_period=%s AND personnel_id=ANY(%s)", (PER, people))[0][0] == 0, flash(r)[1])
bad = dict(form); bad[f"rat_{people[1]}"] = "7"
check("rejects rating outside 1-5", flash(agency.post("/agency/returns", data=bad))[0] == "err")
bad = dict(form); bad["pid"] = people + ["NSP2026000001" if "NSP2026000001" not in people else "NSP2026000002"]
other = bad["pid"][-1]; bad.update({f"rep_{other}": "1", f"exp_{other}": "22", f"pre_{other}": "20"})
r = agency.post("/agency/returns", data=bad)
check("blocks personnel from another site", "not posted to your site" in flash(r)[1], flash(r)[1])

r = agency.post("/agency/returns", data=form)
check("valid submit accepted", flash(r)[0] == "msg", flash(r)[1])
st = sql("SELECT status, count(*) FROM attendance_returns WHERE reporting_period=%s AND personnel_id=ANY(%s) GROUP BY 1", (PER, people))
check("all rows SUBMITTED", st == [("SUBMITTED", len(people))], str(st))
r = agency.post("/agency/returns", data=form)
check("submitted rows are locked", "cannot be changed" in flash(r)[1], flash(r)[1])

# CSV validation
badcsv = "personnel_id,reporting_period,reported_for_duty,days_expected,days_present,rating\n" + f"{people[0]},{PER},yes,22,40,\n"
r = agency.post("/agency/upload", data={"csrf_token": token(agency, "/agency/upload"), "mode": "submit"},
                files={"file": ("bad.csv", badcsv, "text/csv")})
check("bad CSV rejected as a whole", flash(r)[0] == "err", flash(r)[1])
check("CSV template downloads", agency.get(f"/agency/template.csv?period={PER}").text.count("\n") >= len(people))

# ---------- review chain
dist = login("district.tano.north"); region = login("region.ahafo"); hq = login("hq.admin")
other_dist = login("district.kintampo.north")
rv = dist.get("/review")
check("district sees the submitted batch", "DVLA" in rv.text and "Verify all" in rv.text)
act = {"kind": "returns", "agency_id": aid, "district_id": did, "period": PER}
r = other_dist.post("/review/act", data={**act, "csrf_token": token(other_dist, "/visits/new"), "action": "advance"})
check("other district cannot act on this batch", flash(r)[0] == "err", flash(r)[1])
r = dist.post("/review/act", data={**act, "csrf_token": token(dist, "/review"), "action": "reject", "reason": "x"})
check("send-back needs a reason", flash(r)[0] == "err")
r = dist.post("/review/act", data={**act, "csrf_token": token(dist, "/review"), "action": "reject", "reason": "Attendance totals look wrong"})
check("district sends batch back", sql("SELECT count(*) FROM attendance_returns WHERE status='REJECTED' AND reporting_period=%s AND personnel_id=ANY(%s)", (PER, people))[0][0] == len(people))
check("agency sees reject reason", "Attendance totals look wrong" in agency.get(f"/agency/returns?period={PER}").text)
r = agency.post("/agency/returns", data={**form, "csrf": token(agency, f"/agency/returns?period={PER}")})
check("agency corrects and resubmits", flash(r)[0] == "msg", flash(r)[1])
r = dist.post("/review/act", data={**act, "csrf_token": token(dist, "/review"), "action": "advance"})
check("district verifies", sql("SELECT count(*) FROM attendance_returns WHERE status='DISTRICT_VERIFIED' AND reporting_period=%s AND personnel_id=ANY(%s)", (PER, people))[0][0] == len(people))
check("not yet in analytics view", sql("SELECT count(*) FROM personnel_monthly WHERE reporting_period=%s AND personnel_id=ANY(%s)", (PER, people))[0][0] == 0)
r = dist.post("/review/act", data={**act, "csrf_token": token(dist, "/review"), "action": "advance"})
check("district cannot approve its own verified batch (no-op)", sql("SELECT count(*) FROM attendance_returns WHERE status='APPROVED' AND reporting_period=%s AND personnel_id=ANY(%s)", (PER, people))[0][0] == 0)
r = region.post("/review/act", data={**act, "csrf_token": token(region, "/review"), "action": "advance"})
n_app = sql("SELECT count(*) FROM personnel_monthly WHERE reporting_period=%s AND personnel_id=ANY(%s)", (PER, people))[0][0]
check("region approves -> rows appear in analytics view", n_app == len(people), str(n_app))
check("evaluations approved too", sql("SELECT count(*) FROM supervisor_evaluations WHERE status='APPROVED' AND reporting_period=%s AND personnel_id=ANY(%s)", (PER, people))[0][0] == len(people))

# ---------- visits
vt = token(dist, f"/visits/new?district_id={did}&agency_id={aid}")
met = people[:3]
r = dist.post("/visits/new", data={"csrf": vt, "district_id": did, "agency_id": aid, "visit_date": "2027-10-12", "findings": "Good supervision", "met": met})
check("district records visit (awaits region)", sql("SELECT status FROM monitoring_visits WHERE agency_id=%s AND district_id=%s AND reporting_period=%s", (aid, did, PER)) == [("SUBMITTED",)], flash(r)[1])
r = dist.post("/visits/new", data={"csrf": vt, "district_id": did, "agency_id": aid, "visit_date": "2027-10-12", "met": met})
check("duplicate visit blocked", flash(r)[0] == "err", flash(r)[1])
check("visit not counted before approval", sql("SELECT coalesce(sum(monitoring_visits),0) FROM personnel_monthly WHERE reporting_period=%s AND personnel_id=ANY(%s)", (PER, people))[0][0] == 0)
vid = sql("SELECT visit_id FROM monitoring_visits WHERE agency_id=%s AND district_id=%s AND reporting_period=%s", (aid, did, PER))[0][0]
r = dist.post("/review/act", data={"csrf_token": token(dist, "/review"), "kind": "visit", "visit_id": vid, "action": "advance"})
check("district cannot approve visits", flash(r)[0] == "err")
r = region.post("/review/act", data={"csrf_token": token(region, "/review"), "kind": "visit", "visit_id": vid, "action": "advance"})
check("region approves visit -> counted for 3 personnel", sql("SELECT coalesce(sum(monitoring_visits),0) FROM personnel_monthly WHERE reporting_period=%s AND personnel_id=ANY(%s)", (PER, people))[0][0] == 3)

# ---------- service outcomes
pid = people[-1]
r = agency.post("/agency/completion", data={"csrf": token(agency, "/agency/completion"), "pid": [pid], f"oc_{pid}": "DISCONTINUED", f"dt_{pid}": "2030-01-01"})
check("outcome date outside cycle rejected", flash(r)[0] == "err", flash(r)[1])
r = agency.post("/agency/completion", data={"csrf": token(agency, "/agency/completion"), "pid": [pid], f"oc_{pid}": "DISCONTINUED", f"dt_{pid}": "2027-09-20", f"rs_{pid}": "Left for further studies"})
check("outcome proposed", sql("SELECT status, exit_period_no FROM service_completion WHERE personnel_id=%s", (pid,)) == [("SUBMITTED", 11)], flash(r)[1])
cact = {"kind": "completion", "agency_id": aid, "district_id": did}
dist.post("/review/act", data={**cact, "csrf_token": token(dist, "/review"), "action": "advance"})
r = hq.post("/review/act", data={**cact, "csrf_token": token(hq, "/review"), "action": "advance"})
check("HQ cannot certify before region approval", sql("SELECT status FROM service_completion WHERE personnel_id=%s", (pid,)) == [("DISTRICT_VERIFIED",)])
region.post("/review/act", data={**cact, "csrf_token": token(region, "/review"), "action": "advance"})
check("not in analytics until certified", sql("SELECT count(*) FROM service_outcomes WHERE personnel_id=%s", (pid,))[0][0] == 0)
hq.post("/review/act", data={**cact, "csrf_token": token(hq, "/review"), "action": "advance"})
check("HQ certifies -> in analytics view", sql("SELECT completed_service, exit_period_no FROM service_outcomes WHERE personnel_id=%s", (pid,)) == [(False, 11)])

# ---------- permissions
r = dist.get("/agency/returns")
check("district user blocked from agency screens", flash(r)[0] == "err")
r = agency.post("/review/act", data={**act, "csrf_token": token(agency, "/"), "action": "advance"})
check("agency user blocked from review actions", flash(r)[0] == "err")
r = agency.post("/agency/returns", data={**form, "csrf": "wrong"})
check("bad CSRF token refused", "expired" in flash(r)[1])
check("anonymous redirected to login", httpx.get(BASE + "/", follow_redirects=True).url.path == "/login")
hq.post("/hq/period", data={"csrf_token": token(hq, "/hq"), "period": PER, "open_": "0"})
r = agency.post("/agency/returns", data={**form, "csrf": token(agency, "/")})
check("closed period blocks entry", "closed" in flash(r)[1], flash(r)[1])
hq.post("/hq/period", data={"csrf_token": token(hq, "/hq"), "period": PER, "open_": "1"})

# ---------- analytics refresh from HQ screen
hq.post("/hq/refresh", data={"csrf_token": token(hq, "/hq")})
for _ in range(60):
    time.sleep(2)
    page = hq.get("/hq").text
    if "Running…" not in page:
        break
check("HQ refresh completes", "— OK" in page, re.sub(r"<[^>]+>", " ", page)[-400:])
check("HQ page shows logistic table and risk list", "Logistic regression" in page and "Early-warning list" in page)
check("district scorecard shows Tano North for Oct 2027", "Tano North" in dist.get(f"/?period={PER}").text)
n_aud = sql("SELECT count(*) FROM audit_log WHERE user_id IS NOT NULL")[0][0]
check("audit trail recorded actions", n_aud >= 15, str(n_aud))
print(f"\n{'ALL PASSED' if not fails else 'FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
