# Intelligent M&E System for National Service Personnel Performance (Ghana)

PostgreSQL + Python + web input app. Monitoring, attendance, evaluation and completion records are
ENTERED by user agencies and the district / regional / HQ offices, reviewed through an approval chain,
and only approved records feed the indicators and models.

## Who enters what
| Role | Enters | Reviews |
|---|---|---|
| AGENCY (supervisor at a posting site) | monthly attendance returns, supervisor ratings (grid or CSV), service-outcome proposals | - |
| DISTRICT officer | district monitoring visits | verifies agency returns and outcomes |
| REGION officer | regional visits (auto-approved) | approves verified returns, outcomes and district visits |
| HQ | HQ visits, period open/close | certifies outcomes, refreshes analytics, audit log |

Chain: DRAFT -> SUBMITTED -> DISTRICT_VERIFIED -> APPROVED (outcomes then CERTIFIED by HQ). Any reviewer can send a batch
back with a reason. Analytics read the views `personnel_monthly` / `service_outcomes`, which contain approved/certified rows only.

## Run
```
.\.venv\Scripts\python.exe -m pip install pandas numpy scikit-learn statsmodels psycopg2-binary matplotlib openpyxl fastapi uvicorn jinja2 itsdangerous python-multipart httpx
# The web app defaults to ns_me_v2 and outputs/credentials_v2.csv.
# NSME_DB_NAME explicitly selects another database; NSME_DB_PASSWORD supplies its password.
.\.venv\Scripts\python.exe src/run_pipeline.py --init            # initializes the pipeline's default database (ns_me)
.\.venv\Scripts\python.exe src/run_pipeline.py --init --demo     # same, plus SYNTHETIC demo records through the same input tables
.\start_app.ps1                                                  # prompts securely; password is not saved
.\.venv\Scripts\python.exe src/run_pipeline.py                   # analytics refresh (HQ can also press the button)
.\.venv\Scripts\python.exe tests/test_workflow.py                # 40 end-to-end checks of every role and rule (app must be running)
```
`--init` refuses to wipe entered records unless `--force` is given, and stops if an existing `me` schema is incomplete. The web app defaults to database `ns_me_v2` and always authenticates against `outputs/credentials_v2.csv`; start it with `.\start_app.ps1`. A matching active database account is still required. Set `NSME_DB_NAME` only when you intentionally need another database; this does not change the credentials file used by the app. Pipeline commands continue to default to `ns_me`. Keep credentials files on the app host: they contain plaintext passwords, so restrict access and never commit or share them.
Start the web app with `.\start_app.ps1`; it securely prompts for the PostgreSQL password each time, keeps it available only while the app is running, and clears it when Uvicorn exits. The password is not saved. For pipeline or migration commands that connect directly to PostgreSQL, set `NSME_DB_PASSWORD` in that PowerShell window first, for example with `$securePassword = Read-Host "PostgreSQL password" -AsSecureString` and `$env:NSME_DB_PASSWORD = [System.Net.NetworkCredential]::new("", $securePassword).Password`. Do not put the password in source control.
`NSME_ALLOW_FUTURE_DATES=0` should be set in production (the demo cycle Nov 2026 - Oct 2027 lies in the future).

## Migrate the legacy database without overwriting it
The v2 app schema is incompatible with the older `ns_me` schema. Do not run `--init` against the legacy database: its schema script drops and recreates `me`. Instead, create a separate database so `ns_me` remains an untouched archive:
If the app is currently running, press Ctrl+C first. Run these commands in the same PowerShell window where `NSME_DB_PASSWORD` is set, and start the app with `.\start_app.ps1` only after migration succeeds.
```
& 'C:\Program Files\PostgreSQL\18\bin\createdb.exe' -h 127.0.0.1 -U postgres ns_me_v2
$env:NSME_DB_NAME = 'ns_me_v2'
.\.venv\Scripts\python.exe src/run_pipeline.py --init --no-users
.\.venv\Scripts\python.exe src/migrate_legacy.py --check
.\.venv\Scripts\python.exe src/migrate_legacy.py
.\start_app.ps1
```
`createdb` prompts for the PostgreSQL password. Keep `NSME_DB_NAME` and `NSME_DB_PASSWORD` set in the same PowerShell window for initialization and migration. The app defaults to `ns_me_v2` and always uses `outputs/credentials_v2.csv`. The migration reads `ns_me` only, imports its approved attendance, evaluations, visits, service outcomes, and status audit history into `ns_me_v2`, and refreshes analytics. It creates new accounts in `outputs/credentials_v2.csv`; keep that plaintext-password file secure. Legacy visits are marked `LEGACY` because their original level was not recorded. Legacy non-completions are mapped to `DISCONTINUED` at the exit-period end, with the inferred mapping noted in the reason. Historical analytics are recalculated from migrated inputs; the original database remains unchanged.

## Uploading postings, personnel and user agencies (HQ)
HQ menu > **Data upload** (`/hq/data`). Nothing is wiped; every file is checked first and you review a summary before anything is saved.
| Upload | Columns | Notes |
|---|---|---|
| User agencies | `Agency Name, Category` (Public / Private / Quasi) | Agencies named in a postings file are added automatically; use this to register one early or fix a category. |
| Personnel and postings | The NSS posting-sheet layout: `Personnel ID, Surname, Other Names, Gender, DATE OF BIRTH, Age (optional), CONTACT (optional), GHANA CARD NUMBER, Residential Region, Residential District, Institution Attended, Course of Study, Qualification, Region of Choice 1-3, Place of Posting, Category, Region Posted To, District Posted To` | `.xlsx` (sheet "Posting Data", optional "Region District Lookup" sheet to add districts) or `.csv`. Templates can be downloaded on the page. |

Rules: existing people are not overwritten unless "update existing" is ticked; a person who already has returns, evaluations, visits or an outcome keeps their posting; Ghana Card numbers are stored only as a SHA-256 hash and phone numbers are never stored; every upload is written to the audit log. Choose "save nothing if any row has a problem" (default) or "save the valid rows". A 20,000-row file loads in about 2 seconds; creating supervisor logins costs about 0.06 s each (password hashing), so a file with thousands of new sites takes minutes. After an upload, press "Refresh indicators & models" on the HQ page so headcounts are recalculated.
`python tests/test_upload.py` tests the whole flow (app must be running; it removes its own test rows).

## Layout
`sql/` schema + views | `src/validate_load.py` checks and loads postings | `src/create_users.py` accounts |
`src/indicator_engine.py` 7 indicators, GREEN/YELLOW/RED | `src/models.py` logistic regression + Random Forest (skip with a message
if there is not enough approved data) | `src/demo_seed.py` synthetic demo data (not part of production) | `app/` web app | `outputs/powerbi/` BI tables.

## Power BI report (HQ)
HQ menu > **Power BI report** (`/hq/powerbi`, also `/powerbi`) combines a live management dashboard with the project report (`outputs/powerbi/National_Service_ME_Dashboard.pbix`) and individual CSV exports. Period and region filters drive national personnel/completion/monitoring/evaluation KPIs, Green/Yellow/Red district counts, regional performance, all available district indicator results, district monitoring and outstanding-evaluation summaries, and historical scorecard trends. District risk scores are shown as model decision-support signals for further investigation only, never as automatic administrative decisions. Access to the hub and downloads is limited to HQ users.

The live dashboard reads current database views; refresh indicators and models through the app's **Refresh Indicators & Models** action to update scorecards and model risk scores. That action also rebuilds the project CSV exports, but does **not** update the PBIX or refresh the connected Power BI Service semantic model. Refresh that remote model separately in Power BI Service or Desktop with access to its workspace. A PBIX whose modification time predates the CSV exports may not contain the latest exported data.

## Notes for the write-up
* Privacy: Ghana Card stored only as SHA-256; phone not loaded; passwords PBKDF2; role scoping on every query; CSRF tokens; audit log.
* Demo results come from synthetic data and recover the simulation parameters (see header of `src/demo_seed.py`); they show the pipeline works, and the figures should be labeled as synthetic because they are not empirical estimates from live administrative records.
* Logistic predictors are measured in months 1-3, exits from month 4 (no mechanical reverse causality); four specifications show confounding and the district-fixed-effect model is the strongest evidence that raw associations are partly driven by district-level differences rather than within-district effects.
* Early warning: purged temporal split, compared with logistic and persistence baselines; output is decision support, not an automatic action.

### Methodological notes for the model write-up
The synthetic-data disclaimer is important, but it needs to be carried through the narrative. The synthetic demo is not a causal estimate of real programme effects; it is a calibration exercise for the data pipeline and modelling workflow. Similarly, the model comparison should not imply that the Random Forest is uniformly superior. On the synthetic data, the parsimonious logistic baseline matched or slightly exceeded the Random Forest on PR-AUC and ROC-AUC while being simpler to interpret, and the Random Forest remained better calibrated and obtained higher precision at fixed recall. This aligns with the simulation design, where the true data-generating process is logistic in form (see `TRUE_BETA` in `src/demo_seed.py`), and it means the RF's advantage would be expected to grow more clearly on real administrative data with non-linear interactions rather than in the synthetic case.

For the temporal validation design, the rolling-window features and the forecast target are built with an explicit purge gap: the model uses prior months to predict outcomes in a later horizon, and months immediately adjacent to the evaluation boundary are excluded from both train and test so that the split does not leak future information. In other words, the rolling summary is computed within each district before the temporal split, and the label uses a future window separated by the specified risk horizon; the write-up should say that the design is a purged split rather than a naive random split.

The feature-importance findings also need careful interpretation. The Random Forest is learning persistence and autocorrelation (for example, recent composite values and their lags) rather than only a structural early-warning signal. That is expected in synthetic data with an AR(1)-plus-drift process, and it is not a sign of a model failure. A real-world interpretation would emphasise that the actionable levers are the operational indicators (monitoring, evaluations, attendance) rather than a purely statistical carry-over term, so any importance ranking should be read as a feature-engineering result rather than a definitive causal explanation.

Calibration is likewise a limitation that must be framed honestly. The model is useful for ranking and triage, but its probabilities should not be read as literal event rates in the mid-to-high risk bins without recalibration. A decision-support tool can still be useful if the model is used to generate an alert list or top-k districts, while the analyst keeps in mind that the calibrated probabilities are biased upward in some ranges, especially in the synthetic setting where class balancing inflates positive predictions.

A key confounding result is the district fixed-effects comparison. The raw association between supervisor rating and service completion is strong, but once district effects are controlled for, the odds ratio attenuates materially, which is exactly what we would expect if some of the unadjusted relationship is picking up district-level differences. The write-up should state this explicitly, e.g. "after controlling for district fixed effects, the association between supervisor rating and service completion attenuates from OR=2.88 to OR=1.76, suggesting that much of the raw association reflects district-level differences rather than a within-district effect of supervisor ratings."

### Objective-by-objective evidence
| Objective | Deliverable | Evidence |
|---|---|---|
| 1. RBM framework | `sql/01_schema.sql` and the `me_indicators` indicator tables | indicator design and definitions in the schema and generated indicator outputs |
| 2. PostgreSQL database | schema, views, and refresh pipeline | `sql/` scripts and the analytics pipeline in `src/run_pipeline.py` |
| 3. Python analytics | indicator engine and outputs | `src/indicator_engine.py` and the generated indicator achievement tables |
| 4. Logistic regression | `src/models.py::logistic_eval` | model coefficients, odds-ratio tables, and the associated figure outputs |
| 5. Random Forest | `src/models.py::early_warning` | model metrics, calibration summaries, and early-warning plots |
| 6. Decision support | dashboard and HQ refresh workflow | the app pages and refresh actions in `app/` plus the Power BI export workflow |

These notes should be used as the narrative baseline for the write-up and to ensure the demonstration is described as synthetic, model-based decision support, and not as causal evidence from observational data.
