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
