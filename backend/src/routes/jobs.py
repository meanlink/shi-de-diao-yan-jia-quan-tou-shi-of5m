from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.models import Job
from src.schemas.job import JobInput, CapacityInput
from src.middlewares.auth import current_user, can_write, admin_only
from src.services import jobs, scheduling
from src.services.audit import record
from src.utils.serialization import job_dict
router=APIRouter(prefix='/jobs')
@router.get('')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return [job_dict(j) for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(100))]
@router.post('',status_code=201)
def create(data:JobInput,db=Depends(get_db),u=Depends(can_write)):
    j=jobs.create(db,data,u); record(db,u.email,'job.create',{'id':j.id,'params':j.params})
    db.commit(); jobs.wake_worker(); return job_dict(j)
@router.get('/dispatch')
def dispatch_board(db=Depends(get_db),u=Depends(current_user)):
    return scheduling.snapshot(db)
@router.put('/dispatch/capacity')
def set_capacity(data:CapacityInput,db=Depends(get_db),u=Depends(admin_only)):
    result=scheduling.set_capacity(db,data.capacity)
    record(db,u.email,'dispatch.capacity',{'previous':result['previous'],'capacity':result['capacity'],'running':result['running']})
    db.commit(); jobs.wake_worker()
    return {**result,'snapshot':scheduling.snapshot(db)}
@router.post('/dispatch/recover')
def recover(db=Depends(get_db),u=Depends(admin_only)):
    ids=scheduling.recover_expired()
    record(db,u.email,'dispatch.recover',{'jobs':ids}); db.commit(); jobs.wake_worker()
    return {'recovered':ids,'snapshot':scheduling.snapshot(db)}
# Declared before /{id} so "dispatch" is never parsed as a job id.
@router.get('/{id}')
def detail(id:int,db=Depends(get_db),u=Depends(current_user)):
    j=db.get(Job,id)
    if not j: raise HTTPException(404,'分析任务不存在')
    return {**job_dict(j),'trace':scheduling.job_trace(db,id)}
@router.post('/{id}/approve')
def approve(id:int,db=Depends(get_db),u=Depends(admin_only)):
    j=db.get(Job,id)
    if not j: raise HTTPException(404,'分析任务不存在')
    if j.status!='completed':raise HTTPException(409,'仅已完成结果可以批准')
    j.approved=True; record(db,u.email,'job.approve',{'id':id}); db.commit()
    return job_dict(j)
