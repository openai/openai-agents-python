# spending

One row per prime award transaction from NASA. Each row represents a financial action — an initial obligation, modification, amendment, or de-obligation on a federal award.

## Columns

| Column | Type | Description |
|--------|------|-------------|
| rowid | INTEGER PK | Auto-increment row identifier |
| award_id | TEXT | Unique award identifier. Multiple rows share the same award_id when an award has multiple transactions |
| award_piid_fain | TEXT | Human-readable award number: PIID for contracts (e.g., 'NNJ13ZBG001'), FAIN for assistance |
| parent_award_piid | TEXT | Parent IDV contract number. Links task/delivery orders to their parent contract vehicle (contracts only) |
| award_type | TEXT | Category: 'contract', 'grant', 'idv', or 'other' |
| description | TEXT | Free-text description of the transaction or award purpose |
| action_date | TEXT | Date of this transaction (ISO 8601: YYYY-MM-DD) |
| fiscal_year | INTEGER | Federal fiscal year (Oct-Sep; FY2024 = Oct 2023 - Sep 2024) |
| federal_action_obligation | REAL | Dollar amount of this specific transaction. Can be negative for de-obligations |
| total_obligation | REAL | Cumulative obligation for the entire award at the time of this transaction |
| base_and_all_options_value | REAL | Total potential ceiling value of the contract including all unexercised options. Contracts only; NULL for grants |
| recipient_name | TEXT | Legal name of the recipient organization |
| recipient_parent_name | TEXT | Parent company name (e.g., subsidiaries like 'Lockheed Martin Space' roll up to 'Lockheed Martin Corporation'). Contracts only; empty for grants |
| recipient_state | TEXT | Two-letter US state code of recipient's address. Empty for foreign recipients |
| recipient_city | TEXT | City of recipient's address |
| recipient_country | TEXT | Country name (e.g., 'UNITED STATES', 'UNITED KINGDOM') |
| awarding_office | TEXT | NASA center/office that made the award (e.g., 'GODDARD SPACE FLIGHT CENTER', 'JET PROPULSION LABORATORY'). Values are uppercase |
| funding_office | TEXT | NASA center/office providing funding (often same as awarding). Values are uppercase |
| naics_code | TEXT | North American Industry Classification System code. Primarily for contracts; may be empty for grants |
| naics_description | TEXT | Human-readable NAICS description |
| psc_code | TEXT | Product/Service Code for contracts, CFDA number for assistance. Different classification systems in the same column |
| psc_description | TEXT | Human-readable description of the PSC (contracts) or CFDA program (assistance) |
| place_of_performance_state | TEXT | State where work is performed. Two-letter codes for contracts, full names for assistance. May differ from recipient_state |
| place_of_performance_city | TEXT | City where work is performed |
| period_of_perf_start | TEXT | Award period of performance start date (YYYY-MM-DD) |
| period_of_perf_end | TEXT | Award period of performance end date (YYYY-MM-DD). This is the current end date and may reflect extensions |
| extent_competed | TEXT | Competition level. Values include 'Full and Open Competition', 'Not Available for Competition', 'Not Competed', etc. Contracts only; empty for grants |
| type_of_set_aside | TEXT | Small business set-aside type. Values include 'Small Business Set-Aside', '8(a) Set-Aside', 'HUBZone Set-Aside', 'Service-Disabled Veteran-Owned Small Business Set-Aside', 'Women-Owned Small Business', etc. Contracts only |
| number_of_offers | INTEGER | Number of offers/bids received. 1 = effectively sole-source even if technically competed. Contracts only; NULL for grants |
| contract_pricing_type | TEXT | Pricing structure: 'Firm Fixed Price', 'Cost Plus Fixed Fee', 'Cost No Fee', 'Time and Materials', etc. Contracts only |
| business_types | TEXT | Recipient organization type for assistance awards: nonprofit, university, state government, tribal, etc. Grants only; empty for contracts |

## Notes

- **Aggregating to award level**: use `GROUP BY award_id` with `SUM(federal_action_obligation)` to get total spending per award. The `total_obligation` column is a snapshot at each transaction and may not reflect the final total.
- **Contract ceiling vs obligation**: `base_and_all_options_value` is the potential maximum; `total_obligation` is what's actually committed. A contract may have $10M obligated against a $500M ceiling.
- **Parent company roll-up**: Use `COALESCE(NULLIF(recipient_parent_name, ''), recipient_name)` to group subsidiaries under their parent. Only populated for contracts.
- **recipient_name** may vary slightly for the same entity across rows (e.g., 'BOEING CO' vs 'THE BOEING COMPANY'). Use `LIKE` or `UPPER()` for fuzzy matching.
- **award_type** is derived from USAspending type codes: A/B/C/D -> 'contract', 02-05 -> 'grant', IDV_* -> 'idv'.
- **federal_action_obligation** can be negative (de-obligations, corrections). Sum them to get net spending.
- **naics_code** and **naics_description** are only populated for contracts; empty for grants/assistance.
- **psc_code** contains Product/Service Codes for contracts and CFDA numbers for assistance awards. **psc_description** contains the corresponding description. These are different classification systems stored in the same column.
- **Contracts-only columns**: `base_and_all_options_value`, `recipient_parent_name`, `parent_award_piid`, `extent_competed`, `type_of_set_aside`, `number_of_offers`, `contract_pricing_type` are only populated for contracts/IDVs.
- **Grants-only columns**: `business_types` is only populated for assistance awards.
