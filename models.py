import enum
from datetime import datetime

from sqlalchemy import (Boolean, Column, Date, DateTime, Enum as SQLEnum,
                        ForeignKey, Integer, JSON, Numeric, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class UserRole(str, enum.Enum):
    member = 'member'
    approver = 'approver'
    admin = 'admin'


class RecordStatus(str, enum.Enum):
    draft = 'draft'
    pending = 'pending'
    approved = 'approved'
    rejected = 'rejected'
    submitted = 'submitted'


class HourType(str, enum.Enum):
    primary = 'primary'
    secondary = 'secondary'
    other = 'other'
    none = 'none'


class D4HMember(Base):
    __tablename__ = 'd4h_members'

    id = Column(Integer, primary_key=True)
    ref = Column(String(32), nullable=False, index=True)
    name = Column(String(256), nullable=False)
    email = Column(String(256), nullable=True)
    google_username = Column(String(64), nullable=True, index=True)
    status = Column(String(32), nullable=False, default='Operational')
    count_rolling_hours = Column(Integer, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now,
                        onupdate=datetime.now)

    user = relationship('User', back_populates='d4h_member', uselist=False)
    d4h_hours = relationship('D4HHours', back_populates='d4h_member')

    @property
    def is_active(self):
        return self.status != 'Retired'


class D4HHours(Base):
    __tablename__ = 'd4h_hours'

    id = Column(Integer, primary_key=True)
    d4h_attendance_id = Column(Integer, unique=True, nullable=False, index=True)
    d4h_member_id = Column(Integer, ForeignKey('d4h_members.id'), nullable=False, index=True)
    activity_type = Column(String(16), nullable=False)
    d4h_activity_id = Column(Integer, nullable=False)
    activity_name = Column(String(256), nullable=True)
    hour_type = Column(SQLEnum(HourType, native_enum=False), nullable=False, default=HourType.none)
    date = Column(Date, nullable=False)
    hours = Column(Numeric(6, 2), nullable=False)
    synced_at = Column(DateTime, nullable=False, default=datetime.now)

    d4h_member = relationship('D4HMember', back_populates='d4h_hours')


class NotifyPref(str, enum.Enum):
    off = 'off'
    realtime = 'realtime'
    daily = 'daily'
    weekly = 'weekly'


class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    google_sub = Column(String(128), unique=True, nullable=False, index=True)
    email = Column(String(256), unique=True, nullable=False, index=True)
    username = Column(String(64), nullable=False)
    display_name = Column(String(256), nullable=False)
    role = Column(SQLEnum(UserRole, native_enum=False), nullable=False, default=UserRole.member)
    d4h_member_id = Column(Integer, ForeignKey('d4h_members.id'), nullable=True, index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    last_login_at = Column(DateTime, nullable=True)
    notify_approval = Column(SQLEnum(NotifyPref, native_enum=False), nullable=False,
                             default=NotifyPref.off)
    notify_pending = Column(SQLEnum(NotifyPref, native_enum=False), nullable=False,
                            default=NotifyPref.off)
    last_weekly_sent = Column(DateTime, nullable=True)

    records = relationship('HoursRecord', back_populates='user',
                           foreign_keys='HoursRecord.user_id')
    d4h_member = relationship('D4HMember', back_populates='user')


class Category(Base):
    __tablename__ = 'categories'

    id = Column(Integer, primary_key=True)
    name = Column(String(256), nullable=False)
    d4h_tag_id = Column(Integer, nullable=True)
    hour_type = Column(SQLEnum(HourType, native_enum=False), nullable=False, default=HourType.none)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class HoursRecord(Base):
    __tablename__ = 'hours_records'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=False)
    date = Column(Date, nullable=False)
    hours = Column(Numeric(5, 2), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(SQLEnum(RecordStatus, native_enum=False), nullable=False,
                    default=RecordStatus.draft)
    approved_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    d4h_submitted_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    d4h_submitted_at = Column(DateTime, nullable=True)
    d4h_record_id = Column(String(128), nullable=True)
    auto_role_assignment_id = Column(Integer, ForeignKey('admin_role_assignments.id'), nullable=True, index=True)
    d4h_needs_resync = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now,
                        onupdate=datetime.now)

    user = relationship('User', back_populates='records', foreign_keys=[user_id])
    category = relationship('Category')
    approver = relationship('User', foreign_keys=[approved_by])
    d4h_submitter = relationship('User', foreign_keys=[d4h_submitted_by])
    history = relationship('RecordHistory', back_populates='record',
                           order_by='RecordHistory.timestamp')


class RecordHistory(Base):
    __tablename__ = 'record_history'

    id = Column(Integer, primary_key=True)
    record_id = Column(Integer, ForeignKey('hours_records.id'), nullable=False)
    action = Column(String(64), nullable=False)
    performed_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    timestamp = Column(DateTime, nullable=False, default=datetime.now)
    changes = Column(JSON, nullable=True)

    record = relationship('HoursRecord', back_populates='history')
    actor = relationship('User', foreign_keys=[performed_by])


class AdminRole(Base):
    __tablename__ = 'admin_roles'

    id = Column(Integer, primary_key=True)
    name = Column(String(256), nullable=False)
    monthly_hours = Column(Numeric(5, 2), nullable=False)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    category = relationship('Category')
    assignments = relationship('AdminRoleAssignment', back_populates='admin_role',
                               cascade='all, delete-orphan')


class AdminRoleAssignment(Base):
    __tablename__ = 'admin_role_assignments'
    __table_args__ = (UniqueConstraint('user_id', 'admin_role_id', name='uq_user_role'),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    admin_role_id = Column(Integer, ForeignKey('admin_roles.id'), nullable=False, index=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    user = relationship('User')
    admin_role = relationship('AdminRole', back_populates='assignments')


class D4HSubmissionEvent(Base):
    __tablename__ = 'd4h_submission_events'
    __table_args__ = (UniqueConstraint('year', 'month', 'hour_type',
                                       name='uq_submission_event'),)

    id            = Column(Integer, primary_key=True)
    d4h_event_id  = Column(Integer, nullable=False, unique=True)
    year          = Column(Integer, nullable=False)
    month         = Column(Integer, nullable=False)
    hour_type     = Column(SQLEnum(HourType, native_enum=False), nullable=False)
    created_at    = Column(DateTime, nullable=False, default=datetime.now)
