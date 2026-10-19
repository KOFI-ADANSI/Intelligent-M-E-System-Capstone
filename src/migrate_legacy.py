"""Copy legacy M&E records from ns_me into an initialized v2 database.

The legacy source is opened read-only. The destination must be a separate,
initialized database with no v2 input or audit records.
"""
import argparse
from datetime import timedelta
import os

from psycopg2.extras import Json, execute_values
from psycopg2.errors import UndefinedTable

from config import DB, OUT
from db import connect

LEGACY_DB = os.environ.get("NSME_LEGACY_DB_NAME", "ns_me")
CREDENTIALS = OUT / "credentials_v2.csv"
BATCH_SIZE = 5000
STATUS_MAP = {
    "DRAFT": "DRAFT",
    "SUBMITTED": "SUBMITTED",
    "VERIFIED": "DISTRICT_VERIFIED",
    "DISTRICT_VERIFIED": "DISTRICT_VERIFIED",
    "APPROVED": "APPROVED",
    "REJECTED": "REJECTED",
}


class MigrationError(RuntimeError):
    pass


def map_status(value):
    try:
        return STATUS_MAP[value]
    except KeyError as exc:
        raise MigrationError(f"Unsupported legacy review status: {value!r}") from exc


def map_visit_status(value):
    visit_statuses = {
        "SUBMITTED": "SUBMITTED",
        "VERIFIED": "SUBMITTED",
        "DISTRICT_VERIFIED": "SUBMITTED",
        "APPROVED": "APPROVED",
        "REJECTED": "REJECTED",
    }
    try:
        return visit_statuses[value]
    except KeyError as exc:
        raise MigrationError(f"Unsupported legacy visit status: {value!r}") from exc


def month_end(period_start):
    next_month = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return next_month - timedelta(days=1)


def map_service_outcome(completed, exit_period_no, period_end_by_no):
    if completed:
        if exit_period_no is not None:
            raise MigrationError("Legacy completed outcomes unexpectedly have an exit period.")
        return (
            "COMPLETED",
            None,
            period_end_by_no[max(period_end_by_no)],
            "Legacy outcome date inferred as cycle end; no reason was recorded.",
        )
    if exit_period_no is None or exit_period_no not in period_end_by_no:
        raise MigrationError("Legacy non-completion is missing a valid exit period.")
    return (
        "DISCONTINUED",
        exit_period_no,
        period_end_by_no[exit_period_no],
        "DISCONTINUED inferred from completed_service=false; legacy subtype and reason were not recorded.",
    )


def _fetchall(con, sql, params=()):
    with con.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _scalar(con, sql, params=()):
    return _fetchall(con, sql, params)[0][0]


def _count(con, table):
    return int(_scalar(con, f"SELECT count(*) FROM me.{table}"))


def _open_source():
    return connect(database=LEGACY_DB, readonly=True)


def _preflight(source, target):
    target_db = DB["dbname"]
    if target_db == LEGACY_DB:
        raise MigrationError(
            "Destination and legacy database names are the same. Set NSME_DB_NAME to a separate database."
        )

    try:
        target_counts = {
            "attendance_returns": _count(target, "attendance_returns"),
            "supervisor_evaluations": _count(target, "supervisor_evaluations"),
            "monitoring_visits": _count(target, "monitoring_visits"),
            "service_completion": _count(target, "service_completion"),
            "audit_log": _count(target, "audit_log"),
            "app_users": _count(target, "app_users"),
        }
    except UndefinedTable as exc:
        raise MigrationError(
            "Destination is not initialized. Run `python src/run_pipeline.py --init --no-users` "
            "with NSME_DB_NAME set to the new database."
        ) from exc

    if any(target_counts[name] for name in (
        "attendance_returns", "supervisor_evaluations", "monitoring_visits",
        "service_completion", "audit_log",
    )):
        raise MigrationError("Destination already contains v2 records; refusing to merge or overwrite them.")
    if target_counts["app_users"] and not CREDENTIALS.exists():
        raise MigrationError(
            "Destination already has accounts but outputs/credentials_v2.csv is missing; "
            "refusing to risk losing access."
        )
    if not target_counts["app_users"] and CREDENTIALS.exists():
        raise MigrationError(
            "outputs/credentials_v2.csv already exists while the destination has no accounts; "
            "move the file securely before creating new credentials."
        )

    if _scalar(target, "SELECT to_regclass('me.legacy_migrations') IS NOT NULL"):
        if _scalar(target, "SELECT count(*) FROM me.legacy_migrations"):
            raise MigrationError("This legacy database has already been migrated to the destination.")

    source_users = _fetchall(source, "SELECT user_id, role::text FROM me.users ORDER BY user_id")
    if not source_users:
        raise MigrationError("Legacy users table is empty; there is no role mapping for historical audit events.")
    unsupported_roles = sorted({role for _, role in source_users} - {"HQ", "ADMIN"})
    if unsupported_roles:
        raise MigrationError(f"Legacy account roles need an explicit mapping: {unsupported_roles}")

    periods = {
        int(period_no): reporting_period
        for reporting_period, period_no in _fetchall(
            target, "SELECT reporting_period, period_no FROM me.reporting_periods"
        )
    }
    source_periods = {
        int(period_no): reporting_period
        for reporting_period, period_no in _fetchall(
            source, "SELECT reporting_period, period_no FROM me.reporting_periods"
        )
    }
    if periods != source_periods:
        raise MigrationError("Source and destination reporting periods do not match.")
    if not periods:
        raise MigrationError("No reporting periods are configured in the destination.")
    period_end_by_no = {period_no: month_end(start) for period_no, start in periods.items()}

    target_district_ids = {
        (region, district): district_id
        for district_id, region, district in _fetchall(
            target, "SELECT district_id, region_name, district_name FROM me.regions_districts"
        )
    }
    source_districts = {
        district_id: (region, district)
        for district_id, region, district in _fetchall(
            source, "SELECT district_id, region_name, district_name FROM me.regions_districts"
        )
    }
    if set(source_districts.values()) != set(target_district_ids):
        raise MigrationError("Source and destination district/region names do not match.")

    target_agency_ids = {
        name: agency_id
        for agency_id, name in _fetchall(
            target, "SELECT agency_id, agency_name FROM me.user_agencies"
        )
    }
    source_agencies = {
        agency_id: name
        for agency_id, name in _fetchall(source, "SELECT agency_id, agency_name FROM me.user_agencies")
    }
    if set(source_agencies.values()) != set(target_agency_ids):
        raise MigrationError("Source and destination agency names do not match.")

    source_personnel = {row[0] for row in _fetchall(source, "SELECT personnel_id FROM me.personnel")}
    target_postings = {
        personnel_id: (agency_id, district_id)
        for personnel_id, agency_id, district_id in _fetchall(
            target, "SELECT personnel_id, agency_id, district_id FROM me.postings"
        )
    }
    if source_personnel != set(target_postings):
        raise MigrationError("Destination personnel IDs differ from the legacy database.")

    target_sites = {
        personnel_id: (
            target_agency_name,
            target_region,
            target_district,
        )
        for personnel_id, target_agency_name, target_region, target_district in _fetchall(
            target,
            """SELECT po.personnel_id, ua.agency_name, rd.region_name, rd.district_name
               FROM me.postings po
               JOIN me.user_agencies ua USING (agency_id)
               JOIN me.regions_districts rd USING (district_id)""",
        )
    }
    source_sites = {
        personnel_id: (agency_name, region, district)
        for personnel_id, agency_name, region, district in _fetchall(
            source,
            """SELECT po.personnel_id, ua.agency_name, rd.region_name, rd.district_name
               FROM me.postings po
               JOIN me.user_agencies ua USING (agency_id)
               JOIN me.regions_districts rd USING (district_id)""",
        )
    }
    if source_sites != target_sites:
        raise MigrationError("Destination posting sites differ from the legacy database.")

    counts = {
        "personnel": _count(source, "personnel"),
        "postings": _count(source, "postings"),
        "attendance": _count(source, "monthly_attendance_submissions"),
        "evaluations": _count(source, "supervisor_evaluations"),
        "visits": _count(source, "monitoring_visit_submissions"),
        "service_outcomes": _count(source, "service_outcomes"),
        "audit_events": _count(source, "submission_audit_log"),
    }
    if counts["personnel"] != len(target_postings) or counts["postings"] != len(target_postings):
        raise MigrationError("Legacy personnel/posting counts do not match the initialized destination.")

    checks = [
        ("duplicate attendance person-periods",
         "SELECT count(*) FROM (SELECT 1 FROM me.monthly_attendance_submissions "
         "GROUP BY personnel_id, reporting_period HAVING count(*) > 1) d"),
        ("duplicate evaluation person-periods",
         "SELECT count(*) FROM (SELECT 1 FROM me.supervisor_evaluations "
         "GROUP BY personnel_id, reporting_period HAVING count(*) > 1) d"),
        ("duplicate legacy visit IDs",
         "SELECT count(*) - count(DISTINCT visit_id) FROM me.monitoring_visit_submissions"),
        ("invalid attendance rows",
         """SELECT count(*) FROM me.monthly_attendance_submissions
            WHERE personnel_id IS NULL OR reporting_period IS NULL
               OR days_present < 0 OR days_present > 31
               OR days_expected < 0 OR days_expected > 31
               OR days_present > days_expected OR status::text NOT IN
               ('DRAFT','SUBMITTED','VERIFIED','DISTRICT_VERIFIED','APPROVED','REJECTED')"""),
        ("invalid evaluation rows",
         """SELECT count(*) FROM me.supervisor_evaluations
            WHERE personnel_id IS NULL OR reporting_period IS NULL
               OR rating IS NULL OR rating < 1 OR rating > 5
               OR status::text NOT IN
               ('DRAFT','SUBMITTED','VERIFIED','DISTRICT_VERIFIED','APPROVED','REJECTED')"""),
        ("invalid visits",
         """SELECT count(*) FROM me.monitoring_visit_submissions
            WHERE visit_id IS NULL OR personnel_id IS NULL OR district_id IS NULL
               OR reporting_period IS NULL OR visit_date IS NULL
               OR status::text NOT IN
               ('DRAFT','SUBMITTED','VERIFIED','DISTRICT_VERIFIED','APPROVED','REJECTED')"""),
        ("invalid service outcomes",
         """SELECT count(*) FROM me.service_outcomes
            WHERE personnel_id IS NULL OR completed_service IS NULL
               OR (completed_service AND exit_period_no IS NOT NULL)
               OR (NOT completed_service AND exit_period_no IS NULL)
               OR (exit_period_no IS NOT NULL AND exit_period_no NOT IN
                   (SELECT period_no FROM me.reporting_periods))"""),
        ("invalid audit events",
         """SELECT count(*) FROM me.submission_audit_log
            WHERE entity_type IS NULL OR length(entity_type) > 60
               OR new_status IS NULL OR "timestamp" IS NULL"""),
    ]
    for label, sql in checks:
        invalid = int(_scalar(source, sql))
        if invalid:
            raise MigrationError(f"Preflight found {invalid:,} {label}.")

    source_statuses = {
        status
        for table in ("monthly_attendance_submissions", "supervisor_evaluations", "monitoring_visit_submissions")
        for (status,) in _fetchall(source, f"SELECT DISTINCT status::text FROM me.{table}")
    }
    for status in source_statuses:
        map_status(status)
    visit_statuses = {
        status
        for (status,) in _fetchall(
            source, "SELECT DISTINCT status::text FROM me.monitoring_visit_submissions"
        )
    }
    for status in visit_statuses:
        map_visit_status(status)

    return {
        "counts": counts,
        "source_users": source_users,
        "target_counts": target_counts,
        "target_district_ids": target_district_ids,
        "source_districts": source_districts,
        "target_agency_ids": target_agency_ids,
        "source_agencies": source_agencies,
        "target_postings": target_postings,
        "period_end_by_no": period_end_by_no,
    }


def _map_user(user_id, user_ids):
    if user_id is None:
        return None
    try:
        return user_ids[user_id]
    except KeyError as exc:
        raise MigrationError(f"Legacy record references an unmapped user ID {user_id}.") from exc


def _map_id(value, mapping, label):
    try:
        return mapping[value]
    except KeyError as exc:
        raise MigrationError(f"Legacy record references an unknown {label}: {value!r}.") from exc


def _insert_stream(source, target_cur, sql, table, columns, transform, label):
    column_list = ", ".join(columns)
    insert_sql = f"INSERT INTO me.{table} ({column_list}) VALUES %s"
    total = 0
    with source.cursor(name=f"legacy_{table}") as reader:
        reader.itersize = BATCH_SIZE
        reader.execute(sql)
        while True:
            rows = reader.fetchmany(BATCH_SIZE)
            if not rows:
                break
            values = [transform(row) for row in rows]
            execute_values(target_cur, insert_sql, values, page_size=1000)
            total += len(values)
            if total % 100000 < len(values):
                print(f"  {label}: {total:,} rows")
    print(f"  {label}: {total:,} rows complete")
    return total


def _map_visit_rows(source, target_cur, metadata):
    target_sites = metadata["target_postings"]
    source_districts = metadata["source_districts"]
    target_district_ids = metadata["target_district_ids"]
    source_users = metadata["source_users"]
    with target_cur.connection.cursor() as cur:
        cur.execute("SELECT user_id FROM me.app_users WHERE role='HQ' ORDER BY user_id LIMIT 1")
        hq = cur.fetchone()
    if not hq:
        raise MigrationError("No destination HQ account exists for legacy approval records.")
    user_ids = {legacy_id: hq[0] for legacy_id, _ in source_users}

    visit_sql = """SELECT visit_id, district_id, personnel_id, reporting_period, visit_date,
                          remarks, status::text, submitted_by, approved_by,
                          created_at AT TIME ZONE current_setting('TimeZone')
                   FROM me.monitoring_visit_submissions ORDER BY visit_id"""
    total = 0
    with source.cursor(name="legacy_visits") as reader:
        reader.itersize = BATCH_SIZE
        reader.execute(visit_sql)
        while True:
            rows = reader.fetchmany(BATCH_SIZE)
            if not rows:
                break
            visits = []
            people = []
            for (visit_id, legacy_district_id, personnel_id, period, visit_date,
                 remarks, status, submitted_by, approved_by, created_at) in rows:
                agency_id, posting_district_id = _map_id(personnel_id, target_sites, "personnel posting")
                district_key = _map_id(legacy_district_id, source_districts, "district")
                district_id = _map_id(district_key, target_district_ids, "destination district")
                if district_id != posting_district_id:
                    raise MigrationError(f"Legacy visit {visit_id} district conflicts with its personnel posting.")
                visits.append((
                    visit_id, agency_id, district_id, period, visit_date, "LEGACY",
                    "Legacy import; original visit level not recorded.", remarks,
                    map_visit_status(status), _map_user(submitted_by, user_ids), created_at,
                    _map_user(approved_by, user_ids), None, None,
                ))
                people.append((visit_id, personnel_id))
            execute_values(
                target_cur,
                """INSERT INTO me.monitoring_visits
                   (visit_id, agency_id, district_id, reporting_period, visit_date, visit_level,
                    officer_name, findings, status, submitted_by, submitted_at,
                    approved_by, approved_at, reject_reason) VALUES %s""",
                visits,
                page_size=1000,
            )
            execute_values(
                target_cur,
                "INSERT INTO me.monitoring_visit_personnel (visit_id, personnel_id) VALUES %s",
                people,
                page_size=1000,
            )
            total += len(rows)
            if total % 100000 < len(rows):
                print(f"  visits: {total:,} rows")
    print(f"  visits and personnel links: {total:,} rows complete")
    return total


def _set_sequence(target_cur, table, column):
    target_cur.execute(f"SELECT COALESCE(MAX({column}), 0) FROM me.{table}")
    maximum = target_cur.fetchone()[0]
    target_cur.execute(
        "SELECT setval(pg_get_serial_sequence(%s, %s), %s, %s)",
        (f"me.{table}", column, max(maximum, 1), maximum > 0),
    )


def migrate(source, metadata):
    from create_users import main as create_accounts
    from run_pipeline import refresh

    if not metadata["target_counts"]["app_users"]:
        create_accounts(agency_sites="all", credentials_path=CREDENTIALS)

    target = connect()
    try:
        with target:
            with target.cursor() as cur:
                cur.execute("SELECT to_regclass('me.legacy_migrations') IS NOT NULL")
                migration_table = cur.fetchone()
                if migration_table is None:
                    raise MigrationError("Could not inspect the destination migration marker.")
                if migration_table[0]:
                    cur.execute("SELECT count(*) FROM me.legacy_migrations")
                    migrated = cur.fetchone()
                    if migrated is None:
                        raise MigrationError("Could not read the destination migration marker.")
                    if migrated[0]:
                        raise MigrationError("This legacy database has already been migrated to the destination.")

                cur.execute("SELECT user_id FROM me.app_users WHERE role='HQ' ORDER BY user_id LIMIT 1")
                hq = cur.fetchone()
                if not hq:
                    raise MigrationError("No destination HQ account exists.")
                user_ids = {legacy_id: hq[0] for legacy_id, _ in metadata["source_users"]}

                cur.execute("""CREATE TABLE IF NOT EXISTS me.legacy_migrations (
                               migration_name VARCHAR(80) PRIMARY KEY,
                               source_database VARCHAR(80) NOT NULL,
                               completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                               row_counts JSONB NOT NULL)""")

                counts = metadata["counts"]
                _insert_stream(
                    source, cur,
                    """SELECT submission_id, personnel_id, reporting_period, days_expected, days_present,
                              status::text, submitted_by, verified_by, approved_by,
                              created_at AT TIME ZONE current_setting('TimeZone'),
                              updated_at AT TIME ZONE current_setting('TimeZone')
                       FROM me.monthly_attendance_submissions ORDER BY submission_id""",
                    "attendance_returns",
                    ("return_id", "personnel_id", "reporting_period", "reported_for_duty",
                     "days_expected", "days_present", "status", "submitted_by", "submitted_at",
                     "verified_by", "approved_by", "approved_at", "reject_reason"),
                    lambda row: (
                        row[0], row[1], row[2], True, row[3], row[4], map_status(row[5]),
                        _map_user(row[6], user_ids), row[9], _map_user(row[7], user_ids),
                        _map_user(row[8], user_ids), row[10], None,
                    ),
                    "attendance",
                )

                _insert_stream(
                    source, cur,
                    """SELECT eval_id, personnel_id, reporting_period, rating, comments, status::text,
                              submitted_by, approved_by,
                              created_at AT TIME ZONE current_setting('TimeZone')
                       FROM me.supervisor_evaluations ORDER BY eval_id""",
                    "supervisor_evaluations",
                    ("eval_id", "personnel_id", "reporting_period", "rating", "comments", "status",
                     "submitted_by", "submitted_at", "approved_by", "approved_at", "reject_reason"),
                    lambda row: (
                        row[0], row[1], row[2], row[3], row[4], map_status(row[5]),
                        _map_user(row[6], user_ids), row[8], _map_user(row[7], user_ids), None, None,
                    ),
                    "evaluations",
                )

                _map_visit_rows(source, cur, metadata)

                _insert_stream(
                    source, cur,
                    """SELECT personnel_id, completed_service, exit_period_no
                       FROM me.service_outcomes ORDER BY personnel_id""",
                    "service_completion",
                    ("personnel_id", "outcome", "exit_period_no", "outcome_date", "reason", "status"),
                    lambda row: (row[0], *map_service_outcome(row[1], row[2], metadata["period_end_by_no"]), "CERTIFIED"),
                    "service outcomes",
                )

                def audit_row(row):
                    audit_id, entity_type, entity_id, old_status, new_status, changed_by, comments, at = row
                    detail = f"legacy_entity_id={entity_id}; {old_status or 'NULL'} -> {new_status}"
                    if comments:
                        detail += f"; comment={comments}"
                    return (
                        audit_id, at, _map_user(changed_by, user_ids), "LEGACY_STATUS",
                        entity_type, detail,
                    )

                _insert_stream(
                    source, cur,
                    """SELECT audit_id, entity_type, entity_id, previous_status::text, new_status::text,
                              changed_by, comments,
                              \"timestamp\" AT TIME ZONE current_setting('TimeZone')
                       FROM me.submission_audit_log ORDER BY audit_id""",
                    "audit_log",
                    ("audit_id", "at", "user_id", "action", "table_name", "detail"),
                    audit_row,
                    "audit history",
                )

                for table, column in (
                    ("attendance_returns", "return_id"),
                    ("supervisor_evaluations", "eval_id"),
                    ("monitoring_visits", "visit_id"),
                    ("audit_log", "audit_id"),
                ):
                    _set_sequence(cur, table, column)

                cur.execute(
                    "INSERT INTO me.legacy_migrations(migration_name, source_database, row_counts) "
                    "VALUES (%s, %s, %s)",
                    ("legacy_ns_me_v1", LEGACY_DB, Json(counts)),
                )
        print(f"Migration data committed to database {DB['dbname']}.")
    finally:
        target.close()

    print("Refreshing v2 analytics from migrated, approved records.")
    refresh()
    print(f"New account passwords: {CREDENTIALS}")


def main(check_only=False):
    source = _open_source()
    try:
        target = connect()
        try:
            metadata = _preflight(source, target)
        finally:
            target.close()
        print(f"Preflight OK: {metadata['counts']}")
        if check_only:
            print("No data was changed.")
            return
        migrate(source, metadata)
    finally:
        source.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="run read-only source/target validation only")
    args = parser.parse_args()
    main(check_only=args.check)
