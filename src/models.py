"""Layer 3b/3c: logistic-regression evaluation and Random Forest early-warning model."""
import json
import numpy as np
import pandas as pd
import statsmodels.api as sm
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, roc_auc_score, precision_score, recall_score,
                             f1_score, confusion_matrix, brier_score_loss, precision_recall_curve)
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from config import (OUT, SEED, RISK_HORIZON, RISK_COMPOSITE_THRESHOLD, TRAIN_LAST_PERIOD,
                    TEST_FIRST_PERIOD, N_PERIODS)
from db import connect, query, copy_df

# ------------------------------------------------------------------ logistic regression
PERSON_SQL = """
SELECT p.personnel_id, so.completed_service::int AS completed,
       sum(pm.monitoring_visits) AS visits_q1,
       count(*) FILTER (WHERE pm.evaluation_done) AS evals_q1,
       avg(pm.supervisor_rating) AS rating_q1,
       avg(pm.attendance_rate) AS attendance_q1,
       p.gender, p.age_at_posting AS age, p.qualification,
       ua.category, po.choice_rank_met, rd.region_name, po.district_id
FROM personnel p
JOIN postings po USING (personnel_id) JOIN user_agencies ua USING (agency_id)
JOIN regions_districts rd USING (district_id) JOIN service_outcomes so USING (personnel_id)
JOIN personnel_monthly pm USING (personnel_id)
JOIN reporting_periods rp USING (reporting_period)
WHERE rp.period_no <= 3
GROUP BY p.personnel_id, so.completed_service, p.gender, p.age_at_posting, p.qualification,
         ua.category, po.choice_rank_met, rd.region_name, po.district_id
"""


def logistic_eval():
    d = query(PERSON_SQL)
    num = ["completed", "visits_q1", "evals_q1", "rating_q1", "attendance_q1", "age"]
    d[num] = d[num].astype(float)
    d["rating_missing"] = d["rating_q1"].isna().astype(float)
    d["rating_q1"] = d["rating_q1"].fillna(d["rating_q1"].mean())
    d["choice"] = d["choice_rank_met"].map(lambda x: "none" if pd.isna(x) else f"rank{int(x)}")
    core = ["visits_q1", "evals_q1", "rating_q1"]
    ctrl = d[["attendance_q1", "age", "rating_missing"]].copy()
    cat = pd.get_dummies(d[["gender", "qualification", "category", "choice"]], drop_first=True).astype(float)
    reg = pd.get_dummies(d["region_name"], prefix="reg", drop_first=True).astype(float)
    dis = pd.get_dummies(d["district_id"], prefix="dist", drop_first=True).astype(float)
    specs = {
        "A_proposal_core": d[core],
        "B_adjusted": pd.concat([d[core], ctrl, cat], axis=1),
        "C_region_FE": pd.concat([d[core], ctrl, cat, reg], axis=1),
        "D_district_FE": pd.concat([d[core], ctrl, cat, dis], axis=1),
    }
    rows = []
    for name, X in specs.items():
        res = sm.Logit(d["completed"], sm.add_constant(X)).fit(disp=0, maxiter=200)
        ci = res.conf_int()
        for t in res.params.index:
            if t.startswith(("reg_", "dist_")):
                continue
            rows.append(dict(model_name=name, term=t, coef=res.params[t], std_err=res.bse[t],
                             p_value=res.pvalues[t], odds_ratio=np.exp(res.params[t]),
                             or_ci_low=np.exp(ci.loc[t, 0]), or_ci_high=np.exp(ci.loc[t, 1]),
                             n_obs=int(res.nobs)))
        print(f"  {name}: n={int(res.nobs):,}  pseudo-R2={res.prsquared:.3f}")
    r = pd.DataFrame(rows)
    r.to_csv(OUT / "logistic_results.csv", index=False)
    con = connect()
    with con, con.cursor() as cur:
        cur.execute("TRUNCATE eval_logistic_results")
        copy_df(con, r.round(8), "eval_logistic_results",
                ["model_name", "term", "coef", "std_err", "p_value", "odds_ratio", "or_ci_low", "or_ci_high", "n_obs"])
    con.close()

    core_r = r[r.term.isin(core)]
    fig, ax = plt.subplots(figsize=(7, 3.6))
    ys = {"A_proposal_core": 0.3, "B_adjusted": 0.1, "C_region_FE": -0.1, "D_district_FE": -0.3}
    for i, term in enumerate(core):
        for m, off in ys.items():
            row = core_r[(core_r.term == term) & (core_r.model_name == m)].iloc[0]
            ax.errorbar(row.odds_ratio, i + off, xerr=[[row.odds_ratio - row.or_ci_low], [row.or_ci_high - row.odds_ratio]],
                        fmt="o", color={"A_proposal_core": "#9aa5b1", "B_adjusted": "#4a7fb5",
                                        "C_region_FE": "#e08e45", "D_district_FE": "#2a9d8f"}[m],
                        label=m if i == 0 else None, ms=4)
    ax.axvline(1, color="k", lw=.8, ls="--")
    ax.set_yticks(range(len(core))); ax.set_yticklabels(["Monitoring visits (Q1)", "Evaluations done (Q1)", "Supervisor rating (Q1)"])
    ax.set_xlabel("Odds ratio of completing service (95% CI)"); ax.legend(fontsize=7, frameon=False)
    ax.set_title("SYNTHETIC DATA: odds ratios by specification", fontsize=9)
    fig.tight_layout(); fig.savefig(OUT / "fig_odds_ratios.png", dpi=150); plt.close(fig)
    return r


# ------------------------------------------------------------------ early-warning features
def build_district_panel():
    L = query("""SELECT l.district_id, rp.period_no AS t, l.indicator_id, l.achievement_rate
                 FROM indicator_performance_logs l JOIN reporting_periods rp USING (reporting_period)""")
    w = L.pivot_table(index=["district_id", "t"], columns="indicator_id", values="achievement_rate").reset_index()
    w.columns.name = None
    core = ["IND02", "IND03", "IND04", "IND05", "IND06"]
    w["composite"] = w[core].clip(upper=100).mean(axis=1)
    w = w.sort_values(["district_id", "t"])
    g = w.groupby("district_id")["composite"]
    w["comp_lag1"] = g.shift(1); w["comp_lag2"] = g.shift(2)
    w["comp_roll3"] = g.transform(lambda s: s.rolling(3).mean())
    w["comp_delta2"] = w["composite"] - w["comp_lag2"]
    # forward label: mean composite over the next H months below the threshold
    fwd = sum(g.shift(-h) for h in range(1, RISK_HORIZON + 1)) / RISK_HORIZON
    w["future_composite"] = fwd
    w["at_risk"] = np.where(fwd.isna(), np.nan, (fwd < RISK_COMPOSITE_THRESHOLD).astype(float))
    st = query("""SELECT po.district_id, count(*) AS n_posted, count(DISTINCT po.agency_id) AS n_agencies,
                  avg((ua.category='Public')::int) AS share_public, avg((ua.category='Private')::int) AS share_private
                  FROM postings po JOIN user_agencies ua USING (agency_id) GROUP BY po.district_id""")
    st["personnel_per_agency"] = st.n_posted.astype(float) / st.n_agencies.astype(float)
    st[["share_public", "share_private"]] = st[["share_public", "share_private"]].astype(float)
    w = w.merge(st[["district_id", "n_posted", "personnel_per_agency", "share_public", "share_private"]], on="district_id")
    w["n_posted"] = w["n_posted"].astype(float)
    return w


FEATURES = ["IND01", "IND02", "IND03", "IND04", "IND05", "IND06", "composite", "comp_lag1", "comp_lag2",
            "comp_roll3", "comp_delta2", "n_posted", "personnel_per_agency", "share_public", "share_private"]


def eval_scores(y, s, thr=0.5):
    pred = (s >= thr).astype(int)
    return dict(pr_auc=average_precision_score(y, s), roc_auc=roc_auc_score(y, s),
                precision=precision_score(y, pred, zero_division=0), recall=recall_score(y, pred),
                f1=f1_score(y, pred), brier=brier_score_loss(y, s), prevalence=float(np.mean(y)))


def early_warning():
    w = build_district_panel()
    feat_ok = w.dropna(subset=["comp_lag2", "comp_roll3"])
    lab = feat_ok.dropna(subset=["at_risk"])
    train = lab[lab.t <= TRAIN_LAST_PERIOD]
    test = lab[lab.t >= TEST_FIRST_PERIOD]
    print(f"  panel rows labelled={len(lab)}  train(t<={TRAIN_LAST_PERIOD})={len(train)}  test(t>={TEST_FIRST_PERIOD})={len(test)}")
    print(f"  at-risk prevalence: train={train.at_risk.mean():.2f}  test={test.at_risk.mean():.2f}")
    Xtr, ytr, Xte, yte = train[FEATURES], train.at_risk.astype(int), test[FEATURES], test.at_risk.astype(int)

    rf = RandomForestClassifier(n_estimators=300, random_state=SEED, class_weight="balanced",
                                min_samples_leaf=3, n_jobs=-1).fit(Xtr, ytr)
    p_rf = rf.predict_proba(Xte)[:, 1]
    lr = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, class_weight="balanced")).fit(Xtr, ytr)
    p_lr = lr.predict_proba(Xte)[:, 1]
    p_naive = 1 - test["composite"].to_numpy() / 100          # persistence rule: low current composite => risk
    res = {"random_forest": eval_scores(yte, p_rf), "logistic_baseline": eval_scores(yte, p_lr),
           "persistence_baseline": eval_scores(yte, p_naive, thr=1 - RISK_COMPOSITE_THRESHOLD / 100)}
    k = int(round(0.15 * test[test.t == test.t.max()].shape[0]))       # top-k alert list per period
    hits = []
    for tt, g in test.assign(p=p_rf).groupby("t"):
        top = g.nlargest(k, "p")
        hits.append(top.at_risk.mean())
    res["random_forest"]["precision_top15pct_per_period"] = float(np.mean(hits))
    res["random_forest"]["confusion_matrix_0.5"] = confusion_matrix(yte, (p_rf >= .5).astype(int)).tolist()
    print(pd.DataFrame({k_: {m: v for m, v in d.items() if not isinstance(v, list)} for k_, d in res.items()}).round(3).to_string())
    json.dump(res, open(OUT / "early_warning_metrics.json", "w"), indent=2)

    # calibration table
    cal = pd.DataFrame({"p": p_rf, "y": yte.to_numpy()})
    cal["bin"] = pd.cut(cal.p, [0, .2, .4, .6, .8, 1.0], include_lowest=True)
    cal.groupby("bin", observed=True).agg(n=("y", "size"), mean_pred=("p", "mean"), observed_rate=("y", "mean")) \
        .round(3).to_csv(OUT / "rf_calibration.csv")
    imp = pd.Series(rf.feature_importances_, index=FEATURES).sort_values(ascending=False)
    imp.round(4).to_csv(OUT / "rf_feature_importance.csv", header=["importance"])

    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8))
    for nm, s in (("Random Forest", p_rf), ("Logistic baseline", p_lr), ("Persistence rule", p_naive)):
        pr, rc, _ = precision_recall_curve(yte, s); ax[0].plot(rc, pr, label=nm)
    ax[0].axhline(yte.mean(), color="grey", ls=":", lw=1); ax[0].set_xlabel("Recall"); ax[0].set_ylabel("Precision")
    ax[0].set_title("Out-of-time precision-recall (SYNTHETIC)", fontsize=9); ax[0].legend(fontsize=7, frameon=False)
    imp.head(10)[::-1].plot.barh(ax=ax[1], color="#4a7fb5"); ax[1].set_title("RF feature importance", fontsize=9)
    fig.tight_layout(); fig.savefig(OUT / "fig_early_warning.png", dpi=150); plt.close(fig)

    # ---- persist scores: (1) out-of-time test scores, (2) final model on all labelled data -> latest period
    def cls(p): return np.where(p >= 0.6, "HIGH", np.where(p >= 0.3, "MEDIUM", "LOW"))
    periods = query("SELECT period_no, reporting_period FROM reporting_periods").set_index("period_no")["reporting_period"]
    s1 = test[["district_id", "t"]].assign(risk_probability=p_rf, model_version="rf_temporal_v1")
    final = RandomForestClassifier(n_estimators=300, random_state=SEED, class_weight="balanced",
                                   min_samples_leaf=3, n_jobs=-1).fit(lab[FEATURES], lab.at_risk.astype(int))
    latest = feat_ok[feat_ok.t == N_PERIODS]
    s2 = latest[["district_id", "t"]].assign(risk_probability=final.predict_proba(latest[FEATURES])[:, 1],
                                             model_version="rf_final_v1")
    sc = pd.concat([s1, s2], ignore_index=True)
    sc["scored_period"] = sc.t.map(periods); sc["risk_class"] = cls(sc.risk_probability.to_numpy())
    sc["risk_probability"] = sc.risk_probability.round(4)
    con = connect()
    with con, con.cursor() as cur:
        cur.execute("TRUNCATE district_risk_scores")
        copy_df(con, sc, "district_risk_scores",
                ["district_id", "scored_period", "risk_probability", "risk_class", "model_version"])
    con.close()
    w.to_csv(OUT / "district_panel_features.csv", index=False)
    print("  latest-period risk classes:", s2.assign(c=cls(s2.risk_probability.to_numpy())).c.value_counts().to_dict())
    return res


def readiness():
    """Models need enough APPROVED / CERTIFIED input data; otherwise skip with a clear message."""
    n_out = query("SELECT count(*) AS n, count(*) FILTER (WHERE completed_service) AS c FROM service_outcomes").iloc[0]
    n_per = int(query("SELECT count(DISTINCT reporting_period) AS n FROM indicator_performance_logs").n[0])
    n_q1 = int(query("""SELECT count(DISTINCT pm.personnel_id) AS n FROM personnel_monthly pm
                        JOIN reporting_periods rp USING (reporting_period) WHERE rp.period_no <= 3""").n[0])
    logit_ok = int(n_out.n) >= 500 and 0 < int(n_out.c) < int(n_out.n) and n_q1 >= 500
    ew_ok = n_per >= 10
    return logit_ok, ew_ok, dict(certified_outcomes=int(n_out.n), completers=int(n_out.c),
                                 persons_with_q1_data=n_q1, periods_with_indicators=n_per)


def main():
    logit_ok, ew_ok, info = readiness()
    print("Data readiness:", info)
    if logit_ok:
        print("Logistic regression (completion ~ Q1 monitoring)")
        r = logistic_eval()
        print(r[r.term.isin(["visits_q1", "evals_q1", "rating_q1"])][["model_name", "term", "odds_ratio", "or_ci_low", "or_ci_high", "p_value"]].round(3).to_string(index=False))
    else:
        print("SKIPPED logistic regression: needs >=500 certified outcomes (both completed and not) with approved Q1 returns.")
    if ew_ok:
        print("Early-warning model")
        early_warning()
    else:
        print("SKIPPED early-warning model: needs >=10 reporting periods of approved indicator data.")


if __name__ == "__main__":
    main()
