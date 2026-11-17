"""End-to-end API checks for dispatch: permissions, capacity, trace, legacy results.

Run from backend/:
    DATABASE_URL=sqlite:////tmp/api.db SEED_DEMO=false python3 tests/test_dispatch_api.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('SEED_DEMO', 'false')

from fastapi.testclient import TestClient

from migrations.initial import migrate
from src.config.database import Session
from src.models import Dataset, Delivery, Job, JobEvent, Report, SchedulerState, User
from src.services import jobs as jobs_service
from src.services.business import analyze, metadata, seed_records
from src.services.reports import deliver
from src.utils.security import hash_password, token_for
from src.utils.serialization import checksum

# The worker's Redis wake-up is optional; keep the API test offline.
jobs_service.wake_worker = lambda: None

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
        for model in (Delivery, JobEvent, Job, Report, SchedulerState, Dataset):
            db.query(model).delete()
        db.query(User).delete()
        db.commit()
    tokens = {}
    with Session() as db:
        for role, name in (('admin', '负责人'), ('analyst', '分析员'), ('viewer', '观察员')):
            u = User(email=f'{role}@d.local', name=name, role=role, password=hash_password('Demo123!'))
            db.add(u)
            db.flush()
            tokens[role] = token_for(u)
        rows = seed_records()
        db.add(Dataset(name=metadata()['dataset_name'], source='seed.csv', records=rows,
                       checksum=checksum(rows), created_at=time.time()))
        db.commit()
        dataset_id = db.query(Dataset).first().id
    return tokens, dataset_id


def auth(token):
    return {'Authorization': f'Bearer {token}'}


def run():
    from src.main import app
    tokens, dataset_id = reset()
    client = TestClient(app)
    admin, analyst, viewer = tokens['admin'], tokens['analyst'], tokens['viewer']
    params = {p['key']: p['default'] for p in metadata()['parameters']}

    print('\n[1] 权限：只有管理员能调整名额与触发恢复')
    r = client.put('/jobs/dispatch/capacity', json={'capacity': 2}, headers=auth(analyst))
    check('分析员不可调整名额', r.status_code == 403, r.status_code)
    r = client.put('/jobs/dispatch/capacity', json={'capacity': 2}, headers=auth(viewer))
    check('观察员不可调整名额', r.status_code == 403, r.status_code)
    r = client.put('/jobs/dispatch/capacity', json={'capacity': 99}, headers=auth(admin))
    check('超出范围的名额被拒', r.status_code == 422, r.status_code)
    r = client.put('/jobs/dispatch/capacity', json={'capacity': 2}, headers=auth(admin))
    check('管理员可设名额', r.status_code == 200 and r.json()['capacity'] == 2, r.text[:200])
    r = client.post('/jobs/dispatch/recover', headers=auth(analyst))
    check('分析员不可触发恢复', r.status_code == 403, r.status_code)

    print('\n[2] 提交与分析：受理即入轮转，队列可见')
    ids = {}
    for role, token, name in (('analyst', analyst, '分析员甲'), ('analyst', analyst, '分析员乙')):
        r = client.post('/jobs', json={'name': name, 'dataset_id': dataset_id, 'params': params},
                        headers=auth(token))
        check(f'提交 {name}', r.status_code == 201, r.text[:200])
        ids[name] = r.json()['id']
    r = client.post('/jobs', json={'name': '观察员越权', 'dataset_id': dataset_id, 'params': params},
                    headers=auth(viewer))
    check('观察员不能提交', r.status_code == 403, r.status_code)

    r = client.get('/jobs/dispatch', headers=auth(viewer))
    snap = r.json()
    check('只读角色也能查看调度台', r.status_code == 200, r.status_code)
    check('轮转含两名发起人（同一人合并）', len(snap['rotation']) == 1, snap['rotation'])
    check('等待次序含两件', len(snap['waiting']) == 2, snap['waiting'])
    check('名额为2但尚无人占位', snap['running'] == 0 and snap['available'] == 2, snap['running'])

    print('\n[3] 并发派工守住名额')
    from src.services import scheduling
    claims = [scheduling.claim_next(f'w{i}') for i in range(4)]
    taken = [c for c in claims if c]
    check('名额2最多派两件', len(taken) == 2, len(taken))
    check('两件分别占位', len({c['job_id'] for c in taken}) == 2, taken)
    r = client.put('/jobs/dispatch/capacity', json={'capacity': 1}, headers=auth(admin))
    check('调低到1成功且在途保留', r.status_code == 200 and r.json()['running'] == 2, r.text[:300])
    check('空位为0', r.json()['available'] == 0, r.json())

    print('\n[4] 任务生命与派工依据可查')
    job_id = taken[0]['job_id']
    r = client.get(f'/jobs/{job_id}', headers=auth(viewer))
    detail = r.json()
    kinds = [t['kind'] for t in detail['trace']]
    check('任务详情含排队轨迹', 'job.queued' in kinds, kinds)
    check('任务详情含派工轨迹', 'job.dispatched' in kinds, kinds)
    dispatched = [t for t in detail['trace'] if t['kind'] == 'job.dispatched'][0]
    check('派工依据写明轮转理由', '轮转' in dispatched['detail']['reason'], dispatched['detail'])
    check('派工依据含名额', dispatched['detail']['capacity'] == 2, dispatched['detail'].get('capacity'))

    print('\n[5] 晚回结果拒收')
    claim = taken[0]
    who = claim['job_id']
    with Session() as db:
        job = db.get(Job, who)
        job.lease_expires_at = time.time() - 5
        db.commit()
    recovered = scheduling.recover_expired(alive_executors={'w9'})
    check('过期租约被恢复', who in recovered, recovered)
    late = scheduling.finish(claim['job_id'], claim['claim_token'], 'w0',
                             result={'summary': 'late'})
    check('晚回结果拒收', late['accepted'] is False, late)
    r = client.get(f'/jobs/{claim["job_id"]}', headers=auth(admin))
    check('任务处于待恢复', r.json()['status'] == 'recovering', r.json()['status'])
    r = client.get('/jobs/dispatch', headers=auth(admin))
    check('调度台列出待恢复任务', any(x['job_id'] == claim['job_id'] for x in r.json()['recovering']), r.json()['recovering'])

    print('\n[6] 历史成功结果与简报不受影响')
    with Session() as db:
        admin_user = db.query(User).filter(User.role == 'admin').first()
        rows = db.query(Dataset).first().records
        result = analyze(rows, params)
        job = Job(name='历史基线', dataset_id=dataset_id, params=params, status='completed',
                  result=result, approved=True, created_by=admin_user.id,
                  created_at=time.time() - 7200, finished_at=time.time() - 7190)
        db.add(job)
        db.flush()
        report = Report(name='历史简报', job_id=job.id, destination='业务收件箱',
                        interval_seconds=86400, next_run=time.time() + 3600, enabled=True)
        db.add(report)
        db.flush()
        delivery = deliver(db, report)
        db.commit()
        history_id, delivery_id = job.id, delivery.id
    r = client.get(f'/jobs/{history_id}', headers=auth(viewer))
    check('历史结果可读且无调度轨迹', r.status_code == 200 and r.json()['result']['summary'] and r.json()['trace'] == [], r.text[:120])
    r = client.get(f'/jobs/{history_id}', headers=auth(viewer))
    check('历史结果仍可批准状态', r.json()['approved'] is True, r.json()['approved'])
    r = client.get('/deliveries', headers=auth(viewer))
    check('历史简报归档可用', any(d['id'] == delivery_id for d in r.json()), r.json())
    r = client.get('/reports', headers=auth(viewer))
    check('历史分发计划可用', any(x['job_id'] == history_id for x in r.json()), r.json())
    r = client.get('/overview', headers=auth(viewer))
    check('总览统计不受影响', r.status_code == 200 and r.json()['jobs'] >= 3, r.json())

    print('\n[7] 恢复后继续按轮转派工')
    claims_now = []
    while True:
        c = scheduling.claim_next('w9')
        if not c:
            break
        claims_now.append(c)
        scheduling.finish(c['job_id'], c['claim_token'], 'w9', result={'summary': 'ok'})
    check('恢复任务被重新派工完成', any(c['job_id'] == claim['job_id'] for c in claims_now), claims_now)
    with Session() as db:
        finished = db.get(Job, claim['job_id'])
    check('重试后状态完成', finished.status == 'completed', finished.status)

    print()
    if FAILS:
        print('FAILED:', FAILS)
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    run()
