"""领域服务层。

每个服务只负责一件事，彼此可组合：
- ``AssetService``  素材读取与字段完整性
- ``RuleEngine``    规则匹配
- ``ScheduleService`` 时段查询与冲突判定
- ``DraftService``  草稿保存与幂等（T6）
"""

from .asset import AssetService
from .draft import DraftService
from .rules import RuleEngine
from .schedule import ScheduleService

__all__ = ["AssetService", "DraftService", "RuleEngine", "ScheduleService"]
