import os, time, json, logging, threading, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from redis import Redis
from sqlalchemy import select
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Dataset, Job, Report
from src.services import scheduling
from src.services.business import analyze
from src.services.reports import deliver
from src.services.audit import record

logging.basicConfig(level=logging.INFO)

# Each worker has a stable identity. Its heartbeat key in Redis is what lets
# other workers and the recovery path tell "still working" from "disconnected".
WORKER_ID = os.getenv('WORKER_ID') or f"worker-{uuid.uuid4().hex[:8]}"
HEARTBEAT_TTL = 15
HEARTBEAT_KEY = f'analysis:worker:{WORKER_ID}'
WORKER_SET = 'analysis:workers'
_STOP = threading.Event()
# Upper bound on analyses this container can run at once; the real gate is the
# site-wide slot count checked inside scheduling.claim_next(). Claims are safe
# from these threads because the claim path is serialised (process lock + row
# locks) and each claim carries its own token.
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix='analysis')


def heartbeat(redis_client):
    try:
        redis_client.set(HEARTBEAT_KEY, '1', ex=HEARTBEAT_TTL)
        redis_client.sadd(WORKER_SET, WORKER_ID)
    except Exception:
        pass


def alive_workers(redis_client):
    """Return the set of worker ids with a live heartbeat, or None if unknown."""
    try:
        ids = redis_client.smembers(WORKER_SET) or set()
        alive = set()
        for raw in ids:
            wid = raw.decode() if isinstance(raw, bytes) else str(raw)
            if redis_client.exists(f'analysis:worker:{wid}'):
                alive.add(wid)
            else:
                redis_client.srem(WORKER_SET, raw)
        alive.add(WORKER_ID)
        return alive
    except Exception:
        return None


def _keep_alive(redis_client):
    """Extend our own heartbeats/leases while a long analysis is running."""
    while not _STOP.is_set():
        heartbeat(redis_client)
        try:
            scheduling.renew_leases(WORKER_ID)
        except Exception:
            logging.exception('Lease renewal failed')
        _STOP.wait(HEARTBEAT_TTL / 3)


def _run(claim):
    try:
        execute(claim)
    except Exception:
        logging.exception('Execute failed')
        scheduling.finish(claim['job_id'], claim['claim_token'], WORKER_ID,
                          error='执行中断，等待重新派工')


def execute(claim):
    """Run one claimed job to completion and report under its claim token."""
    job_id, token = claim['job_id'], claim['claim_token']
    with Session() as db:
        j = db.get(Job, job_id)
        if not j or j.status != 'running' or j.claim_token != token:
            return
        try:
            d = db.get(Dataset, j.dataset_id)
            result = analyze(d.records, j.params)
            json.dumps(result, allow_nan=False)
        except Exception as e:
            logging.exception('Analysis failed')
            scheduling.finish(job_id, token, WORKER_ID, error=str(e))
            return
    scheduling.finish(job_id, token, WORKER_ID, result=result)


def deliver_due_reports():
    with Session() as db:
        for r in db.scalars(select(Report).where(Report.enabled == True, Report.next_run <= time.time())):
            d = deliver(db, r)
            if d:
                record(db, 'worker', 'report.delivered', {'id': r.id, 'delivery': d.id})
        db.commit()


def tick(redis_client):
    heartbeat(redis_client)
    # Reclaim work whose executor went away, then fill free slots fairly.
    scheduling.recover_expired(alive_executors=alive_workers(redis_client))
    while True:
        claim = scheduling.claim_next(WORKER_ID)
        if not claim:
            break
        _POOL.submit(_run, claim)
    deliver_due_reports()


def main():
    r = Redis.from_url(REDIS_URL, socket_timeout=3)
    Path('/tmp/heartbeat').touch()
    keeper = threading.Thread(target=_keep_alive, args=(r,), daemon=True)
    keeper.start()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            heartbeat(r)
            # Redis only wakes us up; the durable SQL queue decides what runs.
            r.blpop('analysis:queue', timeout=1)
            tick(r)
        except Exception:
            logging.exception('Worker retry')
            time.sleep(2)


if __name__ == '__main__':
    main()
