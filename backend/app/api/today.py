"""Phase 11A Today 路由：GET /api/today?timezone=<IANA>（唯一的新端点，只读）。

- 无请求体、无 LLM、无外部网络、无写入、无后台任务；
- 查询参数只能是恰好一次的 timezone；缺失 / 为空 / 重复 / 非法 / 任何其他参数
  一律返回固定的脱敏 422（不回显被拒绝的值或参数名）——校验在路由内完成，
  不经过全局校验格式化；其他资源的校验行为不变；
- 本端点的所有响应都带 Cache-Control: no-store。
"""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.database import get_db
from app.schemas.today import TodayResponse
from app.services import today_service

TODAY_PATH = "/api/today"
NO_STORE_HEADERS = {"Cache-Control": "no-store"}
INVALID_REQUEST = {"error": {"code": "VALIDATION_ERROR", "message": "Invalid Today request"}}
UNAVAILABLE = {"error": {"code": "SERVICE_UNAVAILABLE", "message": "Database is unavailable"}}

router = APIRouter(tags=["today"])


@router.get(TODAY_PATH, response_model=TodayResponse, summary="今天：只读的本地日聚合投影")
def get_today(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    parameters = request.query_params.multi_items()
    zone = None
    if len(parameters) == 1 and parameters[0][0] == "timezone":
        zone = today_service.resolve_timezone(parameters[0][1])
    if zone is None:
        return JSONResponse(status_code=422, content=INVALID_REQUEST, headers=NO_STORE_HEADERS)
    try:
        result = today_service.build_today(
            db, now=clock.now(), timezone_name=parameters[0][1], zone=zone
        )
    except today_service.TodayUnavailableError:
        return JSONResponse(status_code=503, content=UNAVAILABLE, headers=NO_STORE_HEADERS)
    response.headers["Cache-Control"] = "no-store"
    return result
