from sqlalchemy import String, JSON, Double, Integer, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default='queued')
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(1000), default='')
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    # Round-robin dispatch bookkeeping. round_no is the rotation cycle the job was
    # most recently dispatched in; a job recovered back to queued no longer counts
    # as "served" for its submitter, so it can be retried without skipping a turn.
    round_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    round_pos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispatch_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    executor: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_expires_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    dispatched_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
