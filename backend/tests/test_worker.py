"""Worker-side checks: real analysis through a claim, lease renewal, recovery.

Run from backend/:
    DATABASE_URL=sqlite:////tmp/worker.db SEED_DEMO=false python3 tests/test_worker.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('ARTIFACT_DIR', '/tmp/artifacts')

from migrations.initial import migrate
from src.config.database import Session
from src.models import Dataset, Job, JobEvent, SchedulerState, User
from src.services import scheduling
from src.services.business import analyze, metadata, seed_records
from src.utils.security import hash_password
from src.utils.serialization import checksum
import src.worker as worker

FAILS = []


def check(name, condition, detail=''):
    if condition:
        print(f'  ok   {name}')
    else:
        FAILS.append(name)
        print(f'  FAIL {name} {detail}')


def reset():
    migrate()
    with Session() as db:
        for model in (JobEvent, Job, SchedulerState, Dataset):
            db.query(model).delete()
        db.query(User).delete()
        db.commit()
    with Session() as db:
        u = User(email='a@d.local', name='a', role='analyst', password=hash_password('x'))
        db.add(u)
        db.flush()
        rows = seed_records()
        d = Dataset(name=metadata()['dataset_name'], source='seed.csv', records=rows,
                    checksum=checksum(rows), created_at=time.time())
        db.add(d)
        db.commit()
        return u.id, d.id


def submit(user_id, dataset_id, params, name='任务'):
    with Session() as db:
        scheduling.register_submitter(db, user_id)
        j = Job(name=name, dataset_id=dataset_id, params=params, created_by=user_id,
                created_at=time.time(), status='queued', attempts=0, recovery_count=0)
        db.add(j)
        db.flush()
        scheduling.event(db, j.id, 'job.queued', {'submitter_id': user_id})
        db.commit()
        return j.id


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, ex=None):
        self.store[key] = value

    def sadd(self, key, value):
        self.store.setdefault(key, set()).add(value)

    def smembers(self, key):
        return self.store.get(key, set())

    def exists(self, key):
        return 1 if key in self.store else 0

    def srem(self, key, value):
        self.store.get(key, set()).discard(value)


def run():
    user_id, dataset_id = reset()
    params = {p['key']: p['default'] for p in metadata()['parameters']}
    fake = FakeRedis()

    print('\n[1] worker 领取并完成真实分析')
    job_id = submit(user_id, dataset_id, params, '真实计算')
    worker.heartbeat(fake)
    claim = scheduling.claim_next(worker.WORKER_ID)
    check('领取成功', claim is not None and claim['job_id'] == job_id, claim)
    worker.execute(claim)
    with Session() as db:
        job = db.get(Job, job_id)
        kinds = [e.kind for e in db.query(JobEvent).filter(JobEvent.job_id == job_id)]
    check('分析完成', job.status == 'completed', job.status)
    check('结果含加权结论', 'summary' in (job.result or {}) and bool(job.result.get('table')), (job.result or {}).keys())
    check('轨迹含完成事件', 'job.completed' in kinds, kinds)
    check('完成后释放占位', job.claim_token is None and job.lease_expires_at is None, (job.claim_token, job.lease_expires_at))

    print('\n[2] 分析失败如实记录')
    bad_id = submit(user_id, dataset_id, {'weight_mode': 'equal', 'minimum_cell': 5000}, '失败任务')
    claim = scheduling.claim_next(worker.WORKER_ID)
    worker.execute(claim)
    with Session() as db:
        job = db.get(Job, bad_id)
    check('失败态与原因落库', job.status == 'failed' and job.error, (job.status, job.error))

    print('\n[3] 执行期间续租，不会被判失联')
    long_id = submit(user_id, dataset_id, params, '续租任务')
    claim = scheduling.claim_next(worker.WORKER_ID, lease_seconds=0.2)
    time.sleep(0.3)
    # Another worker sees a live heartbeat for WORKER_ID, so it must not steal.
    alive = worker.alive_workers(fake)
    check('心跳可见', worker.WORKER_ID in alive, alive)
    recovered = scheduling.recover_expired(alive_executors=alive)
    check('存活执行者不被抢占', long_id not in recovered, recovered)
    renewed = scheduling.renew_leases(worker.WORKER_ID, lease_seconds=30)
    check('续租生效', renewed == 1, renewed)
    with Session() as db:
        job = db.get(Job, long_id)
    check('租约延长', job.lease_expires_at > time.time() + 20, job.lease_expires_at)
    worker.execute(claim)

    print('\n[4] 执行者失联：任务待恢复，晚回结果拒收')
    lost_id = submit(user_id, dataset_id, params, '失联任务')
    claim = scheduling.claim_next('ghost-worker')
    with Session() as db:
        db.get(Job, lost_id).lease_expires_at = time.time() - 1
        db.commit()
    stale = scheduling.claim_next('ghost-worker')
    check('同一名额下不会重复领取', stale is None, stale)
    alive = worker.alive_workers(fake)
    check('失联者不在心跳集合', 'ghost-worker' not in alive, alive)
    recovered = scheduling.recover_expired(alive_executors=alive)
    check('失联任务进入待恢复', lost_id in recovered, recovered)
    late = scheduling.finish(claim['job_id'], claim['claim_token'], 'ghost-worker',
                             result={'summary': 'late'})
    check('晚回结果拒收', late['accepted'] is False, late)
    retry = scheduling.claim_next(worker.WORKER_ID)
    check('恢复任务优先重派', retry and retry['job_id'] == lost_id, retry)
    worker.execute(retry)
    with Session() as db:
        job = db.get(Job, lost_id)
        rejected = db.query(JobEvent).filter(JobEvent.job_id == lost_id,
                                             JobEvent.kind == 'job.result_rejected').count()
    check('重试后完成', job.status == 'completed', job.status)
    check('拒收有据可查', rejected == 1, rejected)

    print('\n[5] worker.tick 自动恢复失联任务并派工')
    with Session() as db:
        scheduling._state(db).capacity = 2
        db.commit()
    stranded_id = submit(user_id, dataset_id, params, '孤儿任务')
    c = scheduling.claim_next('ghost-worker')
    with Session() as db:
        db.get(Job, stranded_id).lease_expires_at = time.time() - 1
        db.commit()
    # ghost-worker is not in the heartbeat set, so tick reclaims and re-runs it.
    worker.tick(fake)
    deadline = time.time() + 10
    while time.time() < deadline:
        with Session() as db:
            job = db.get(Job, stranded_id)
        if job.status in ('completed', 'failed'):
            break
        time.sleep(0.05)
    check('tick 恢复并完成孤儿任务', job.status == 'completed', job.status)

    print()
    if FAILS:
        print('FAILED:', FAILS)
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    run()
