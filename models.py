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
    google_sub = Column(String(128), unique=True, nullable=True, index=True)
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
    notify_monthly_summary = Column(Boolean, nullable=False, default=False)
    notify_tax_credit = Column(Boolean, nullable=False, default=True)
    last_weekly_sent = Column(DateTime, nullable=True)
    tax_credit_notified_year = Column(Integer, nullable=True)

    records = relationship('HoursRecord', back_populates='user',
                           foreign_keys='HoursRecord.user_id')
    submitted_entries = relationship('HoursEntry', back_populates='submitter',
                                     foreign_keys='HoursEntry.submitted_by')
    approved_entries = relationship('HoursEntry', back_populates='approver_user',
                                    foreign_keys='HoursEntry.approved_by')
    d4h_member = relationship('D4HMember', back_populates='user')


class Category(Base):
    __tablename__ = 'categories'

    id = Column(Integer, primary_key=True)
    name = Column(String(256), nullable=False)
    d4h_tag_id = Column(Integer, nullable=True)
    hour_type = Column(SQLEnum(HourType, native_enum=False), nullable=False, default=HourType.none)
    is_active = Column(Boolean, nullable=False, default=True)
    is_system = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    approvers = relationship('CategoryApprover', back_populates='category',
                             cascade='all, delete-orphan')


class HoursEntry(Base):
    __tablename__ = 'hours_entries'

    id = Column(Integer, primary_key=True)
    submitted_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=False)
    date = Column(Date, nullable=False)
    hours = Column(Numeric(5, 2), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(SQLEnum(RecordStatus, native_enum=False), nullable=False,
                    default=RecordStatus.draft)
    approved_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    auto_role_assignment_id = Column(Integer, ForeignKey('admin_role_assignments.id'),
                                     nullable=True, index=True)
    d4h_needs_resync = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now,
                        onupdate=datetime.now)

    submitter = relationship('User', back_populates='submitted_entries',
                             foreign_keys=[submitted_by])
    approver_user = relationship('User', back_populates='approved_entries',
                                 foreign_keys=[approved_by])
    category = relationship('Category')
    records = relationship('HoursRecord', back_populates='entry',
                           cascade='all, delete-orphan')
    history = relationship('EntryHistory', back_populates='entry',
                           order_by='EntryHistory.id',
                           cascade='all, delete-orphan')
    auto_role_assignment = relationship('AdminRoleAssignment', back_populates='entries')


class HoursRecord(Base):
    __tablename__ = 'hours_records'

    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey('hours_entries.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    d4h_record_id = Column(String(64), nullable=True)
    d4h_needs_resync = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    entry = relationship('HoursEntry', back_populates='records')
    user = relationship('User', back_populates='records', foreign_keys=[user_id])


class EntryHistory(Base):
    __tablename__ = 'record_history'

    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey('hours_entries.id'), nullable=False)
    action = Column(String(64), nullable=False)
    performed_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    timestamp = Column(DateTime, nullable=False, default=datetime.now)
    changes = Column(JSON, nullable=True)

    entry = relationship('HoursEntry', back_populates='history')
    actor = relationship('User', foreign_keys=[performed_by])


class CategoryApprover(Base):
    __tablename__ = 'category_approvers'
    __table_args__ = (UniqueConstraint('user_id', 'category_id', name='uq_cat_approver'),)

    id          = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=False, index=True)
    user_id     = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    created_at  = Column(DateTime, nullable=False, default=datetime.now)

    category = relationship('Category', back_populates='approvers')
    user     = relationship('User')


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
    entries = relationship('HoursEntry', back_populates='auto_role_assignment')


class Setting(Base):
    __tablename__ = 'settings'

    key        = Column(String(64), primary_key=True)
    value      = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=True)


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
