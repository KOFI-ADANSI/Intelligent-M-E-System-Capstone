"""Create accounts: HQ admin, one officer per region and district, and agency accounts per posting site.
Passwords default to outputs/credentials.csv unless a credentials_path is supplied."""
import csv, secrets, re
import pandas as pd
from auth import hash_password
from config import OUT
from db import connect, query


def slug(s):
    return re.sub(r"[^a-z0-9]+", ".", s.lower()).strip(".")


def agency_username(agency_name, district_name, agency_id, district_id):
    username = f"agency.{slug(agency_name)}.{slug(district_name)}"
    if len(username) <= 60:
        return username
    suffix = f".{agency_id}.{district_id}"
    return f"{username[:60 - len(suffix)].rstrip('.')}{suffix}"


def agency_accounts_for_sites(cur, sites):
    """Create AGENCY supervisor accounts for posting sites (agency_id, district_id) that have none yet.
    Runs inside the caller's transaction. Returns [(username, password, role, full_name)] for the new accounts;
    the caller appends them to the credentials file once the transaction has committed."""
    cur.execute("SELECT agency_id, district_id FROM app_users WHERE role='AGENCY'")
    have = set(cur.fetchall())
    todo = sorted({(int(a), int(d)) for a, d in sites} - have)
    if not todo:
        return []
    cur.execute("SELECT agency_id, agency_name FROM user_agencies"); an = dict(cur.fetchall())
    cur.execute("SELECT district_id, district_name FROM regions_districts");     dn = dict(cur.fetchall())
    rows = []
    for a, d in todo:
        user = agency_username(an[a], dn[d], a, d)
        pw = secrets.token_urlsafe(8)
        cur.execute("""INSERT INTO app_users(username,full_name,password_hash,role,agency_id,district_id)
                       VALUES (%s,%s,%s,'AGENCY',%s,%s) ON CONFLICT (username) DO NOTHING RETURNING user_id""",
                    (user, f"{an[a]} - {dn[d]} supervisor"[:150], hash_password(pw), a, d))
        if cur.fetchone():
            rows.append((user, pw, "AGENCY", f"{an[a]} - {dn[d]} supervisor"[:150]))
    return rows


def append_credentials(rows, path=None):
    """Append (username, password, role, name) rows to the credentials file, never overwriting earlier passwords."""
    path = path or OUT / "credentials.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["username", "password", "role", "name"])
        w.writerows(rows)
    return path


def main(agency_sites="top", n_top=20, credentials_path=None):
    con = connect()
    rows = []

    def add(cur, username, full, role, **kw):
        pw = secrets.token_urlsafe(8)
        cur.execute("""INSERT INTO app_users(username,full_name,password_hash,role,agency_id,district_id,region_name)
                       VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING RETURNING user_id""",
                    (username, full, hash_password(pw), role, kw.get("agency_id"), kw.get("district_id"), kw.get("region_name")))
        if cur.fetchone():
            rows.append((username, pw, role, full))

    with con, con.cursor() as cur:
        add(cur, "hq.admin", "HQ M&E Administrator", "HQ")
        cur.execute("SELECT DISTINCT region_name FROM regions_districts ORDER BY 1")
        for (r,) in cur.fetchall():
            add(cur, f"region.{slug(r)}", f"{r} Regional M&E Officer", "REGION", region_name=r)
        cur.execute("SELECT district_id, district_name FROM regions_districts ORDER BY 1")
        for d, nm in cur.fetchall():
            add(cur, f"district.{slug(nm)}", f"{nm} District M&E Officer", "DISTRICT", district_id=d)
        # agency accounts: one per posting site (agency + district). 'top' = the n largest sites, 'all' = every site
        cur.execute("""SELECT po.agency_id, po.district_id, ua.agency_name, rd.district_name, count(*) n
                       FROM postings po JOIN user_agencies ua USING (agency_id) JOIN regions_districts rd USING (district_id)
                       GROUP BY 1,2,3,4 ORDER BY n DESC, 3, 4""")
        sites = cur.fetchall()
        for a, d, an, dn, n in (sites if agency_sites == "all" else sites[:n_top]):
            add(cur, agency_username(an, dn, a, d), f"{an} - {dn} supervisor", "AGENCY", agency_id=a, district_id=d)
    con.close()
    path = credentials_path or OUT / "credentials.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", newline="") as f:          # append: never lose passwords issued earlier
        w = csv.writer(f)
        if new_file:
            w.writerow(["username", "password", "role", "name"])
        w.writerows(rows)
    print(f"Created {len(rows)} accounts -> {path}")


if __name__ == "__main__":
    import sys
    main("all" if "--all-sites" in sys.argv else "top")
