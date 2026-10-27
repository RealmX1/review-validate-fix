"""请求内容身份与处理进度分开，重复入口不会创造另一条同义请求。"""

import hashlib
import json
from typing import Any


def reviewer_request_content_identity(request: dict[str, Any]) -> str:
    fields = ("type", "name", "command", "reason", "domain", "scope", "metric", "subtask_type", "need", "target")
    semantic_content = {field: request[field] for field in fields if field in request}
    return hashlib.sha256(json.dumps(semantic_content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
