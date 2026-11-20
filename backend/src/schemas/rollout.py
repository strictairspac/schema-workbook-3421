from typing import Any
from pydantic import BaseModel,Field,field_validator,model_validator
import re
TABLE_RE=re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
def _scalar(value):
    return value is None or isinstance(value,(str,int,float,bool))
def _validate_rows(rows):
    for row in rows:
        if not isinstance(row,dict): raise ValueError('历史行必须是对象')
        for key,value in row.items():
            if not isinstance(key,str) or not _scalar(value): raise ValueError('历史行只允许属性名到标量值的映射')
    return rows
class RolloutInput(BaseModel):
    report_id:int=Field(gt=0)
    parent_index:int|None=Field(default=None,ge=0)
    detail_index:int|None=Field(default=None,ge=0)
    parent_attributes:list[str]|None=None
    detail_attributes:list[str]|None=None
    parent_table:str|None=None
    detail_table:str|None=None
    parent_key:list[str]|None=None
    parent_rows:list[dict[str,Any]]=Field(default_factory=list)
    detail_rows:list[dict[str,Any]]=Field(default_factory=list)
    @field_validator('parent_table','detail_table')
    @classmethod
    def table_name(cls,value):
        if value is not None and not TABLE_RE.match(value): raise ValueError('表名只能使用字母、数字和下划线，且以字母开头')
        return value
    @field_validator('parent_rows','detail_rows')
    @classmethod
    def rows_shape(cls,value): return _validate_rows(value)
    @field_validator('parent_key','parent_attributes','detail_attributes')
    @classmethod
    def attrs_unique(cls,value):
        if value is not None and (len(set(value))!=len(value) or any(not x.strip() for x in value)): raise ValueError('属性名不能重复或为空')
        return value
class RelinkInput(BaseModel):
    orphan_id:int|None=None
    detail_row_ref:str|None=None
    key_values:dict[str,Any]=Field(default_factory=dict)
    parent_row:dict[str,Any]|None=None
    create_parent:bool=False
    @field_validator('key_values','parent_row')
    @classmethod
    def scalar_values(cls,value):
        if value is not None:
            for key,item in value.items():
                if not isinstance(key,str) or not _scalar(item): raise ValueError('补挂值必须是属性名到标量值的映射')
        return value
    @model_validator(mode='after')
    def has_identity(self):
        if self.orphan_id is None and not self.detail_row_ref: raise ValueError('需要指定孤儿记录')
        return self
class RowInput(BaseModel):
    values:dict[str,Any]|None=None
    rows:list[dict[str,Any]]=Field(default_factory=list)
    @field_validator('values')
    @classmethod
    def scalar_values(cls,value):
        if value is not None:
            for key,item in value.items():
                if not isinstance(key,str) or not _scalar(item): raise ValueError('行值必须是属性名到标量值的映射')
        return value
    @field_validator('rows')
    @classmethod
    def rows_shape(cls,value): return _validate_rows(value)
    @model_validator(mode='after')
    def has_rows(self):
        if self.values is None and not self.rows: raise ValueError('请提供一行或多行数据')
        return self
class InventoryInput(BaseModel):
    max_rows:int|None=Field(default=None,gt=0,le=100000)
    chunk_size:int=Field(default=200,gt=0,le=2000)
    restart:bool=False
class EnableInput(BaseModel):
    dry_run:bool=False
class DeleteParentInput(BaseModel):
    key_values:dict[str,Any]=Field(default_factory=dict)
    @field_validator('key_values')
    @classmethod
    def scalar_values(cls,value):
        for key,item in value.items():
            if not isinstance(key,str) or not _scalar(item): raise ValueError('父键必须是属性名到标量值的映射')
        return value
