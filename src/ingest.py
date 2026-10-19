"""HQ data upload: user agencies, and personnel + postings.

Non-destructive and in two steps, unlike validate_load.py (which rebuilds the whole schema):
  1. preview(kind, file)  - read, validate every row, compare with the database, stage a cleaned copy.
                             Nothing is written to the M&E tables.
  2. commit(token, ...)   - re-check against the database and load inside one transaction.

Rules that protect entered M&E records
  * existing personnel are never overwritten unless HQ ticks "update existing";
  * a person who already has attendance / evaluation / visit / outcome records cannot be re-posted
    to another agency or district (that would rewrite history), only their personal details can change;
  * the Ghana Card number is stored only as a SHA-256 hash and the phone number is never stored;
    the uploaded file is deleted as soon as it has been read.
"""
import hashlib
import json
import re
import secrets
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

from config import CYCLE, OUT
from db import connect, copy_df

STAGE = OUT / "staged_uploads"
MAX_BYTES = 60 * 1024 * 1024
MAX_ROWS = 250_000
TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")
CARD_RE = r"GHA-\d{9}-\d"

GENDERS = {"male": "Male", "female": "Female"}
QUALS = {q.lower(): q for q in ("Degree", "HND", "Diploma", "Masters")}
CATS = {c.lower(): c for c in ("Public", "Private", "Quasi")}

POSTING_REQUIRED = ["Personnel ID", "Surname", "Other Names", "Gender", "DATE OF BIRTH", "GHANA CARD NUMBER",
                    "Residential Region", "Residential District", "Institution Attended", "Course of Study",
                    "Qualification", "Region of Choice 1", "Region of Choice 2", "Region of Choice 3",
                    "Place of Posting", "Category", "Region Posted To", "District Posted To"]
POSTING_OPTIONAL = ["Age", "CONTACT"]            # Age is derived when absent; CONTACT is checked, never stored
POSTING_ALIASES = {"nss number": "Personnel ID", "date of birth": "DATE OF BIRTH", "ghana card number": "GHANA CARD NUMBER",
                   "ghana card": "GHANA CARD NUMBER", "contact": "CONTACT"}
AGENCY_REQUIRED = ["Agency Name", "Category"]
AGENCY_ALIASES = {"user agency": "Agency Name", "agency": "Agency Name", "name": "Agency Name"}
LENGTHS = {"Surname": 100, "Other Names": 150, "Residential Region": 100, "Residential District": 100,
           "Institution Attended": 150, "Course of Study": 100, "Region of Choice 1": 100,
           "Region of Choice 2": 100, "Region of Choice 3": 100}
PERSON_FIELDS = ["surname", "other_names", "gender", "date_of_birth", "age_at_posting", "ghana_card_hash",
                 "residential_region", "residential_district", "institution_attended", "course_of_study", "qualification"]
POSTING_FIELDS = ["agency_name", "district_name", "choice_1_region", "choice_2_region", "choice_3_region", "choice_rank_met"]
CLEAN_COLS = (["row_no", "personnel_id"] + PERSON_FIELDS +
              ["agency_name", "category", "district_name", "choice_1_region", "choice_2_region", "choice_3_region",
               "choice_rank_met", "row_error"])


class IngestError(Exception):
    """A problem with the file as a whole (shown to the user as-is)."""


# ------------------------------------------------------------------ reading
def _norm(s):
    return re.sub(r"[\s_]+", " ", str(s).strip().lower())


def _canonical(df, required, optional, aliases):
    lookup = {_norm(c): c for c in required + optional}
    lookup.update({k: v for k, v in aliases.items()})
    ren = {}
    for c in df.columns:
        k = _norm(c)
        if k in lookup and lookup[k] not in ren.values():
            ren[c] = lookup[k]
    df = df.rename(columns=ren)
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise IngestError("Missing required column(s): " + ", ".join(missing) +
                          ". Download the template to see the expected headings.")
    return df[[c for c in required + optional if c in df.columns]]


def save_upload(fileobj, filename):
    """Stream an upload to a private temp file, enforcing type and size."""
    ext = Path(filename or "").suffix.lower()
    if ext not in (".csv", ".xlsx"):
        raise IngestError("Upload a .xlsx or .csv file (save an old .xls file as .xlsx first).")
    inc = STAGE / "_incoming"
    inc.mkdir(parents=True, exist_ok=True)
    path = inc / f"{secrets.token_hex(8)}{ext}"
    size = 0
    with open(path, "wb") as f:
        while chunk := fileobj.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                f.close(); path.unlink(missing_ok=True)
                raise IngestError(f"File is larger than {MAX_BYTES // (1024 * 1024)} MB.")
            f.write(chunk)
    if size == 0:
        path.unlink(missing_ok=True)
        raise IngestError("The file is empty.")
    return path


def read_file(path, kind):
    """-> (DataFrame of strings, lookup DataFrame or None)."""
    path = Path(path)
    lookup = None
    try:
        if path.suffix.lower() == ".csv":
            df = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False, na_values=[""])
        else:
            xl = pd.ExcelFile(path, engine="openpyxl")
            want = {"postings": ["posting data"], "agencies": ["user agencies", "agencies"]}[kind]
            sheet = next((s for s in xl.sheet_names if s.strip().lower() in want), xl.sheet_names[0])
            df = xl.parse(sheet, dtype=str, keep_default_na=False, na_values=[""])
            lk = next((s for s in xl.sheet_names if s.strip().lower() == "region district lookup"), None)
            if kind == "postings" and lk:
                lookup = xl.parse(lk, dtype=str, keep_default_na=False, na_values=[""])
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError(f"Could not read the file ({type(exc).__name__}). Check it is a valid .xlsx or .csv.") from exc
    df = df.dropna(how="all")
    if df.empty:
        raise IngestError("The file has no data rows.")
    if len(df) > MAX_ROWS:
        raise IngestError(f"The file has {len(df):,} rows; the limit is {MAX_ROWS:,}. Split it.")
    if kind == "postings":
        df = _canonical(df, POSTING_REQUIRED, POSTING_OPTIONAL, POSTING_ALIASES)
    else:
        df = _canonical(df, AGENCY_REQUIRED, [], AGENCY_ALIASES)
    if lookup is not None:
        lookup = _canonical(lookup, ["Region", "District"], [], {})
        lookup = lookup.apply(lambda s: s.astype(object).str.strip()).dropna()
    return df, lookup


# ------------------------------------------------------------------ validation
class _Issues:
    def __init__(self):
        self.items, self.first, self.checks = [], {}, []

    def check(self, name, n, bad, blocking=True):
        self.checks.append(dict(name=name, checked=int(n), issues=int(bad), blocking=blocking))

    def flag(self, mask, field, msg):
        for i in mask[mask].index:
            self.items.append((int(i) + 2, field, msg))
            self.first.setdefault(int(i), f"{field}: {msg}")

    def check_flag(self, name, mask, field, msg, n=None):
        self.check(name, len(mask) if n is None else n, mask.sum())
        self.flag(mask, field, msg)


def _prep(df):
    d = df.reset_index(drop=True).astype(object)
    for c in d.columns:
        d[c] = d[c].where(d[c].isna(), d[c].astype(str).str.strip()).replace("", np.nan)
    return d


def build_district_map(cur, lookup):
    """District -> region from the database plus the optional lookup sheet. -> (map, new [(region, district)])."""
    cur.execute("SELECT district_name, region_name FROM regions_districts")
    dmap = dict(cur.fetchall())
    new = []
    if lookup is not None:
        for region, district in lookup.itertuples(index=False):
            if district in dmap and dmap[district] != region:
                raise IngestError(f"Lookup sheet: district '{district}' is in region '{dmap[district]}' in the "
                                  f"database but '{region}' in the file.")
            if district not in dmap:
                dmap[district] = region
                new.append((region, district))
    return dmap, new


def validate_postings(df, dmap):
    d = _prep(df)
    n = len(d)
    iss = _Issues()
    regions = set(dmap.values())
    miss = d[POSTING_REQUIRED].isna()
    iss.check("required_values_present", miss.size, miss.to_numpy().sum())
    for c in POSTING_REQUIRED:
        iss.flag(miss[c], c, "required value is missing")

    pid = d["Personnel ID"]
    iss.check_flag("personnel_id_length_max_20", pid.str.len() > 20, "Personnel ID", "longer than 20 characters")
    iss.check_flag("duplicate_personnel_id_in_file", pid.notna() & pid.duplicated(keep=False), "Personnel ID",
                   "appears more than once in this file")
    card = d["GHANA CARD NUMBER"].str.upper()
    iss.check_flag("ghana_card_format", card.notna() & ~card.str.fullmatch(CARD_RE, na=False), "GHANA CARD NUMBER",
                   "must look like GHA-123456789-0")
    iss.check_flag("duplicate_ghana_card_in_file", card.notna() & card.duplicated(keep=False), "GHANA CARD NUMBER",
                   "appears more than once in this file")
    if "CONTACT" in d:
        c = d["CONTACT"]
        iss.check("contact_9_digits (warning; phone is never stored)", n, (c.notna() & ~c.str.fullmatch(r"\d{9}", na=False)).sum(), blocking=False)

    g = d["Gender"].str.lower().map(GENDERS)
    iss.check_flag("gender_domain", d["Gender"].notna() & g.isna(), "Gender", "must be Male or Female")
    qual = d["Qualification"].str.lower().map(QUALS)
    iss.check_flag("qualification_domain", d["Qualification"].notna() & qual.isna(), "Qualification",
                   "must be Degree, HND, Diploma or Masters")
    cat = d["Category"].str.lower().map(CATS)
    iss.check_flag("category_domain", d["Category"].notna() & cat.isna(), "Category", "must be Public, Private or Quasi")

    s = d["DATE OF BIRTH"]
    dob = pd.to_datetime(s, format="ISO8601", errors="coerce")
    dob = dob.fillna(pd.to_datetime(s.where(dob.isna()), format="%d/%m/%Y", errors="coerce"))
    bad_dob = s.notna() & (dob.isna() | (dob.dt.year < 1940) | (dob > pd.Timestamp.today()))
    iss.check_flag("date_of_birth_valid", bad_dob, "DATE OF BIRTH", "not a valid date (use yyyy-mm-dd or dd/mm/yyyy)")
    cy = int(CYCLE[:4])
    exp = (cy - dob.dt.year).astype("float64")
    if "Age" in d:
        a = pd.to_numeric(d["Age"], errors="coerce").astype("float64")
        given = d["Age"].notna()
        iss.check_flag("age_numeric", given & (a.isna() | (a % 1 != 0)), "Age", "must be a whole number")
        iss.check_flag("age_matches_dob", given & a.notna() & dob.notna() & (a != exp), "Age",
                       f"does not match date of birth ({cy} minus birth year)")
        age = a.fillna(exp)
    else:
        age = exp
    iss.check_flag("age_in_range_18_60", age.notna() & ~age.between(18, 60), "DATE OF BIRTH", "age is outside 18-60")

    for col in ("Residential District", "District Posted To"):
        iss.check_flag(f"{col.lower().replace(' ', '_')}_known", d[col].notna() & ~d[col].isin(list(dmap)), col,
                       "unknown district (add it to the 'Region District Lookup' sheet)")
    dp, rp = d["District Posted To"], d["Region Posted To"]
    er = dp.map(dmap)
    iss.check_flag("posted_region_matches_district", dp.notna() & rp.notna() & er.notna() & (er != rp),
                   "Region Posted To", "does not match the region of the posted district")
    rr = d["Residential District"].map(dmap)
    iss.check_flag("residential_region_matches_district", d["Residential District"].notna() & d["Residential Region"].notna()
                   & rr.notna() & (rr != d["Residential Region"]), "Residential Region",
                   "does not match the region of the residential district")
    for k in (1, 2, 3):
        c = d[f"Region of Choice {k}"]
        iss.check_flag(f"region_of_choice_{k}_known", c.notna() & ~c.isin(regions), f"Region of Choice {k}", "unknown region")

    agency = []
    for p, dist in zip(d["Place of Posting"], dp):
        a = None
        if isinstance(p, str) and isinstance(dist, str):
            for sep in (" – ", " — ", " - "):
                tail = sep + dist
                if p.endswith(tail) and len(p) > len(tail):
                    a = p[:-len(tail)].strip()
                    break
        agency.append(a)
    agency = pd.Series(agency, index=d.index, dtype=object)
    iss.check_flag("place_of_posting_matches_district", d["Place of Posting"].notna() & dp.notna() & agency.isna(),
                   "Place of Posting", "must read '<User Agency> – <District Posted To>'")
    iss.check_flag("agency_name_length_max_150", agency.str.len() > 150, "Place of Posting", "agency name over 150 characters")
    tmp = pd.DataFrame({"a": agency, "c": cat}).dropna()
    multi = tmp.groupby("a")["c"].nunique()
    iss.check_flag("agency_single_category_in_file", agency.isin(list(multi[multi > 1].index)), "Category",
                   "this agency appears with more than one category in the file")
    for col, lim in LENGTHS.items():
        iss.check_flag(f"{col.lower().replace(' ', '_')}_length_max_{lim}", d[col].str.len() > lim, col,
                       f"longer than {lim} characters")

    rank = pd.Series(np.nan, index=d.index, dtype="float64")
    for k in (3, 2, 1):
        rank[(rp == d[f"Region of Choice {k}"]) & rp.notna()] = k

    out = pd.DataFrame({
        "row_no": d.index + 2, "personnel_id": pid, "surname": d["Surname"], "other_names": d["Other Names"],
        "gender": g, "date_of_birth": dob.dt.strftime("%Y-%m-%d"), "age_at_posting": age.round().astype("Int64"),
        "ghana_card_hash": card.map(lambda v: hashlib.sha256(v.encode()).hexdigest() if isinstance(v, str) else None),
        "residential_region": d["Residential Region"], "residential_district": d["Residential District"],
        "institution_attended": d["Institution Attended"], "course_of_study": d["Course of Study"],
        "qualification": qual, "agency_name": agency, "category": cat, "district_name": dp,
        "choice_1_region": d["Region of Choice 1"], "choice_2_region": d["Region of Choice 2"],
        "choice_3_region": d["Region of Choice 3"], "choice_rank_met": rank.astype("Int64")})
    out["row_error"] = pd.Series(iss.first).reindex(out.index)
    return out[CLEAN_COLS], iss


def validate_agencies(df):
    d = _prep(df)
    iss = _Issues()
    miss = d[AGENCY_REQUIRED].isna()
    iss.check("required_values_present", miss.size, miss.to_numpy().sum())
    for c in AGENCY_REQUIRED:
        iss.flag(miss[c], c, "required value is missing")
    name = d["Agency Name"]
    iss.check_flag("agency_name_length_max_150", name.str.len() > 150, "Agency Name", "longer than 150 characters")
    cat = d["Category"].str.lower().map(CATS)
    iss.check_flag("category_domain", d["Category"].notna() & cat.isna(), "Category", "must be Public, Private or Quasi")
    key = name.str.lower()
    tmp = pd.DataFrame({"k": key, "c": cat}).dropna()
    multi = tmp.groupby("k")["c"].nunique()
    iss.check_flag("agency_single_category_in_file", key.isin(list(multi[multi > 1].index)), "Category",
                   "the same agency is listed with different categories")
    dup = name.notna() & key.duplicated(keep="first") & ~key.isin(list(multi[multi > 1].index))
    iss.check("repeated_agency_rows (warning; extra copies ignored)", len(d), dup.sum(), blocking=False)
    out = pd.DataFrame({"row_no": d.index + 2, "agency_name": name, "category": cat})
    out["row_error"] = pd.Series(iss.first).reindex(out.index)
    out["duplicate"] = dup
    return out, iss


# ------------------------------------------------------------------ comparison with the database
def _existing(cur):
    cur.execute("""
        SELECT p.personnel_id, p.surname, p.other_names, p.gender, p.date_of_birth::text AS date_of_birth,
               p.age_at_posting::text AS age_at_posting, p.ghana_card_hash, p.residential_region, p.residential_district,
               p.institution_attended, p.course_of_study, p.qualification, ua.agency_name, rd.district_name,
               po.choice_1_region, po.choice_2_region, po.choice_3_region, po.choice_rank_met::text AS choice_rank_met,
               (EXISTS (SELECT 1 FROM attendance_returns x WHERE x.personnel_id = p.personnel_id)
                OR EXISTS (SELECT 1 FROM supervisor_evaluations x WHERE x.personnel_id = p.personnel_id)
                OR EXISTS (SELECT 1 FROM monitoring_visit_personnel x WHERE x.personnel_id = p.personnel_id)
                OR EXISTS (SELECT 1 FROM service_completion x WHERE x.personnel_id = p.personnel_id)) AS has_records
        FROM personnel p LEFT JOIN postings po USING (personnel_id)
        LEFT JOIN user_agencies ua USING (agency_id) LEFT JOIN regions_districts rd USING (district_id)""")
    return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])


def plan_postings(cur, clean):
    """Add status / plan_error / changed_fields. Statuses: new, unchanged, update, blocked, error."""
    c = clean.copy()
    ex = _existing(cur).astype({"has_records": bool})
    exi = ex.set_index("personnel_id")
    cur.execute("SELECT agency_name, category FROM user_agencies")
    db_cat = dict(cur.fetchall())
    hash_owner = dict(zip(ex.ghana_card_hash, ex.personnel_id))

    c["plan_error"] = None
    owner = c.ghana_card_hash.map(hash_owner)
    clash = owner.notna() & (owner != c.personnel_id)
    c.loc[clash, "plan_error"] = "GHANA CARD NUMBER: already registered to personnel " + owner[clash].astype(str)
    dbc = c.agency_name.map(db_cat)
    ag_clash = dbc.notna() & (dbc != c.category) & c.plan_error.isna()
    c.loc[ag_clash, "plan_error"] = ("Category: agency is registered as " + dbc[ag_clash].astype(str) +
                                    " in the database (change it under User agencies first)")

    in_db = c.personnel_id.isin(exi.index)
    c["status"] = np.where(c.row_error.notna() | c.plan_error.notna(), "error", np.where(in_db, "unchanged", "new"))
    c["changed_fields"] = ""
    chk = (c.status == "unchanged")
    if chk.any():
        sub = c[chk]
        db = exi.reindex(sub.personnel_id)
        db.index = sub.index
        diffs = pd.DataFrame({f: sub[f].astype(object).where(sub[f].notna(), "").astype(str) !=
                              db[f].astype(object).where(db[f].notna(), "").astype(str)
                              for f in PERSON_FIELDS + POSTING_FIELDS})
        names = diffs.apply(lambda r: ", ".join(k for k, v in r.items() if v), axis=1)
        changed = diffs.any(axis=1)
        posting_changed = diffs[["agency_name", "district_name"]].any(axis=1)
        c.loc[sub.index, "changed_fields"] = names
        c.loc[changed[changed].index, "status"] = "update"
        blocked = posting_changed & db.has_records.astype(bool)
        c.loc[blocked[blocked].index, "status"] = "blocked"
        c.loc[blocked[blocked].index, "plan_error"] = ("Posting: person already has recorded returns, evaluations, "
                                                       "visits or an outcome, so the agency/district cannot change")
    return c


def plan_agencies(cur, clean):
    c = clean.copy()
    cur.execute("SELECT lower(agency_name), category FROM user_agencies")
    db = dict(cur.fetchall())
    k = c.agency_name.str.lower()
    dbc = k.map(db)
    c["status"] = np.where(c.row_error.notna(), "error",
                  np.where(c.duplicate, "repeat", np.where(dbc.isna(), "new", np.where(dbc == c.category, "unchanged", "update"))))
    c["changed_fields"] = np.where(c.status == "update", "category (" + dbc.astype(str) + " → " + c.category.astype(str) + ")", "")
    c["plan_error"] = None
    return c


# ------------------------------------------------------------------ staging
def _dir(token):
    if not TOKEN_RE.match(token or ""):
        raise IngestError("Unknown or expired upload. Upload the file again.")
    return STAGE / token


def _cleanup():
    if STAGE.exists():
        for p in STAGE.iterdir():
            if p.is_dir() and time.time() - p.stat().st_mtime > 24 * 3600:
                shutil.rmtree(p, ignore_errors=True)
        for p in (STAGE / "_incoming").glob("*"):
            if time.time() - p.stat().st_mtime > 3600:
                p.unlink(missing_ok=True)


def load_staged(token, user_id):
    d = _dir(token)
    if not (d / "meta.json").exists():
        raise IngestError("Unknown or expired upload. Upload the file again.")
    meta = json.loads((d / "meta.json").read_text())
    if meta["user_id"] != user_id:
        raise IngestError("This upload belongs to another user.")
    clean = pd.read_csv(d / "clean.csv", dtype=str, keep_default_na=False, na_values=[""])
    clean["row_no"] = clean["row_no"].astype(int)
    if "duplicate" in clean:
        clean["duplicate"] = clean["duplicate"].eq("True")
    lookup = pd.read_csv(d / "lookup.csv", dtype=str, keep_default_na=False) if (d / "lookup.csv").exists() else None
    return meta, clean, lookup


def error_report(token, user_id):
    d = _dir(token)
    meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else None
    if not meta or meta["user_id"] != user_id or not (d / "errors.csv").exists():
        raise IngestError("Unknown or expired upload.")
    return (d / "errors.csv").read_text(encoding="utf-8")


# ------------------------------------------------------------------ preview
def preview(kind, src_path, filename, user_id):
    src_path = Path(src_path)
    try:
        _cleanup()
        df, lookup = read_file(src_path, kind)
    except Exception:
        src_path.unlink(missing_ok=True)
        raise
    token = secrets.token_hex(8)
    d = STAGE / token
    d.mkdir(parents=True)
    con = connect()
    try:
        with con.cursor() as cur:
            if kind == "postings":
                dmap, new_districts = build_district_map(cur, lookup)
                clean, iss = validate_postings(df, dmap)
                plan = plan_postings(cur, clean)
            else:
                new_districts = []
                clean, iss = validate_agencies(df)
                plan = plan_agencies(cur, clean)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    finally:
        con.rollback(); con.close()
        src_path.unlink(missing_ok=True)       # raw file (with full Ghana Card numbers) is not kept

    errs = list(iss.items)
    pe = plan[plan.plan_error.notna()]
    errs += [(int(r.row_no), *(r.plan_error.split(": ", 1) if ": " in r.plan_error else ("", r.plan_error)))
             for r in pe.itertuples() if pd.isna(r.row_error)]
    errs.sort(key=lambda t: (t[0], t[1]))
    pd.DataFrame(errs, columns=["row", "field", "problem"]).to_csv(d / "errors.csv", index=False)
    plan.drop(columns=["status", "plan_error", "changed_fields"]).to_csv(d / "clean.csv", index=False, na_rep="")
    if new_districts:
        pd.DataFrame(new_districts, columns=["region", "district"]).to_csv(d / "lookup.csv", index=False)

    counts = plan.status.value_counts().to_dict()
    counts = {k: int(counts.get(k, 0)) for k in ("new", "unchanged", "update", "blocked", "repeat", "error")}
    counts["differs"] = counts.pop("update")      # 'update' would shadow dict.update in the templates
    sample = plan[plan.status == "update"].head(8)
    res = dict(token=token, kind=kind, filename=filename, n_rows=int(len(plan)), counts=counts,
               checks=iss.checks, n_error_rows=counts["error"], n_problems=len(errs),
               errors=[dict(row=r, field=f, problem=p) for r, f, p in errs[:60]],
               new_districts=len(new_districts),
               sample_updates=[dict(row=int(r.row_no), key=(r.personnel_id if kind == "postings" else r.agency_name),
                                    fields=r.changed_fields) for r in sample.itertuples()])
    if kind == "postings":
        ok = plan[plan.status.isin(["new", "update"])]
        cur_names = set(db_agency_names())
        res["new_agencies"] = int(ok.loc[~ok.agency_name.isin(cur_names), "agency_name"].nunique())
        res["sites_without_account"] = _sites_without_account(plan[plan.status == "new"])
    (d / "meta.json").write_text(json.dumps(dict(kind=kind, filename=filename, user_id=user_id, created=time.time())))
    return res


def _sites_without_account(new_rows):
    """Distinct (agency, district) sites of newly added people that have no supervisor login yet."""
    if new_rows.empty:
        return 0
    con = connect()
    try:
        with con.cursor() as cur:
            cur.execute("""SELECT ua.agency_name, rd.district_name FROM app_users u
                           JOIN user_agencies ua USING (agency_id) JOIN regions_districts rd ON rd.district_id = u.district_id
                           WHERE u.role = 'AGENCY'""")
            have = set(cur.fetchall())
    finally:
        con.close()
    sites = set(zip(new_rows.agency_name, new_rows.district_name))
    return len(sites - have)


def db_agency_names():
    con = connect()
    try:
        with con.cursor() as cur:
            cur.execute("SELECT agency_name FROM user_agencies")
            return [r[0] for r in cur.fetchall()]
    finally:
        con.close()


# ------------------------------------------------------------------ commit
def commit(token, user_id, update_existing=False, on_errors="reject_all", create_accounts=False, credentials_path=None):
    meta, clean, lookup = load_staged(token, user_id)
    kind, filename = meta["kind"], meta["filename"]
    if on_errors not in ("reject_all", "skip_bad_rows"):
        raise IngestError("Choose what to do with rows that have problems.")
    con = connect()
    try:
        with con, con.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(7001)")        # one upload at a time
            if kind == "postings":
                res = _commit_postings(cur, clean, lookup, update_existing, on_errors, filename, user_id, create_accounts)
            else:
                res = _commit_agencies(cur, clean, update_existing, on_errors, filename, user_id)
    finally:
        con.close()
    shutil.rmtree(_dir(token), ignore_errors=True)
    accounts = res.pop("_accounts", [])
    if accounts:                                  # only after the database commit succeeded
        import create_users
        res["credentials_file"] = str(create_users.append_credentials(accounts, credentials_path))
    return res


def _audit(cur, user_id, action, table, detail):
    cur.execute("INSERT INTO audit_log(user_id,action,table_name,detail) VALUES (%s,%s,%s,%s)",
                (user_id, action, table, detail))


def _apply_mode(plan, update_existing, on_errors):
    if not update_existing:
        plan.loc[plan.status.isin(["update", "blocked"]), "status"] = "skipped"
    n_err = int((plan.status == "error").sum() + (plan.status == "blocked").sum())
    if n_err and on_errors == "reject_all":
        raise IngestError(f"{n_err:,} row(s) have problems, so nothing was saved. Fix the file and upload again, "
                          "or choose 'load the valid rows and skip the rest'.")
    return plan


def _commit_agencies(cur, clean, update_existing, on_errors, filename, user_id):
    plan = _apply_mode(plan_agencies(cur, clean), update_existing, on_errors)
    new = plan[plan.status == "new"].drop_duplicates("agency_name")
    for r in new.itertuples():
        cur.execute("INSERT INTO user_agencies(agency_name,category) VALUES (%s,%s) ON CONFLICT (agency_name) DO NOTHING",
                    (r.agency_name, r.category))
    upd = plan[plan.status == "update"]
    for r in upd.itertuples():
        cur.execute("UPDATE user_agencies SET category=%s WHERE lower(agency_name)=lower(%s)", (r.category, r.agency_name))
    res = dict(kind="agencies", filename=filename, added=len(new), updated=len(upd),
               unchanged=int((plan.status.isin(["unchanged", "repeat"])).sum()),
               skipped=int((plan.status.isin(["skipped", "error"])).sum()))
    _audit(cur, user_id, "UPLOAD_AGENCIES", "user_agencies",
           f"{filename}: {res['added']} added, {res['updated']} updated, {res['unchanged']} unchanged, {res['skipped']} skipped")
    return res


def _commit_postings(cur, clean, lookup, update_existing, on_errors, filename, user_id, create_accounts=False):
    plan = _apply_mode(plan_postings(cur, clean), update_existing, on_errors)
    new_d = 0
    if lookup is not None:
        for region, district in lookup.itertuples(index=False):
            cur.execute("INSERT INTO regions_districts(region_name,district_name) VALUES (%s,%s) "
                        "ON CONFLICT (district_name) DO NOTHING", (region, district))
            new_d += cur.rowcount
    load = plan[plan.status.isin(["new", "update"])].copy()
    cur.execute("SELECT agency_name, category FROM user_agencies")
    have = dict(cur.fetchall())
    ags = load[["agency_name", "category"]].drop_duplicates("agency_name")
    new_ag = 0
    for r in ags.itertuples():
        if r.agency_name not in have:
            cur.execute("INSERT INTO user_agencies(agency_name,category) VALUES (%s,%s)", (r.agency_name, r.category))
            new_ag += 1
    cur.execute("SELECT agency_name, agency_id FROM user_agencies"); agmap = dict(cur.fetchall())
    cur.execute("SELECT district_name, district_id FROM regions_districts"); dmap = dict(cur.fetchall())
    load["agency_id"] = load.agency_name.map(agmap)
    load["district_id"] = load.district_name.map(dmap)
    if load[["agency_id", "district_id"]].isna().any().any():
        raise IngestError("A posted district or agency could not be matched in the database. Upload the file again.")
    cur.execute("SELECT DISTINCT agency_id, district_id FROM postings")
    sites_before = set(cur.fetchall())
    sites_new = {(int(a), int(d)) for a, d in zip(load.agency_id, load.district_id)} - sites_before

    con = cur.connection
    new = load[load.status == "new"]
    pers_cols = ["personnel_id"] + PERSON_FIELDS
    copy_df(con, new, "personnel", pers_cols)
    copy_df(con, new.assign(service_cycle=CYCLE), "postings",
            ["personnel_id", "agency_id", "district_id", "choice_1_region", "choice_2_region", "choice_3_region",
             "choice_rank_met", "service_cycle"])
    upd = load[load.status == "update"]
    if len(upd):
        cur.execute("""CREATE TEMP TABLE stg_upd (personnel_id varchar(20), surname varchar(100), other_names varchar(150),
                       gender varchar(10), date_of_birth date, age_at_posting smallint, ghana_card_hash char(64),
                       residential_region varchar(100), residential_district varchar(100), institution_attended varchar(150),
                       course_of_study varchar(100), qualification varchar(20), agency_id int, district_id int,
                       choice_1_region varchar(100), choice_2_region varchar(100), choice_3_region varchar(100),
                       choice_rank_met smallint) ON COMMIT DROP""")
        copy_df(con, upd, "stg_upd", pers_cols + ["agency_id", "district_id", "choice_1_region", "choice_2_region",
                                                  "choice_3_region", "choice_rank_met"])
        cur.execute("""UPDATE personnel p SET surname=s.surname, other_names=s.other_names, gender=s.gender,
                       date_of_birth=s.date_of_birth, age_at_posting=s.age_at_posting, ghana_card_hash=s.ghana_card_hash,
                       residential_region=s.residential_region, residential_district=s.residential_district,
                       institution_attended=s.institution_attended, course_of_study=s.course_of_study,
                       qualification=s.qualification FROM stg_upd s WHERE p.personnel_id=s.personnel_id""")
        cur.execute("""UPDATE postings po SET agency_id=s.agency_id, district_id=s.district_id,
                       choice_1_region=s.choice_1_region, choice_2_region=s.choice_2_region,
                       choice_3_region=s.choice_3_region, choice_rank_met=s.choice_rank_met
                       FROM stg_upd s WHERE po.personnel_id=s.personnel_id""")
    accounts = []
    if create_accounts and len(new):
        import create_users
        accounts = create_users.agency_accounts_for_sites(cur, zip(new.agency_id, new.district_id))
    st = plan.status
    res = dict(kind="postings", filename=filename, added=len(new), updated=len(upd), unchanged=int((st == "unchanged").sum()),
               skipped=int(st.isin(["skipped", "error", "blocked"]).sum()), new_agencies=new_ag, new_districts=new_d,
               new_sites=len(sites_new), new_accounts=len(accounts), _accounts=accounts)
    _audit(cur, user_id, "UPLOAD_POSTINGS", "personnel",
           f"{filename}: {res['added']} added, {res['updated']} updated, {res['unchanged']} unchanged, "
           f"{res['skipped']} skipped; {new_ag} new agencies, {new_d} new districts, {len(sites_new)} new posting sites, "
           f"{len(accounts)} supervisor accounts created")
    return res


# ------------------------------------------------------------------ templates
def template_csv(kind):
    if kind == "agencies":
        return "Agency Name,Category\nExample Agency Ltd,Private\n"
    con = connect()
    try:
        with con.cursor() as cur:
            cur.execute("SELECT region_name, district_name FROM regions_districts ORDER BY district_id LIMIT 1")
            region, district = cur.fetchone() or ("Region", "District")
    finally:
        con.close()
    cols = POSTING_REQUIRED[:5] + ["Age", "CONTACT"] + POSTING_REQUIRED[5:]
    row = {"Personnel ID": "NSP2026999999", "Surname": "Mensah", "Other Names": "Ama", "Gender": "Female",
           "DATE OF BIRTH": "2001-03-15", "Age": "25", "CONTACT": "244123456", "GHANA CARD NUMBER": "GHA-123456789-0",
           "Residential Region": region, "Residential District": district, "Institution Attended": "University of Cape Coast",
           "Course of Study": "Economics", "Qualification": "Degree", "Region of Choice 1": region,
           "Region of Choice 2": region, "Region of Choice 3": region, "Place of Posting": f"Example Agency Ltd – {district}",
           "Category": "Private", "Region Posted To": region, "District Posted To": district}
    return ",".join(cols) + "\n" + ",".join(f'"{row[c]}"' for c in cols) + "\n"
