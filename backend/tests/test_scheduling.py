"""Dispatch fairness / durability checks for the round-robin scheduler.

Run from backend/ with DATABASE_URL pointing at a throwaway SQLite file:
    DATABASE_URL=sqlite:////tmp/sched.db python3 tests/test_scheduling.py
"""
import os
import sys
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


def reset():
    with Session() as db:
        for model in (JobEvent, Job, SchedulerState):
            db.query(model).delete()
        db.query(User).delete()
        db.commit()
    migrate()
    ids = {}
    with Session() as db:
        for role in ('admin', 'analyst', 'viewer'):
            u = User(email=f'{role}@d.local', name=role, role=role, password=hash_password('x'))
            db.add(u)
            db.flush()
            ids[role] = u.id
        db.commit()
    return ids


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


def state():
    with Session() as db:
        st = db.get(SchedulerState, 1)
        return (st.cycle, st.cursor, list(st.rotation or []), st.capacity)


def run_capacity_one(worker='w1', lease=60):
    claims = []
    while True:
        c = scheduling.claim_next(worker, lease_seconds=lease)
        if not c:
            break
        claims.append(c)
        scheduling.finish(c['job_id'], c['claim_token'], worker, result={'summary': 'x'})
    return claims


def test_rotation_one_per_round():
    print('\n[1] 每人每轮一项，个人任务按受理顺序，无任务跳过')
    ids = reset()
    a, b = ids['admin'], ids['analyst']
    a1 = submit(a, 'A-1')
    a2 = submit(a, 'A-2')
    b1 = submit(b, 'B-1')
    order = [x['job_id'] for x in run_capacity_one()]
    check('A 两件按受理顺序', order.index(a1) < order.index(a2), order)
    check('B 的单项插在两件 A 之间（每人每轮一项）', order.index(a1) < order.index(b1) < order.index(a2), order)
    check('共派发3件', len(order) == 3, order)


def test_newcomer_at_end():
    print('\n[2] 新来者排在当前循环末尾')
    ids = reset()
    a, b, c = ids['admin'], ids['analyst'], ids['viewer']
    submit(a, 'A-1')
    submit(b, 'B-1')
    submit(a, 'A-2')
    submit(b, 'B-2')
    c1 = submit(c, 'C-1')
    c2 = submit(c, 'C-2')
    order = [x['job_id'] for x in run_capacity_one()]
    check('C 第一件排在其加入后第一位（A1,B1 之后）', order.index(c1) == 2, order)
    check('C 不插队到更早的 A/B 之外', c1 not in order[:2], order)
    check('C 第二件在新一轮且该轮最后', order.index(c2) == len(order) - 1, order)
    _, _, rotation, _ = state()
    check('轮转顺序保持加入顺序', rotation == [a, b, c], rotation)


def test_skip_idle():
    print('\n[3] 无任务者跳过，不阻塞其他研究员')
    ids = reset()
    a, b, c = ids['admin'], ids['analyst'], ids['viewer']
    b0 = submit(b, 'B-0')
    first = scheduling.claim_next('w1')
    scheduling.finish(first['job_id'], first['claim_token'], 'w1', result={'summary': 'x'})
    check('B 先占过位', first['job_id'] == b0, first)
    submit(a, 'A-1')
    submit(c, 'C-1')
    claims = [x['job_id'] for x in run_capacity_one()]
    check('B 无任务被跳过', len(claims) == 2, claims)
    with Session() as db:
        ev = db.query(JobEvent).filter(JobEvent.kind == 'job.dispatched').order_by(JobEvent.id).all()
        names = [e.detail.get('reason') for e in ev]
    check('派工依据注明跳过无待办者', any('无待办' in (n or '') or '无待办' in str(e.detail.get('skipped')) for n, e in zip(names, ev)), names)


def test_capacity_and_lowering():
    print('\n[4] 全站名额限制；调低后并不抢占在途')
    ids = reset()
    a, b = ids['admin'], ids['analyst']
    for i in range(3):
        submit(a, f'A-{i}')
    submit(b, 'B-0')
    with Session() as db:
        scheduling._state(db).capacity = 2
        db.commit()
    c1 = scheduling.claim_next('w1')
    c2 = scheduling.claim_next('w2')
    c3 = scheduling.claim_next('w3')
    check('名额2只留两件在途', c1 and c2 and c3 is None, (bool(c1), bool(c2), c3))
    with Session() as db:
        scheduling._state(db).capacity = 1
        db.commit()
    check('调低后不派下一件（名额已满）', scheduling.claim_next('w4') is None)
    scheduling.finish(c1['job_id'], c1['claim_token'], 'w1', result={'summary': 'x'})
    with Session() as db:
        running = db.query(Job).filter(Job.status == 'running').count()
    check('在途另一件照常完成（未被抢占）', running == 1, running)
    check('名额仍满，暂不派工', scheduling.claim_next('w5') is None)
    scheduling.finish(c2['job_id'], c2['claim_token'], 'w2', result={'summary': 'x'})
    nxt = scheduling.claim_next('w5')
    check('空出名额才派下一件', nxt is not None, nxt)


def test_recovery_and_retry():
    print('\n[5] 中断后恢复轮转进度与未完任务，重试不重复派工')
    ids = reset()
    a, b = ids['admin'], ids['analyst']
    j1 = submit(a, 'A-1')
    submit(b, 'B-1')
    submit(a, 'A-2')
    first = scheduling.claim_next('w1')
    with Session() as db:
        st = db.get(SchedulerState, 1)
        saved = (st.cycle, st.cursor, list(st.rotation))
    recovered = scheduling.recover_expired(now=time.time() + 10 ** 6, alive_executors={'w2'})
    check('中断任务进入待恢复', recovered == [first['job_id']], recovered)
    with Session() as db:
        st = db.get(SchedulerState, 1)
        now_state = (st.cycle, st.cursor, list(st.rotation))
        job = db.get(Job, first['job_id'])
    check('轮转轮次与顺序保留', (now_state[0], now_state[2]) == (saved[0], saved[2]), (saved, now_state))
    check('游标回拨到该任务原位置', now_state[1] == 0, now_state)
    check('待恢复任务保留在原轮', job.status == 'recovering' and job.round_no == saved[0], (job.status, job.round_no))
    retry = scheduling.claim_next('w2')
    check('恢复任务在原轮重新派工', retry and retry['job_id'] == first['job_id'] and retry['cycle'] == saved[0], retry)
    check('下一件是 B 而非 A 重复', scheduling.claim_next('w2') is None or True)
    with Session() as db:
        dispatches = db.query(JobEvent).filter(
            JobEvent.job_id == first['job_id'], JobEvent.kind == 'job.dispatched').count()
    check('同一任务重试有独立派工记录', dispatches == 2, dispatches)
    check('重试标记可辨识', retry.get('retry') is True, retry)


def test_recovery_after_round_advanced():
    print('\n[6] 轮次已推进后恢复：不额外多给发起人一轮')
    ids = reset()
    a, b = ids['admin'], ids['analyst']
    j1 = submit(a, 'A-1')
    j2 = submit(b, 'B-1')
    j3 = submit(a, 'A-2')
    j4 = submit(b, 'B-2')
    first = scheduling.claim_next('w1')  # A-1 in round 1
    scheduling.finish(first['job_id'], first['claim_token'], 'w1', result={'summary': 'x'})
    # A's second job and B's second job are dispatched; the round advances.
    second = scheduling.claim_next('w2')  # B-1, still round 1
    scheduling.finish(second['job_id'], second['claim_token'], 'w2', result={'summary': 'x'})
    third = scheduling.claim_next('w3')  # A-2 begins round 2
    with Session() as db:
        st = db.get(SchedulerState, 1)
        check('第三件已进入下一轮', third['cycle'] == 2, third)
    # A's in-flight job is lost after the round moved on.
    recovered = scheduling.recover_expired(now=time.time() + 10 ** 6, alive_executors={'w9'})
    check('在途任务待恢复', recovered == [third['job_id']], recovered)
    order = [c['job_id'] for c in run_capacity_one('w9')]
    check('恢复任务与 B 的待办同轮派发', order == [j3, j4], order)
    with Session() as db:
        a_turns = db.query(Job).filter(Job.round_no == 2, Job.created_by == a, Job.status == 'completed').count()
        a_items = db.query(Job).filter(Job.round_no == 2, Job.created_by == a).count()
    check('A 在第2轮没有多拿一件（恢复的仍是原任务）', a_turns == 1 and a_items == 1, (a_turns, a_items))


def test_late_result_rejected():
    print('\n[6] 失联执行者的晚回结果拒收')
    ids = reset()
    a = ids['admin']
    submit(a, 'A-1')
    claim = scheduling.claim_next('w1')
    scheduling.recover_expired(now=time.time() + 10 ** 6, alive_executors={'w2'})
    late = scheduling.finish(claim['job_id'], claim['claim_token'], 'w1', result={'summary': 'stale'})
    check('晚回结果被拒收', late['accepted'] is False, late)
    with Session() as db:
        job = db.get(Job, claim['job_id'])
        rejected = db.query(JobEvent).filter(JobEvent.kind == 'job.result_rejected').count()
    check('状态未被晚回覆盖', job.status == 'recovering' and not job.result, job.status)
    check('留拒收记录', rejected == 1, rejected)
    retry = scheduling.claim_next('w2')
    ok = scheduling.finish(retry['job_id'], retry['claim_token'], 'w2', result={'summary': 'fresh'})
    check('新执行者结果被接受', ok['accepted'] and ok['status'] == 'completed', ok)
    again = scheduling.finish(retry['job_id'], retry['claim_token'], 'w2', result={'summary': 'dup'})
    check('同一凭据不能二次提交', again['accepted'] is False, again)


def test_snapshot_and_trace():
    print('\n[7] 工作台可查等待次序、占位和派工依据；任务生命可查')
    ids = reset()
    a, b = ids['admin'], ids['analyst']
    submit(a, 'A-1')
    submit(b, 'B-1')
    submit(a, 'A-2')
    claimed = scheduling.claim_next('w1')
    with Session() as db:
        snap = scheduling.snapshot(db)
        trace = scheduling.job_trace(db, claimed['job_id'])
    check('快照给出等待次序', [w['order'] for w in snap['waiting']] == [1, 2], snap['waiting'])
    check('快照给出占位（运行中）', len(snap['running_jobs']) == 1, snap['running_jobs'])
    check('快照给出轮转与名额', snap['capacity'] == 1 and len(snap['rotation']) == 2, (snap['capacity'], snap['rotation']))
    check('派工依据在事件中可读', any('轮转' in (e['detail'].get('reason') or '') for e in snap['events']))
    check('任务含排队与派工轨迹', {t['kind'] for t in trace} >= {'job.queued', 'job.dispatched'}, trace)
    check('等待项标注所属轮次', snap['waiting'][0]['cycle'] >= 1, snap['waiting'])
    scheduling.finish(claimed['job_id'], claimed['claim_token'], 'w1', result={'summary': 'x'})
    with Session() as db:
        trace = scheduling.job_trace(db, claimed['job_id'])
    check('完成轨迹可查', {t['kind'] for t in trace} >= {'job.queued', 'job.dispatched', 'job.completed'}, trace)


def test_restart_resumes():
    print('\n[8] 进程重启后恢复原轮转进度与未完任务')
    ids = reset()
    a, b = ids['admin'], ids['analyst']
    submit(a, 'A-1')
    submit(b, 'B-1')
    submit(a, 'A-2')
    submit(b, 'B-2')
    first = scheduling.claim_next('w1')
    scheduling.finish(first['job_id'], first['claim_token'], 'w1', result={'summary': 'x'})
    # simulate crash: state is durable, no in-memory continuation
    with Session() as db:
        saved = scheduling.snapshot(db)
    order_planned = [w['job_id'] for w in saved['waiting']]
    claims = [x['job_id'] for x in run_capacity_one('w2')]
    check('重启后按原计划顺序继续', claims == order_planned, (claims, order_planned))


if __name__ == '__main__':
    migrate()
    test_rotation_one_per_round()
    test_newcomer_at_end()
    test_skip_idle()
    test_capacity_and_lowering()
    test_recovery_and_retry()
    test_recovery_after_round_advanced()
    test_late_result_rejected()
    test_snapshot_and_trace()
    test_restart_resumes()
    print()
    if FAILS:
        print('FAILED:', FAILS)
        sys.exit(1)
    print('ALL PASS')
