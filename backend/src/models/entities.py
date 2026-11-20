from sqlalchemy import Column,Integer,String,JSON,DateTime,ForeignKey,Text,Boolean,UniqueConstraint
from datetime import datetime,timezone
from src.config.database import Base
class User(Base):
    __tablename__='users'
    id=Column(Integer,primary_key=True);email=Column(String(120),unique=True,nullable=False);password=Column(String(300),nullable=False);role=Column(String(20),nullable=False)
class Relation(Base):
    __tablename__='relations'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False);title=Column(String(160),nullable=False);attributes=Column(JSON,nullable=False);note=Column(String(300),default='')
class Dependency(Base):
    __tablename__='dependencies'
    id=Column(Integer,primary_key=True);relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False);lhs=Column(JSON,nullable=False);rhs=Column(JSON,nullable=False);note=Column(String(300),default='')
class Report(Base):
    __tablename__='reports'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False);relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False)
    status=Column(String(20),nullable=False,default='queued');input_snapshot=Column(JSON,nullable=False);result=Column(JSON,nullable=True);error=Column(Text,nullable=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class Audit(Base):
    __tablename__='audit_logs'
    id=Column(Integer,primary_key=True);actor=Column(String(120),nullable=False);action=Column(String(160),nullable=False);created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class FkRollout(Base):
    """一次按拆解报告装外键的盘点任务：父表 / 明细表 → 载入历史行 → 盘点 → 补挂 → 启用。"""
    __tablename__='fk_rollouts'
    __table_args__=(UniqueConstraint('report_id','parent_table','detail_table',name='uq_rollout_tables'),)
    id=Column(Integer,primary_key=True)
    pair_key=Column(String(32),unique=True,nullable=False)
    report_id=Column(Integer,ForeignKey('reports.id'),nullable=False)
    parent_table=Column(String(64),nullable=False);detail_table=Column(String(64),nullable=False)
    parent_attributes=Column(JSON,nullable=False);detail_attributes=Column(JSON,nullable=False)
    parent_columns=Column(JSON,nullable=False);detail_columns=Column(JSON,nullable=False)
    parent_key_attributes=Column(JSON,nullable=False)
    fk_parent_columns=Column(JSON,nullable=False);fk_detail_columns=Column(JSON,nullable=False)
    status=Column(String(24),nullable=False,default='pending')
    parent_rows=Column(JSON,nullable=False);detail_rows=Column(JSON,nullable=False)
    parent_loaded=Column(Boolean,nullable=False,default=False);detail_loaded=Column(Boolean,nullable=False,default=False)
    parent_count=Column(Integer,nullable=False,default=0);detail_count=Column(Integer,nullable=False,default=0)
    orphan_count=Column(Integer,nullable=False,default=0)
    scan_cursor=Column(Integer,nullable=False,default=0)
    scan_complete=Column(Boolean,nullable=False,default=False)
    scan_generation=Column(Integer,nullable=False,default=0)
    schema_ready=Column(Boolean,nullable=False,default=False)
    guards_installed=Column(Boolean,nullable=False,default=False)
    constraint_name=Column(String(64),nullable=True)
    error=Column(Text,nullable=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),onupdate=lambda:datetime.now(timezone.utc))
class FkOrphan(Base):
    """盘点发现的孤儿明细行。保留明细原行，只登记等待补挂。"""
    __tablename__='fk_orphans'
    __table_args__=(UniqueConstraint('rollout_id','row_ref',name='uq_orphan_row'),)
    id=Column(Integer,primary_key=True)
    rollout_id=Column(Integer,ForeignKey('fk_rollouts.id'),nullable=False)
    generation=Column(Integer,nullable=False,default=1)
    row_ref=Column(String(64),nullable=False)
    row_identity=Column(JSON,nullable=False)
    row_data=Column(JSON,nullable=False)
    key_values=Column(JSON,nullable=False)
    status=Column(String(20),nullable=False,default='pending')
    resolution=Column(String(40),nullable=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    resolved_at=Column(DateTime(timezone=True),nullable=True)
