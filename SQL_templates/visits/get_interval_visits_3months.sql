-- ============================================================================
-- 15-Minute Interval Visit Count Extraction Query (Past 3 Months)
-- ============================================================================
-- Purpose: Count visits per 15-min interval for outlet {selected_lounge}
-- Date Range: {start_datetime} to {end_datetime} (exclusive)
-- Output: Time series of 15-min visit counts with outlet metadata
-- ============================================================================

WITH deduplicate_visits AS (
    SELECT
        VISIT_ID,
        OUTLET_ID AS outlet_code,
        EXPERIENCE_DATE AS visit_start,
        MEMBER_COUNT,
        GUEST_COUNT
    FROM STAGING_QUALITY.PPASS.SQ_PPASS__CONSOLIDATED_VISITS
    WHERE EXPERIENCE_DATE >= '{start_datetime}'::timestamp
      AND EXPERIENCE_DATE < '{end_datetime}'::timestamp
      AND (
          experience_date < '2023-06-01'::date
          OR payment_status <> 'NA'
      )
      AND OUTLET_ID = '{selected_lounge}'
    GROUP BY VISIT_ID, OUTLET_ID, EXPERIENCE_DATE, MEMBER_COUNT, GUEST_COUNT
),
visit AS (
    SELECT
        outlet_code,
        visit_start,
        (MEMBER_COUNT + GUEST_COUNT) AS total_visits,
        VISIT_ID
    FROM deduplicate_visits
),
time_window AS (
    -- Generate continuous 15-minute intervals for the analysis period
    SELECT
        TIMESTAMPADD(MINUTE, value * 15, '{start_datetime}'::timestamp) AS window_start,
        TIMESTAMPADD(MINUTE, (value + 1) * 15, '{start_datetime}'::timestamp) AS window_end
    FROM (
        SELECT
            '{start_datetime}'::timestamp,
            ARRAY_GENERATE_RANGE(
                0, 4 * 24 * TIMESTAMPDIFF(DAY, '{start_datetime}'::timestamp, '{end_datetime}'::timestamp) + 4 * 24
            ) AS int_array
    ), TABLE(FLATTEN(int_array))
),
outlet_info AS (
    SELECT
        LEGACYCODE AS outlet_code,
        LOCATION_CODE AS airport_code,
        LOCATION_TERMINAL AS terminal
    FROM STAGING_QUALITY.PRODUCT_CATALOGUE.SQ_PRODUCT_CATALOGUE__OUTLETS
    WHERE LEGACYCODE = '{selected_lounge}'
    QUALIFY ROW_NUMBER() OVER (PARTITION BY LEGACYCODE ORDER BY __CREATED_AT DESC) = 1
)
SELECT
    t1.window_start AS visit_slot,
    '{selected_lounge}' AS outlet_code,
    oi.airport_code,
    oi.terminal,
    COALESCE(SUM(t2.total_visits), 0) AS total_visits,
    COUNT(DISTINCT t2.VISIT_ID) AS visit_ref_count
FROM time_window t1
CROSS JOIN outlet_info oi
LEFT JOIN visit t2
    ON t2.visit_start >= t1.window_start
    AND t2.visit_start < t1.window_end
WHERE t1.window_start >= '{start_datetime}'::timestamp
  AND t1.window_start < '{end_datetime}'::timestamp
GROUP BY t1.window_start, oi.airport_code, oi.terminal
ORDER BY t1.window_start
