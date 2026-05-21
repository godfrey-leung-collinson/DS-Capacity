-- ============================================================================
-- 15-Minute Interval Visit Count Extraction Query (Past 3 Months)
-- Using Pre-Aggregated Table
-- ============================================================================
-- Purpose: Count visits per 15-min interval for outlet {selected_lounge}
-- Date Range: {start_datetime} to {end_datetime} (exclusive)
-- Output: Time series of 15-min visit counts with outlet metadata
-- Source: ANALYTICS.DATAMART.AGG__VISITS_AGG_15MIN
-- Note: The aggregated table already contains continuous 15-min intervals
-- ============================================================================

SELECT
    VISIT_INTERVAL AS visit_slot,
    OUTLET_ID AS outlet_code,
    AIRPORT_CODE AS airport_code,
    TERMINAL,
    TOTAL_VISITS AS total_visits
    -- Note: visit_ref_count not available in aggregated table
FROM ANALYTICS.DATAMART.AGG__VISITS_AGG_15MIN
WHERE OUTLET_ID = '{selected_lounge}'
  AND VISIT_INTERVAL >= '{start_datetime}'::timestamp
  AND VISIT_INTERVAL < '{end_datetime}'::timestamp
ORDER BY VISIT_INTERVAL


