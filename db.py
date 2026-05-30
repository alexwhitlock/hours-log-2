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
    """Migrate the database schema to the two-level HoursEntry/HoursRecord model."""
    sql = __import__('sqlalchemy').text
    with _engine.connect() as conn:
        # ── 1. Recreate users table with google_sub nullable ──────────────────
        existing_cols = [r[1] for r in conn.execute(sql('PRAGMA table_info(users)'))]
        if existing_cols:
            # Check if google_sub is currently NOT NULL by inspecting notnull flag
            col_info = {r[1]: r for r in conn.execute(sql('PRAGMA table_info(users)'))}
            google_sub_notnull = col_info.get('google_sub', (None, None, None, None, None, None))[3]
            if google_sub_notnull:
                # Need to recreate the table with google_sub nullable
                conn.execute(sql('PRAGMA foreign_keys=OFF'))
                conn.execute(sql('''
                    CREATE TABLE users_new (
                        id INTEGER PRIMARY KEY,
                        google_sub TEXT UNIQUE,
                        email TEXT NOT NULL UNIQUE,
                        username TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'member',
                        d4h_member_id INTEGER REFERENCES d4h_members(id),
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL,
                        last_login_at DATETIME,
                        notify_approval TEXT NOT NULL DEFAULT 'weekly',
                        notify_pending TEXT NOT NULL DEFAULT 'weekly',
                        notify_monthly_summary INTEGER NOT NULL DEFAULT 0,
                        notify_tax_credit INTEGER NOT NULL DEFAULT 1,
                        last_weekly_sent DATETIME,
                        tax_credit_notified_year INTEGER
                    )
                '''))
                conn.execute(sql('''
                    INSERT INTO users_new
                        (id, google_sub, email, username, display_name, role,
                         d4h_member_id, is_active, created_at, last_login_at,
                         notify_approval, notify_pending, notify_monthly_summary,
                         notify_tax_credit, last_weekly_sent, tax_credit_notified_year)
                    SELECT
                        id, google_sub, email, username, display_name, role,
                        d4h_member_id, is_active, created_at, last_login_at,
                        COALESCE(notify_approval, 'weekly'),
                        COALESCE(notify_pending, 'weekly'),
                        COALESCE(notify_monthly_summary, 0),
                        COALESCE(notify_tax_credit, 1),
                        last_weekly_sent,
                        tax_credit_notified_year
                    FROM users
                '''))
                conn.execute(sql('DROP TABLE users'))
                conn.execute(sql('ALTER TABLE users_new RENAME TO users'))
                conn.execute(sql('PRAGMA foreign_keys=ON'))

        # ── 2. Drop old history and records tables ────────────────────────────
        # Check if old record_history references hours_records (old schema)
        rh_cols = [r[1] for r in conn.execute(sql('PRAGMA table_info(record_history)'))]
        if 'record_id' in rh_cols:
            conn.execute(sql('DROP TABLE IF EXISTS record_history'))

        # Drop old hours_records if it has old schema (category_id column = old model)
        hr_cols = [r[1] for r in conn.execute(sql('PRAGMA table_info(hours_records)'))]
        if 'category_id' in hr_cols:
            conn.execute(sql('DROP TABLE IF EXISTS hours_records'))

        # Drop hours_entries if it exists with wrong schema (shouldn't happen, but safe)
        # Let create_all() create hours_entries, new hours_records, record_history fresh

        conn.commit()

    # ── 3. create_all() creates new tables ───────────────────────────────────
    from models import Base
    Base.metadata.create_all(_engine)

    with _engine.connect() as conn:
        # ── 4. Notify preference defaults ────────────────────────────────────
        conn.execute(sql("UPDATE users SET notify_approval = 'weekly' WHERE notify_approval = 'off'"))
        conn.execute(sql("UPDATE users SET notify_pending  = 'weekly' WHERE notify_pending  = 'off'"))

        # ── 5. Seed system categories ─────────────────────────────────────────
        for ht in ('primary', 'secondary', 'other'):
            exists = conn.execute(sql(
                f"SELECT id FROM categories WHERE is_system=1 AND hour_type='{ht}'"
            )).fetchone()
            if not exists:
                conn.execute(sql(
                    f"INSERT INTO categories (name, hour_type, is_active, is_system, created_at) "
                    f"VALUES ('System: {ht.capitalize()}', '{ht}', 1, 1, datetime('now'))"
                ))

        # ── 6. Seed default settings ──────────────────────────────────────────
        from settings import DEFAULTS
        for key, value in DEFAULTS.items():
            existing = conn.execute(sql(f"SELECT key FROM settings WHERE key = '{key}'")).fetchone()
            if not existing:
                conn.execute(sql(f"INSERT INTO settings (key, value) VALUES ('{key}', '{value}')"))

        conn.commit()
