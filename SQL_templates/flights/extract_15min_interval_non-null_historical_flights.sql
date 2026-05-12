WITH selected_flights AS (
  SELECT
    -- column names may not be the same on Snowflake when historical data get loaded to Snowflake
    FLIGHTID,
    SCHEDDEPLOCAL,
    DEPTERMINAL,
    CASE
        WHEN (ALTDEPAPT <> '' AND ALTDEPAPT IS NOT NULL) THEN ALTDEPAPT
        ELSE SCHEDDEPAPT
    END AS dep_actual_airport_cd
  FROM STAGING_QUALITY.OAG.SQ_OAG__ACTUALS     -- Finalised staging raw OAG actual flight table on Snowflake
  WHERE CANCELLED = 0  -- {is_cancelled}  -- boolean, false / true
  AND dep_actual_airport_cd IN (
  --- get and extract only the list of "active" airports where we have visit records within the period of interest
    SELECT
        DISTINCT airport_code
    FROM analytics.datamart.ENRICHED_VISITS
	WHERE experience_date >= '{start_datetime}'::date  --- NOTE: enriched visits table only have visit since May 2023
	AND experience_date < '{end_datetime}'::date
--    AND payment_status <> 'NA'
	--- NOTE: remove payment_status is 'NA' for some visits in enriched visit table for non-ODS migrated visits
	AND (
        experience_date < '2023-06-01'::date
        OR payment_status <> 'NA'
    )
  )
  AND SERVICETYPE = '{flight_service_type}'
  AND SCHEDDEPLOCAL >= '{start_datetime}'::timestamp
  AND SCHEDDEPLOCAL < '{end_datetime}'::timestamp
  AND OPAIRLINECODE IS NULL
)
SELECT
    dep_actual_airport_cd AS airport_code,
    DEPTERMINAL AS terminal,
    TIME_SLICE(
        SCHEDDEPLOCAL, 15, 'MINUTE'
    ) AS visit_slot,
--    COALESCE(SUM(total_visits), 0) AS total_visits,
    COUNT(DISTINCT FLIGHTID) AS departure_flight_count
FROM selected_flights
GROUP BY airport_code, terminal, visit_slot
ORDER BY visit_slot, airport_code, terminal