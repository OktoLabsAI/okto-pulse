"""Community-owned SQLAlchemy declarative metadata."""

from sqlalchemy.orm import declarative_base

Base = declarative_base()

__all__ = ["Base"]
