--- NOTE: Tested on Snowflake (Prod Env)
-- ============================================================================
-- Scheduled Seat Counts from Historical OAG Snapshots (Past 3 Months)
-- ============================================================================
-- For each calendar month in the lookback window the FILE_DATE used is the
-- last Monday of the *previous* calendar month.  This gives a forward-looking
-- snapshot consistent with how scheduled capacity would have been known at
-- the time (i.e. the schedule published just before the month began).
--
-- Parameters:
--   {start_datetime}      – start of the lookback window (e.g. 3 months ago)
--   {end_datetime}        – end of the lookback window   (e.g. today)
--   {selected_airport}    – airport IATA code, e.g. 'LHR' or 'LGW'
--   {flight_service_type} – OAG service code, e.g. 'J' (scheduled passenger)
-- ============================================================================

WITH month_periods AS (
    -- One row per calendar month covered by the lookback window.
    -- snapshot_date = last Monday of the *previous* calendar month.
    --
    -- Example (today = 2026-05-08, lookback = 3 months):
    --   seq=0  month = May-2026   snapshot = last Monday of Apr-2026 (2026-04-27)
    --   seq=1  month = Apr-2026   snapshot = last Monday of Mar-2026 (2026-03-30)
    --   seq=2  month = Mar-2026   snapshot = last Monday of Feb-2026 (2026-02-23)
    --   seq=3  month = Feb-2026   snapshot = last Monday of Jan-2026 (2026-01-26)
    SELECT
        DATE_TRUNC('month',
            DATEADD('month', -seq, DATE_TRUNC('month', '{end_datetime}'::date))
        )                                                                   AS month_start,
        LAST_DAY(
            DATEADD('month', -seq, DATE_TRUNC('month', '{end_datetime}'::date))
        )                                                                   AS month_end,
        -- Last Monday of the previous month:
        --   Start from LAST_DAY of (current_month - 1), then step back
        --   enough days to land on a Monday.
        --   Snowflake DAYOFWEEK: Sun=0, Mon=1, Tue=2, ..., Sat=6
        DATEADD('day',
            -MOD(
                DAYOFWEEK(
                    LAST_DAY(DATEADD('month', -seq - 1, DATE_TRUNC('month', '{end_datetime}'::date)))
                ) - 1 + 7,
                7
            ),
            LAST_DAY(DATEADD('month', -seq - 1, DATE_TRUNC('month', '{end_datetime}'::date)))
        )                                                                   AS snapshot_date
    FROM (
        SELECT 0 AS seq UNION ALL
        SELECT 1        UNION ALL
        SELECT 2        UNION ALL
        SELECT 3
    )
    -- Keep only months that fall within the requested lookback window
    WHERE DATE_TRUNC('month',
              DATEADD('month', -seq, DATE_TRUNC('month', '{end_datetime}'::date))
          ) >= DATE_TRUNC('month', '{start_datetime}'::date)
),

time_slots AS (
    -- 15-minute intervals covering the full lookback window (matches visit SQL granularity)
    SELECT
        TIMESTAMPADD('minute', value * 15, '{start_datetime}'::timestamp) AS slot_start,
        TIMESTAMPADD('minute', (value + 1) * 15, '{start_datetime}'::timestamp) AS slot_end
    FROM (
        SELECT
            ARRAY_GENERATE_RANGE(
                0,
                4 * 24 * TIMESTAMPDIFF('day', '{start_datetime}'::timestamp, '{end_datetime}'::timestamp) + 4 * 24
            ) AS arr
    ), TABLE(FLATTEN(arr))
    WHERE TIMESTAMPADD('minute', value * 15, '{start_datetime}'::timestamp)
          < '{end_datetime}'::timestamp
),

selected_flights AS (
    -- Pull each flight from the snapshot whose FILE_DATE matches the
    -- last-Monday-of-previous-month for the month the flight departs in.
    SELECT
        CONCAT(
            CAST(f.fltno AS VARCHAR), f.depapt, f.depterm,
            f.flight_date, CAST(f.deptim AS VARCHAR)
        )  AS flight_key_id,
        CAST(
            CONCAT(
                f.flight_date, ' ',
                SUBSTR(LPAD(CAST(f.deptim AS VARCHAR), 4, '0'), 1, 2), ':',
                SUBSTR(LPAD(CAST(f.deptim AS VARCHAR), 4, '0'), 3, 4), ':00'
            ) AS TIMESTAMP
        )                                                    AS dep_schedule_local_time,
        f.depterm,
        f.depapt,
        mp.snapshot_date,
        f.TOTAL_SEATS,
        f.FIRST_CLASS_SEATS,
        f.BUSINESS_CLASS_SEATS,
        f.PREMIUM_ECONOMY_CLASS_SEATS,
        f.ECONOMY_PLUS_CLASS_SEATS,
        f.ECONOMY_CLASS_SEATS
    FROM RAW.OAG.COLLINSON f
    -- Match each flight to its calendar month, and enforce the correct snapshot FILE_DATE
    INNER JOIN month_periods mp
        ON  f.flight_date >= mp.month_start
        AND f.flight_date <= mp.month_end
        AND f.file_date   =  mp.snapshot_date
    WHERE UPPER(f.depapt) = '{selected_airport}'
      AND f.operating <> 'N'
      AND f.service    = '{flight_service_type}'
)

SELECT
    t.slot_start                           AS slot_start,
    f.depapt                               AS dep_airport,
    f.depterm                              AS dep_terminal,
    f.snapshot_date                        AS oag_snapshot_date,
    COUNT(DISTINCT f.flight_key_id)        AS dep_flight_count,
    SUM(f.TOTAL_SEATS)                     AS total_seats,
    SUM(f.FIRST_CLASS_SEATS)               AS first_class_seats,
    SUM(f.BUSINESS_CLASS_SEATS)            AS business_class_seats,
    SUM(f.PREMIUM_ECONOMY_CLASS_SEATS)     AS premium_economy_seats,
    SUM(f.ECONOMY_PLUS_CLASS_SEATS)        AS economy_plus_seats,
    SUM(f.ECONOMY_CLASS_SEATS)             AS economy_class_seats
FROM time_slots t
LEFT JOIN selected_flights f
    ON  f.dep_schedule_local_time >  t.slot_start
    AND f.dep_schedule_local_time <= t.slot_end
GROUP BY t.slot_start, f.depapt, f.depterm, f.snapshot_date
ORDER BY t.slot_start
