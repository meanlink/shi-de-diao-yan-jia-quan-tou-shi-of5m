"""Concurrency check: many claimers racing must never exceed capacity or skip turns.

The in-process lock plus PostgreSQL row locks are the real guard. This test
exercises the same-shaped contention with threads against SQLite to prove the
claim path is all-or-nothing (one job, one claim token, one dispatch record).

Run from backend/:
    DATABASE_URL=sqlite:////tmp/concurrency.db python3 tests/test_concurrency.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migrations.initial import migrate
from src.config.database import Session
from src.models import Job, JobEvent, SchedulerState, User
from src.services import scheduling
from src.utils.security import hash_password

FAILS = []


def check(name, condition, detail=''):
    if condition:
        print(f'  ok   {name}')
    else:
        FAILS.append(name)
        print(f'  FAIL {name} {detail}')


def reset(capacity=2):
    migrate()
    with Session() as db:
        for model in (JobEvent, Job, SchedulerState):
            db.query(model).delete()
        db.query(User).delete()
        db.commit()
    with Session() as db:
        users = []
        for i in range(3):
            u = User(email=f'u{i}@d.local', name=f'u{i}', role='analyst', password=hash_password('x'))
            db.add(u)
            db.flush()
            users.append(u.id)
        st = scheduling._state(db)
        st.capacity = capacity
        db.commit()
    return users


def submit(user_id, name):
    with Session() as db:
        scheduling.register_submitter(db, user_id)
        j = Job(name=name, dataset_id=1, params={}, created_by=user_id,
                created_at=time.time(), status='queued', attempts=0, recovery_count=0)
        db.add(j)
        db.flush()
        scheduling.event(db, j.id, 'job.queued', {'submitter_id': user_id})
        db.commit()
        return j.id


def run():
    users = reset(capacity=3)
    jobs = []
    for i in range(12):
        jobs.append(submit(users[i % 3], f'job-{i}'))

    print('\n[1] 12 个占位请求并发争抢 3 个名额')
    results = []
    lock = threading.Lock()

    def claimer(i):
        c = scheduling.claim_next(f'w{i}')
        with lock:
            results.append(c)

    threads = [threading.Thread(target=claimer, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = [r for r in results if r]
    check('恰好 3 件被领取（不超过名额）', len(wins) == 3, len(wins))
    check('三件互不相同', len({w['job_id'] for w in wins}) == 3, wins)
    check('三个不同执行者胜出（名额未被同一请求占用两次）', len({w['claim_token'] for w in wins}) == 3, wins)
    with Session() as db:
        running = db.query(Job).filter(Job.status == 'running').count()
        dispatched = db.query(JobEvent).filter(JobEvent.kind == 'job.dispatched').count()
        same_round = set()
        for j in db.query(Job).filter(Job.status == 'running'):
            same_round.add(j.round_no)
    check('DB 中在途确实是 3', running == 3, running)
    check('派工记录与领取一一对应', dispatched == 3, dispatched)
    check('并发领取都在同一轮内', same_round == {1}, same_round)
    with Session() as db:
        submitters = [j.created_by for j in db.query(Job).filter(Job.status == 'running')]
    check('三人各取一件（每人每轮一项）', len(set(submitters)) == 3, submitters)

    print('\n[2] 并发释放并重抢：完成与再派工不重复')
    for w in wins:
        scheduling.finish(w['job_id'], w['claim_token'], w['worker'], result={'summary': 'x'})
    with Session() as db:
        completed = db.query(Job).filter(Job.status == 'completed').count()
        running = db.query(Job).filter(Job.status == 'running').count()
    check('三个合法凭据全部完成', completed == 3, completed)
    check('在途清空', running == 0, running)

    print('\n[3] 冒名提交被拒，任务不会被错误完成')
    submit(users[0], 'impostor')
    c = scheduling.claim_next('owner')
    fake = scheduling.finish(c['job_id'], c['claim_token'], 'someone-else', result={'summary': 'x'})
    check('错误执行者被拒', fake['accepted'] is False, fake)
    real = scheduling.finish(c['job_id'], c['claim_token'], 'owner', result={'summary': 'x'})
    check('正确执行者被接受', real['accepted'] is True, real)
    with Session() as db:
        job = db.get(Job, c['job_id'])
    check('任务已完成且结果正确', job.status == 'completed' and job.result.get('summary') == 'x',
          (job.status, job.result))

    print('\n[4] 所有任务最终完成且每件只派工一次')
    scheduling.recover_expired(now=time.time() + 10 ** 6, alive_executors=set())
    drained = 0
    while True:
        c = scheduling.claim_next('clean')
        if not c:
            break
        out = scheduling.finish(c['job_id'], c['claim_token'], 'clean', result={'summary': 'x'})
        if out['accepted']:
            drained += 1
    with Session() as db:
        pending = db.query(Job).filter(Job.status.in_(('queued', 'recovering'))).count()
        per_job = {}
        for e in db.query(JobEvent).filter(JobEvent.kind == 'job.completed'):
            per_job[e.job_id] = per_job.get(e.job_id, 0) + 1
    check('无遗留排队/待恢复', pending == 0, pending)
    check('每件任务最多完成一次', all(v == 1 for v in per_job.values()), per_job)

    print()
    if FAILS:
        print('FAILED:', FAILS)
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    run()
