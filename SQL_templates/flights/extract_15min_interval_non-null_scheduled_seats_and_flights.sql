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
        )  AS dep_schedule_local_time,
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
    WHERE UPPER(f.depapt) IN (
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
      AND f.operating <> 'N'
      AND f.service    = '{flight_service_type}'
)
SELECT
    depapt AS airport_code,
    depterm AS terminal,
    TIME_SLICE(
        dep_schedule_local_time, 15, 'MINUTE'
    ) AS visit_slot,
    COUNT(DISTINCT flight_key_id) AS departure_flight_count,
    snapshot_date,
    SUM(TOTAL_SEATS)                     AS total_seats,
    SUM(FIRST_CLASS_SEATS)               AS first_class_seats,
    SUM(BUSINESS_CLASS_SEATS)            AS business_class_seats,
    SUM(PREMIUM_ECONOMY_CLASS_SEATS)     AS premium_economy_seats,
    SUM(ECONOMY_PLUS_CLASS_SEATS)        AS economy_plus_seats,
    SUM(ECONOMY_CLASS_SEATS)             AS economy_class_seats
FROM selected_flights
GROUP BY airport_code, terminal, visit_slot, snapshot_date
ORDER BY visit_slot, airport_code, terminal, snapshot_date