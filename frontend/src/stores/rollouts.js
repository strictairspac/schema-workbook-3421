import {rolloutsApi} from '../api/rollouts.js';
export const rolloutsStore={items:[],async refresh(reportId){this.items=await rolloutsApi.list(reportId);return this.items;}};
