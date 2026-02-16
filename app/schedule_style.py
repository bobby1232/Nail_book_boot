from __future__ import annotations

DAY_TIMELINE_STYLE = {
    "slot_colors": {
        "free": (72, 201, 93),
        "booked": (220, 70, 70),
        "hold": (246, 191, 64),
        "break": (255, 245, 180),
    },
    "font_sizes": {
        "title": 28,
        "time": 24,
        "legend": 20,
    },
    "padding": 28,
    "col_gap": 18,
    "row_gap": 16,
    "square_size": 18,
    "background_color": (29, 24, 40),
    "title_color": (245, 245, 245),
    "time_color": (230, 230, 230),
    "legend_text_color": (230, 230, 230),
    "legend_square_radius": 4,
}

WEEK_SCHEDULE_STYLE = {
    "font_sizes": {
        "title": 26,
        "header": 18,
        "time": 18,
        "appointment": 16,
    },
    "padding": 24,
    "header_height": 42,
    "hour_height": 80,
    "background_color": (248, 248, 248),
    "title_color": (40, 40, 40),
    "header_text_color": (60, 60, 60),
    "grid_line_color": (190, 190, 190),
    "hour_line_color": (200, 200, 200),
    "time_text_color": (110, 110, 110),
    "appointment_text_color": (60, 60, 60),
    "appointment_colors": {
        "booked": {
            "fill": (248, 209, 223),
            "outline": (196, 85, 128),
        },
        "hold": {
            "fill": (240, 224, 176),
            "outline": (191, 162, 88),
        },
        "break": {
            "fill": (255, 245, 190),
            "outline": (209, 194, 117),
        },
    },
    "appointment_min_height": 18,
    "appointment_corner_radius": 6,
    "appointment_outline_width": 2,
    "appointment_text_padding_x": 6,
    "appointment_text_padding_y": 4,
}

PROJECT_PARAMETERS = [
    {
        "key": "schedule_style",
        "type": -1,
        "value": {
            "day_timeline": DAY_TIMELINE_STYLE,
            "week_schedule": WEEK_SCHEDULE_STYLE,
        },
    }
]
