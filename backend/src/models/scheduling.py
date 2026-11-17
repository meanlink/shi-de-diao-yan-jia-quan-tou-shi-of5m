from sqlalchemy import String, JSON, Double, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class SchedulerState(Base):
    """Singleton row holding durable round-robin cursor and site-wide capacity."""

    __tablename__ = 'scheduler_state'
    id: Mapped[int] = mapped_column(primary_key=True)
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    cycle: Mapped[int] = mapped_column(Integer, default=1)
    rotation: Mapped[list] = mapped_column(JSON, default=list)
    cursor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispatch_seq: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[float] = mapped_column(Double)


class JobEvent(Base):
    """Append-only lifecycle trace: queued → dispatched → completed/failed/recovered."""

    __tablename__ = 'job_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'))
    kind: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[float] = mapped_column(Double)
