import io
import psycopg2
import pandas as pd
from config import DB, SCHEMA


def connect(database: str | None = None, readonly: bool = False):
    con = psycopg2.connect(
        host=DB["host"],
        port=DB["port"],
        user=DB["user"],
        dbname=database or DB["dbname"],
        password=DB["password"],
    )
    if readonly:
        con.set_session(readonly=True)
    with con.cursor() as cur:
        cur.execute(f"SET search_path = {SCHEMA}, public")
    con.commit()
    return con


def run_sql_file(path):
    con = connect()
    with con, con.cursor() as cur:
        cur.execute(open(path, encoding="utf-8").read())
    con.close()


def copy_df(con, df, table, columns=None):
    """Bulk load a DataFrame with COPY (fast for the 600k-row monthly table)."""
    columns = columns or list(df.columns)
    buf = io.StringIO()
    df[columns].to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    with con.cursor() as cur:
        cur.copy_expert(
            f"COPY {table} ({','.join(columns)}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf)


def query(sql, params=None):
    con = connect()
    try:
        with con.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                raise RuntimeError("query() requires a statement that returns rows.")
            return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
    finally:
        con.close()
