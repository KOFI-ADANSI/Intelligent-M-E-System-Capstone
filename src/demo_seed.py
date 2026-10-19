"""DEMO SEED ONLY. Not part of the production system: real records come from the web app.

SYNTHETIC monitoring / attendance / evaluation / outcome generator.

The posting sheet has no operational records, so this module simulates a full
12-month service cycle (Nov 2026 - Oct 2027) on top of the REAL posting table.

Data-generating process (documented so results can be audited):
  * each district has a latent quality q_d and a monitoring intensity m_d (m_d correlated with q_d,
    so monitoring is partly confounded with district quality - as in real administrative data)
  * each person has a latent diligence u_i
  * monthly district shocks follow AR(1) plus a district-specific drift, so performance moves over time
  * completion is drawn from a logistic model of FIRST-QUARTER monitoring visits, evaluations,
    supervisor rating, attendance, district quality, and posting-choice match
  * non-completers exit in months 4-12 and show a mild attendance / evaluation dip before exit

IMPORTANT: logistic-regression "findings" on this data recover the parameters set below
(TRUE_BETA); they demonstrate that the pipeline works, they are NOT empirical findings.
"""
import numpy as np
import pandas as pd
from config import SEED, N_PERIODS, CYCLE_START
from db import connect, copy_df, query

TRUE_BETA = dict(visits_q1=0.30, evals_q1=0.20, rating_q1=0.50, attendance_q1=0.04,
                 district_quality=0.35, choice_1=0.20, choice_none=-0.30)
TARGET_COMPLETION = 0.92


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def main(through=N_PERIODS):
    rng = np.random.default_rng(SEED)
    p = query("""SELECT p.personnel_id, po.district_id, po.agency_id, po.choice_rank_met
                 FROM personnel p JOIN postings po USING (personnel_id) ORDER BY p.personnel_id""")
    n, T = len(p), N_PERIODS
    dist_ids = np.sort(p["district_id"].unique())
    didx = pd.Series(np.arange(len(dist_ids)), index=dist_ids)[p["district_id"]].to_numpy()
    nd = len(dist_ids)

    # ---- latent structure
    q = rng.normal(0, 1, nd)
    m = 0.4 * q + np.sqrt(1 - 0.16) * rng.normal(0, 1, nd)
    drift = rng.normal(0, 0.05, nd)
    shock = np.zeros((nd, T))
    e = rng.normal(0, 0.35, (nd, T))
    for t in range(T):
        prev = shock[:, t - 1] if t else 0
        shock[:, t] = 0.7 * prev + e[:, t]
    shock += drift[:, None] * (np.arange(T)[None, :] - 5.5)
    u = rng.normal(0, 1, n)
    qi, mi = q[didx], m[didx]

    # ---- reporting for duty
    r1 = rng.random(n) < sigmoid(2.0 + 0.5 * qi)
    r2 = rng.random(n) < 0.7
    report_month = np.where(r1, 1, np.where(r2, 2, 3))

    months = np.arange(1, T + 1)[None, :]
    reported = months >= report_month[:, None]
    S = shock[didx]                                           # n x T

    # ---- activity (before exits are applied)
    lam = 0.33 * np.exp(0.30 * mi[:, None] + 0.25 * S)
    visits = np.where(reported, rng.poisson(lam), 0)
    p_eval = sigmoid(2.0 + 0.5 * qi[:, None] + 0.5 * S)
    evals = reported & (rng.random((n, T)) < p_eval)
    rating = np.clip(3.7 + 0.25 * qi[:, None] + 0.30 * u[:, None] + 0.10 * S
                     + rng.normal(0, 0.4, (n, T)), 1, 5)
    rating = np.where(evals, rating, np.nan)
    att = np.clip(90 + 3 * qi[:, None] + 3 * u[:, None] + 3 * S + rng.normal(0, 4, (n, T)), 40, 100)
    att = np.where(reported, att, np.nan)

    # ---- first-quarter aggregates -> completion
    v_q1 = visits[:, :3].sum(1)
    e_q1 = evals[:, :3].sum(1)
    r_q1 = np.nanmean(np.where(np.isnan(rating[:, :3]), np.nan, rating[:, :3]), axis=1)
    r_q1 = np.where(np.isnan(r_q1), np.nanmean(r_q1), r_q1)      # impute (very few persons)
    a_q1 = np.nan_to_num(np.nanmean(att[:, :3], axis=1), nan=np.nanmean(att[:, :3]))
    c1 = (p["choice_rank_met"] == 1).to_numpy()
    cn = p["choice_rank_met"].isna().to_numpy()
    B = TRUE_BETA
    lin = (B["visits_q1"] * v_q1 + B["evals_q1"] * e_q1 + B["rating_q1"] * (r_q1 - 3.7)
           + B["attendance_q1"] * (a_q1 - 90) + B["district_quality"] * qi
           + B["choice_1"] * c1 + B["choice_none"] * cn)
    lo, hi = -10.0, 15.0                                           # solve intercept for target rate
    for _ in range(60):
        mid = (lo + hi) / 2
        if sigmoid(lin + mid).mean() > TARGET_COMPLETION:
            hi = mid
        else:
            lo = mid
    completed = rng.random(n) < sigmoid(lin + (lo + hi) / 2)
    exit_month = np.where(completed, 0, rng.integers(4, 13, n))   # 0 = completed

    # ---- apply exits: pre-exit dip, then remove rows from the exit month on
    pre = (~completed)[:, None] & (months >= np.maximum(exit_month - 2, 4)[:, None]) & (months < exit_month[:, None])  # dip never touches Q1
    att = np.where(pre, att - 12, att)
    evals = np.where(pre, evals & (rng.random((n, T)) < 0.6), evals)
    rating = np.where(evals, rating, np.nan)
    active = (exit_month[:, None] == 0) | (months < exit_month[:, None])

    # ---- convert to INPUT-TABLE records (all pre-approved by a system user, clearly marked in the audit log)
    periods = pd.date_range(CYCLE_START, periods=T, freq="MS").date
    con = connect()
    with con, con.cursor() as cur:
        cur.execute("""INSERT INTO app_users(username,full_name,password_hash,role)
                       VALUES ('demo.seed','Demo seed (synthetic data loader)','!disabled','HQ')
                       ON CONFLICT (username) DO NOTHING""")
        cur.execute("SELECT user_id FROM app_users WHERE username='demo.seed'")
        uid = cur.fetchone()[0]
    pid = p["personnel_id"].to_numpy()
    ii, tt = np.nonzero(active & (months <= through))
    stamp = dict(submitted_by=uid, verified_by=uid, approved_by=uid)
    ts = pd.Timestamp("2026-01-01")
    att_df = pd.DataFrame({
        "personnel_id": pid[ii], "reporting_period": np.array(periods)[tt],
        "reported_for_duty": reported[ii, tt],
        "days_expected": np.where(reported[ii, tt], 22, 0),
        "days_present": np.where(reported[ii, tt], np.round(np.nan_to_num(att[ii, tt]) / 100 * 22), 0).astype(int),
        "status": "APPROVED", **stamp, "submitted_at": ts, "verified_at": ts, "approved_at": ts})
    ev = evals[ii, tt]
    ev_df = pd.DataFrame({
        "personnel_id": pid[ii][ev], "reporting_period": np.array(periods)[tt][ev],
        "rating": np.round(rating[ii, tt][ev], 2), "status": "APPROVED", **stamp,
        "submitted_at": ts, "verified_at": ts, "approved_at": ts})
    # visits: one visit record per (site, month, j) covering personnel with >= j visits that month
    vv = pd.DataFrame({"personnel_id": pid[ii], "period": np.array(periods)[tt],
                       "agency_id": p["agency_id"].to_numpy()[ii], "district_id": p["district_id"].to_numpy()[ii],
                       "k": visits[ii, tt]})
    vv = vv[vv.k > 0]
    rows = []
    for j in range(1, int(vv.k.max()) + 1):
        x = vv[vv.k >= j][["personnel_id", "period", "agency_id", "district_id"]].copy()
        x["j"] = j
        rows.append(x)
    vp = pd.concat(rows, ignore_index=True)
    sites = vp[["agency_id", "district_id", "period", "j"]].drop_duplicates().reset_index(drop=True)
    sites["visit_id"] = np.arange(1, len(sites) + 1)
    sites["visit_date"] = [pd.Timestamp(d) + pd.Timedelta(days=int(10 + 3 * j)) for d, j in zip(sites.period, sites.j)]
    sites = sites.rename(columns={"period": "reporting_period"})
    sites["visit_level"] = "DISTRICT"; sites["officer_name"] = "Demo seed"; sites["status"] = "APPROVED"
    sites["submitted_by"] = uid; sites["approved_by"] = uid
    vp = vp.merge(sites[["agency_id", "district_id", "reporting_period", "j", "visit_id"]],
                  left_on=["agency_id", "district_id", "period", "j"],
                  right_on=["agency_id", "district_id", "reporting_period", "j"])
    # outcomes (certified only once known)
    last_exit_known = through
    known = completed if through >= T else np.zeros(n, bool)
    out_mask = known | ((~completed) & (exit_month <= last_exit_known))
    oc = pd.DataFrame({
        "personnel_id": pid[out_mask],
        "outcome": np.where(completed[out_mask], "COMPLETED",
                            np.where(rng.random(out_mask.sum()) < 0.25, "ABSCONDED", "DISCONTINUED")),
        "exit_period_no": pd.array(np.where(completed[out_mask], np.nan, exit_month[out_mask]), dtype="Int64"),
        "outcome_date": [(pd.Timestamp(periods[(0 if c else m) - 1 if not c else T - 1]) + pd.offsets.MonthEnd(0)).date()
                         for c, m in zip(completed[out_mask], exit_month[out_mask])],
        "status": "CERTIFIED", "submitted_by": uid, "verified_by": uid, "approved_by": uid, "certified_by": uid,
        "submitted_at": ts, "verified_at": ts, "approved_at": ts, "certified_at": ts})

    con = connect()
    with con, con.cursor() as cur:
        cur.execute("TRUNCATE attendance_returns, supervisor_evaluations, monitoring_visit_personnel, "
                    "monitoring_visits, service_completion RESTART IDENTITY CASCADE")
        copy_df(con, att_df, "attendance_returns")
        copy_df(con, ev_df, "supervisor_evaluations")
        vcols = ["visit_id", "agency_id", "district_id", "reporting_period", "visit_date", "visit_level",
                 "officer_name", "status", "submitted_by", "approved_by"]
        copy_df(con, sites, "monitoring_visits", vcols)
        copy_df(con, vp[["visit_id", "personnel_id"]], "monitoring_visit_personnel")
        cur.execute("SELECT setval(pg_get_serial_sequence('monitoring_visits','visit_id'), (SELECT max(visit_id) FROM monitoring_visits))")
        copy_df(con, oc, "service_completion")
        cur.execute("INSERT INTO audit_log(user_id,action,detail) VALUES (%s,'DEMO_SEED',%s)",
                    (uid, f"Loaded SYNTHETIC demo data through period {through}"))
    con.close()
    print(f"DEMO SEED (synthetic): {len(att_df):,} returns, {len(ev_df):,} evaluations, {len(sites):,} visits "
          f"({len(vp):,} personnel links), {len(oc):,} certified outcomes")


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else N_PERIODS)
