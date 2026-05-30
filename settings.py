"""
App-level settings stored in the database.
Provides typed get/set with defaults.
"""

from datetime import datetime

DEFAULTS = {
    'tax_credit_min_total':   '200',
    'tax_credit_min_primary': '101',
}


def get(db, key: str):
    from models import Setting
    row = db.get(Setting, key)
    return row.value if row else DEFAULTS.get(key)


def get_float(db, key: str) -> float:
    return float(get(db, key) or DEFAULTS.get(key, 0))


def set_value(db, key: str, value: str) -> None:
    from models import Setting
    row = db.get(Setting, key)
    if row:
        row.value = value
        row.updated_at = datetime.now()
    else:
        db.add(Setting(key=key, value=value, updated_at=datetime.now()))
    db.commit()


def get_eligibility_settings(db) -> dict:
    return {
        'min_total':   get_float(db, 'tax_credit_min_total'),
        'min_primary': get_float(db, 'tax_credit_min_primary'),
    }


def check_eligibility(summary: dict, es: dict) -> tuple[bool, bool]:
    """Return (hours_ok, primary_ok) given a summary dict and eligibility settings."""
    hours_ok   = float(summary['total'])   >= es['min_total']
    primary_ok = float(summary['primary']) >= es['min_primary']
    return hours_ok, primary_ok
