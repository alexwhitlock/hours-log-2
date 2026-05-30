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
        ('users', 'notify_approval', "TEXT NOT NULL DEFAULT 'off'"),
        ('users', 'notify_pending',  "TEXT NOT NULL DEFAULT 'off'"),
        ('users', 'last_weekly_sent', 'DATETIME'),
        ('hours_records', 'auto_role_assignment_id', 'INTEGER REFERENCES admin_role_assignments(id)'),
    ]
    with _engine.connect() as conn:
        for table, col, col_def in new_columns:
            existing = [r[1] for r in conn.execute(
                __import__('sqlalchemy').text(f'PRAGMA table_info({table})')
            )]
            if col not in existing:
                conn.execute(__import__('sqlalchemy').text(
                    f'ALTER TABLE {table} ADD COLUMN {col} {col_def}'
                ))
