import {api} from './client.js';
export const rolloutsApi={
  list:reportId=>api('/rollouts'+(reportId?'?report_id='+reportId:'')),
  detail:id=>api('/rollouts/'+id),
  create:data=>api('/rollouts','POST',data),
  inventory:id=>api('/rollouts/'+id+'/inventory','POST',{}),
  orphans:(id,status)=>api('/rollouts/'+id+'/orphans'+(status?'?status='+status:'')),
  rows:(id,offset=0,limit=50)=>api('/rollouts/'+id+'/rows?offset='+offset+'&limit='+limit),
  relink:(id,data)=>api('/rollouts/'+id+'/relink','POST',data),
  addDetail:(id,data)=>api('/rollouts/'+id+'/details','POST',data),
  addParent:(id,data)=>api('/rollouts/'+id+'/parents','POST',data),
  enable:id=>api('/rollouts/'+id+'/enable','POST',{}),
};
