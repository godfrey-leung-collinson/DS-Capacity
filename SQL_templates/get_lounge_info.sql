-- =============================================================================
-- Get Lounge Information for Dashboard
-- =============================================================================
-- Fetches lounge metadata from SF_PARTNERSHIP for the turn-away dashboard.
-- Based on: src/preprocessing/SQL_templates/inventory/get_lounge_info_sf_partnership.sql
-- =============================================================================

SELECT
    CODE,
    NAME,
    LOCATION_CODE_TEXT AS AIRPORT_CODE,
    NUMBER_OF_SEATS,
    TERMINAL,
    CONCOURSE,
    INVENTORY_CATEGORY,
    ISO_COUNTRY_CODE,
    AIRSIDE_LANDSIDE,
    -- Opening hours by day of week
    MONDAY_OPEN_TIME,
    MONDAY_CLOSE_TIME,
    TUESDAY_OPEN_TIME,
    TUESDAY_CLOSE_TIME,
    WEDNESDAY_OPEN_TIME,
    WEDNESDAY_CLOSE_TIME,
    THURSDAY_OPEN_TIME,
    THURSDAY_CLOSE_TIME,
    FRIDAY_OPEN_TIME,
    FRIDAY_CLOSE_TIME,
    SATURDAY_OPEN_TIME,
    SATURDAY_CLOSE_TIME,
    SUNDAY_OPEN_TIME,
    SUNDAY_CLOSE_TIME,
    LAST_MODIFIED_DATE
FROM STAGING_QUALITY.SF_PARTNERSHIP.SQ_SF_PARTNERSHIP__ACCOUNT
WHERE CODE IN ({lounge_codes})
  AND ACCOUNT_STATUS = 'Active'
ORDER BY CODE, LAST_MODIFIED_DATE DESC

