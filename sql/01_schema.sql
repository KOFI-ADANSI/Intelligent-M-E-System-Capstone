-- Intelligent M&E System for National Service Personnel Performance (Ghana)
-- PostgreSQL schema. Idempotent: drops and recreates everything.
-- Tables marked [SIM] are populated with SYNTHETIC data by src/simulate_me.py
-- because the posting sheet contains no monitoring / attendance / outcome records.

DROP SCHEMA IF EXISTS me CASCADE;
CREATE SCHEMA me;
SET search_path = me, public;

-- ---------- Reference data (from posting sheet "Region District Lookup") ----------
CREATE TABLE regions_districts (
    district_id           SERIAL PRIMARY KEY,
    region_name           VARCHAR(100) NOT NULL,
    district_name         VARCHAR(100) NOT NULL UNIQUE,
    me_officer_in_charge  VARCHAR(150)           -- left NULL: not in source data
);
CREATE INDEX ix_rd_region ON regions_districts(region_name);

CREATE TABLE user_agencies (
    agency_id    SERIAL PRIMARY KEY,
    agency_name  VARCHAR(150) NOT NULL UNIQUE,
    category     VARCHAR(10)  NOT NULL CHECK (category IN ('Public','Private','Quasi'))
);

-- ---------- Personnel and postings (real columns from the sheet) ----------
-- Privacy by design: Ghana Card number is stored only as a SHA-256 hash and
-- the phone number is not loaded at all (not needed for M&E analytics).
CREATE TABLE personnel (
    personnel_id          VARCHAR(20) PRIMARY KEY,
    surname               VARCHAR(100) NOT NULL,
    other_names           VARCHAR(150) NOT NULL,
    gender                VARCHAR(10)  NOT NULL CHECK (gender IN ('Male','Female')),
    date_of_birth         DATE NOT NULL,
    age_at_posting        SMALLINT NOT NULL CHECK (age_at_posting BETWEEN 18 AND 60),
    ghana_card_hash       CHAR(64) NOT NULL UNIQUE,
    residential_region    VARCHAR(100) NOT NULL,
    residential_district  VARCHAR(100) NOT NULL,
    institution_attended  VARCHAR(150) NOT NULL,
    course_of_study       VARCHAR(100) NOT NULL,
    qualification         VARCHAR(20)  NOT NULL CHECK (qualification IN ('Degree','HND','Diploma','Masters'))
);

CREATE TABLE postings (
    posting_id        SERIAL PRIMARY KEY,
    personnel_id      VARCHAR(20) NOT NULL UNIQUE REFERENCES personnel(personnel_id),
    agency_id         INT NOT NULL REFERENCES user_agencies(agency_id),
    district_id       INT NOT NULL REFERENCES regions_districts(district_id),
    choice_1_region   VARCHAR(100) NOT NULL,
    choice_2_region   VARCHAR(100) NOT NULL,
    choice_3_region   VARCHAR(100) NOT NULL,
    choice_rank_met   SMALLINT CHECK (choice_rank_met BETWEEN 1 AND 3),  -- NULL = posted outside all 3 choices
    service_cycle     VARCHAR(10) NOT NULL DEFAULT '2026/27'
);
CREATE INDEX ix_post_district ON postings(district_id);
CREATE INDEX ix_post_agency   ON postings(agency_id);

-- ---------- M&E framework ----------
CREATE TABLE reporting_periods (
    reporting_period  DATE PRIMARY KEY,         -- first day of month
    period_no         SMALLINT NOT NULL UNIQUE, -- 1..12 within the service cycle
    is_open           BOOLEAN NOT NULL DEFAULT TRUE,   -- HQ closes a period to stop new entries
    agency_deadline   DATE
);

CREATE TABLE me_indicators (
    indicator_id         VARCHAR(20) PRIMARY KEY,
    indicator_name       VARCHAR(255) NOT NULL,
    result_level         VARCHAR(30) CHECK (result_level IN ('INPUT','ACTIVITY','OUTPUT','OUTCOME','IMPACT')),
    unit                 VARCHAR(30),
    baseline_val         NUMERIC(10,2),
    target_val           NUMERIC(10,2) NOT NULL,
    green_threshold      NUMERIC(5,2) NOT NULL DEFAULT 95,   -- configurable per indicator
    yellow_threshold     NUMERIC(5,2) NOT NULL DEFAULT 80,
    data_source          VARCHAR(150),
    reporting_frequency  VARCHAR(30),
    measurement_method   TEXT,
    responsible_officer  VARCHAR(150)
);

-- ---------- Users and roles ----------
-- Four input levels: AGENCY (user agency at a posting site), DISTRICT, REGION, HQ.
CREATE TABLE app_users (
    user_id        SERIAL PRIMARY KEY,
    username       VARCHAR(60) NOT NULL UNIQUE,
    full_name      VARCHAR(150) NOT NULL,
    password_hash  VARCHAR(200) NOT NULL,
    role           VARCHAR(10) NOT NULL CHECK (role IN ('AGENCY','DISTRICT','REGION','HQ')),
    agency_id      INT REFERENCES user_agencies(agency_id),     -- AGENCY: site = agency + district
    district_id    INT REFERENCES regions_districts(district_id), -- AGENCY and DISTRICT
    region_name    VARCHAR(100),                                  -- REGION
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ DEFAULT now(),
    CHECK ( (role='AGENCY'   AND agency_id IS NOT NULL AND district_id IS NOT NULL)
         OR (role='DISTRICT' AND district_id IS NOT NULL)
         OR (role='REGION'   AND region_name IS NOT NULL)
         OR (role='HQ') )
);

-- ---------- INPUT TABLES (entered through the web app / CSV upload) ----------
-- Review chain for returns and evaluations:
--   DRAFT -> SUBMITTED (agency) -> DISTRICT_VERIFIED (district office) -> APPROVED (regional office)
--   any reviewer may send back: REJECTED (with reason); agency edits and resubmits.
-- Only APPROVED records feed the indicators and models.

CREATE TABLE attendance_returns (
    return_id          BIGSERIAL PRIMARY KEY,
    personnel_id       VARCHAR(20) NOT NULL REFERENCES personnel(personnel_id),
    reporting_period   DATE NOT NULL REFERENCES reporting_periods(reporting_period),
    reported_for_duty  BOOLEAN NOT NULL,
    days_expected      SMALLINT NOT NULL CHECK (days_expected BETWEEN 0 AND 31),
    days_present       SMALLINT NOT NULL CHECK (days_present BETWEEN 0 AND 31),
    status             VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
                       CHECK (status IN ('DRAFT','SUBMITTED','DISTRICT_VERIFIED','APPROVED','REJECTED')),
    submitted_by INT REFERENCES app_users(user_id), submitted_at TIMESTAMPTZ,
    verified_by  INT REFERENCES app_users(user_id), verified_at  TIMESTAMPTZ,
    approved_by  INT REFERENCES app_users(user_id), approved_at  TIMESTAMPTZ,
    reject_reason TEXT,
    UNIQUE (personnel_id, reporting_period),
    CHECK (days_present <= days_expected),
    CHECK (reported_for_duty OR days_present = 0)
);
CREATE INDEX ix_att_status ON attendance_returns(status, reporting_period);

CREATE TABLE supervisor_evaluations (
    eval_id            BIGSERIAL PRIMARY KEY,
    personnel_id       VARCHAR(20) NOT NULL REFERENCES personnel(personnel_id),
    reporting_period   DATE NOT NULL REFERENCES reporting_periods(reporting_period),
    rating             NUMERIC(3,2) NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comments           TEXT,
    status             VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
                       CHECK (status IN ('DRAFT','SUBMITTED','DISTRICT_VERIFIED','APPROVED','REJECTED')),
    submitted_by INT REFERENCES app_users(user_id), submitted_at TIMESTAMPTZ,
    verified_by  INT REFERENCES app_users(user_id), verified_at  TIMESTAMPTZ,
    approved_by  INT REFERENCES app_users(user_id), approved_at  TIMESTAMPTZ,
    reject_reason TEXT,
    UNIQUE (personnel_id, reporting_period)
);
CREATE INDEX ix_eval_status ON supervisor_evaluations(status, reporting_period);

-- A monitoring visit is made to a posting site (agency at a district) and covers one or more personnel.
-- DISTRICT-level visits: SUBMITTED -> APPROVED by regional office.  REGION/HQ visits: APPROVED on entry.
CREATE TABLE monitoring_visits (
    visit_id           BIGSERIAL PRIMARY KEY,
    agency_id          INT NOT NULL REFERENCES user_agencies(agency_id),
    district_id        INT NOT NULL REFERENCES regions_districts(district_id),
    reporting_period   DATE NOT NULL REFERENCES reporting_periods(reporting_period),
    visit_date         DATE NOT NULL,
    visit_level        VARCHAR(10) NOT NULL CHECK (visit_level IN ('DISTRICT','REGION','HQ','LEGACY')),
    officer_name       VARCHAR(150),
    findings           TEXT,
    status             VARCHAR(20) NOT NULL DEFAULT 'SUBMITTED'
                       CHECK (status IN ('SUBMITTED','APPROVED','REJECTED')),
    submitted_by INT REFERENCES app_users(user_id), submitted_at TIMESTAMPTZ DEFAULT now(),
    approved_by  INT REFERENCES app_users(user_id), approved_at  TIMESTAMPTZ,
    reject_reason TEXT
);
CREATE INDEX ix_visit_site ON monitoring_visits(district_id, agency_id, reporting_period);

CREATE TABLE monitoring_visit_personnel (
    visit_id      BIGINT NOT NULL REFERENCES monitoring_visits(visit_id) ON DELETE CASCADE,
    personnel_id  VARCHAR(20) NOT NULL REFERENCES personnel(personnel_id),
    PRIMARY KEY (visit_id, personnel_id)
);
CREATE INDEX ix_mvp_person ON monitoring_visit_personnel(personnel_id);

-- Service outcome. Chain: SUBMITTED (agency) -> DISTRICT_VERIFIED -> APPROVED (region) -> CERTIFIED (HQ).
CREATE TABLE service_completion (
    personnel_id    VARCHAR(20) PRIMARY KEY REFERENCES personnel(personnel_id),
    outcome         VARCHAR(20) NOT NULL CHECK (outcome IN ('COMPLETED','DISCONTINUED','ABSCONDED','DECEASED')),
    exit_period_no  SMALLINT,                     -- period in which service ended early; NULL when COMPLETED
    outcome_date    DATE NOT NULL,
    reason          TEXT,
    status          VARCHAR(20) NOT NULL DEFAULT 'SUBMITTED'
                    CHECK (status IN ('SUBMITTED','DISTRICT_VERIFIED','APPROVED','CERTIFIED','REJECTED')),
    submitted_by INT REFERENCES app_users(user_id), submitted_at TIMESTAMPTZ DEFAULT now(),
    verified_by  INT REFERENCES app_users(user_id), verified_at  TIMESTAMPTZ,
    approved_by  INT REFERENCES app_users(user_id), approved_at  TIMESTAMPTZ,
    certified_by INT REFERENCES app_users(user_id), certified_at TIMESTAMPTZ,
    reject_reason TEXT,
    CHECK ((outcome = 'COMPLETED') = (exit_period_no IS NULL))
);

CREATE TABLE audit_log (
    audit_id    BIGSERIAL PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id     INT REFERENCES app_users(user_id),
    action      VARCHAR(40) NOT NULL,
    table_name  VARCHAR(60),
    detail      TEXT
);

-- Analytics-facing views: ONLY approved / certified input counts.
CREATE VIEW personnel_monthly AS
SELECT a.personnel_id, a.reporting_period, a.reported_for_duty,
       CASE WHEN a.days_expected > 0 THEN ROUND(100.0 * a.days_present / a.days_expected, 2) END AS attendance_rate,
       COALESCE(v.n, 0)::smallint AS monitoring_visits,
       (e.eval_id IS NOT NULL) AS evaluation_done,
       e.rating AS supervisor_rating
FROM attendance_returns a
LEFT JOIN supervisor_evaluations e
       ON e.personnel_id = a.personnel_id AND e.reporting_period = a.reporting_period AND e.status = 'APPROVED'
LEFT JOIN (SELECT vp.personnel_id, mv.reporting_period, count(*) AS n
           FROM monitoring_visits mv JOIN monitoring_visit_personnel vp USING (visit_id)
           WHERE mv.status = 'APPROVED' GROUP BY 1, 2) v
       ON v.personnel_id = a.personnel_id AND v.reporting_period = a.reporting_period
WHERE a.status = 'APPROVED';

CREATE VIEW service_outcomes AS
SELECT personnel_id, (outcome = 'COMPLETED') AS completed_service, exit_period_no
FROM service_completion WHERE status = 'CERTIFIED';

-- ---------- Indicator results (written by the Python engine) ----------
CREATE TABLE indicator_performance_logs (
    log_id            SERIAL PRIMARY KEY,
    indicator_id      VARCHAR(20) NOT NULL REFERENCES me_indicators(indicator_id),
    district_id       INT NOT NULL REFERENCES regions_districts(district_id),
    reporting_period  DATE NOT NULL REFERENCES reporting_periods(reporting_period),
    actual_val        NUMERIC(10,2) NOT NULL,
    achievement_rate  NUMERIC(8,2),
    status_flag       VARCHAR(20) CHECK (status_flag IN ('GREEN','YELLOW','RED')),
    remarks           TEXT,
    UNIQUE (indicator_id, district_id, reporting_period)
);

-- ---------- Analytics outputs ----------
CREATE TABLE eval_logistic_results (
    run_id        SERIAL,
    model_name    VARCHAR(50) NOT NULL,
    term          VARCHAR(80) NOT NULL,
    coef          NUMERIC(10,4),
    std_err       NUMERIC(10,4),
    p_value       NUMERIC(12,8),
    odds_ratio    NUMERIC(10,4),
    or_ci_low     NUMERIC(10,4),
    or_ci_high    NUMERIC(10,4),
    n_obs         INT,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE district_risk_scores (
    district_id       INT NOT NULL REFERENCES regions_districts(district_id),
    scored_period     DATE NOT NULL,
    risk_probability  NUMERIC(6,4) NOT NULL,
    risk_class        VARCHAR(10) NOT NULL CHECK (risk_class IN ('LOW','MEDIUM','HIGH')),
    model_version     VARCHAR(40) NOT NULL,
    PRIMARY KEY (district_id, scored_period, model_version)
);

CREATE TABLE data_validation_log (
    check_id     SERIAL PRIMARY KEY,
    run_at       TIMESTAMPTZ DEFAULT now(),
    check_name   VARCHAR(100) NOT NULL,
    records_checked INT,
    issues_found INT NOT NULL,
    detail       TEXT
);
