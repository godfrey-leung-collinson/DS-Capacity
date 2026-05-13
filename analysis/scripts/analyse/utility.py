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
