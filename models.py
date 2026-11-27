from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from db import Base

VALID_ROLES = ("responder", "dispatcher", "supervisor")
VALID_STATUSES = ("open", "dispatched", "in_clearance", "cleared")


def utcnow():
    return datetime.now(timezone.utc)


class Role(Base):
    """Normalized stakeholder role definition."""

    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(32), unique=True, nullable=False, index=True)
    description = Column(String(255), nullable=False)

    users = relationship("User", back_populates="role")


class User(Base):
    """Registered TOC stakeholder account."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String(120), nullable=False)
    email = Column(String(180), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    role = relationship("Role", back_populates="users")
    reported_incidents = relationship(
        "Incident", foreign_keys="Incident.reported_by_id", back_populates="reported_by"
    )
    assigned_incidents = relationship(
        "Incident", foreign_keys="Incident.assigned_to_id", back_populates="assigned_to"
    )


class Incident(Base):
    """Core traffic incident record with telemetry snapshot and lifecycle status."""

    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint("latitude >= -90.0 AND latitude <= 90.0", name="ck_incident_lat"),
        CheckConstraint("longitude >= -180.0 AND longitude <= 180.0", name="ck_incident_lon"),
        CheckConstraint("month >= 1 AND month <= 12", name="ck_incident_month"),
        CheckConstraint("hour >= 0 AND hour <= 23", name="ck_incident_hour"),
        CheckConstraint(
            "status IN ('open', 'dispatched', 'in_clearance', 'cleared')",
            name="ck_incident_status",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    reference_code = Column(String(32), unique=True, nullable=False, index=True)
    reported_by_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    assigned_to_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    location_label = Column(String(300), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    occurred_at = Column(String(64), nullable=False)
    month = Column(Integer, nullable=False)
    hour = Column(Integer, nullable=False)

    temperature = Column(Float, nullable=True)
    humidity = Column(Float, nullable=True)
    visibility = Column(Float, nullable=True)
    wind_speed = Column(Float, nullable=True)
    precipitation = Column(Float, nullable=True)

    crossing = Column(Boolean, nullable=False, default=False)
    junction = Column(Boolean, nullable=False, default=False)
    traffic_signal = Column(Boolean, nullable=False, default=False)

    weather_condition = Column(String(80), nullable=False, default="Unknown")
    sunrise_sunset = Column(String(16), nullable=False, default="Unknown")
    telemetry_provenance = Column(Text, nullable=False, default="{}")

    status = Column(String(24), nullable=False, default="open", index=True)
    notes = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    reported_by = relationship("User", foreign_keys=[reported_by_id], back_populates="reported_incidents")
    assigned_to = relationship("User", foreign_keys=[assigned_to_id], back_populates="assigned_incidents")
    assessments = relationship(
        "SeverityAssessment",
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="SeverityAssessment.created_at.desc()",
    )
    audit_logs = relationship(
        "IncidentAuditLog",
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="IncidentAuditLog.created_at.desc()",
    )


class SeverityAssessment(Base):
    """Persisted ML prediction and DOT advisory tier for an incident."""

    __tablename__ = "severity_assessments"
    __table_args__ = (
        CheckConstraint("predicted_severity >= 1 AND predicted_severity <= 4", name="ck_assessment_severity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(Integer, ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True)
    assessed_by_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)

    predicted_severity = Column(Integer, nullable=False)
    impact_tier = Column(String(80), nullable=False)
    estimated_clearance_time = Column(String(80), nullable=False)
    dispatch_priority = Column(String(24), nullable=False)
    recommended_action = Column(Text, nullable=False)
    confidence_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    incident = relationship("Incident", back_populates="assessments")
    assessed_by = relationship("User")


class IncidentAuditLog(Base):
    """Operational audit trail tracking incident creation, re-assessments, and dispatch updates."""

    __tablename__ = "incident_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(Integer, ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)

    action = Column(String(48), nullable=False)
    from_status = Column(String(24), nullable=True)
    to_status = Column(String(24), nullable=True)
    detail = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    incident = relationship("Incident", back_populates="audit_logs")
    actor = relationship("User")
