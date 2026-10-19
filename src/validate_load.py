"""Layer 1: validate the posting sheet and load the real (non-simulated) tables."""
import hashlib
import re
import pandas as pd
from psycopg2.errors import UndefinedTable
from config import DATA_XLSX, SQL, CYCLE
from db import connect, run_sql_file, copy_df

CARD = re.compile(r"^GHA-\d{9}-\d$")
VALID_QUAL = {"Degree", "HND", "Diploma", "Masters"}
VALID_CAT = {"Public", "Private", "Quasi"}


def validate(d, lk):
    """Return list of (check, n_checked, n_issues, detail). Records issues, never silently drops."""
    log = []

    def add(name, n, bad, detail=""):
        log.append((name, int(n), int(bad), detail))

    add("missing_values", d.size, d.isna().sum().sum())
    add("duplicate_personnel_id", len(d), d["Personnel ID"].duplicated().sum())
    add("duplicate_ghana_card", len(d), d["GHANA CARD NUMBER"].duplicated().sum())
    add("ghana_card_format", len(d), (~d["GHANA CARD NUMBER"].astype(str).str.match(CARD)).sum())
    add("contact_9_digits", len(d), (d["CONTACT"].astype(str).str.len() != 9).sum())
    add("gender_domain", len(d), (~d["Gender"].isin(["Male", "Female"])).sum())
    add("qualification_domain", len(d), (~d["Qualification"].isin(VALID_QUAL)).sum())
    add("category_domain", len(d), (~d["Category"].isin(VALID_CAT)).sum())
    add("age_matches_dob", len(d), ((2026 - d["DATE OF BIRTH"].dt.year) != d["Age"]).sum(),
        "age = 2026 - birth year")
    add("age_in_range_18_60", len(d), (~d["Age"].between(18, 60)).sum())
    lkmap = dict(zip(lk["District"], lk["Region"]))
    add("posted_district_in_lookup", len(d), (~d["District Posted To"].isin(lkmap)).sum())
    add("posted_region_matches_district", len(d),
        (d["District Posted To"].map(lkmap) != d["Region Posted To"]).sum())
    add("residential_district_in_lookup", len(d), (~d["Residential District"].isin(lkmap)).sum())
    add("residential_region_matches_district", len(d),
        (d["Residential District"].map(lkmap) != d["Residential Region"]).sum())
    suffix = d["Place of Posting"].str.split(" – ", n=1).str[1]
    add("place_of_posting_district_consistent", len(d), (suffix != d["District Posted To"]).sum())
    return log


def main(force=False):
    """DESTRUCTIVE: drops schema `me` (including all entered returns). Refuses if input data exist."""
    from db import query
    if not force:
        try:
            n = int(query("SELECT (SELECT count(*) FROM me.attendance_returns) + (SELECT count(*) FROM me.service_completion) "
                          "+ (SELECT count(*) FROM me.monitoring_visits) AS n").n[0])
        except UndefinedTable as exc:
            schema_exists = bool(query("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='me') AS present").present[0])
            if schema_exists:
                raise SystemExit("Refusing to rebuild: existing schema `me` is incomplete, so input data cannot be verified.") from exc
            n = 0
        if n:
            raise SystemExit(f"Refusing to rebuild: {n:,} entered input records exist. Use --force to wipe them.")
    d = pd.read_excel(DATA_XLSX, sheet_name="Posting Data")
    lk = pd.read_excel(DATA_XLSX, sheet_name="Region District Lookup")
    d.columns = [c.strip() for c in d.columns]

    log = validate(d, lk)
    for n, tot, bad, det in log:
        print(f"  {'OK ' if bad == 0 else 'ISSUE'} {n:45s} {bad:>6d} / {tot}")

    run_sql_file(SQL / "01_schema.sql")
    con = connect()
    with con, con.cursor() as cur:
        for n, tot, bad, det in log:
            cur.execute("INSERT INTO data_validation_log(check_name,records_checked,issues_found,detail) "
                        "VALUES (%s,%s,%s,%s)", (n, tot, bad, det))

        # regions_districts
        for r, dist in lk.itertuples(index=False):
            cur.execute("INSERT INTO regions_districts(region_name,district_name) VALUES (%s,%s)", (r, dist))

        # user agencies
        d["agency"] = d["Place of Posting"].str.split(" – ", n=1).str[0]
        ag = d[["agency", "Category"]].drop_duplicates().sort_values("agency")
        for a, c in ag.itertuples(index=False):
            cur.execute("INSERT INTO user_agencies(agency_name,category) VALUES (%s,%s)", (a, c))

        # reporting periods
        from config import CYCLE_START, N_PERIODS
        for i, p in enumerate(pd.date_range(CYCLE_START, periods=N_PERIODS, freq="MS"), start=1):
            cur.execute("INSERT INTO reporting_periods(reporting_period,period_no,agency_deadline) VALUES (%s,%s,%s)",
                        (p.date(), i, (p + pd.offsets.MonthEnd(0) + pd.Timedelta(days=10)).date()))

        # personnel (PII minimised)
        pers = pd.DataFrame({
            "personnel_id": d["Personnel ID"], "surname": d["Surname"], "other_names": d["Other Names"],
            "gender": d["Gender"], "date_of_birth": d["DATE OF BIRTH"].dt.date,
            "age_at_posting": d["Age"],
            "ghana_card_hash": d["GHANA CARD NUMBER"].map(lambda s: hashlib.sha256(s.encode()).hexdigest()),
            "residential_region": d["Residential Region"], "residential_district": d["Residential District"],
            "institution_attended": d["Institution Attended"], "course_of_study": d["Course of Study"],
            "qualification": d["Qualification"]})
        copy_df(con, pers, "personnel")

        # postings
        cur.execute("SELECT agency_name, agency_id FROM user_agencies"); agmap = dict(cur.fetchall())
        cur.execute("SELECT district_name, district_id FROM regions_districts"); dmap = dict(cur.fetchall())
        rank = pd.Series(pd.NA, index=d.index, dtype="Int64")
        for k, col in ((3, "Region of Choice 3"), (2, "Region of Choice 2"), (1, "Region of Choice 1")):
            rank[d["Region Posted To"] == d[col]] = k      # best (lowest) rank wins
        post = pd.DataFrame({
            "personnel_id": d["Personnel ID"], "agency_id": d["agency"].map(agmap),
            "district_id": d["District Posted To"].map(dmap),
            "choice_1_region": d["Region of Choice 1"], "choice_2_region": d["Region of Choice 2"],
            "choice_3_region": d["Region of Choice 3"], "choice_rank_met": rank, "service_cycle": CYCLE})
        copy_df(con, post, "postings")
    con.close()
    print(f"Loaded {len(pers):,} personnel, {len(lk)} districts, {len(ag)} agencies.")


if __name__ == "__main__":
    import sys
    main(force="--force" in sys.argv)
