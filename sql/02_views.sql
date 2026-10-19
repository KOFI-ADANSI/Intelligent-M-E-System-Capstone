SET search_path = me, public;

-- Proposal section 4.1 achievement-rate query, as a view (live recalculation from raw logs)
CREATE OR REPLACE VIEW vw_indicator_achievement AS
SELECT p.log_id, p.indicator_id, i.indicator_name, i.result_level, p.district_id,
       d.region_name, d.district_name, p.reporting_period, p.actual_val, i.target_val,
       ROUND((p.actual_val / NULLIF(i.target_val,0)) * 100, 2) AS achievement_rate,
       p.status_flag
FROM indicator_performance_logs p
JOIN me_indicators i USING (indicator_id)
JOIN regions_districts d USING (district_id);

-- District x month scorecard: green/yellow/red counts across indicators
CREATE OR REPLACE VIEW vw_district_scorecard AS
SELECT district_id, region_name, district_name, reporting_period,
       count(*) FILTER (WHERE status_flag='GREEN')  AS n_green,
       count(*) FILTER (WHERE status_flag='YELLOW') AS n_yellow,
       count(*) FILTER (WHERE status_flag='RED')    AS n_red,
       ROUND(avg(LEAST(achievement_rate,100)) FILTER (WHERE indicator_id <> 'IND01'),2) AS composite_achievement
FROM vw_indicator_achievement
GROUP BY district_id, region_name, district_name, reporting_period;

-- National KPIs by month
CREATE OR REPLACE VIEW vw_national_kpis AS
SELECT reporting_period,
       count(DISTINCT district_id) FILTER (WHERE composite_achievement >= 95) AS districts_green,
       count(DISTINCT district_id) FILTER (WHERE composite_achievement >= 80 AND composite_achievement < 95) AS districts_yellow,
       count(DISTINCT district_id) FILTER (WHERE composite_achievement < 80)  AS districts_red,
       ROUND(avg(composite_achievement),2) AS mean_composite
FROM vw_district_scorecard GROUP BY reporting_period;

-- Latest early-warning list
CREATE OR REPLACE VIEW vw_district_risk_latest AS
SELECT r.district_id, d.region_name, d.district_name, r.scored_period, r.risk_probability, r.risk_class, r.model_version
FROM district_risk_scores r JOIN regions_districts d USING (district_id)
WHERE r.model_version = 'rf_final_v1';
