--- NOTE: TESTED ON Snowflake Prod, STAGING_QUALITY.OAG.SQ_OAG__ACTUALS
WITH timestamp_table AS (
    select
        timestampadd(MINUTE, value * 15, '{start_datetime}'::timestamp) AS window_start,
        timestampadd(MINUTE, (value + 1) * 15, '{start_datetime}'::timestamp) AS window_end
    from (
        select
            '{start_datetime}'::timestamp,
            array_generate_range(
                0, 1 + 24 * (60 / 15) * timestampdiff(DAY, '{start_datetime}'::timestamp, '{end_datetime}'::timestamp)
            ) AS int_array
    ), table(flatten(int_array))
),
selected_flights AS (
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
  WHERE CASE
        WHEN (ALTDEPAPT <> '' AND ALTDEPAPT IS NOT NULL) THEN ALTDEPAPT
        ELSE SCHEDDEPAPT
  END = '{selected_airport}'
  AND CANCELLED = 0  -- {is_cancelled}  -- boolean, false / true
  AND SERVICETYPE = '{flight_service_type}'
  AND SCHEDDEPLOCAL >= '{start_datetime}'::timestamp
  AND SCHEDDEPLOCAL < DATEADD('day', 1, '{end_datetime}'::timestamp)
  AND OPAIRLINECODE IS NULL
)
SELECT
    t1.window_start AS VISIT_DATE,
    t2.DEPTERMINAL,
    t2.dep_actual_airport_cd AS dep_airport,
    COUNT(DISTINCT FLIGHTID) AS dep_flight_count
FROM timestamp_table t1
LEFT JOIN selected_flights t2
ON t2.SCHEDDEPLOCAL > t1.window_start
AND t2.SCHEDDEPLOCAL <= t1.window_end
WHERE t1.window_start < '{end_datetime}'::timestamp
AND t2.dep_actual_airport_cd = '{selected_airport}'
GROUP BY t1.window_start, t2.DEPTERMINAL, t2.dep_actual_airport_cd
ORDER BY t1.window_start
