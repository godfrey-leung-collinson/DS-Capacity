-- ============================================================================
-- 15-Minute Interval Visit Count Extraction Query
-- ============================================================================
-- Purpose: Count visits per 15-min interval and outlet
-- Date Range: {start_datetime} to {end_datetime} (exclusive)
-- Output: Time series of 15-min visit counts with outlet metadata (non-null visits only)
-- ============================================================================

WITH deduplicate_visits AS (
    SELECT
        VISIT_ID,
        --- TEMP, ADHOC FIX: ensure all outlet codes are upper case (TO check with data engineers why such issue happen)
        UPPER(OUTLET_ID) AS outlet_code,
        EXPERIENCE_DATE AS visit_start,
--        SUM(MEMBER_COUNT + GUEST_COUNT) AS total_visits,
        MEMBER_COUNT,
        GUEST_COUNT
    FROM STAGING_QUALITY.PPASS.SQ_PPASS__CONSOLIDATED_VISITS
    WHERE EXPERIENCE_DATE >= '{start_datetime}'::timestamp
      AND EXPERIENCE_DATE < '{end_datetime}'::timestamp
      AND (
          experience_date < '2023-06-01'::date
          OR payment_status <> 'NA'
      )
    GROUP BY VISIT_ID, OUTLET_ID, EXPERIENCE_DATE, MEMBER_COUNT, GUEST_COUNT
),
visit AS (
    SELECT
        outlet_code,
        visit_start,
        (MEMBER_COUNT + GUEST_COUNT) AS total_visits,
        VISIT_ID
    FROM deduplicate_visits
)
SELECT
    outlet_code,
    TIME_SLICE(
        visit_start, 15, 'MINUTE'
    ) AS visit_slot,
    COALESCE(SUM(total_visits), 0) AS total_visits,
    COUNT(DISTINCT VISIT_ID) AS visit_ref_count
FROM visit
GROUP BY outlet_code, visit_slot
ORDER BY visit_slot, outlet_code
