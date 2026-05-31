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
    sql = __import__('sqlalchemy').text
    with _engine.connect() as conn:
        conn.execute(sql('DROP TABLE IF EXISTS users_new'))
        conn.execute(sql('DROP TABLE IF EXISTS d4h_hours_new'))
        conn.execute(sql('PRAGMA foreign_keys=OFF'))

        existing_user_cols = [r[1] for r in conn.execute(sql('PRAGMA table_info(users)'))]

        # ── Merge d4h_members into users ──────────────────────────────────────
        d4h_members_exists = conn.execute(sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='d4h_members'"
        )).fetchone()

        if d4h_members_exists and existing_user_cols:
            # Step 1: add D4H columns to users if missing
            for col, defn in [
                ('d4h_id',             'INTEGER'),
                ('ref',                'TEXT'),
                ('d4h_status',         'TEXT'),
                ('count_rolling_hours','INTEGER'),
                ('last_synced_at',     'DATETIME'),
                ('google_username',    'TEXT'),
            ]:
                if col not in existing_user_cols:
                    conn.execute(sql(f'ALTER TABLE users ADD COLUMN {col} {defn}'))

            # Step 2: populate D4H columns from d4h_members for linked users
            conn.execute(sql('''
                UPDATE users SET
                    d4h_id             = (SELECT dm.id               FROM d4h_members dm WHERE dm.id = users.d4h_member_id),
                    ref                = (SELECT dm.ref              FROM d4h_members dm WHERE dm.id = users.d4h_member_id),
                    display_name       = COALESCE((SELECT dm.name    FROM d4h_members dm WHERE dm.id = users.d4h_member_id), display_name),
                    d4h_status         = (SELECT dm.status           FROM d4h_members dm WHERE dm.id = users.d4h_member_id),
                    count_rolling_hours= (SELECT dm.count_rolling_hours FROM d4h_members dm WHERE dm.id = users.d4h_member_id),
                    last_synced_at     = (SELECT dm.last_synced_at   FROM d4h_members dm WHERE dm.id = users.d4h_member_id),
                    google_username    = COALESCE(google_username,
                                            (SELECT dm.google_username FROM d4h_members dm WHERE dm.id = users.d4h_member_id),
                                            username)
                WHERE d4h_member_id IS NOT NULL
            '''))

            # Step 3: create user rows for D4H members that have no user yet
            conn.execute(sql('''
                INSERT OR IGNORE INTO users
                    (d4h_id, ref, display_name, d4h_status, count_rolling_hours,
                     last_synced_at, google_username, role, is_active, created_at,
                     notify_approval, notify_pending, notify_monthly_summary, notify_tax_credit)
                SELECT
                    dm.id, dm.ref, dm.name, dm.status, dm.count_rolling_hours,
                    dm.last_synced_at,
                    CASE WHEN dm.google_username IS NOT NULL AND dm.google_username != ''
                         THEN dm.google_username ELSE NULL END,
                    'member', 1, datetime('now'),
                    'off', 'off', 0, 1
                FROM d4h_members dm
                WHERE dm.status != 'Retired'
                  AND dm.id NOT IN (SELECT d4h_id FROM users WHERE d4h_id IS NOT NULL)
            '''))

        # ── Migrate d4h_hours: d4h_member_id → user_id ────────────────────────
        d4h_hours_cols = [r[1] for r in conn.execute(sql('PRAGMA table_info(d4h_hours)'))]
        if 'd4h_member_id' in d4h_hours_cols:
            conn.execute(sql('''
                CREATE TABLE d4h_hours_new (
                    id INTEGER PRIMARY KEY,
                    d4h_attendance_id INTEGER NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    activity_type TEXT NOT NULL,
                    d4h_activity_id INTEGER NOT NULL,
                    activity_name TEXT,
                    hour_type TEXT NOT NULL DEFAULT 'none',
                    date DATE NOT NULL,
                    hours NUMERIC NOT NULL,
                    synced_at DATETIME NOT NULL
                )
            '''))
            conn.execute(sql('''
                INSERT INTO d4h_hours_new
                    (id, d4h_attendance_id, user_id, activity_type, d4h_activity_id,
                     activity_name, hour_type, date, hours, synced_at)
                SELECT
                    dh.id, dh.d4h_attendance_id,
                    u.id,
                    dh.activity_type, dh.d4h_activity_id, dh.activity_name,
                    dh.hour_type, dh.date, dh.hours, dh.synced_at
                FROM d4h_hours dh
                JOIN users u ON u.d4h_id = dh.d4h_member_id
            '''))
            conn.execute(sql('DROP TABLE d4h_hours'))
            conn.execute(sql('ALTER TABLE d4h_hours_new RENAME TO d4h_hours'))

        # ── Recreate users without email/username/d4h_member_id ───────────────
        existing_user_cols = [r[1] for r in conn.execute(sql('PRAGMA table_info(users)'))]
        if 'email' in existing_user_cols or 'd4h_member_id' in existing_user_cols:
            # ensure google_username is populated from username if still missing
            if 'username' in existing_user_cols and 'google_username' in existing_user_cols:
                conn.execute(sql('''
                    UPDATE users SET google_username = username
                    WHERE google_username IS NULL AND username IS NOT NULL
                      AND username NOT LIKE 'd4h_%'
                '''))
            conn.execute(sql('''
                CREATE TABLE users_new (
                    id INTEGER PRIMARY KEY,
                    d4h_id INTEGER UNIQUE,
                    ref TEXT,
                    display_name TEXT NOT NULL,
                    d4h_status TEXT,
                    count_rolling_hours INTEGER,
                    last_synced_at DATETIME,
                    google_sub TEXT UNIQUE,
                    google_username TEXT,
                    role TEXT NOT NULL DEFAULT 'member',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL,
                    last_login_at DATETIME,
                    notify_approval TEXT NOT NULL DEFAULT 'off',
                    notify_pending TEXT NOT NULL DEFAULT 'off',
                    notify_monthly_summary INTEGER NOT NULL DEFAULT 0,
                    notify_tax_credit INTEGER NOT NULL DEFAULT 1,
                    last_weekly_sent DATETIME,
                    tax_credit_notified_year INTEGER
                )
            '''))
            conn.execute(sql('''
                INSERT INTO users_new
                    (id, d4h_id, ref, display_name, d4h_status, count_rolling_hours,
                     last_synced_at, google_sub, google_username, role, is_active,
                     created_at, last_login_at, notify_approval, notify_pending,
                     notify_monthly_summary, notify_tax_credit, last_weekly_sent,
                     tax_credit_notified_year)
                SELECT
                    id, d4h_id, ref, display_name, d4h_status, count_rolling_hours,
                    last_synced_at, google_sub, google_username, role, is_active,
                    created_at, last_login_at,
                    COALESCE(notify_approval, 'off'),
                    COALESCE(notify_pending,  'off'),
                    COALESCE(notify_monthly_summary, 0),
                    COALESCE(notify_tax_credit, 1),
                    last_weekly_sent, tax_credit_notified_year
                FROM users
            '''))
            conn.execute(sql('DROP TABLE users'))
            conn.execute(sql('ALTER TABLE users_new RENAME TO users'))

        # ── Drop d4h_members ──────────────────────────────────────────────────
        if d4h_members_exists:
            conn.execute(sql('DROP TABLE d4h_members'))

        conn.execute(sql('PRAGMA foreign_keys=ON'))
        conn.commit()

    # create_all adds any tables/columns not yet present
    from models import Base
    Base.metadata.create_all(_engine)

    with _engine.connect() as conn:
        # ── Seed system categories ─────────────────────────────────────────────
        for ht in ('primary', 'secondary', 'other'):
            exists = conn.execute(sql(
                f"SELECT id FROM categories WHERE is_system=1 AND hour_type='{ht}'"
            )).fetchone()
            if not exists:
                conn.execute(sql(
                    f"INSERT INTO categories (name, hour_type, is_active, is_system, created_at) "
                    f"VALUES ('System: {ht.capitalize()}', '{ht}', 1, 1, datetime('now'))"
                ))

        # ── Seed default settings ──────────────────────────────────────────────
        from settings import DEFAULTS
        for key, value in DEFAULTS.items():
            existing = conn.execute(sql(f"SELECT key FROM settings WHERE key = '{key}'")).fetchone()
            if not existing:
                conn.execute(sql(f"INSERT INTO settings (key, value) VALUES ('{key}', '{value}')"))

        conn.commit()
