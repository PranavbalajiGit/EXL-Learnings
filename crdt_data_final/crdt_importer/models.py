"""SQLAlchemy mappings for existing tables. Do not call Base.metadata.create_all()."""
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Computed, DateTime, ForeignKeyConstraint,
    Index, Integer, String, Text, UniqueConstraint, func
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        CheckConstraint("audit_count = cardinality(audit_ids)",
                        name="ck_pipeline_runs_audit_count_matches_ids"),
    )

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline_version: Mapped[str] = mapped_column(String(100))
    model_name: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    category_version: Mapped[str] = mapped_column(Text)
    audit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    audit_ids: Mapped[list] = mapped_column(ARRAY(BigInteger), nullable=False, server_default="{}")
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AuditCode(Base):
    __tablename__ = "audit_codes"
    __table_args__ = (
        UniqueConstraint("audit_id", "code_norm", name="uq_audit_codes_audit_id_code_norm"),
    )

    audit_code_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    code_norm: Mapped[str] = mapped_column(
        Text,
        Computed("upper(regexp_replace(code, '[^A-Za-z0-9]', '', 'g'))", persisted=True),
        nullable=False,
    )
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OpinionPrediction(Base):
    __tablename__ = "opinion_predictions"
    __table_args__ = (
        ForeignKeyConstraint(["run_id"], ["pipeline_runs.run_id"]),
        ForeignKeyConstraint(["audit_code_id"], ["audit_codes.audit_code_id"]),
        Index("ix_opinion_predictions_run_audit_code", "run_id", "audit_code_id", unique=True),
    )

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_code_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    analysis: Mapped[str] = mapped_column(Text)
    uhdds_analysis: Mapped[str] = mapped_column(Text)
    findings: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    finding_outcome: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rationale: Mapped[str] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class AuditorReview(Base):
    __tablename__ = "auditor_reviews"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "audit_code_id"],
                             ["opinion_predictions.run_id", "opinion_predictions.audit_code_id"]),
        UniqueConstraint("run_id", "audit_code_id", name="uq_auditor_reviews_run_audit_code"),
        CheckConstraint("model_outcome IN ('Correct', 'Incorrect')",
                        name="ck_auditor_reviews_model_outcome"),
    )

    review_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    audit_code_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    auditor_name: Mapped[str] = mapped_column(Text)
    model_outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    auditor_comments: Mapped[str] = mapped_column(Text)
    ai_analysis_gap: Mapped[str] = mapped_column(Text)
    diagnosis_type: Mapped[str] = mapped_column(Text)
    insights: Mapped[str] = mapped_column(Text)
    auditor_category: Mapped[str] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class GroundTruth(Base):
    __tablename__ = "ground_truth"
    __table_args__ = (
        ForeignKeyConstraint(["audit_code_id"], ["audit_codes.audit_code_id"]),
        ForeignKeyConstraint(["review_id"], ["auditor_reviews.review_id"]),
        UniqueConstraint("audit_code_id", name="uq_ground_truth_audit_code_id"),
        UniqueConstraint("review_id", name="uq_ground_truth_review_id"),
        CheckConstraint("source = 'CANONICAL' OR review_id IS NOT NULL",
                        name="ck_ground_truth_source_requires_review"),
    )

    ground_truth_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_code_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    finding_outcome: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    review_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
