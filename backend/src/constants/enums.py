ROLES = ('admin', 'operator', 'viewer')
RUN_STATES = ('queued', 'running', 'completed', 'failed')
# 盘点任务：待盘点 -> 盘点中 -> 待补挂/待启用 -> 已启用；切换失败可重试
ROLLOUT_STATES = ('pending', 'scanning', 'awaiting_relink', 'ready', 'enabled', 'failed')
ORPHAN_STATES = ('pending', 'resolved')
