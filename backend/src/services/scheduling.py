"""Fair round-robin dispatch for analysis jobs.

Rules implemented here:

* queued work is served per submitter: one item per submitter per round, and a
  submitter's own items in acceptance order;
* submitters with no pending item are skipped; a newcomer joins the end of the
  running rotation;
* a site-wide running-slot count caps concurrency. Lowering it lets in-flight
  analyses finish; a new item is dispatched only once a slot is free;
* the dispatch record (JobEvent), the rotation cursor and the job claim are all
  written in one transaction, so the log and the claim cannot disagree;
* SchedulerState is durable, so a restart resumes the same rotation cursor and
  unfinished jobs; recovering a job rewinds the cursor to that job's position in
  its original round, so the retry is not deferred to a later round and the
  submitter is not granted a second turn;
* a result is accepted only from the executor that still owns the claim token,
  so a late result from a disconnected executor is rejected.
"""
import os
import threading
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.config.database import Session
from src.models import Job, JobEvent, SchedulerState, User

CAPACITY_MIN, CAPACITY_MAX = 1, 8
ELIGIBLE_STATES = ('queued', 'recovering')
DEFAULT_LEASE_SECONDS = float(os.getenv('JOB_LEASE_SECONDS', '30'))
DEFAULT_CAPACITY = int(os.getenv('SITE_CAPACITY', '1'))

# Process-local guard. PostgreSQL row locks give the cross-process guarantee;
# this also keeps SQLite local runs and same-process API/worker calls serialised.
_LOCK = threading.RLock()


def _now():
    return time.time()


def _default_capacity():
    try:
        value = int(DEFAULT_CAPACITY)
    except (TypeError, ValueError):
        value = 1
    return max(CAPACITY_MIN, min(CAPACITY_MAX, value))


def _state(db, lock=True):
    st = db.get(SchedulerState, 1, with_for_update=lock, populate_existing=True)
    if st is not None:
        return st
    st = SchedulerState(
        id=1, capacity=_default_capacity(), cycle=1, rotation=[],
        cursor=0, dispatch_seq=0, updated_at=_now(),
    )
    try:
        with db.begin_nested():
            db.add(st)
            db.flush()
    except IntegrityError:
        st = db.get(SchedulerState, 1, with_for_update=lock, populate_existing=True)
    return st


def event(db, job_id, kind, detail=None):
    db.add(JobEvent(job_id=job_id, kind=kind, detail=detail or {}, at=_now()))


def register_submitter(db, user_id):
    """Append a newcomer to the end of the current rotation (idempotent)."""
    with _LOCK:
        st = _state(db)
        rotation = list(st.rotation or [])
        appended = user_id not in rotation
        if appended:
            rotation.append(user_id)
            st.rotation = rotation
            if st.cursor is None:
                st.cursor = 0
            st.updated_at = _now()
            db.flush()
        return {
            'appended': appended,
            'position': rotation.index(user_id) + 1,
            'rotation_size': len(rotation),
            'cycle': st.cycle or 1,
        }


def _queued_jobs(db, rotation):
    if not rotation:
        return []
    return list(db.scalars(
        select(Job)
        .where(Job.created_by.in_(rotation), Job.status.in_(ELIGIBLE_STATES))
        .order_by(Job.created_at, Job.id)
    ))


def _served_users(db, cycle):
    """Submitters who already took their one turn in ``cycle``.

    A recovering job does not count as served: it is an unfinished task being
    restored, not a completed turn.
    """
    return set(db.scalars(
        select(Job.created_by).where(
            Job.round_no == cycle, Job.status != 'recovering'
        )
    ))


def _eligible(job, cycle):
    # A recovered job keeps its original round number, so it must stay eligible
    # in that round; otherwise the retry would slip into a later round.
    return job.round_no is None or job.round_no < cycle or job.status == 'recovering'


def _plan(rotation, cursor, cycle, served, pending):
    """Project the dispatch order without mutating stored state.

    Returns a list of ``(job, meta)`` in dispatch order.
    """
    size = len(rotation)
    if not size or not pending:
        return []
    remaining = {}
    for job in pending:
        remaining.setdefault(job.created_by, []).append(job)
    served_by_cycle = {cycle: set(served)}
    order = []
    current_cycle = cycle
    start = cursor if cursor is not None and 0 <= cursor < size else 0
    guard = 0
    guard_limit = len(pending) + len(rotation) + 10
    while any(remaining.values()) and guard < guard_limit:
        guard += 1
        current_served = served_by_cycle.setdefault(current_cycle, set())
        skipped = []
        advanced = current_cycle != cycle
        for idx in range(start, size):
            if not any(remaining.values()):
                break
            user_id = rotation[idx]
            if user_id in current_served:
                skipped.append({'user_id': user_id, 'reason': '本轮已领取'})
                continue
            items = remaining.get(user_id) or []
            candidate = next((j for j in items if _eligible(j, current_cycle)), None)
            if candidate is None:
                skipped.append({'user_id': user_id, 'reason': '无待办'})
                continue
            items.remove(candidate)
            current_served.add(user_id)
            order.append((candidate, {
                'cycle': current_cycle, 'position': idx, 'round_size': size,
                'skipped': list(skipped), 'advanced_round': advanced,
            }))
            skipped = []
            advanced = False
        current_cycle += 1
        start = 0
    return order


def _pick(db, st):
    rotation = list(st.rotation or [])
    pending = _queued_jobs(db, rotation)
    plan = _plan(rotation, st.cursor, st.cycle or 1,
                 _served_users(db, st.cycle or 1), pending)
    if not plan:
        return None
    job, meta = plan[0]
    nxt = meta['position'] + 1
    meta['next_cycle'] = meta['cycle'] + (1 if nxt >= meta['round_size'] else 0)
    meta['next_cursor'] = 0 if nxt >= meta['round_size'] else nxt
    return job, meta


def claim_next(worker_id, lease_seconds=None):
    """Atomically reserve the next fair job for ``worker_id`` or return None."""
    lease_seconds = lease_seconds or DEFAULT_LEASE_SECONDS
    with _LOCK:
        with Session() as db:
            st = _state(db)
            now = _now()
            running = db.scalar(select(func.count()).select_from(Job).where(Job.status == 'running')) or 0
            if running >= st.capacity:
                db.rollback()
                return None
            picked = _pick(db, st)
            if picked is None:
                db.rollback()
                return None
            job, meta = picked
            # Lock the chosen row too: the state row serialises claimers, this
            # makes the claim itself safe if anything else touches the job.
            job = db.get(Job, job.id, with_for_update=True, populate_existing=True)
            st.cycle = meta['next_cycle']
            st.cursor = meta['next_cursor']
            st.dispatch_seq = (st.dispatch_seq or 0) + 1
            st.updated_at = now
            rotation = list(st.rotation or [])
            users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(rotation)))}
            submitter = users.get(job.created_by)
            skipped_names = [
                (users[s['user_id']].email if s['user_id'] in users else str(s['user_id'])) + f"（{s['reason']}）"
                for s in meta['skipped']
            ]
            retry = job.status == 'recovering'
            reason = (
                ('恢复重试：' if retry else '')
                + f"第{meta['cycle']}轮按发起人轮转：第{meta['position'] + 1}位"
                + f"（{submitter.email if submitter else job.created_by}）领取其最早受理任务"
                + (f"；跳过{', '.join(skipped_names)}" if skipped_names else '')
                + ('；本轮已无可派任务，进入下一轮' if meta['advanced_round'] else '')
            )
            job.status = 'running'
            job.executor = worker_id
            job.claim_token = uuid.uuid4().hex
            job.round_no = meta['cycle']
            job.round_pos = meta['position']
            job.dispatch_seq = st.dispatch_seq
            job.dispatched_at = now
            job.lease_expires_at = now + lease_seconds
            job.attempts = (job.attempts or 0) + 1
            db.flush()
            waiting_after = len(_queued_jobs(db, rotation))
            event(db, job.id, 'job.dispatched', {
                'cycle': meta['cycle'], 'position': meta['position'] + 1, 'round_size': meta['round_size'],
                'submitter_id': job.created_by, 'submitter': submitter.email if submitter else None,
                'slot_seq': st.dispatch_seq, 'worker': worker_id, 'reason': reason,
                'retry': retry, 'attempt': job.attempts,
                'skipped': meta['skipped'], 'advanced_round': meta['advanced_round'],
                'waiting_after': waiting_after, 'running_before': running, 'capacity': st.capacity,
            })
            db.commit()
            return {
                'job_id': job.id, 'claim_token': job.claim_token, 'worker': worker_id,
                'cycle': meta['cycle'], 'slot': st.dispatch_seq,
                'retry': retry, 'reason': reason,
            }


def finish(job_id, claim_token, worker_id, result=None, error=None):
    """Finalise a claim; reject results from a stale/disconnected executor."""
    with _LOCK:
        with Session() as db:
            job = db.get(Job, job_id, with_for_update=True, populate_existing=True)
            if job is None:
                return {'accepted': False, 'reason': 'missing'}
            if (job.status != 'running' or not job.claim_token
                    or job.claim_token != claim_token or job.executor != worker_id):
                event(db, job.id, 'job.result_rejected', {
                    'worker': worker_id, 'status': job.status, 'current_executor': job.executor,
                    'reason': '任务已恢复或已重新派工，失联执行者的晚回结果拒收',
                })
                db.commit()
                return {'accepted': False, 'reason': 'stale_claim'}
            failed = error is not None
            if failed:
                job.status = 'failed'
                job.error = str(error)[:990]
            else:
                job.status = 'completed'
                job.result = result or {}
                job.error = ''
            job.finished_at = _now()
            job.claim_token = None
            job.lease_expires_at = None
            event(db, job.id, 'job.failed' if failed else 'job.completed', {
                'worker': worker_id, 'cycle': job.round_no,
                'error': job.error if failed else None,
            })
            db.commit()
            return {'accepted': True, 'status': job.status}


def recover_expired(now=None, alive_executors=None):
    """Restore jobs owned by dead/expired executors to ``recovering`` (待恢复).

    The rotation cursor is rewound to the job's position in its original round,
    so the restored task is retried inside that round instead of being pushed to
    the next one.
    """
    now = now or _now()
    recovered = []
    with _LOCK:
        with Session() as db:
            st = _state(db)
            candidates = list(db.scalars(select(Job.id).where(Job.status == 'running')))
            for job_id in candidates:
                job = db.get(Job, job_id, with_for_update=True, populate_existing=True)
                if job is None or job.status != 'running':
                    continue
                stale_lease = job.lease_expires_at is None or job.lease_expires_at <= now
                lost_executor = bool(
                    alive_executors is not None and job.executor
                    and job.executor not in alive_executors
                )
                lease_orphan = bool(
                    stale_lease and (alive_executors is None or not job.executor
                                     or job.executor not in alive_executors)
                )
                if not (lost_executor or lease_orphan):
                    continue
                old_executor = job.executor
                job.status = 'recovering'
                job.executor = None
                job.claim_token = None
                job.lease_expires_at = None
                job.recovery_count = (job.recovery_count or 0) + 1
                rotation = list(st.rotation or [])
                rewind = None
                if job.round_no is not None and job.round_no < st.cycle:
                    # The rotation already moved on. Re-attach the unfinished job
                    # to the current round so retrying it does not skip ahead, and
                    # do not hand its submitter a second turn if they already had
                    # one in this round (the plan will defer it to the next round).
                    job.round_no = st.cycle
                    if job.created_by in rotation and job.created_by not in _served_users(db, st.cycle):
                        rewind = rotation.index(job.created_by)
                elif job.round_no == st.cycle and job.round_pos is not None:
                    rewind = job.round_pos
                if rewind is not None and 0 <= rewind < len(rotation):
                    st.cursor = min(st.cursor if st.cursor is not None else rewind, rewind)
                event(db, job.id, 'job.recovered', {
                    'old_executor': old_executor, 'round_no': job.round_no,
                    'round_pos': job.round_pos,
                    'reason': '执行者失联' if lost_executor else '执行租约过期',
                    'round_preserved': job.round_no,
                    'cursor_after': st.cursor,
                    'attempts': job.attempts or 0,
                })
                recovered.append(job.id)
            if recovered:
                st.updated_at = now
                db.commit()
            else:
                db.rollback()
    return recovered


def renew_leases(worker_id, lease_seconds=None):
    """Extend leases of jobs this executor still legitimately owns."""
    lease_seconds = lease_seconds or DEFAULT_LEASE_SECONDS
    with _LOCK:
        with Session() as db:
            jobs = list(db.scalars(select(Job).where(
                Job.status == 'running', Job.executor == worker_id, Job.claim_token.is_not(None)
            )))
            if not jobs:
                db.rollback()
                return 0
            deadline = _now() + lease_seconds
            for job in jobs:
                job.lease_expires_at = deadline
            db.commit()
            return len(jobs)


def set_capacity(db, capacity):
    if not isinstance(capacity, int) or isinstance(capacity, bool) or not CAPACITY_MIN <= capacity <= CAPACITY_MAX:
        raise HTTPException(422, f'全站运行名额须为 {CAPACITY_MIN} 至 {CAPACITY_MAX} 的整数')
    with _LOCK:
        st = _state(db)
        previous = st.capacity
        st.capacity = capacity
        st.updated_at = _now()
        running = db.scalar(select(func.count()).select_from(Job).where(Job.status == 'running')) or 0
        db.flush()
        return {'previous': previous, 'capacity': capacity, 'running': running,
                'available': max(0, capacity - running)}


def snapshot(db):
    st = db.get(SchedulerState, 1, populate_existing=True)
    if st is None:
        # Read-only view for a site that has not dispatched anything yet; do not
        # create a row from a GET request.
        st = SchedulerState(
            id=1, capacity=_default_capacity(), cycle=1, rotation=[],
            cursor=0, dispatch_seq=0, updated_at=_now(),
        )
    rotation = list(st.rotation or [])
    cycle = st.cycle or 1
    pending = _queued_jobs(db, rotation)
    running_jobs = list(db.scalars(
        select(Job).where(Job.status == 'running').order_by(Job.dispatched_at, Job.id)
    ))
    plan = _plan(rotation, st.cursor, cycle, _served_users(db, cycle), pending)
    waiting = [{
        'order': index + 1, 'job_id': job.id, 'name': job.name,
        'submitter_id': job.created_by, 'status': job.status,
        'cycle': meta['cycle'], 'position': meta['position'] + 1,
        'round_no': job.round_no, 'attempts': job.attempts or 0,
        'created_at': job.created_at, 'advanced_round': meta['advanced_round'],
    } for index, (job, meta) in enumerate(plan)]

    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(rotation)))}
    running_by_user, waiting_by_user = {}, {}
    for job in running_jobs:
        running_by_user.setdefault(job.created_by, []).append(job)
    for job in pending:
        waiting_by_user[job.created_by] = waiting_by_user.get(job.created_by, 0) + 1
    served = _served_users(db, cycle)
    rotation_rows = []
    for idx, user_id in enumerate(rotation):
        user = users.get(user_id)
        mine_running = running_by_user.get(user_id, [])
        mine_waiting = waiting_by_user.get(user_id, 0)
        if mine_running:
            state_label = '占位计算中'
        elif user_id in served:
            state_label = '本轮已领取'
        elif mine_waiting:
            state_label = '等待本轮'
        else:
            state_label = '空闲'
        rotation_rows.append({
            'position': idx + 1, 'user_id': user_id,
            'name': user.name if user else str(user_id), 'email': user.email if user else '',
            'state': state_label, 'waiting': mine_waiting,
            'running': [{'job_id': j.id, 'name': j.name} for j in mine_running],
        })

    events = list(db.scalars(select(JobEvent).order_by(JobEvent.id.desc()).limit(40)))
    job_ids = ({e.job_id for e in events} | {j.id for j in pending}
               | {j.id for j in running_jobs})
    jobs = {j.id: j for j in db.scalars(select(Job).where(Job.id.in_(job_ids)))} if job_ids else {}

    # Resolve every submitter the board references so it never shows a bare id.
    referenced = ({j.created_by for j in running_jobs} | {j.created_by for j in pending}
                  | {jobs[e.job_id].created_by for e in events if e.job_id in jobs})
    missing = referenced - set(users)
    if missing:
        for user in db.scalars(select(User).where(User.id.in_(missing))):
            users[user.id] = user

    def who(user_id):
        user = users.get(user_id) if user_id is not None else None
        return {
            'submitter_id': user_id,
            'submitter': user.name if user else (str(user_id) if user_id is not None else None),
            'submitter_email': user.email if user else None,
        }

    for row in waiting:
        row.update(who(row['submitter_id']))
    event_rows = [{
        'id': e.id, 'job_id': e.job_id, 'kind': e.kind, 'at': e.at,
        'job_name': jobs[e.job_id].name if e.job_id in jobs else f'#{e.job_id}',
        **who(jobs[e.job_id].created_by if e.job_id in jobs else None),
        'detail': dict(e.detail or {}),
    } for e in events]

    running_count = len(running_jobs)
    return {
        'capacity': st.capacity, 'cycle': cycle, 'cursor': st.cursor or 0,
        'dispatch_seq': st.dispatch_seq or 0, 'running': running_count,
        'available': max(0, st.capacity - running_count),
        'lease_seconds': DEFAULT_LEASE_SECONDS, 'updated_at': st.updated_at,
        'rotation': rotation_rows,
        'waiting': waiting,
        'running_jobs': [{
            'job_id': j.id, 'name': j.name, 'executor': j.executor, 'cycle': j.round_no,
            'position': (j.round_pos + 1) if j.round_pos is not None else None,
            'dispatch_seq': j.dispatch_seq, 'dispatched_at': j.dispatched_at,
            'lease_expires_at': j.lease_expires_at, 'attempts': j.attempts or 0,
            **who(j.created_by),
        } for j in running_jobs],
        'recovering': [{
            'job_id': j.id, 'name': j.name, 'round_no': j.round_no,
            'round_pos': j.round_pos, 'recovery_count': j.recovery_count or 0,
            'attempts': j.attempts or 0, **who(j.created_by),
        } for j in pending if j.status == 'recovering'],
        'events': event_rows,
    }


def job_trace(db, job_id):
    """Lifecycle rows for one job: queued → dispatched → completed/failed/recovered."""
    return [{
        'id': e.id, 'kind': e.kind, 'at': e.at, 'detail': dict(e.detail or {}),
    } for e in db.scalars(
        select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
    )]
