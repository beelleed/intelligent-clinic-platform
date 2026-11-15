"""Shared synthetic directory and recurring hours for both clinic databases."""

DEMO_DOCTORS = (
    ("dr-lee", "Dr. Amanda Lee", "Family Medicine"),
    ("dr-chen", "Dr. Michael Chen", "General Surgery"),
    ("dr-rivera", "Dr. Sofia Rivera", "Dermatology"),
    ("dr-park", "Dr. Daniel Park", "Otolaryngology (ENT)"),
)

DEMO_SCHEDULES = (
    ("dr-lee", "Monday", "09:00", "12:00", "Clinic A"),
    ("dr-lee", "Wednesday", "13:00", "17:00", "Clinic A"),
    ("dr-lee", "Friday", "09:00", "12:00", "Clinic B"),
    ("dr-chen", "Tuesday", "09:00", "12:00", "Surgery Clinic"),
    ("dr-chen", "Thursday", "13:00", "17:00", "Surgery Clinic"),
    ("dr-rivera", "Monday", "13:00", "17:00", "Specialty Suite"),
    ("dr-rivera", "Thursday", "09:00", "12:00", "Specialty Suite"),
    ("dr-park", "Tuesday", "13:00", "17:00", "Clinic B"),
    ("dr-park", "Friday", "09:00", "12:00", "Clinic B"),
)
