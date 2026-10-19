"""Layer 3a: automated indicator engine (proposal section 4.2, extended).

SQL aggregates the monthly records to district x month x indicator values; the Python
class then computes achievement rates and GREEN / YELLOW / RED flags using the
configurable per-indicator thresholds."""
import numpy as np
import pandas as pd
from config import INDICATORS, THRESHOLDS
from db import connect, copy_df, query

AGG_SQL = """
WITH posted AS (
  SELECT district_id, count(*) AS n_posted FROM postings GROUP BY district_id),
first_rep AS (
  SELECT personnel_id, min(reporting_period) AS first_period
  FROM personnel_monthly WHERE reported_for_duty GROUP BY personnel_id),
dep AS (
  SELECT po.district_id, rp.reporting_period, count(f.personnel_id) AS n_deployed
  FROM postings po CROSS JOIN reporting_periods rp
  LEFT JOIN first_rep f ON f.personnel_id = po.personnel_id AND f.first_period <= rp.reporting_period
  GROUP BY po.district_id, rp.reporting_period),
ext AS (
  SELECT po.district_id, rp.reporting_period, count(*) AS n_exited
  FROM postings po JOIN service_outcomes so USING (personnel_id)
  JOIN reporting_periods rp ON so.exit_period_no <= rp.period_no
  GROUP BY po.district_id, rp.reporting_period),
sub AS (
  SELECT po.district_id, ar.reporting_period, count(*) AS n_submitted
  FROM attendance_returns ar JOIN postings po USING (personnel_id)
  WHERE ar.status IN ('SUBMITTED','DISTRICT_VERIFIED','APPROVED')
  GROUP BY po.district_id, ar.reporting_period),
act AS (
  SELECT po.district_id, pm.reporting_period,
         count(*) FILTER (WHERE pm.reported_for_duty)                         AS n_active,
         sum(pm.monitoring_visits)                                            AS visits,
         count(*) FILTER (WHERE pm.reported_for_duty AND pm.evaluation_done)  AS evals,
         avg(pm.attendance_rate)                                              AS att,
         avg(pm.supervisor_rating)                                            AS rating
  FROM personnel_monthly pm JOIN postings po USING (personnel_id)
  GROUP BY po.district_id, pm.reporting_period)
SELECT p.district_id, d.reporting_period, p.n_posted, d.n_deployed,
       coalesce(e.n_exited,0) AS n_exited, a.n_active, a.visits, a.evals, a.att, a.rating, coalesce(s.n_submitted,0) AS n_submitted
FROM posted p
JOIN dep d USING (district_id)
LEFT JOIN ext e ON e.district_id = d.district_id AND e.reporting_period = d.reporting_period
JOIN act a ON a.district_id = d.district_id AND a.reporting_period = d.reporting_period   -- only district-months with approved returns
LEFT JOIN sub s ON s.district_id = d.district_id AND s.reporting_period = d.reporting_period
"""


class IntelligentMESystem:
    def __init__(self, indicators=INDICATORS, thresholds=THRESHOLDS):
        self.ind = pd.DataFrame(indicators, columns=[
            "indicator_id", "indicator_name", "result_level", "unit", "baseline_val",
            "target_val", "data_source", "measurement_method"])
        self.th = thresholds

    @staticmethod
    def actual_values(raw):
        """Raw district-month aggregates -> long table of indicator actuals."""
        r = raw.astype({c: float for c in ["n_posted", "n_deployed", "n_exited", "n_active",
                                           "visits", "evals", "att", "rating", "n_submitted"]})
        vals = {
            "IND01": r.n_deployed / r.n_posted * 100,
            "IND02": r.visits / r.n_active * 100,
            "IND03": r.evals / r.n_active * 100,
            "IND04": r.att,
            "IND05": r.rating,
            "IND06": (r.n_posted - r.n_exited) / r.n_posted * 100,
            "IND07": r.n_submitted / (r.n_posted - r.n_exited) * 100,
        }
        out = []
        for k, v in vals.items():
            out.append(pd.DataFrame({"indicator_id": k, "district_id": r.district_id,
                                     "reporting_period": r.reporting_period, "actual_val": v}))
        return pd.concat(out, ignore_index=True).dropna(subset=["actual_val"])

    def process_indicator_logs(self, logs):
        m = logs.merge(self.ind[["indicator_id", "target_val"]], on="indicator_id", how="left")
        m["achievement_rate"] = np.round(m["actual_val"].astype(float) / m["target_val"].astype(float) * 100, 2)
        green = m["indicator_id"].map(lambda i: self.th[i][0])
        yellow = m["indicator_id"].map(lambda i: self.th[i][1])
        m["status_flag"] = np.select([m.achievement_rate >= green, m.achievement_rate >= yellow],
                                     ["GREEN", "YELLOW"], default="RED")
        m["actual_val"] = m["actual_val"].astype(float).round(2)
        return m


def main():
    eng = IntelligentMESystem()
    con = connect()
    with con, con.cursor() as cur:
        cur.execute("TRUNCATE indicator_performance_logs, me_indicators CASCADE")
        for (i, nm, lvl, unit, base, tgt, src, meth) in INDICATORS:
            g, y = THRESHOLDS[i]
            cur.execute("""INSERT INTO me_indicators(indicator_id,indicator_name,result_level,unit,baseline_val,
                           target_val,green_threshold,yellow_threshold,data_source,reporting_frequency,
                           measurement_method,responsible_officer) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'Monthly',%s,
                           'District M&E Officer')""", (i, nm, lvl, unit, base, tgt, g, y, src, meth))
    con.close()

    raw = query(AGG_SQL)
    logs = eng.process_indicator_logs(eng.actual_values(raw))
    logs["remarks"] = None
    con = connect()
    with con:
        copy_df(con, logs, "indicator_performance_logs",
                ["indicator_id", "district_id", "reporting_period", "actual_val",
                 "achievement_rate", "status_flag", "remarks"])
    con.close()
    print(f"Wrote {len(logs):,} indicator logs")
    print(logs.groupby("indicator_id")["status_flag"].value_counts(normalize=True).unstack().round(2))
    print(logs.groupby("indicator_id")["achievement_rate"].describe().round(1))


if __name__ == "__main__":
    main()
