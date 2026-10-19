"""End-to-end test of the HQ data upload (agencies, personnel + postings) against the running app
(http://127.0.0.1:8000) and the database. Creates only rows with Personnel ID 'TST...' / agencies 'TST ...'
and removes them at the end.   Run:  python tests/test_upload.py"""
import csv, io, re, sys, time
import httpx
import pandas as pd
sys.path.insert(0, "src")
from db import connect
from config import DATA_XLSX

BASE = "http://127.0.0.1:8000"
creds = {r["username"]: r["password"] for r in csv.DictReader(open("outputs/credentials.csv"))}
db = connect(); db.autocommit = True
cur = db.cursor()
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def sql(q, p=()):
    cur.execute(q, p or None)
    return cur.fetchall() if cur.description else None


def login(user):
    c = httpx.Client(base_url=BASE, follow_redirects=True, timeout=600)
    c.post("/login", data={"username": user, "password": creds[user]})
    return c


def tok(c, path="/hq/data"):
    return re.search(r'name="csrf" content="([0-9a-f]+)"', c.get(path).text).group(1)


def flash(r):
    m = re.search(r'<div class="(msg|err)"[^>]*>(.*?)</div>', r.text, re.S)
    return (m.group(1), m.group(2)) if m else (None, "")


def kpis(html):
    out = {}
    for n, label in re.findall(r'<div class="kpi"><b[^>]*>(\d+)</b>([^<]*)', html):
        out[label.strip()] = int(n)
    return out


def preview(c, kind, name, data):
    return c.post("/hq/data/preview", data={"csrf_token": tok(c), "kind": kind}, files={"file": (name, data)})


def token_of(r):
    m = re.search(r'name="token" value="([0-9a-f]{16})"', r.text) or re.search(r'/hq/data/errors/([0-9a-f]{16})\.csv', r.text)
    return m.group(1)


def commit(c, r, update=False, on_errors="reject_all"):
    d = {"csrf_token": tok(c), "token": token_of(r), "on_errors": on_errors}
    if update:
        d["update_existing"] = "1"
    return c.post("/hq/data/commit", data=d)


def cleanup():
    sql("DELETE FROM postings WHERE personnel_id LIKE 'TST%'")
    sql("DELETE FROM personnel WHERE personnel_id LIKE 'TST%'")
    sql("DELETE FROM user_agencies WHERE agency_name LIKE 'TST %'")
    sql("DELETE FROM regions_districts WHERE district_name LIKE 'TST %'")


cleanup()
hq = login("hq.admin")
n0 = sql("SELECT count(*) FROM personnel")[0][0]

# ---------- access control and basic errors
dist = login("district.tano.north")
r = dist.get("/hq/data")
check("district officer cannot open the upload page", "Check file" not in r.text and "Your role cannot do this" in r.text)
check("HQ upload page loads", "Check file" in hq.get("/hq/data").text)
check("templates download", "Agency Name" in hq.get("/hq/data/template/agencies.csv").text and
      "Personnel ID" in hq.get("/hq/data/template/postings.csv").text)
r = preview(hq, "postings", "x.pdf", b"abc")
check("wrong file type rejected", "Upload a .xlsx or .csv" in flash(r)[1], flash(r)[1])
r = preview(hq, "postings", "x.csv", b"Surname,Gender\nA,Male\n")
check("missing columns named", "Missing required column" in flash(r)[1] and "Personnel ID" in flash(r)[1], flash(r)[1])
r = preview(hq, "postings", "x.csv", b"")
check("empty file rejected", flash(r)[0] == "err", flash(r)[1])

# ---------- the real 50,000-row workbook must be accepted and recognised as already loaded
t = time.time()
r = preview(hq, "postings", "posting.xlsx", open(DATA_XLSX, "rb").read())
k = kpis(r.text)
print(f"   (50,000-row workbook checked in {time.time() - t:.0f}s)  {k}")
check("real workbook: no problems", k.get("rows with problems") == 0, str(k))
check("real workbook: all rows recognised as already loaded", k.get("already in the system, same") == n0, str(k))
check("real workbook: nothing offered to save", "Nothing to save" in r.text)

# ---------- build test rows from real rows
src = pd.read_excel(DATA_XLSX, sheet_name="Posting Data", nrows=40)
dist_row = src.iloc[0]
D, R = dist_row["District Posted To"], dist_row["Region Posted To"]
rows = []
for i in range(5):
    b = src.iloc[i].copy()
    b["Personnel ID"] = f"TST{i + 1:03d}"
    b["GHANA CARD NUMBER"] = f"GHA-99900000{i + 1}-1"
    b["Place of Posting"] = f"TST Agency One – {b['District Posted To']}"
    b["Category"] = "Private"
    b["DATE OF BIRTH"] = pd.Timestamp(b["DATE OF BIRTH"]).strftime("%d/%m/%Y") if i == 0 else pd.Timestamp(b["DATE OF BIRTH"]).strftime("%Y-%m-%d")
    rows.append(b)
good = pd.DataFrame(rows)
cols_no_age = [c for c in good.columns if c != "Age"]


def csv_bytes(df, cols=None):
    return df[cols or list(df.columns)].to_csv(index=False).encode()


# ---------- new people via CSV (dd/mm/yyyy date, Age column dropped, phone ignored)
r = preview(hq, "postings", "new.csv", csv_bytes(good, cols_no_age))
k = kpis(r.text)
check("5 new people recognised", k.get("new people") == 5 and k.get("rows with problems") == 0, str(k))
check("new agency and sites reported", "New agencies that would be created: <b>1</b>" in r.text)
check("preview wrote nothing", sql("SELECT count(*) FROM personnel WHERE personnel_id LIKE 'TST%'")[0][0] == 0)
aud0 = sql("SELECT count(*) FROM audit_log WHERE action='UPLOAD_POSTINGS'")[0][0]
r2 = commit(hq, r)
check("commit succeeds", flash(r2)[0] == "msg" and "5 added" in flash(r2)[1], flash(r2)[1])
check("people saved", sql("SELECT count(*) FROM personnel WHERE personnel_id LIKE 'TST%'")[0][0] == 5)
check("postings saved with agency and district", sql("""SELECT count(*) FROM postings po JOIN user_agencies ua USING (agency_id)
      WHERE po.personnel_id LIKE 'TST%' AND ua.agency_name='TST Agency One'""")[0][0] == 5)
check("agency category stored", sql("SELECT category FROM user_agencies WHERE agency_name='TST Agency One'")[0][0] == "Private")
check("dd/mm/yyyy date parsed", str(sql("SELECT date_of_birth FROM personnel WHERE personnel_id='TST001'")[0][0]) ==
      pd.Timestamp(src.iloc[0]["DATE OF BIRTH"]).strftime("%Y-%m-%d"))
check("age derived from date of birth", sql("SELECT age_at_posting FROM personnel WHERE personnel_id='TST002'")[0][0] == 2026 - pd.Timestamp(src.iloc[1]["DATE OF BIRTH"]).year)
check("ghana card stored only as hash", sql("SELECT length(ghana_card_hash) FROM personnel WHERE personnel_id='TST001'")[0][0] == 64)
check("audit entry written", sql("SELECT count(*) FROM audit_log WHERE action='UPLOAD_POSTINGS'")[0][0] == aud0 + 1)
check("total personnel increased by 5", sql("SELECT count(*) FROM personnel")[0][0] == n0 + 5)

# ---------- same file again: nothing new
r = preview(hq, "postings", "new.csv", csv_bytes(good, cols_no_age))
k = kpis(r.text)
check("re-upload: all unchanged", k.get("already in the system, same") == 5 and k.get("new people") == 0, str(k))

# ---------- changed details: only applied when asked
chg = good.copy(); chg.loc[chg.index[1], "Surname"] = "Changedname"
r = preview(hq, "postings", "chg.csv", csv_bytes(chg, cols_no_age))
check("difference detected", kpis(r.text).get("already there but different") == 1 and "surname" in r.text)
commit(hq, r)
check("not updated without the tick", sql("SELECT surname FROM personnel WHERE personnel_id='TST002'")[0][0] != "Changedname")
r = preview(hq, "postings", "chg.csv", csv_bytes(chg, cols_no_age))
commit(hq, r, update=True)
check("updated when ticked", sql("SELECT surname FROM personnel WHERE personnel_id='TST002'")[0][0] == "Changedname")

# ---------- re-posting a person who has records is blocked
sql("INSERT INTO attendance_returns(personnel_id,reporting_period,reported_for_duty,days_expected,days_present,status) "
    "VALUES ('TST003','2026-11-01',TRUE,20,20,'DRAFT')")
other = src[src["District Posted To"] != good.iloc[2]["District Posted To"]].iloc[0]
mv = good.copy()
mv.loc[mv.index[2], ["District Posted To", "Region Posted To"]] = [other["District Posted To"], other["Region Posted To"]]
mv.loc[mv.index[2], "Place of Posting"] = f"TST Agency One – {other['District Posted To']}"
r = preview(hq, "postings", "mv.csv", csv_bytes(mv, cols_no_age))
k = kpis(r.text)
check("posting change flagged as locked", k.get("different but have records (posting locked)") == 1, str(k))
r2 = commit(hq, r, update=True)
check("locked posting stops the save by default", flash(r2)[0] == "err", flash(r2)[1])
r = preview(hq, "postings", "mv.csv", csv_bytes(mv, cols_no_age))
commit(hq, r, update=True, on_errors="skip_bad_rows")
check("locked person's posting unchanged", sql("""SELECT rd.district_name FROM postings po JOIN regions_districts rd USING (district_id)
      WHERE po.personnel_id='TST003'""")[0][0] == good.iloc[2]["District Posted To"])
sql("DELETE FROM attendance_returns WHERE personnel_id='TST003'")

# ---------- bad rows
bad = pd.DataFrame([src.iloc[i].copy() for i in range(10, 18)])
bad["Personnel ID"] = [f"TST1{i:02d}" for i in range(8)]
bad["GHANA CARD NUMBER"] = [f"GHA-99800000{i}-2" for i in range(8)]
bad["Place of Posting"] = [f"TST Agency Two – {d}" for d in bad["District Posted To"]]
bad["Category"] = "Public"
bad.iloc[0, bad.columns.get_loc("GHANA CARD NUMBER")] = "GHA-12345"                       # bad format
bad.iloc[1, bad.columns.get_loc("Personnel ID")] = bad.iloc[2]["Personnel ID"]            # duplicate id in file
bad.iloc[3, bad.columns.get_loc("District Posted To")] = "Nowhere"                         # unknown district
bad.iloc[4, bad.columns.get_loc("Gender")] = "M"                                           # bad gender
bad.iloc[5, bad.columns.get_loc("Age")] = 99                                               # age vs dob
bad.iloc[6, bad.columns.get_loc("GHANA CARD NUMBER")] = src.iloc[30]["GHANA CARD NUMBER"]  # card of an existing person
r = preview(hq, "postings", "bad.csv", csv_bytes(bad))
k = kpis(r.text)
for needle in ("must look like GHA-123456789-0", "appears more than once", "unknown district", "must be Male or Female",
               "does not match date of birth", "already registered to personnel"):
    check(f"problem reported: {needle}", needle in r.text)
check("problem rows counted", k.get("rows with problems", 0) >= 6, str(k))
check("error report downloadable", "problem" in hq.get(f"/hq/data/errors/{token_of(r)}.csv").text)
r2 = commit(hq, r)
check("reject-all saves nothing", flash(r2)[0] == "err" and sql("SELECT count(*) FROM personnel WHERE personnel_id LIKE 'TST1%'")[0][0] == 0, flash(r2)[1])
r = preview(hq, "postings", "bad.csv", csv_bytes(bad))
r2 = commit(hq, r, on_errors="skip_bad_rows")
loaded = sql("SELECT count(*) FROM personnel WHERE personnel_id LIKE 'TST1%'")[0][0]
check("skip-bad-rows saves only the valid row (1 of 8)", flash(r2)[0] == "msg" and loaded == 1, f"{loaded} {flash(r2)[1]}")

# ---------- agencies
r = preview(hq, "agencies", "ag.csv", b"Agency Name,Category\nTST Alpha,Public\nTST Beta,Quasi\nTST Beta,Quasi\nTST Agency One,Public\n")
k = kpis(r.text)
check("agency preview counts", k.get("new agencies") == 2 and k.get("already there but different") == 1 and k.get("repeated rows (ignored)") == 1, str(k))
r2 = commit(hq, r)
check("agencies added, category change not applied by default",
      sql("SELECT count(*) FROM user_agencies WHERE agency_name IN ('TST Alpha','TST Beta')")[0][0] == 2
      and sql("SELECT category FROM user_agencies WHERE agency_name='TST Agency One'")[0][0] == "Private", flash(r2)[1])
r = preview(hq, "agencies", "ag.csv", b"Agency Name,Category\nTST Agency One,Public\n")
commit(hq, r, update=True)
check("agency category updated when ticked", sql("SELECT category FROM user_agencies WHERE agency_name='TST Agency One'")[0][0] == "Public")
r = preview(hq, "agencies", "ag.csv", b"Agency Name,Category\nTST Gamma,Secret\n,Public\n")
check("bad agency rows reported", "must be Public, Private or Quasi" in r.text and "required value is missing" in r.text)
r = preview(hq, "postings", "conf.csv", csv_bytes(good.assign(Category="Private").iloc[:1].assign(**{"Personnel ID": "TST900", "GHANA CARD NUMBER": "GHA-997000001-1", "Place of Posting": good.iloc[0]["Place of Posting"]}), cols_no_age))
check("agency category conflict with database reported", "registered as Public in the database" in r.text)

# ---------- xlsx with lookup sheet adds a district
buf = io.BytesIO()
nd = good.iloc[:1].copy()
nd["Personnel ID"] = "TST950"; nd["Category"] = "Public"; nd["GHANA CARD NUMBER"] = "GHA-996000001-1"
nd["District Posted To"] = "TST New District"; nd["Region Posted To"] = R
nd["Place of Posting"] = "TST Agency One – TST New District"
with pd.ExcelWriter(buf, engine="openpyxl") as w:
    nd.to_excel(w, sheet_name="Posting Data", index=False)
    pd.DataFrame([[R, "TST New District"]], columns=["Region", "District"]).to_excel(w, sheet_name="Region District Lookup", index=False)
r = preview(hq, "postings", "lk.xlsx", buf.getvalue())
check("lookup sheet district accepted in preview", kpis(r.text).get("new people") == 1 and "new districts from the lookup sheet: <b>1</b>" in r.text,
      str(kpis(r.text)))
commit(hq, r)
check("new district created and used", sql("""SELECT count(*) FROM postings po JOIN regions_districts rd USING (district_id)
      WHERE po.personnel_id='TST950' AND rd.district_name='TST New District'""")[0][0] == 1)

cleanup()
check("test data cleaned up", sql("SELECT count(*) FROM personnel")[0][0] == n0)
print("\nFAILED: " + ", ".join(fails) if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
