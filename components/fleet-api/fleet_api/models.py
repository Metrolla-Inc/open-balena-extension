"""The Fleet API's own state, in SQLite on the service's data volume (next to metrolla_auth's
auth_sessions, api_tokens, local_users and audit_log tables).

SQLite because this is one process on one host with a small write rate, and it lives on a
volume that survives container rebuilds without adding a database to the openBalena host.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Index, Integer, JSON, Text, TypeDecorator, UniqueConstraint,
                        create_engine, event, inspect, text)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

Base = declarative_base()


class UTCDateTime(TypeDecorator):
    """Stored as UTC (SQLite keeps no offset), always read back timezone-aware."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def utcnow():
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return '%s_%s' % (prefix, uuid.uuid4().hex[:20])


def aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt):
    dt = aware(dt)
    return dt.astimezone(timezone.utc).isoformat() if dt else None


class Build(Base):
    __tablename__ = 'fleet_builds'
    id = Column(Text, primary_key=True)
    fleet = Column(Text, nullable=False)
    git = Column(JSON, nullable=False)
    state = Column(Text, nullable=False)        # queued | cloning | building | finalizing | success | failed
    release_id = Column(Integer)
    commit = Column(Text)
    source_commit = Column(Text)
    finalized = Column(Boolean, default=False)
    error = Column(Text)
    log = Column(Text, default='')
    created_by = Column(Text)
    change_id = Column(Text)
    created_at = Column(UTCDateTime(), default=utcnow)
    started_at = Column(UTCDateTime())
    finished_at = Column(UTCDateTime())


class Image(Base):
    __tablename__ = 'fleet_images'
    id = Column(Text, primary_key=True)
    fleet = Column(Text, nullable=False)
    params = Column(JSON)                       # never holds the Wi-Fi key
    imagemaker_id = Column(Text)
    status = Column(Text, nullable=False)       # building | ready | failed
    error = Column(Text)
    name = Column(Text)
    created_by = Column(Text)
    created_at = Column(UTCDateTime(), default=utcnow)


class ChangeSet(Base):
    __tablename__ = 'fleet_change_sets'
    id = Column(Text, primary_key=True)
    title = Column(Text)
    ticket = Column(Text)
    state = Column(Text, nullable=False)
    items = Column(JSON, nullable=False)
    fleets = Column(JSON, nullable=False)
    base = Column(JSON)
    plan = Column(JSON)
    plan_hash = Column(Text)
    policy = Column(JSON)
    author_id = Column(Text, nullable=False)
    author_kind = Column(Text)
    author_permissions = Column(JSON)           # what the author held when authoring (sorted)
    signature_kid = Column(Text)
    emergency = Column(Boolean, default=False)
    reason = Column(Text)
    window = Column(Text)
    not_before = Column(UTCDateTime())
    scheduled_for = Column(UTCDateTime())
    window_end = Column(UTCDateTime())
    applied_at = Column(UTCDateTime())
    finished_at = Column(UTCDateTime())
    progress = Column(JSON)
    verify_result = Column(JSON)
    error = Column(Text)
    idempotency_key = Column(Text, unique=True)
    created_at = Column(UTCDateTime(), default=utcnow)
    updated_at = Column(UTCDateTime(), default=utcnow)


Index('ix_fleet_change_sets_state', ChangeSet.state, ChangeSet.created_at)


class ChangeApproval(Base):
    __tablename__ = 'fleet_change_approvals'
    change_id = Column(Text, primary_key=True)
    approver_id = Column(Text, primary_key=True)
    plan_hash = Column(Text, primary_key=True)
    permissions = Column(JSON)                  # the plan's required permissions the approver held
    comment = Column(Text)
    approved_at = Column(UTCDateTime(), default=utcnow)


class ChangeEvent(Base):
    __tablename__ = 'fleet_change_events'
    id = Column(Integer, primary_key=True, autoincrement=True)
    change_id = Column(Text, nullable=False, index=True)
    ts = Column(UTCDateTime(), default=utcnow)
    state = Column(Text)
    actor = Column(Text)
    detail = Column(JSON)


class AppliedEnv(Base):
    """The last value this API applied per variable; drift compares the live value with it."""
    __tablename__ = 'fleet_applied_env'
    id = Column(Integer, primary_key=True, autoincrement=True)
    fleet = Column(Text, nullable=False)
    device = Column(Text, nullable=False, default='')
    service = Column(Text, nullable=False, default='')
    name = Column(Text, nullable=False)
    present = Column(Boolean, nullable=False)
    value = Column(Text)                        # null for secrets
    value_sha256 = Column(Text)
    applied_by = Column(Text)
    change_id = Column(Text)
    applied_at = Column(UTCDateTime(), default=utcnow)
    __table_args__ = (UniqueConstraint('fleet', 'device', 'service', 'name', name='uq_applied_env_scope'),)


class AppliedTarget(Base):
    """The last target release this API pinned, per fleet (device '') or device."""
    __tablename__ = 'fleet_applied_targets'
    fleet = Column(Text, primary_key=True)
    device = Column(Text, primary_key=True, default='')
    release_id = Column(Integer)
    applied_by = Column(Text)
    change_id = Column(Text)
    applied_at = Column(UTCDateTime(), default=utcnow)


class ServerOperation(Base):
    __tablename__ = 'fleet_server_operations'
    id = Column(Text, primary_key=True)
    type = Column(Text, nullable=False)
    params = Column(JSON)
    state = Column(Text, nullable=False)        # running | succeeded | failed
    result = Column(JSON)
    created_by = Column(Text)
    created_at = Column(UTCDateTime(), default=utcnow)
    finished_at = Column(UTCDateTime())


def make_engine(url: str):
    if url in ('sqlite://', 'sqlite:///:memory:'):
        engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    else:
        engine = create_engine(url, connect_args={'check_same_thread': False, 'timeout': 30}
                               if url.startswith('sqlite') else {})
    if url.startswith('sqlite'):
        @event.listens_for(engine, 'connect')
        def _pragmas(conn, _):
            cur = conn.cursor()
            cur.execute('PRAGMA journal_mode=WAL')
            cur.execute('PRAGMA busy_timeout=30000')
            cur.close()
    return engine


# Columns added after the tables first shipped: (table, column), added on start when missing.
ADDED_COLUMNS = (('fleet_change_sets', 'author_permissions'), ('fleet_change_approvals', 'permissions'))


def init_db(engine):
    from metrolla_auth.models import create_tables
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    for table, column in ADDED_COLUMNS:
        if column not in {c['name'] for c in insp.get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text('ALTER TABLE %s ADD COLUMN %s JSON' % (table, column)))
    create_tables(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
