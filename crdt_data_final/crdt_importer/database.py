from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def create_database_engine(database_url):
    return create_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
