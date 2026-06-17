# src/access_gate/main.py
import os
import base64
import uuid
import re
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status, Path
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

# ---------- Configuration ----------
SERVICE_NAME = os.getenv("SERVICE_NAME", "access-gate-service")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.1")
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "local-dev-token")

# ---------- FastAPI App ----------
app = FastAPI(
    title="Smart Campus — Access Gate Service API",
    version=SERVICE_VERSION,
)

# ---------- Enums ----------
class Direction(str, Enum):
    IN = "IN"
    OUT = "OUT"

class AccessStatus(str, Enum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"

class HolderRole(str, Enum):
    STUDENT = "STUDENT"
    STAFF = "STAFF"
    GUEST = "GUEST"
    CONTRACTOR = "CONTRACTOR"

class GateState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    LOCKED = "LOCKED"
    FAULT = "FAULT"

class CardStatus(str, Enum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"

# ---------- Pydantic Models (dùng string cho UUID để tránh lỗi serialization) ----------
class HealthResponse(BaseModel):
    status: str
    service: str
    time: str

class AccessLog(BaseModel):
    logId: str
    cardId: str
    gateId: str
    direction: Direction
    timestamp: datetime
    status: AccessStatus
    note: Optional[str] = None

class AccessLogPage(BaseModel):
    items: List[AccessLog]
    nextCursor: Optional[str] = None
    hasMore: bool

class AccessLogDetail(AccessLog):
    holderName: str
    holderRole: HolderRole
    readerModel: str

class GateStatus(BaseModel):
    gateId: str
    status: GateState
    lastActivityAt: datetime
    firmwareVersion: str

class CardDetail(BaseModel):
    cardId: str
    holderName: str
    holderRole: HolderRole
    status: CardStatus
    issuedAt: datetime
    expiresAt: datetime

# ---------- In-memory Data Store (có dữ liệu cố định cho test) ----------
MOCK_LOGS: List[AccessLog] = []
MOCK_DETAILS: Dict[str, AccessLogDetail] = {}
MOCK_GATES: Dict[str, GateStatus] = {}
MOCK_CARDS: Dict[str, CardDetail] = {}

def init_mock_data():
    global MOCK_LOGS, MOCK_DETAILS, MOCK_GATES, MOCK_CARDS
    base_time = datetime.now(timezone.utc)

    # ---- Thêm dữ liệu cố định cho test collection ----
    fixed_log_id = "0196fb3d-4ad7-7d1e-9f49-5d5148d2cafe"
    fixed_card_id = "CARD-123456"
    
    # Thêm card theo yêu cầu test
    MOCK_CARDS[fixed_card_id] = CardDetail(
        cardId=fixed_card_id,
        holderName="Nguyễn Văn Hưởng",
        holderRole=HolderRole.STUDENT,
        status=CardStatus.ACTIVE,
        issuedAt=base_time - timedelta(days=365),
        expiresAt=base_time + timedelta(days=365),
    )
    
    # Thêm log theo yêu cầu test
    fixed_log = AccessLog(
        logId=fixed_log_id,
        cardId=fixed_card_id,
        gateId="GATE-01",
        direction=Direction.IN,
        timestamp=base_time,
        status=AccessStatus.ALLOWED,
        note=None,
    )
    MOCK_LOGS.append(fixed_log)
    MOCK_DETAILS[fixed_log_id] = AccessLogDetail(
        logId=fixed_log_id,
        cardId=fixed_card_id,
        gateId="GATE-01",
        direction=Direction.IN,
        timestamp=base_time,
        status=AccessStatus.ALLOWED,
        note=None,
        holderName="Nguyễn Văn Hưởng",
        holderRole=HolderRole.STUDENT,
        readerModel="RFID-RDR-V3.2",
    )

    # ---- Tạo thêm 25 logs ngẫu nhiên ----
    for i in range(1, 26):
        log_id = str(uuid.uuid4())
        card_id = f"CARD-{100000 + i}"
        gate_id = f"GATE-{(i % 10) + 1:02d}"
        direction = Direction.IN if i % 2 == 0 else Direction.OUT
        status_val = AccessStatus.ALLOWED if i % 5 != 0 else AccessStatus.DENIED
        log = AccessLog(
            logId=log_id,
            cardId=card_id,
            gateId=gate_id,
            direction=direction,
            timestamp=base_time - timedelta(hours=i),
            status=status_val,
            note="After hours" if status_val == AccessStatus.DENIED else None,
        )
        MOCK_LOGS.append(log)
        MOCK_DETAILS[log_id] = AccessLogDetail(
            logId=log_id,
            cardId=card_id,
            gateId=gate_id,
            direction=direction,
            timestamp=log.timestamp,
            status=status_val,
            note=log.note,
            holderName=f"Student {i}",
            holderRole=HolderRole.STUDENT,
            readerModel="RFID-RDR-V3.2",
        )
    
    # Sắp xếp logs mới nhất trước
    MOCK_LOGS.sort(key=lambda x: x.timestamp, reverse=True)

    # ---- Tạo 10 gates ----
    for g in range(1, 11):
        gate_id = f"GATE-{g:02d}"
        MOCK_GATES[gate_id] = GateStatus(
            gateId=gate_id,
            status=GateState.CLOSED if g % 3 != 0 else GateState.OPEN,
            lastActivityAt=base_time - timedelta(minutes=5 * g),
            firmwareVersion=f"gate-fw-v1.4.{g % 5}",
        )

    # ---- Tạo thêm 49 cards khác (tổng 50) ----
    for c in range(1, 50):
        card_id = f"CARD-{100000 + c}"
        if card_id == fixed_card_id:
            continue
        MOCK_CARDS[card_id] = CardDetail(
            cardId=card_id,
            holderName=f"Card Holder {c}",
            holderRole=HolderRole.STAFF if c % 3 == 0 else HolderRole.STUDENT,
            status=CardStatus.ACTIVE if c % 7 != 0 else CardStatus.BLOCKED,
            issuedAt=base_time - timedelta(days=365),
            expiresAt=base_time + timedelta(days=365),
        )

init_mock_data()

# ---------- Helper functions ----------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"

def build_problem(
    status_code: int,
    title: str,
    detail: str,
    instance: Optional[str] = None,
    problem_type: str = "about:blank",
) -> Dict:
    problem = {
        "type": problem_type,
        "title": title,
        "status": status_code,
        "detail": detail,
    }
    if instance:
        problem["instance"] = instance
    return problem

def verify_bearer_token(authorization: Optional[str] = Header(default=None)) -> None:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=build_problem(
                status_code=status.HTTP_401_UNAUTHORIZED,
                title="Unauthorized",
                detail="Missing Authorization header",
                instance="/access/logs/recent",
            ),
        )
    expected = f"Bearer {AUTH_TOKEN}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=build_problem(
                status_code=status.HTTP_401_UNAUTHORIZED,
                title="Unauthorized",
                detail="Invalid bearer token",
                instance="/access/logs/recent",
            ),
        )

def get_paginated_logs(cursor: Optional[str], limit: int) -> Tuple[List[AccessLog], Optional[str], bool]:
    start = 0
    if cursor:
        try:
            start = int(base64.b64decode(cursor).decode())
        except:
            start = 0
    end = min(start + limit, len(MOCK_LOGS))
    items = MOCK_LOGS[start:end]
    has_more = end < len(MOCK_LOGS)
    next_cursor = None
    if has_more:
        next_cursor = base64.b64encode(str(end).encode()).decode()
    return items, next_cursor, has_more

# ---------- Exception Handlers ----------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        problem = exc.detail
    else:
        problem = build_problem(
            status_code=exc.status_code,
            title=status.HTTP_STATUS_CODES.get(exc.status_code, "Error"),
            detail=str(exc.detail),
            instance=str(request.url.path),
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=problem,
        media_type="application/problem+json",
        headers=getattr(exc, "headers", None),
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    first_error = errors[0] if errors else {}
    location = ".".join(str(item) for item in first_error.get("loc", []))
    message = first_error.get("msg", "Validation error")
    detail = f"{location}: {message}" if location else message
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=build_problem(
            status_code=422,
            title="Validation Error",
            detail=detail,
            instance=str(request.url.path),
        ),
        media_type="application/problem+json",
    )

# ---------- Endpoints ----------
@app.get("/health", response_model=HealthResponse)
def get_health():
    return HealthResponse(status="ok", service=SERVICE_NAME, time=now_iso())

@app.get("/access/logs/recent", response_model=AccessLogPage)
def get_access_logs_recent(
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    _ = Depends(verify_bearer_token)
):
    items, next_cursor, has_more = get_paginated_logs(cursor, limit)
    return AccessLogPage(items=items, nextCursor=next_cursor, hasMore=has_more)

@app.get("/access/logs/{logId}", response_model=AccessLogDetail)
def get_access_log_by_id(
    logId: str,
    _ = Depends(verify_bearer_token)
):
    # Validate UUID format
    try:
        uuid.UUID(logId)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=build_problem(
                status_code=422,
                title="Invalid UUID",
                detail=f"logId '{logId}' is not a valid UUID",
                instance=f"/access/logs/{logId}",
            ),
        )
    detail = MOCK_DETAILS.get(logId)
    if not detail:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=build_problem(
                status_code=404,
                title="Not Found",
                detail=f"Access log with id {logId} does not exist",
                instance=f"/access/logs/{logId}",
            ),
        )
    return detail

@app.get("/gates/{gateId}/status", response_model=GateStatus)
def get_gate_status(
    gateId: str,
    _ = Depends(verify_bearer_token)
):
    if not re.match(r'^GATE-[0-9]{2}$', gateId):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=build_problem(
                status_code=422,
                title="Invalid gateId",
                detail=f"gateId '{gateId}' does not match pattern GATE-XX",
                instance=f"/gates/{gateId}/status",
            ),
        )
    gate = MOCK_GATES.get(gateId)
    if not gate:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=build_problem(
                status_code=404,
                title="Not Found",
                detail=f"Gate {gateId} does not exist",
                instance=f"/gates/{gateId}/status",
            ),
        )
    return gate

@app.get("/cards/{cardId}", response_model=CardDetail)
def get_card_detail(
    cardId: str,
    _ = Depends(verify_bearer_token)
):
    if not re.match(r'^CARD-[0-9]{6}$', cardId):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=build_problem(
                status_code=422,
                title="Invalid cardId",
                detail=f"cardId '{cardId}' does not match pattern CARD-XXXXXX",
                instance=f"/cards/{cardId}",
            ),
        )
    card = MOCK_CARDS.get(cardId)
    if not card:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=build_problem(
                status_code=404,
                title="Not Found",
                detail=f"Card {cardId} does not exist",
                instance=f"/cards/{cardId}",
            ),
        )
    return card

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)