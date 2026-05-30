from flask import g
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

_engine = None
_Session = None


def init_db(database_url: str) -> None:
    global _engine, _Session
    _engine = create_engine(
        database_url,
        connect_args={'check_same_thread': False},
    )

    @event.listens_for(_engine, 'connect')
    def set_pragmas(conn, _):
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')

    _Session = sessionmaker(bind=_engine)


def get_engine():
    return _engine


def get_db():
    if 'db' not in g:
        g.db = _Session()
    return g.db


def close_db(e=None) -> None:
    db = g.pop('db', None)
    if db is not None:
        db.close()


def run_migrations() -> None:
    """Add columns that may not exist in an older database."""
    new_columns = [
        ('users', 'notify_approval', "TEXT NOT NULL DEFAULT 'weekly'"),
        ('users', 'notify_pending',  "TEXT NOT NULL DEFAULT 'weekly'"),
        ('users', 'last_weekly_sent', 'DATETIME'),
        ('users', 'notify_monthly_summary', 'INTEGER NOT NULL DEFAULT 0'),
        ('users', 'notify_tax_credit', 'INTEGER NOT NULL DEFAULT 1'),
        ('users', 'tax_credit_notified_year', 'INTEGER'),
        ('hours_records', 'auto_role_assignment_id', 'INTEGER REFERENCES admin_role_assignments(id)'),
        ('hours_records', 'd4h_needs_resync', 'INTEGER NOT NULL DEFAULT 0'),
        ('categories', 'is_system', 'INTEGER NOT NULL DEFAULT 0'),
    ]
    sql = __import__('sqlalchemy').text
    with _engine.connect() as conn:
        for table, col, col_def in new_columns:
            existing = [r[1] for r in conn.execute(sql(f'PRAGMA table_info({table})'))]
            if col not in existing:
                conn.execute(sql(f'ALTER TABLE {table} ADD COLUMN {col} {col_def}'))

        # Set new defaults on existing users who still have old 'off' values
        conn.execute(sql("UPDATE users SET notify_approval = 'weekly' WHERE notify_approval = 'off'"))
        conn.execute(sql("UPDATE users SET notify_pending  = 'weekly' WHERE notify_pending  = 'off'"))

        # Seed system categories for auto-role hours
        for ht in ('primary', 'secondary', 'other'):
            exists = conn.execute(sql(
                f"SELECT id FROM categories WHERE is_system=1 AND hour_type='{ht}'"
            )).fetchone()
            if not exists:
                conn.execute(sql(
                    f"INSERT INTO categories (name, hour_type, is_active, is_system) "
                    f"VALUES ('System: {ht.capitalize()}', '{ht}', 1, 1)"
                ))

        # Seed default settings if not present
        from settings import DEFAULTS
        for key, value in DEFAULTS.items():
            existing = conn.execute(sql(f"SELECT key FROM settings WHERE key = '{key}'")).fetchone()
            if not existing:
                conn.execute(sql(f"INSERT INTO settings (key, value) VALUES ('{key}', '{value}')"))

        conn.commit()
