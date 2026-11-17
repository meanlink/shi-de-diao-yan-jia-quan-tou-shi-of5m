import time
from fastapi import HTTPException
from redis import Redis
from src.config.settings import REDIS_URL
from src.models import Dataset, Job
from src.services import scheduling
from src.services.business import metadata
def create(db, data, user):
    if not db.get(Dataset,data.dataset_id): raise HTTPException(404,'数据批次不存在')
    allowed={p['key']:p for p in metadata().get('parameters',[])}
    if set(data.params)-set(allowed): raise HTTPException(422,'包含未知分析参数')
    params={k:p['default'] for k,p in allowed.items()}
    params.update(data.params)
    for key,p in allowed.items():
        v=params[key]
        if p['type']=='number':
            try:
                v=float(v)
                if not (p.get('min',-1e9)<=v<=p.get('max',1e9)): raise ValueError()
                params[key]=v
            except (ValueError,TypeError): raise HTTPException(422, f'{p["label"]} 超出范围或非数值')
        elif str(v) not in [str(o['value']) for o in p.get('options',[])]:
            raise HTTPException(422,f'{p["label"]} 不是有效选项')
    # Newcomers join the end of the running rotation in the same transaction that
    # accepts the job, so dispatch and its audit trail cannot disagree.
    ticket=scheduling.register_submitter(db,user.id)
    j=Job(name=data.name, dataset_id=data.dataset_id, params=params, created_by=user.id, created_at=time.time(), status='queued', attempts=0, recovery_count=0)
    db.add(j); db.flush()
    scheduling.event(db,j.id,'job.queued',{
        'submitter_id':user.id,'submitter':user.email,'rotation_position':ticket['position'],
        'rotation_size':ticket['rotation_size'],'cycle':ticket['cycle'],
        'reason':f"受理：{user.email} 在当前第{ticket['cycle']}轮轮转中位于第{ticket['position']}位",
    })
    return j
def wake_worker():
    try:
        r=Redis.from_url(REDIS_URL); r.lpush('analysis:queue','wake'); r.ltrim('analysis:queue',0,999); r.close()
    except Exception:
        pass
