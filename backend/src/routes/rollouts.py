from fastapi import APIRouter,Depends,Query
from src.config.database import db_session
from src.utils.security import current,writer
from src.schemas.rollout import RolloutInput,RelinkInput,RowInput,InventoryInput,EnableInput,DeleteParentInput
from src.services import rollouts as service
router=APIRouter()
@router.get('/rollouts')
def listing(report_id:int|None=Query(default=None),status:str|None=Query(default=None),db=Depends(db_session),user=Depends(current)):
    return service.listing(db,report_id,status)
@router.post('/rollouts',status_code=201)
def create(data:RolloutInput,db=Depends(db_session),user=Depends(writer)):return service.create(db,data,user)
@router.get('/rollouts/{ident}')
def detail(ident:int,db=Depends(db_session),user=Depends(current)):return service.detail(db,ident)
@router.get('/rollouts/{ident}/rows')
def detail_rows(ident:int,offset:int=0,limit:int=50,db=Depends(db_session),user=Depends(current)):
    return service.detail_rows(db,ident,offset,limit)
@router.post('/rollouts/{ident}/inventory',status_code=201)
def scan(ident:int,data:InventoryInput|None=None,db=Depends(db_session),user=Depends(writer)):
    return service.scan(db,ident,data or InventoryInput(),user)
@router.get('/rollouts/{ident}/orphans')
def orphans(ident:int,status:str|None=Query(default=None),db=Depends(db_session),user=Depends(current)):
    return service.orphan_listing(db,ident,status)
@router.post('/rollouts/{ident}/orphans/{orphan_id}/relink')
def relink_one(ident:int,orphan_id:int,data:RelinkInput|None=None,db=Depends(db_session),user=Depends(writer)):
    payload=data or RelinkInput(orphan_id=orphan_id)
    if payload.orphan_id is None:payload.orphan_id=orphan_id
    return service.relink(db,ident,payload,user)
@router.post('/rollouts/{ident}/relink')
def relink(ident:int,data:RelinkInput,db=Depends(db_session),user=Depends(writer)):
    return service.relink(db,ident,data,user)
@router.post('/rollouts/{ident}/details',status_code=201)
def add_details(ident:int,data:RowInput,db=Depends(db_session),user=Depends(writer)):
    return service.add_detail_rows(db,ident,data,user)
@router.post('/rollouts/{ident}/parents',status_code=201)
def add_parents(ident:int,data:RowInput,db=Depends(db_session),user=Depends(writer)):
    return service.add_parent_rows(db,ident,data,user)
@router.delete('/rollouts/{ident}/parents')
def delete_parent(ident:int,data:DeleteParentInput,db=Depends(db_session),user=Depends(writer)):
    return service.delete_parent_row(db,ident,data,user)
@router.post('/rollouts/{ident}/enable')
def enable(ident:int,data:EnableInput|None=None,db=Depends(db_session),user=Depends(writer)):
    return service.enable(db,ident,data or EnableInput(),user)
