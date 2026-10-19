"""One command rebuilds everything:  python src/run_pipeline.py"""
import warnings
warnings.filterwarnings("ignore")
import pandas as pd
import validate_load, indicator_engine, models
from config import SQL, OUT
from db import run_sql_file, query


def export_powerbi():
    d = OUT / "powerbi"; d.mkdir(exist_ok=True)
    tables = {
        "dim_district": "SELECT district_id, region_name, district_name FROM regions_districts",
        "dim_indicator": "SELECT indicator_id, indicator_name, result_level, unit, baseline_val, target_val, green_threshold, yellow_threshold FROM me_indicators",
        "fact_indicator_achievement": "SELECT indicator_id, district_id, reporting_period, actual_val, target_val, achievement_rate, status_flag FROM vw_indicator_achievement",
        "fact_district_scorecard": "SELECT * FROM vw_district_scorecard",
        "fact_national_kpis": "SELECT * FROM vw_national_kpis",
        "fact_district_risk": "SELECT * FROM district_risk_scores",
        "fact_logistic_results": "SELECT model_name, term, odds_ratio, or_ci_low, or_ci_high, p_value, n_obs FROM eval_logistic_results",
    }
    for n, q in tables.items():
        query(q).to_csv(d / f"{n}.csv", index=False)
    export_powerbi_extra(d)
    print("Power BI CSVs ->", d)


PERIOD_KPI_SQL = """
WITH first_rep AS (
  SELECT personnel_id, min(reporting_period) AS fp
  FROM personnel_monthly WHERE reported_for_duty GROUP BY personnel_id)
SELECT rp.reporting_period, rp.period_no,
       (SELECT count(*) FROM postings)                                              AS n_posted,
       (SELECT count(*) FROM first_rep f WHERE f.fp <= rp.reporting_period)         AS n_deployed,
       count(*) FILTER (WHERE pm.reported_for_duty)                                 AS n_active,
       count(*) FILTER (WHERE pm.reported_for_duty AND pm.monitoring_visits > 0)    AS n_monitored,
       count(*) FILTER (WHERE pm.reported_for_duty AND pm.evaluation_done)          AS n_evaluated,
       coalesce(sum(pm.monitoring_visits), 0)                                       AS n_visits
FROM reporting_periods rp JOIN personnel_monthly pm USING (reporting_period)
GROUP BY rp.reporting_period, rp.period_no ORDER BY rp.reporting_period
"""


def export_powerbi_extra(d):
    """Tables added for the Power BI dashboard (proposal section 4.5 KPIs, risk list, model performance)."""
    import json
    # dim_period: one row per reporting month (slicers, relationships, sorting)
    per = query("SELECT reporting_period, period_no, is_open FROM reporting_periods ORDER BY reporting_period")
    per["period_label"] = pd.to_datetime(per.reporting_period).dt.strftime("%b %Y")
    per.to_csv(d / "dim_period.csv", index=False)

    # fact_period_kpis: national headline KPIs per month + district RAG counts
    k = query(PERIOD_KPI_SQL)
    num = [c for c in k.columns if c not in ("reporting_period",)]
    k[num] = k[num].astype(float)
    k["deployment_rate"] = (k.n_deployed / k.n_posted * 100).round(2)
    k["monitoring_coverage_pct"] = (k.n_monitored / k.n_active * 100).round(2)
    k["evaluation_completion_pct"] = (k.n_evaluated / k.n_active * 100).round(2)
    k = k.merge(query("SELECT * FROM vw_national_kpis"), on="reporting_period", how="left")
    k.to_csv(d / "fact_period_kpis.csv", index=False)

    # fact_outcome_summary: certified service outcomes (completion rate = COMPLETED share)
    o = query("SELECT outcome, count(*) AS n FROM service_completion WHERE status = 'CERTIFIED' GROUP BY outcome ORDER BY outcome")
    o["n"] = o.n.astype(float)
    o["share_pct"] = (o.n / o.n.sum() * 100).round(2) if len(o) else []
    o.to_csv(d / "fact_outcome_summary.csv", index=False)

    # latest early-warning list with names, ranked (decision-support list, not an automatic action)
    r = query("SELECT * FROM vw_district_risk_latest ORDER BY risk_probability DESC")
    r["risk_rank"] = range(1, len(r) + 1)
    r.to_csv(d / "fact_district_risk_latest.csv", index=False)

    # model performance, from the files models.py writes
    mf = OUT / "early_warning_metrics.json"
    if mf.exists():
        m = json.load(open(mf)); rows = []
        for model, met in m.items():
            for name, v in met.items():
                if isinstance(v, (int, float)):
                    rows.append((model, name, float(v)))
        pd.DataFrame(rows, columns=["model", "metric", "value"]).to_csv(d / "fact_model_metrics.csv", index=False)
        cm = m.get("random_forest", {}).get("confusion_matrix_0.5")
        if cm:
            pd.DataFrame([("Not at risk", "Predicted not at risk", cm[0][0]), ("Not at risk", "Predicted at risk", cm[0][1]),
                          ("At risk", "Predicted not at risk", cm[1][0]), ("At risk", "Predicted at risk", cm[1][1])],
                         columns=["actual", "predicted", "n"]).to_csv(d / "fact_confusion_matrix.csv", index=False)
    for src, dst in (("rf_feature_importance.csv", "fact_rf_importance.csv"), ("rf_calibration.csv", "fact_rf_calibration.csv")):
        if (OUT / src).exists():
            df = pd.read_csv(OUT / src)
            if src.startswith("rf_feature"):
                df = df.rename(columns={df.columns[0]: "feature"})
            df.to_csv(d / dst, index=False)


def refresh():
    """Routine operation (also triggered from the HQ screen): indicators -> views -> models -> exports."""
    from db import connect
    con = connect(); con.autocommit = True
    con.cursor().execute("ANALYZE"); con.close()       # keep planner statistics fresh after bulk entry
    indicator_engine.main()
    run_sql_file(SQL / "02_views.sql")
    models.main()
    export_powerbi()


if __name__ == "__main__":
    import sys
    if "--init" in sys.argv:           # DESTRUCTIVE one-time setup (or --force to wipe entered data)
        print("[init] schema + reference data"); validate_load.main(force="--force" in sys.argv)
        if "--no-users" not in sys.argv:
            import create_users; create_users.main()
    if "--demo" in sys.argv:           # SYNTHETIC demo data through the same input tables
        import demo_seed; demo_seed.main()
    refresh()
