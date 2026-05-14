from typing import Dict, List


def get_opening_hours_list(
    opening_hour_row: Dict[str, any]
) -> List:
    """
    Get opening hours dict from the outlet info DataFrame

    Returns
    -------
    Dict[str, List[Dict[str, str]]]
        A dictionary mapping outlet_code to a list of opening hours rows, where each row is a dict with keys "Day", "Open", "Close"
    """

    opening_hours_list = []

    day_map = [
        ("Mon", "monday"), ("Tue", "Tuesday"), ("Wed", "wednesday"),
        ("Thu", "thursday"), ("Fri", "friday"), ("Sat", "saturday"),
        ("Sun", "sunday"),
    ]
    for short, col in day_map:
        open_t = opening_hour_row.get(f"{col}_open_time", None)
        close_t = opening_hour_row.get(f"{col}_close_time", None)
        opening_hours_list.append({
            "Day": short,
            "Open": str(open_t)[:5] if open_t else "—",
            "Close": str(close_t)[:5] if close_t else "—",
        })

    return opening_hours_list


def get_weekday_hour_is_open_flag(
    opening_hours_list: List[Dict[str, str]]
) -> Dict[str, Dict[int, bool]]:
    """
    Get a dict indicating whether the outlet is open for each weekday and hour based on the opening hours list

    Returns
    -------
    Dict[str, Dict[int, bool]]
        A dictionary mapping weekday abbreviation to a dict of hour (0-23) to is_open flag (True/False)
    """

    DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    DAY_TO_DOW = {d: i for i, d in enumerate(DAY_ORDER)}

    weekday_hour_is_open = {day: {h: False for h in range(24)} for day in DAY_ORDER}

    for row in opening_hours_list:
        day   = row.get("Day", "")
        open_s  = row.get("Open", "—")
        close_s = row.get("Close", "—")
        if day not in DAY_TO_DOW:
            continue
        if open_s == "—":
            open_s = "00:00"
        if close_s == "—":
            close_s = "24:00"
        try:
            oh = int(open_s.split(":")[0])
            ch = int(close_s.split(":")[0])
            ch_minute = int(close_s.split(":")[1])
            if ch == 0:
                ch = 24
            if ch_minute != 0:
                ch = ch + 1
            ch = min(ch, 24)
        except (ValueError, IndexError):
            continue
        for h in range(oh, ch):
            weekday_hour_is_open[day][f"{h:02d}:00"] = True

    return weekday_hour_is_open
