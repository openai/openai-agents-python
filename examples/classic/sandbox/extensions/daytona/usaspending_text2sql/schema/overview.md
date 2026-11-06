## Database: usaspending.db

NASA federal spending data from USAspending.gov. Each row is a single spending transaction (obligation or de-obligation) on a federal award.

### Table: spending

One row per transaction. Multiple transactions can share the same `award_id` (an award's initial obligation plus subsequent modifications, amendments, and de-obligations).

**Key columns:**
- `award_id` — unique award identifier (many transactions share one award_id)
- `award_piid_fain` — human-readable contract number (PIID) or assistance award number (FAIN)
- `parent_award_piid` — parent IDV contract number (links task orders to their contract vehicle; contracts only)
- `award_type` — 'contract', 'grant', 'idv', or 'other'
- `action_date` — date of this transaction (YYYY-MM-DD)
- `fiscal_year` — federal fiscal year (Oct-Sep; FY2024 = Oct 2023 - Sep 2024)
- `federal_action_obligation` — dollar amount of this transaction (can be negative for de-obligations)
- `total_obligation` — cumulative obligation for the entire award at time of this transaction
- `base_and_all_options_value` — total potential ceiling value including unexercised options (contracts only)
- `recipient_name` — who received the funds
- `recipient_parent_name` — parent company (e.g., subsidiaries roll up; contracts only)
- `recipient_state`, `recipient_city`, `recipient_country` — recipient location
- `awarding_office` — NASA center/office that made the award (e.g., 'GODDARD SPACE FLIGHT CENTER', 'JET PROPULSION LABORATORY')
- `funding_office` — NASA center/office providing funding (often same as awarding)
- `naics_code`, `naics_description` — industry classification (primarily for contracts)
- `psc_code`, `psc_description` — product/service classification
- `place_of_performance_state`, `place_of_performance_city` — where work is performed
- `period_of_perf_start`, `period_of_perf_end` — award period of performance dates (YYYY-MM-DD)
- `extent_competed` — competition level: 'Full and Open Competition', 'Not Competed', etc. (contracts only)
- `type_of_set_aside` — small business set-aside type: '8(a)', 'HUBZone', 'SDVOSB', etc. (contracts only)
- `number_of_offers` — number of offers received (contracts only)
- `contract_pricing_type` — pricing structure: 'Firm Fixed Price', 'Cost Plus', etc. (contracts only)
- `business_types` — recipient type for assistance: nonprofit, university, state govt, etc. (grants only)
- `description` — free-text description of the transaction

### Common query patterns

```sql
-- Total spending by fiscal year
SELECT fiscal_year, SUM(federal_action_obligation) AS total
FROM spending GROUP BY fiscal_year ORDER BY fiscal_year;

-- Top recipients (roll up by parent company)
SELECT COALESCE(NULLIF(recipient_parent_name, ''), recipient_name) AS entity,
       SUM(federal_action_obligation) AS total
FROM spending GROUP BY entity ORDER BY total DESC LIMIT 10;

-- Spending by award type
SELECT award_type, COUNT(*), SUM(federal_action_obligation) AS total
FROM spending GROUP BY award_type;

-- Competitive vs sole-source contracts
SELECT extent_competed, COUNT(DISTINCT award_id) AS awards,
       SUM(federal_action_obligation) AS total
FROM spending WHERE award_type = 'contract'
GROUP BY extent_competed ORDER BY total DESC;

-- Spending by NASA center
SELECT awarding_office, SUM(federal_action_obligation) AS total
FROM spending GROUP BY awarding_office ORDER BY total DESC;
```
