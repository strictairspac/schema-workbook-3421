"""按拆解报告为父表/明细表启用外键：载入历史行、可续跑盘点、补挂、幂等切换。"""
import re
from datetime import datetime,timezone
from sqlalchemy import select,text
from sqlalchemy.exc import IntegrityError,DBAPIError
from fastapi import HTTPException
from src.config.database import engine
from src.models.entities import FkRollout,FkOrphan,Report,Audit,Base
from src.services.normalization import local_3nf
from src.utils.serialization import record

TABLE_RE=re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
RESERVED_TABLES={name.lower() for name in Base.metadata.tables}|{'information_schema','mysql','performance_schema','sys'}
MAX_KEY_CHARS=64
MAX_VALUE_CHARS=512

# ---------- 基础工具 ----------
def _q(name):
    if not TABLE_RE.match(name or ''): raise HTTPException(422,'非法标识符：%s'%name)
    return '`'+name+'`'

def _now(): return datetime.now(timezone.utc)

def _norm(value):
    if value is None:return None
    if isinstance(value,bool):return '1' if value else '0'
    if isinstance(value,(int,float)):return repr(value) if isinstance(value,float) else str(value)
    return str(value)

def _error_text(exc):
    """把触发器 SIGNAL、唯一键与长度错误翻译成可读信息。"""
    raw=str(getattr(exc,'orig',exc))
    for code,message in (
        ('FK_ORPHAN','明细引用了不存在的父行，不能新增孤儿'),
        ('FK_NULL_KEY','外键列不能为空，请补齐父键或稍后补挂'),
        ('FK_PARENT_REFERENCED','父行已被明细引用，不能删除'),
        ('FK_PARENT_KEY_CHANGED','父行键已被明细引用，不能修改'),
    ):
        if code in raw:return message
    if 'Duplicate entry' in raw:return '键值重复，请调整后重试'
    if 'Data too long' in raw:return '属性值超出列长度限制'
    if 'Deadlock' in raw or 'Lock wait timeout' in raw:return '并发冲突，请重试该操作'
    if 'errno: 121' in raw:return '外键名已被数据库其他表占用，请清理遗留约束后重试'
    if 'foreign key constraint' in raw.lower():return '外键约束拒绝该操作'
    if isinstance(exc,RuntimeError):return str(exc)
    detail=raw.split('\n')[0][:160]
    return '数据库拒绝了该操作' + ('：'+detail if detail else '')

def _db_error(exc): return HTTPException(409,_error_text(exc))

def _require(obj,message='盘点任务不存在'):
    if not obj:raise HTTPException(404,message)
    return obj

def _attrs(value): return [str(x) for x in (value or [])]

def _columns(count):return ['c%d'%(i+1) for i in range(count)]

def _table_exists(conn,name):
    return bool(conn.execute(text("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:n"),{'n':name}).scalar())

def _key_where(columns,key_attrs,prefix='k'):
    return ' AND '.join('`%s`=:%s%d'%(columns[a],prefix,i) for i,a in enumerate(key_attrs))

def _key_params(columns,key_attrs,values,prefix='k'):
    return {('%s%d'%(prefix,i)):values.get(a) for i,a in enumerate(key_attrs)}

# ---------- 从拆解报告确定父表/明细表与键 ----------
def _report_keys(report,index,attrs):
    result=report.result or {}
    checks=result.get('normal_form_checks') or []
    if index<len(checks) and [str(x) for x in checks[index].get('attributes') or []]==list(attrs):
        keys=[[str(a) for a in key] for key in checks[index].get('candidate_keys') or []]
        if keys:return keys
    deps=(report.input_snapshot or {}).get('dependencies') or []
    fds=[(frozenset(d.get('lhs') or []),rhs) for d in deps for rhs in d.get('rhs') or []]
    return [list(k) for k in local_3nf(attrs,fds)['candidate_keys']]

def _match_index(decomposition,index,attrs,label):
    if attrs is not None:
        wanted=[str(x) for x in attrs]
        matches=[i for i,r in enumerate(decomposition) if len(r)==len(wanted) and set(r)==set(wanted)]
        if not matches:raise HTTPException(422,'%s属性集合不在报告分解结果中'%label)
        found=matches[0]
        if index is not None and index!=found:raise HTTPException(422,'%s序号与属性集合不一致'%label)
        return found
    if index is not None:
        if index>=len(decomposition):raise HTTPException(422,'%s序号超出报告分解结果'%label)
        return index
    return None

def _valid_pairs(report,decomposition):
    pairs=[]
    for pi,parent in enumerate(decomposition):
        keys=_report_keys(report,pi,parent)
        for di,detail in enumerate(decomposition):
            if pi!=di and any(set(k)<=set(detail) for k in keys):pairs.append((pi,di))
    return pairs

def _build_spec(report,data):
    result=report.result or {}
    decomposition=[[str(a) for a in x] for x in result.get('decomposition') or []]
    if len(decomposition)<2:raise HTTPException(422,'报告分解结果不足两张关系，不能选择父表和明细表')
    pi=_match_index(decomposition,data.parent_index,data.parent_attributes,'父表')
    di=_match_index(decomposition,data.detail_index,data.detail_attributes,'明细表')
    if pi is None or di is None:
        pairs=_valid_pairs(report,decomposition)
        if len(pairs)==1 and pi is None and di is None:pi,di=pairs[0]
        elif not pairs:raise HTTPException(422,'报告中没有父表候选键被明细表包含的可用组合')
        else:raise HTTPException(422,'请按报告显式选择父表和明细表；可用组合：'+'；'.join('%d→%d'%(a+1,b+1) for a,b in pairs))
    if pi==di:raise HTTPException(422,'父表与明细表不能是同一张关系')
    parent_attrs=decomposition[pi];detail_attrs=decomposition[di]
    keys=_report_keys(report,pi,parent_attrs)
    if not keys:raise HTTPException(422,'父表在报告中没有候选键，不能作为外键目标')
    if data.parent_key is not None:
        wanted=set(str(x) for x in data.parent_key)
        matched=[k for k in keys if set(k)==wanted]
        if not matched:raise HTTPException(422,'指定的键不是报告中该关系的候选键')
        parent_key=list(matched[0])
    else:
        eligible=[k for k in keys if set(k)<=set(detail_attrs)]
        if not eligible:raise HTTPException(422,'父表候选键未被明细表包含，无法按报告键建立外键')
        parent_key=sorted(min(eligible,key=lambda k:(len(k),sorted(k))),key=parent_attrs.index)
    if not set(parent_key)<=set(detail_attrs):raise HTTPException(422,'父表候选键未被明细表包含，无法建立外键')
    return {'report_id':report.id,'parent_index':pi,'detail_index':di,
            'parent_attributes':parent_attrs,'detail_attributes':detail_attrs,
            'parent_key_attributes':parent_key}

def _table_names(report_id,pi,di,data):
    parent=data.parent_table or 'rpt%d_p%d'%(report_id,pi)
    detail=data.detail_table or 'rpt%d_d%d'%(report_id,di)
    for name in (parent,detail):
        if not TABLE_RE.match(name):raise HTTPException(422,'表名只能使用字母、数字和下划线，且以字母开头、不超过64字符')
        if name.lower() in RESERVED_TABLES:raise HTTPException(422,'表名 %s 为系统保留名'%name)
    if parent==detail:raise HTTPException(422,'父表与明细表不能同名')
    return parent,detail

# ---------- 行归一化 ----------
def _normalize_row(row,attrs,key_attrs,label,need_key):
    if not isinstance(row,dict):raise HTTPException(422,'%s历史行必须是对象'%label)
    unknown=set(row)-set(attrs)
    if unknown:raise HTTPException(422,'%s历史行包含关系外属性：%s'%(label,'、'.join(sorted(unknown))))
    out={}
    for attr in attrs:
        value=_norm(row.get(attr))
        if value is not None and len(value)>MAX_VALUE_CHARS:raise HTTPException(422,'%s属性 %s 的值超过%d字符'%(label,attr,MAX_VALUE_CHARS))
        if attr in key_attrs and value is not None and len(value)>MAX_KEY_CHARS:
            raise HTTPException(422,'%s键属性 %s 的值超过%d字符，无法建立索引'%(label,attr,MAX_KEY_CHARS))
        out[attr]=value
    if need_key:
        missing=[a for a in key_attrs if out.get(a) in (None,'')]
        if missing:raise HTTPException(422,'%s父行缺少键值：%s'%(label,'、'.join(missing)))
    return out

def _normalize_rows(rows,attrs,key_attrs,label,need_key):
    normalized=[];seen=set()
    for index,row in enumerate(rows or []):
        item=_normalize_row(row,attrs,key_attrs,'%s第%d行'%(label,index+1),need_key)
        key=tuple(item[a] for a in key_attrs)
        if all(v not in (None,'') for v in key):
            if key in seen:raise HTTPException(422,'%s键值重复：%s'%(label,'、'.join(key)))
            seen.add(key)
        normalized.append(item)
    return normalized

# ---------- 物理表与库内守卫 ----------
def _col_type(attr,key_attrs):return 'VARCHAR(%d)'%(MAX_KEY_CHARS if attr in key_attrs else MAX_VALUE_CHARS)

def _create_tables(conn,obj):
    pa=_attrs(obj.parent_attributes);da=_attrs(obj.detail_attributes);pkey=_attrs(obj.parent_key_attributes)
    pcols=_columns(len(pa));dcols=_columns(len(da))
    if not _table_exists(conn,obj.parent_table):
        parts=['`_row_id` BIGINT NOT NULL AUTO_INCREMENT']
        parts+=['`%s` %s NULL'%(c,_col_type(a,pkey)) for c,a in zip(pcols,pa)]
        parts+=['PRIMARY KEY (`_row_id`)',
                'UNIQUE KEY `uq_%s_key` (%s)'%(obj.pair_key,','.join('`%s`'%c for c,a in zip(pcols,pa) if a in pkey))]
        conn.execute(text('CREATE TABLE IF NOT EXISTS %s (%s) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'%(_q(obj.parent_table),', '.join(parts))))
    if not _table_exists(conn,obj.detail_table):
        parts=['`_row_id` BIGINT NOT NULL AUTO_INCREMENT']
        parts+=['`%s` %s NULL'%(c,_col_type(a,pkey)) for c,a in zip(dcols,da)]
        parts+=['PRIMARY KEY (`_row_id`)',
                'KEY `ix_%s_fk` (%s)'%(obj.pair_key,','.join('`%s`'%c for c,a in zip(dcols,da) if a in pkey))]
        conn.execute(text('CREATE TABLE IF NOT EXISTS %s (%s) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'%(_q(obj.detail_table),', '.join(parts))))

def _load_rows(conn,table,cols,rows):
    if not rows:return
    names=['`_row_id`']+['`%s`'%c for c in cols]
    placeholders=[':r']+[':v%d'%i for i in range(len(cols))]
    payload=[]
    for index,row in enumerate(rows):
        params={'r':index+1}
        params.update({('v%d'%i):row.get(a) for i,a in enumerate(row)})
        payload.append(params)
    conn.execute(text('INSERT IGNORE INTO %s (%s) VALUES (%s)'%(table,','.join(names),','.join(placeholders))),payload)

def _trigger_names(obj):
    base='trg_%s'%obj.pair_key
    return {'detail_insert':base+'_di','detail_update':base+'_du','parent_delete':base+'_pd','parent_update':base+'_pu'}

def _guard_bodies(obj):
    pa=_attrs(obj.parent_attributes);da=_attrs(obj.detail_attributes);pkey=_attrs(obj.parent_key_attributes)
    pcols=dict(zip(pa,_columns(len(pa))));dcols=dict(zip(da,_columns(len(da))))
    pkeys=['`%s`'%pcols[a] for a in pkey];dkeys=['`%s`'%dcols[a] for a in pkey]
    null_check=' OR '.join('NEW.%s IS NULL'%c for c in dkeys)
    match=' AND '.join('p.%s=NEW.%s'%(pc,dc) for pc,dc in zip(pkeys,dkeys))
    referenced=' AND '.join('d.%s=OLD.%s'%(dc,pc) for pc,dc in zip(pkeys,dkeys))
    parent=_q(obj.parent_table);detail=_q(obj.detail_table)
    check=("IF %s THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='FK_NULL_KEY'; END IF;"
           " IF NOT EXISTS (SELECT 1 FROM %s p WHERE %s FOR UPDATE) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='FK_ORPHAN'; END IF;")%(null_check,parent,match)
    same=' AND '.join('OLD.%s=NEW.%s'%(c,c) for c in pkeys)
    return {
        'detail_insert':check,
        'detail_update':check,
        # 父行删除/改键：锁定明细表对应键的记录，与补挂的加锁顺序一致。
        'parent_delete':("IF EXISTS (SELECT 1 FROM %s d WHERE %s FOR UPDATE) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='FK_PARENT_REFERENCED'; END IF;")%(detail,referenced),
        'parent_update':("IF NOT (%s) AND EXISTS (SELECT 1 FROM %s d WHERE %s FOR UPDATE) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='FK_PARENT_KEY_CHANGED'; END IF;")%(same,detail,referenced),
    }

def _install_guards(conn,obj):
    names=_trigger_names(obj);bodies=_guard_bodies(obj)
    targets={'detail_insert':obj.detail_table,'detail_update':obj.detail_table,'parent_delete':obj.parent_table,'parent_update':obj.parent_table}
    events={'detail_insert':'BEFORE INSERT','detail_update':'BEFORE UPDATE','parent_delete':'BEFORE DELETE','parent_update':'BEFORE UPDATE'}
    for key,name in names.items():
        body=bodies[key]
        for attempt in (0,1):
            try:
                conn.execute(text('DROP TRIGGER IF EXISTS %s'%_q(name)))
                conn.execute(text('CREATE TRIGGER %s %s ON %s FOR EACH ROW BEGIN %s END'%(
                    _q(name),events[key],_q(targets[key]),body)))
                break
            except DBAPIError:
                conn.rollback()
                if attempt:raise
                body=body.replace(' FOR UPDATE','')
    obj.guards_installed=True

def _drop_guards(conn,obj):
    for name in _trigger_names(obj).values():
        conn.execute(text('DROP TRIGGER IF EXISTS %s'%_q(name)))
    obj.guards_installed=False

def _ensure_schema(db,obj):
    """建表、载入历史行、装库内守卫；中断后重复调用不会重复建表或重复装约束。"""
    if obj.schema_ready and obj.guards_installed:return obj
    if obj.status=='enabled':return obj
    try:
        with engine.connect() as conn:
            _create_tables(conn,obj);conn.commit()
        if not obj.parent_loaded:
            rows=_normalize_rows(obj.parent_rows,_attrs(obj.parent_attributes),_attrs(obj.parent_key_attributes),'父表',False)
            with engine.begin() as conn:
                _load_rows(conn,_q(obj.parent_table),_columns(len(_attrs(obj.parent_attributes))),rows)
            obj.parent_loaded=True;db.commit()
        if not obj.detail_loaded:
            rows=_normalize_rows(obj.detail_rows,_attrs(obj.detail_attributes),_attrs(obj.parent_key_attributes),'明细表',False)
            with engine.begin() as conn:
                _load_rows(conn,_q(obj.detail_table),_columns(len(_attrs(obj.detail_attributes))),rows)
            obj.detail_loaded=True;db.commit()
        with engine.connect() as conn:
            _install_guards(conn,obj);conn.commit()
        obj.schema_ready=True;obj.error=None;db.commit()
        return obj
    except HTTPException as exc:
        obj.error=str(exc.detail);db.commit();raise
    except Exception as exc:
        db.rollback();obj.error=_error_text(exc);db.commit()
        raise HTTPException(503,'盘点表初始化失败：'+obj.error)

# ---------- 查询 ----------
def _pending_count(db,ident):
    return int(db.scalar(text("SELECT COUNT(*) FROM fk_orphans WHERE rollout_id=:r AND status='pending'"),{'r':ident}) or 0)

def _physical_orphans(db,obj):
    pa=_attrs(obj.parent_attributes);da=_attrs(obj.detail_attributes);pkey=_attrs(obj.parent_key_attributes)
    pcols=dict(zip(pa,_columns(len(pa))));dcols=dict(zip(da,_columns(len(da))))
    match=' AND '.join('p.`%s`=d.`%s`'%(pcols[a],dcols[a]) for a in pkey)
    select_cols=', '.join('d.`%s`'%dcols[a] for a in da)
    return db.execute(text('SELECT d.`_row_id`, %s FROM %s d LEFT JOIN %s p ON %s WHERE p.`_row_id` IS NULL ORDER BY d.`_row_id`'%(
        select_cols,_q(obj.detail_table),_q(obj.parent_table),match))).all()

def _refresh_counts(db,obj):
    obj.parent_count=int(db.scalar(text('SELECT COUNT(*) FROM %s'%_q(obj.parent_table))) or 0)
    obj.detail_count=int(db.scalar(text('SELECT COUNT(*) FROM %s'%_q(obj.detail_table))) or 0)
    obj.orphan_count=len(_physical_orphans(db,obj))

def _fk_columns(db,obj):
    if not obj.constraint_name:return []
    rows=db.execute(text(
        "SELECT kcu.COLUMN_NAME, kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME "
        "FROM information_schema.KEY_COLUMN_USAGE kcu "
        "JOIN information_schema.TABLE_CONSTRAINTS tc ON tc.CONSTRAINT_SCHEMA=kcu.CONSTRAINT_SCHEMA "
        "AND tc.CONSTRAINT_NAME=kcu.CONSTRAINT_NAME AND tc.TABLE_NAME=kcu.TABLE_NAME "
        "WHERE kcu.CONSTRAINT_SCHEMA=DATABASE() AND kcu.TABLE_NAME=:t AND kcu.CONSTRAINT_NAME=:n "
        "AND tc.CONSTRAINT_TYPE='FOREIGN KEY' ORDER BY kcu.ORDINAL_POSITION"
    ),{'t':obj.detail_table,'n':obj.constraint_name}).all()
    return [(r[0],r[1],r[2]) for r in rows]

def _foreign_name_owner(conn,name):
    return conn.execute(text(
        "SELECT TABLE_NAME FROM information_schema.REFERENTIAL_CONSTRAINTS "
        "WHERE CONSTRAINT_SCHEMA=DATABASE() AND CONSTRAINT_NAME=:n"
    ),{'n':name}).scalar()

def _constraint_exists(conn,table,name):
    return bool(conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
        "WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME=:t AND CONSTRAINT_NAME=:n"
    ),{'t':table,'n':name}).scalar())

def _fk_state(db,obj):
    rows=_fk_columns(db,obj)
    expected=list(zip(_attrs(obj.fk_detail_columns),_attrs(obj.fk_parent_columns)))
    return bool(rows) and len(rows)==len(expected) and all(
        row[0]==d and row[1]==obj.parent_table and row[2]==p for row,(d,p) in zip(rows,expected))

def _fk_name(obj):return ('fk_%s'%obj.detail_table)[:64]

LIST_FIELDS=('id','report_id','parent_table','detail_table','parent_key_attributes','status','constraint_name',
             'parent_count','detail_count','orphan_count','scan_complete','guards_installed','schema_ready','error',
             'created_at','updated_at')

def listing(db,report_id=None,status=None):
    query=select(FkRollout).order_by(FkRollout.id.desc())
    if report_id:query=query.where(FkRollout.report_id==report_id)
    if status:query=query.where(FkRollout.status==status)
    items=[]
    for obj in db.scalars(query):
        row=record(obj)
        item={key:row[key] for key in LIST_FIELDS}
        item['pending_orphans']=_pending_count(db,obj.id)
        items.append(item)
    return items

def detail(db,ident):
    obj=_require(db.get(FkRollout,ident))
    payload=record(obj)
    payload['pending_orphans']=_pending_count(db,ident)
    payload['fk_verified']=_fk_state(db,obj)
    payload['fk_columns']=_fk_columns(db,obj)
    payload['orphans']=[record(x) for x in db.scalars(select(FkOrphan).where(FkOrphan.rollout_id==ident).order_by(FkOrphan.status.desc(),FkOrphan.id))]
    return payload

def orphan_listing(db,ident,status=None):
    _require(db.get(FkRollout,ident))
    query=select(FkOrphan).where(FkOrphan.rollout_id==ident).order_by(FkOrphan.id)
    if status:query=query.where(FkOrphan.status==status)
    return [record(x) for x in db.scalars(query)]

def detail_rows(db,ident,offset=0,limit=50):
    """报告页查询孤儿所在明细行（保留的原行）。"""
    obj=_require(db.get(FkRollout,ident))
    attrs=_attrs(obj.detail_attributes);cols=_columns(len(attrs))
    offset=max(int(offset or 0),0);limit=min(max(int(limit or 50),1),200)
    rows=db.execute(text('SELECT `_row_id`, %s FROM %s ORDER BY `_row_id` LIMIT :n OFFSET :o'%(
        ', '.join('`%s`'%c for c in cols),_q(obj.detail_table))),{'n':limit,'o':offset}).all()
    pending={x.row_ref:x for x in db.scalars(select(FkOrphan).where(FkOrphan.rollout_id==ident,FkOrphan.status=='pending'))}
    out=[]
    for row in rows:
        ref=str(row[0])
        out.append({'row_ref':ref,'values':dict(zip(attrs,row[1:])),'orphan':record(pending[ref]) if ref in pending else None})
    return {'items':out,'offset':offset,'limit':limit}

# ---------- 创建 ----------
def create(db,data,user):
    report=db.get(Report,data.report_id)
    if not report:raise HTTPException(404,'报告不存在')
    if report.status!='completed' or not report.result:raise HTTPException(422,'报告尚未完成，不能用于外键盘点')
    spec=_build_spec(report,data)
    pair_key='r%06dp%02dd%02d'%(report.id,spec['parent_index'],spec['detail_index'])
    parent_table,detail_table=_table_names(report.id,spec['parent_index'],spec['detail_index'],data)
    existing=db.scalar(select(FkRollout).where(FkRollout.pair_key==pair_key))
    if existing:
        if existing.parent_table!=parent_table or existing.detail_table!=detail_table:
            raise HTTPException(409,'该报告此父表/明细表组合已按 %s → %s 盘点，请沿用原表名'%(existing.parent_table,existing.detail_table))
        _ensure_schema(db,existing);return detail(db,existing.id)
    clash=db.scalar(select(FkRollout).where(
        (FkRollout.parent_table==parent_table)|(FkRollout.detail_table==detail_table)|(FkRollout.parent_table==detail_table)|(FkRollout.detail_table==parent_table)))
    if clash:raise HTTPException(409,'表名已被另一个盘点任务占用')
    with engine.connect() as conn:
        if _table_exists(conn,parent_table) or _table_exists(conn,detail_table):
            raise HTTPException(409,'目标表名已存在，请换一个表名或完成已有盘点')
    parent_rows=_normalize_rows(data.parent_rows,spec['parent_attributes'],spec['parent_key_attributes'],'父表',False)
    detail_rows=_normalize_rows(data.detail_rows,spec['detail_attributes'],spec['parent_key_attributes'],'明细表',False)
    parent_columns=_columns(len(spec['parent_attributes']));detail_columns=_columns(len(spec['detail_attributes']))
    obj=FkRollout(
        pair_key=pair_key,report_id=report.id,parent_table=parent_table,detail_table=detail_table,
        parent_attributes=spec['parent_attributes'],detail_attributes=spec['detail_attributes'],
        parent_columns=parent_columns,detail_columns=detail_columns,parent_key_attributes=spec['parent_key_attributes'],
        fk_parent_columns=[parent_columns[spec['parent_attributes'].index(a)] for a in spec['parent_key_attributes']],
        fk_detail_columns=[detail_columns[spec['detail_attributes'].index(a)] for a in spec['parent_key_attributes']],
        status='pending',parent_rows=parent_rows,detail_rows=detail_rows,
        parent_loaded=False,detail_loaded=False,schema_ready=False,guards_installed=False)
    db.add(obj);db.add(Audit(actor=user.email,action='创建外键盘点 %s → %s'%(parent_table,detail_table)))
    try:db.commit()
    except IntegrityError:
        db.rollback();raise HTTPException(409,'该报告的同名父表/明细表盘点已存在')
    try:_ensure_schema(db,obj)
    except HTTPException:
        obj.status='failed';db.commit();raise
    _refresh_counts(db,obj);db.commit()
    return detail(db,obj.id)

# ---------- 盘点（分批、可续跑） ----------
def scan(db,ident,data,user):
    obj=_require(db.get(FkRollout,ident))
    if obj.status=='enabled':raise HTTPException(409,'外键已启用，不能再盘点')
    _ensure_schema(db,obj)
    obj=db.execute(select(FkRollout).where(FkRollout.id==ident).with_for_update()).scalar_one()
    if data.restart:
        db.execute(text("DELETE FROM fk_orphans WHERE rollout_id=:r AND status='pending'"),{'r':ident})
        obj.scan_cursor=0;obj.scan_complete=False;obj.orphan_count=0
    if obj.scan_complete:
        db.commit();return detail(db,ident)
    obj.scan_generation=(obj.scan_generation or 0)+1
    obj.status='scanning';db.commit()
    chunk=data.chunk_size or 200;budget=data.max_rows
    processed=0
    while True:
        obj=db.execute(select(FkRollout).where(FkRollout.id==ident).with_for_update()).scalar_one_or_none()
        _require(obj)
        if obj.scan_complete:break
        size=chunk if budget is None else min(chunk,budget-processed)
        if size<=0:break
        rows=_scan_chunk(db,obj,size)
        processed+=len(rows)
        if not rows:
            obj.scan_complete=True
            _sync_records(db,obj)
            db.flush()
            _refresh_counts(db,obj)
            obj.status='awaiting_relink' if obj.orphan_count else 'ready';obj.error=None
            db.add(Audit(actor=user.email,action='完成外键盘点 %s → %s'%(obj.parent_table,obj.detail_table)))
            db.commit();break
        if budget is not None and processed>=budget:
            db.commit();break
        db.commit()
    return detail(db,ident)

def _scan_chunk(db,obj,size):
    da=_attrs(obj.detail_attributes);pkey=_attrs(obj.parent_key_attributes)
    pa=_attrs(obj.parent_attributes)
    pcols=dict(zip(pa,_columns(len(pa))));dcols=dict(zip(da,_columns(len(da))))
    match=' AND '.join('p.`%s`=d.`%s`'%(pcols[a],dcols[a]) for a in pkey)
    select_cols=', '.join('d.`%s`'%dcols[a] for a in da)
    rows=db.execute(text('SELECT d.`_row_id`, %s, (p.`_row_id` IS NULL) AS missing FROM %s d LEFT JOIN %s p ON %s WHERE d.`_row_id`>:cur ORDER BY d.`_row_id` LIMIT :n'%(
        select_cols,_q(obj.detail_table),_q(obj.parent_table),match)),{'cur':obj.scan_cursor,'n':size}).all()
    if not rows:return []
    known={x.row_ref:x for x in db.scalars(select(FkOrphan).where(FkOrphan.rollout_id==obj.id))}
    for row in rows:
        mapping=dict(zip(da,row[1:1+len(da)]));missing=bool(row[-1])
        keys={a:_norm(mapping.get(a)) for a in pkey}
        if missing or any(v in (None,'') for v in keys.values()):
            ref=str(row[0]);data={a:_norm(mapping.get(a)) for a in da}
            orphan=known.get(ref)
            if orphan:
                orphan.row_data=data;orphan.key_values=keys;orphan.status='pending';orphan.resolution=None;orphan.resolved_at=None
            else:
                db.add(FkOrphan(rollout_id=obj.id,generation=obj.scan_generation or 1,row_ref=ref,
                    row_identity={'_row_id':row[0]},row_data=data,key_values=keys,status='pending'))
    obj.scan_cursor=int(rows[-1][0]);db.flush()
    return rows

def _sync_records(db,obj):
    """以物理表为准对齐孤儿登记：行已修好/已删除则销账，新出现的孤儿补登记。"""
    da=_attrs(obj.detail_attributes);pkey=_attrs(obj.parent_key_attributes)
    physical={str(row[0]):row for row in _physical_orphans(db,obj)}
    for orphan in list(db.scalars(select(FkOrphan).where(FkOrphan.rollout_id==obj.id,FkOrphan.status=='pending'))):
        row=physical.get(orphan.row_ref)
        if row is None:
            orphan.status='resolved';orphan.resolution='row_fixed';orphan.resolved_at=_now()
        else:
            mapping=dict(zip(da,row[1:]))
            orphan.row_data={a:_norm(mapping.get(a)) for a in da}
            orphan.key_values={a:_norm(mapping.get(a)) for a in pkey}
    known={x.row_ref for x in db.scalars(select(FkOrphan).where(FkOrphan.rollout_id==obj.id))}
    for ref,row in physical.items():
        if ref in known:continue
        mapping=dict(zip(da,row[1:]))
        db.add(FkOrphan(rollout_id=obj.id,generation=obj.scan_generation or 1,row_ref=ref,row_identity={'_row_id':row[0]},
            row_data={a:_norm(mapping.get(a)) for a in da},key_values={a:_norm(mapping.get(a)) for a in pkey},status='pending'))
    db.flush()

# ---------- 盘点期间录入 / 删父行 ----------
def add_detail_rows(db,ident,data,user):
    obj=_require(db.get(FkRollout,ident))
    if obj.status=='enabled':raise HTTPException(409,'外键已启用，明细请在正式表维护')
    _ensure_schema(db,obj)
    obj=db.execute(select(FkRollout).where(FkRollout.id==ident).with_for_update()).scalar_one()
    rows=[data.values] if data.values is not None else data.rows
    attrs=_attrs(obj.detail_attributes);pkey=_attrs(obj.parent_key_attributes)
    normalized=[_normalize_row(row,attrs,pkey,'明细',False) for row in rows]
    cols=_columns(len(attrs));ids=[]
    try:
        for row in normalized:
            result=db.execute(text('INSERT INTO %s (%s) VALUES (%s)'%(
                _q(obj.detail_table),','.join('`%s`'%c for c in cols),','.join(':v%d'%i for i in range(len(attrs))))),
                {('v%d'%i):row[a] for i,a in enumerate(attrs)})
            ids.append(int(result.lastrowid))
        _sync_records(db,obj);db.flush()
        if obj.scan_complete:obj.status='awaiting_relink' if _physical_orphans(db,obj) else 'ready'
        _refresh_counts(db,obj)
        db.add(Audit(actor=user.email,action='录入合法明细 %d 行 → %s'%(len(ids),obj.detail_table)))
        db.commit()
    except DBAPIError as exc:
        db.rollback();raise _db_error(exc)
    return {'inserted':len(ids),'row_ids':ids,'rollout':record(db.get(FkRollout,ident))}

def add_parent_rows(db,ident,data,user):
    obj=_require(db.get(FkRollout,ident))
    if obj.status=='enabled':raise HTTPException(409,'外键已启用，父行请在正式表维护')
    _ensure_schema(db,obj)
    obj=db.execute(select(FkRollout).where(FkRollout.id==ident).with_for_update()).scalar_one()
    rows=[data.values] if data.values is not None else data.rows
    attrs=_attrs(obj.parent_attributes)
    normalized=[_normalize_row(row,attrs,_attrs(obj.parent_key_attributes),'父行',True) for row in rows]
    cols=_columns(len(attrs))
    try:
        for row in normalized:
            db.execute(text('INSERT INTO %s (%s) VALUES (%s)'%(
                _q(obj.parent_table),','.join('`%s`'%c for c in cols),','.join(':v%d'%i for i in range(len(attrs))))),
                {('v%d'%i):row[a] for i,a in enumerate(attrs)})
        _sync_records(db,obj);db.flush();_refresh_counts(db,obj)
        if obj.scan_complete:obj.status='awaiting_relink' if obj.orphan_count else 'ready'
        db.add(Audit(actor=user.email,action='补齐父行 %d 行 → %s'%(len(normalized),obj.parent_table)));db.commit()
    except DBAPIError as exc:
        db.rollback();raise _db_error(exc)
    return {'inserted':len(normalized),'rollout':record(db.get(FkRollout,ident))}

def delete_parent_row(db,ident,data,user):
    obj=_require(db.get(FkRollout,ident))
    attrs=_attrs(obj.parent_attributes);pkey=_attrs(obj.parent_key_attributes)
    values={a:_norm(data.key_values.get(a)) for a in pkey}
    missing=[a for a in pkey if values.get(a) in (None,'')]
    if missing:raise HTTPException(422,'缺少父键属性：'+'、'.join(missing))
    pcols=dict(zip(attrs,_columns(len(attrs))))
    try:
        result=db.execute(text('DELETE FROM %s WHERE %s'%(_q(obj.parent_table),_key_where(pcols,pkey))),
                          _key_params(pcols,pkey,values))
        if not result.rowcount:
            db.rollback();raise HTTPException(404,'父行不存在')
        db.add(Audit(actor=user.email,action='删除父行 %s'%('、'.join(values[a] or '' for a in pkey))))
        db.commit()
    except DBAPIError as exc:
        db.rollback();raise _db_error(exc)
    obj=db.get(FkRollout,ident);_refresh_counts(db,obj);db.commit()
    return detail(db,ident)

# ---------- 补挂 ----------
def relink(db,ident,data,user):
    obj=_require(db.get(FkRollout,ident))
    if obj.status=='enabled':raise HTTPException(409,'外键已启用，不能补挂盘点期孤儿')
    _ensure_schema(db,obj)
    obj=db.execute(select(FkRollout).where(FkRollout.id==ident).with_for_update()).scalar_one()
    orphan=db.get(FkOrphan,data.orphan_id) if data.orphan_id is not None else None
    if orphan is None and data.detail_row_ref:
        orphan=db.scalar(select(FkOrphan).where(FkOrphan.rollout_id==ident,FkOrphan.row_ref==data.detail_row_ref))
    if not orphan or orphan.rollout_id!=ident:raise HTTPException(404,'待补挂的孤儿记录不存在')
    if orphan.status!='pending':raise HTTPException(409,'该孤儿已补挂，无需重复处理')
    pa=_attrs(obj.parent_attributes);pkey=_attrs(obj.parent_key_attributes);da=_attrs(obj.detail_attributes)
    pcols=dict(zip(pa,_columns(len(pa))));dcols=dict(zip(da,_columns(len(da))))
    values=dict(orphan.key_values or {})
    allowed=set(pkey)
    for source in (data.parent_row or {},data.key_values or {}):
        values.update({k:_norm(v) for k,v in source.items() if k in allowed})
    missing=[a for a in pkey if values.get(a) in (None,'')]
    if missing:raise HTTPException(422,'补挂需要完整父键：'+'、'.join(missing))
    try:
        # 先锁父行、再锁明细原行，与父行删除触发器（先锁父行再扫明细）保持同一加锁顺序，避免死锁。
        where=_key_where(pcols,pkey)
        parent=db.execute(text('SELECT 1 FROM %s WHERE %s FOR UPDATE'%(_q(obj.parent_table),where)),
                          _key_params(pcols,pkey,values)).first()
        locked=db.execute(text('SELECT `_row_id` FROM %s WHERE `_row_id`=:rid FOR UPDATE'%_q(obj.detail_table)),
                          {'rid':int(orphan.row_ref)}).first()
        if not locked:raise HTTPException(409,'明细原行已不存在')
        if not parent:
            if not data.create_parent:raise HTTPException(409,'父行不存在；可先补齐父行，或选择新建父行并补挂')
            row={a:None for a in pa}
            row.update({k:_norm(v) for k,v in (data.parent_row or {}).items() if k in pa})
            row.update({a:values[a] for a in pkey})
            cols=_columns(len(pa))
            db.execute(text('INSERT INTO %s (%s) VALUES (%s)'%(
                _q(obj.parent_table),','.join('`%s`'%c for c in cols),','.join(':v%d'%i for i in range(len(pa))))),
                {('v%d'%i):row[a] for i,a in enumerate(pa)})
        db.execute(text('UPDATE %s SET %s WHERE `_row_id`=:rid'%(
            _q(obj.detail_table),', '.join('`%s`=:k%d'%(dcols[a],i) for i,a in enumerate(pkey)))),
            dict(_key_params(dcols,pkey,values),rid=int(orphan.row_ref)))
        orphan.status='resolved';orphan.resolution='parent_created' if data.create_parent else 'relinked';orphan.resolved_at=_now()
        db.flush();_refresh_counts(db,obj)
        if obj.scan_complete:obj.status='awaiting_relink' if obj.orphan_count else 'ready'
        db.add(Audit(actor=user.email,action='补挂孤儿明细 %s → %s'%(orphan.row_ref,obj.detail_table)))
        db.commit()
    except DBAPIError as exc:
        db.rollback();raise _db_error(exc)
    return detail(db,ident)

# ---------- 启用外键 ----------
def enable(db,ident,data,user):
    obj=_require(db.get(FkRollout,ident))
    _ensure_schema(db,obj)
    obj=db.execute(select(FkRollout).where(FkRollout.id==ident).with_for_update()).scalar_one()
    if not obj.scan_complete:raise HTTPException(409,'盘点尚未完成，不能启用外键')
    if _fk_state(db,obj):
        # 外键已在库中（例如切换后崩溃）：只需撤下守卫并标记启用，不重复装约束。
        try:
            with engine.connect() as conn:
                _drop_guards(conn,obj);conn.commit()
        except Exception:pass
        obj.status='enabled';obj.error=None;db.commit()
        return detail(db,ident)
    _sync_records(db,obj);db.flush();_refresh_counts(db,obj)
    pending=_pending_count(db,ident)
    if obj.orphan_count or pending:
        obj.status='awaiting_relink';obj.error=None;db.commit()
        raise HTTPException(409,'仍有 %d 行孤儿待补挂，孤儿清零后才能启用'%max(obj.orphan_count,pending))
    if data.dry_run:
        db.commit();return {'would_enable':True,'orphans':0,'fk_exists':False,'rollout':record(obj)}
    name=obj.constraint_name or _fk_name(obj)
    with engine.connect() as conn:
        owner=_foreign_name_owner(conn,name)
        if owner and owner!=obj.detail_table:
            db.rollback()
            obj=db.get(FkRollout,ident);obj.error='外键名 %s 已被表 %s 占用'%(name,owner);db.commit()
            raise HTTPException(409,'数据库中外键名 %s 已被表 %s 占用，请清理遗留约束后重试'%(name,owner))
    # DDL 需要单独连接；先结束本会话事务释放它对这些表的元数据锁，否则 ALTER 会等自己。
    db.commit()
    lock_name='fk_enable_%s'%obj.pair_key
    try:
        with engine.connect() as conn:
            # 用命名锁串行化并发启用，重试不会与另一次切换交叠。
            acquired=conn.execute(text('SELECT GET_LOCK(:n,20)'),{'n':lock_name}).scalar()
            if acquired!=1:raise HTTPException(409,'另一次外键切换正在进行，请稍后重试')
            try:
                # 切换时限时等待元数据锁：并发补挂/录入若占用明细表，宁可失败回滚也不挂死。
                conn.execute(text('SET SESSION lock_wait_timeout=20'))
                conn.execute(text('SET SESSION innodb_lock_wait_timeout=20'))
                # 幂等：先清掉与本任务同名/同记录的残留下再装，重试不会重复装约束。
                for constraint in {name,_fk_name(obj)}:
                    if _constraint_exists(conn,obj.detail_table,constraint):
                        conn.execute(text('ALTER TABLE %s DROP FOREIGN KEY %s'%(_q(obj.detail_table),_q(constraint))))
                conn.execute(text('ALTER TABLE %s ADD CONSTRAINT %s FOREIGN KEY (%s) REFERENCES %s (%s) ON DELETE RESTRICT ON UPDATE RESTRICT'%(
                    _q(obj.detail_table),_q(name),','.join('`%s`'%c for c in _attrs(obj.fk_detail_columns)),
                    _q(obj.parent_table),','.join('`%s`'%c for c in _attrs(obj.fk_parent_columns)))))
                conn.commit()
            finally:
                conn.execute(text('SELECT RELEASE_LOCK(:n)'),{'n':lock_name})
        obj=db.get(FkRollout,ident);obj.constraint_name=name;db.commit()
        if not _fk_state(db,obj):raise RuntimeError('information_schema 未查到外键')
        with engine.connect() as conn:
            _drop_guards(conn,obj);conn.commit()
        obj.status='enabled';obj.error=None
        db.add(Audit(actor=user.email,action='启用外键 %s → %s'%(obj.detail_table,obj.parent_table)))
        db.commit()
    except Exception as exc:
        try:db.rollback()
        except Exception:pass
        # 切换失败：回滚已装的外键并恢复守卫，已录入的行原样保留。
        try:
            with engine.begin() as conn:
                for constraint in {name,_fk_name(obj)}:
                    if _constraint_exists(conn,obj.detail_table,constraint):
                        conn.execute(text('ALTER TABLE %s DROP FOREIGN KEY %s'%(_q(obj.detail_table),_q(constraint))))
        except Exception:pass
        try:
            obj=db.get(FkRollout,ident)
            try:
                with engine.connect() as conn:
                    _install_guards(conn,obj);conn.commit()
            except Exception:pass
            obj.status='failed';obj.error=_error_text(exc)
            db.commit()
        except Exception:pass
        raise HTTPException(409,'外键切换失败，已回滚并保留已录入的行：'+_error_text(exc))
    return detail(db,ident)
