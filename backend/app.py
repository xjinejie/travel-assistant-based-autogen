from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel, Field, ValidationError
from typing import Optional
from 旅行助手 import TravelAssistantService
import asyncio
import json
import uuid

app = FastAPI(title="智能旅行助手 API")


class SessionStore:
    """
    按 session_id 保存独立的 TravelAssistantService 实例。

    设计目的：
    1. 每个浏览器页面或客户端会话都应该拥有自己独立的 Agent 上下文。
    2. 首轮规划、追加修改、最终确认都必须落到同一个 service 实例上，才能复用该会话的 Swarm 历史消息。
    3. 多个页面并发访问时，不能再共享同一个全局 assistant，否则不同用户的上下文会互相覆盖。

    这里使用 asyncio.Lock 的原因：
    - FastAPI 在异步环境下可能同时处理多个请求。
    - 如果两个请求恰好在同一时刻为同一个 session_id 创建 service，
      需要保证最终只会落库一份实例，避免重复创建和状态竞争。
    """

    def __init__(self):
        """初始化内存态会话仓库和并发保护锁。"""
        self._sessions: dict[str, TravelAssistantService] = {}
        self._lock = asyncio.Lock() # 当两个相同 session_id 的请求同时到达时，锁住创建流程，确保只创建一个实例。

    async def get_or_create(self, session_id: str) -> TravelAssistantService:
        """
        读取指定 session_id 对应的 service；如果不存在，则创建一份新的独立实例。

        适用场景：
        - 用户首次发起规划
        - 客户端主动沿用某个已有 session_id 继续同一会话
        """
        async with self._lock:
            service = self._sessions.get(session_id)
            if service is None:
                service = TravelAssistantService()
                self._sessions[session_id] = service
            return service

    async def get_existing(self, session_id: str) -> Optional[TravelAssistantService]:
        """
        仅读取已存在的 service，不负责创建。

        适用场景：
        - 反馈接口必须命中一个已经存在的会话
        - 如果会话不存在，说明客户端传错了 session_id，或者先反馈后规划
        """
        async with self._lock:
            return self._sessions.get(session_id)


# 全局只保留“会话仓库”这一层单例，而不是保留单个旅行助手实例。
# 这样同一进程内可以同时服务多个 session_id，并保证每个 session_id 各自拥有独立上下文。
session_store = SessionStore()

# 统一记录测试页面文件路径，避免在多个路由中重复手写路径字符串。
# 这里使用 Path(__file__) 的原因是：
# - 无论当前工作目录在哪里，只要 app.py 本身位置不变，就能稳定定位到同目录下的 ws_test.html。
# - 比直接写相对路径 "ws_test.html" 更稳妥，避免从其他目录启动 uvicorn 时找不到文件。
WS_TEST_PAGE_PATH = Path(__file__).with_name("ws_test.html")


# ──────────────────────────────────────
# 详细数据模型
# ──────────────────────────────────────

class TravelRequest(BaseModel):
    """
    结构化旅行需求数据模型
    前端通过表单收集各字段，后端自动拼接成自然语言 prompt 输入给 Agent 框架。
    """

    # ── 会话标识 ──
    session_id: Optional[str] = Field(
        default=None,
        description="当前对话会话 ID；首次规划可不传，后端会自动生成；后续反馈必须沿用同一个 session_id"
    )

    # ── 目的地与出发地 ──
    destination: str = Field(..., description="目的地，如：北京、日本东京、云南大理")
    departure_city: Optional[str] = Field(default=None, description="出发城市，如：上海、广州")

    # ── 日期信息 ──
    start_date: Optional[str] = Field(default=None, description="出发日期，格式：YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="返回日期，格式：YYYY-MM-DD")
    flexible_dates: bool = Field(default=False, description="日期是否灵活可调")

    # ── 出行人员 ──
    travelers_count: int = Field(default=1, ge=1, le=20, description="出行人数")
    has_children: bool = Field(default=False, description="是否携带儿童（12岁以下）")
    has_elderly: bool = Field(default=False, description="是否携带老人（65岁以上）")

    # ── 偏好设置 ──
    budget_level: str = Field(
        default="中等",
        description="预算级别：经济 / 中等 / 高端 / 豪华"
    )
    travel_styles: list[str] = Field(
        default_factory=list,
        description="旅行风格偏好（可多选）：文化历史 / 自然风景 / 美食探索 / 购物 / 休闲度假 / 冒险运动"
    )
    accommodation_preference: str = Field(
        default="酒店",
        description="住宿偏好：青旅 / 民宿 / 酒店 / 高端酒店"
    )
    transport_preference: str = Field(
        default="公共交通",
        description="交通偏好：公共交通 / 自驾 / 打车 / 混合"
    )
    dietary_restrictions: Optional[str] = Field(
        default=None,
        description="饮食限制或偏好，如：素食、清真、海鲜过敏"
    )

    # ── 补充信息 ──
    additional_notes: Optional[str] = Field(
        default=None,
        description="其他补充说明，如：想看日出、需要无障碍设施、蜜月旅行等"
    )

    def build_prompt(self) -> str:
        """
        将所有结构化字段拼接成一段自然语言描述，
        直接作为 Agent 框架的用户输入。
        """
        parts = []

        # 1. 核心需求
        parts.append(f"我想去{self.destination}旅行。")

        if self.departure_city:
            parts.append(f"出发城市：{self.departure_city}。")

        # 2. 日期
        if self.start_date and self.end_date:
            parts.append(f"旅行时间：{self.start_date} 至 {self.end_date}。")
        elif self.start_date:
            parts.append(f"出发日期：{self.start_date}。")

        if self.flexible_dates:
            parts.append("日期可以灵活调整。")

        # 3. 人员
        parts.append(f"共 {self.travelers_count} 人出行。")
        special_groups = []
        if self.has_children:
            special_groups.append("有儿童同行")
        if self.has_elderly:
            special_groups.append("有老人同行")
        if special_groups:
            parts.append(f"特殊人群：{'，'.join(special_groups)}，请注意行程强度和安全。")

        # 4. 偏好
        parts.append(f"预算级别：{self.budget_level}。")

        if self.travel_styles:
            parts.append(f"旅行风格偏好：{'、'.join(self.travel_styles)}。")

        parts.append(f"住宿偏好：{self.accommodation_preference}。")
        parts.append(f"交通偏好：{self.transport_preference}。")

        if self.dietary_restrictions:
            parts.append(f"饮食限制：{self.dietary_restrictions}。")

        # 5. 补充
        if self.additional_notes:
            parts.append(f"补充说明：{self.additional_notes}")

        return "\n".join(parts)


# 保留简单的纯文本请求模型（向后兼容）
class ChatRequest(BaseModel):
    """
    简单纯文本规划请求模型。

    这里也补上 session_id，是为了让简单接口和结构化接口保持同样的会话隔离能力。
    """

    session_id: Optional[str] = Field(
        default=None,
        description="当前对话会话 ID；首次纯文本规划可不传，后端会自动生成"
    )
    message: str


class FeedbackRequest(BaseModel):
    """
    用户对当前旅行方案的反馈数据模型。
    这个模型只负责承载“确认接受”或“继续修改”的反馈文本，
    不改变原有的 TravelRequest 结构。
    """

    session_id: Optional[str] = Field(
        default=None,
        description="当前对话会话 ID；必须与首轮规划使用的 session_id 保持一致"
    )

    feedback: str = Field(
        ...,
        min_length=1,
        description="用户对当前旅行方案的反馈，可用于确认接受或提出新增需求/修改意见"
    )


def build_done_content(conversation_status: str) -> str:
    """根据会话状态生成前端展示的结束提示文案。"""
    if conversation_status == "awaiting_feedback":
        return "当前方案已审核通过，等待用户确认或追加修改意见。"
    return "本轮对话结束"


def resolve_session_id(raw_session_id: Optional[str], allow_create: bool) -> str:
    """
    统一清洗并校验 session_id。

    规则说明：
    1. 首轮规划允许不传 session_id，后端会自动生成新的 UUID。
    2. 反馈请求不允许缺失 session_id，因为反馈必须绑定到某一条已存在的会话链路上。
    3. 所有入口都先经过这里，避免每个接口重复写一套判空和报错逻辑。
    """
    cleaned_session_id = (raw_session_id or "").strip()
    if cleaned_session_id:
        return cleaned_session_id

    if allow_create:
        return uuid.uuid4().hex

    raise ValueError("缺少 session_id。请先发起一次规划，并在后续反馈时复用同一个 session_id。")


async def get_session_assistant(session_id: str, allow_create: bool) -> TravelAssistantService:
    """
    根据 session_id 获取对应的 TravelAssistantService。

    设计要点：
    - 首轮规划：如果 session_id 对应的会话不存在，允许自动创建。
    - 反馈阶段：必须命中已有会话，否则说明客户端会话链路已经断开，或者传错了 session_id。
    """
    if allow_create:
        return await session_store.get_or_create(session_id)

    assistant = await session_store.get_existing(session_id)
    if assistant is None:
        raise RuntimeError(
            f"未找到 session_id={session_id} 对应的会话。请先用同一个 session_id 发起首轮规划。"
        )
    return assistant


def get_ws_test_page_response() -> FileResponse:
    """
    返回测试页面文件响应。

    单独抽成函数的原因：
    1. 根路径 `/` 和 `/ws_test.html` 都要复用同一份页面。
    2. 如果后续要替换成正式前端首页，只需要改这一处即可。
    3. 这里顺带做文件存在性检查，避免页面文件缺失时直接抛出不友好的服务器异常。
    """
    if not WS_TEST_PAGE_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=f"测试页面文件不存在：{WS_TEST_PAGE_PATH}"
        )

    # 显式指定 text/html，确保浏览器把它当成网页而不是普通下载文件。
    return FileResponse(path=WS_TEST_PAGE_PATH, media_type="text/html")


# ──────────────────────────────────────
# 页面路由与 API 端点
# ──────────────────────────────────────

@app.get("/")
async def serve_index_page():
    """
    提供根路径首页。

    用户访问 `http://host:port/` 时，直接返回 ws_test.html。
    这样别人不需要事先拿到本地 HTML 文件，只要输入网址就能打开测试页面。
    """
    return get_ws_test_page_response()


@app.get("/ws_test.html")
async def serve_ws_test_page():
    """
    兼容显式访问测试页文件名的场景。

    保留这个路由的原因：
    - 旧的书签或文档可能仍然写的是 `/ws_test.html`
    - 同时保留 `/` 和 `/ws_test.html` 两个入口，迁移成本最低
    """
    return get_ws_test_page_response()

@app.post("/api/plan")
async def get_travel_plan(request: TravelRequest):
    """
    接收结构化的旅行需求，拼接为自然语言后传递给 AutoGen 团队进行规划。
    """
    try:
        # 首轮规划允许客户端不传 session_id。
        # 这种情况下由后端创建新的 UUID，并据此分配独立的 TravelAssistantService 实例。
        session_id = resolve_session_id(request.session_id, allow_create=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    assistant = await get_session_assistant(session_id, allow_create=True)
    prompt = request.build_prompt()

    print(f"\n{'='*60}")
    print(f"收到结构化请求，session_id={session_id}，拼接后的 Prompt：")
    print(f"{'='*60}")
    print(prompt)
    print(f"{'='*60}\n")

    result = await assistant.chat(prompt)
    return {
        "status": "success",
        "session_id": session_id,
        "reply": result.reply,
        "conversation_status": result.conversation_status,
    }


@app.post("/api/plan/simple")
async def get_travel_plan_simple(request: ChatRequest):
    """
    向后兼容的简单接口，直接接收纯文本消息。
    """
    try:
        # 纯文本接口与结构化接口保持一致：首次规划允许自动生成 session_id。
        session_id = resolve_session_id(request.session_id, allow_create=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    assistant = await get_session_assistant(session_id, allow_create=True)
    user_input = request.message
    print(f"收到简单文本请求，session_id={session_id}: {user_input} ...")
    result = await assistant.chat(user_input)
    return {
        "status": "success",
        "session_id": session_id,
        "reply": result.reply,
        "conversation_status": result.conversation_status,
    }


@app.post("/api/plan/feedback")
async def submit_travel_plan_feedback(request: FeedbackRequest):
    """
    接收用户对当前旅行方案的反馈。
    反馈不会回到 Planner，而是先 handoff 给 Reviewer，由 Reviewer 判断：
    - 用户已接受方案 -> 输出 TERMINATE
    - 用户提出新增/修改要求 -> handoff 给 Planner 继续改稿
    """
    try:
        # 反馈阶段必须携带 session_id。
        # 否则后端无法知道这条反馈属于哪一次已经生成过的方案。
        session_id = resolve_session_id(request.session_id, allow_create=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        assistant = await get_session_assistant(session_id, allow_create=False)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    feedback = request.feedback.strip()
    print(f"收到用户反馈，session_id={session_id}: {feedback}")

    try:
        result = await assistant.chat(feedback, is_feedback=True)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": "success",
        "session_id": session_id,
        "reply": result.reply,
        "conversation_status": result.conversation_status,
    }


@app.websocket("/ws/plan")
async def websocket_plan(websocket: WebSocket):
    """
    WebSocket 端点：建立长连接后，接收结构化 JSON 数据，
    实时推送 Agent 的每一个工作步骤。
    """
    await websocket.accept()

    try:
        session_id = None

        # 接收前端发来的 JSON 字符串
        raw_data = await websocket.receive_text()
        print(f"WebSocket 收到原始数据: {raw_data[:200]}...")

        # 尝试解析为结构化模型，如果解析失败则当作纯文本处理
        try:
            data = json.loads(raw_data)
            travel_request = TravelRequest(**data)
            # WebSocket 首轮规划同样允许不传 session_id。
            # 后端会自动创建，并在返回消息里把 session_id 回传给前端，便于后续反馈继续复用。
            session_id = resolve_session_id(travel_request.session_id, allow_create=True)
            assistant = await get_session_assistant(session_id, allow_create=True)
            user_input = travel_request.build_prompt()
            print(f"\n解析为结构化请求，session_id={session_id}，生成 Prompt：\n{user_input}\n")
        except json.JSONDecodeError:
            # 向后兼容：纯文本直接使用
            session_id = uuid.uuid4().hex
            assistant = await get_session_assistant(session_id, allow_create=True)
            user_input = raw_data
            print(f"作为纯文本处理，session_id={session_id}: {user_input}")
        except ValidationError as exc:
            await websocket.send_json({
                "type": "error",
                "source": "System",
                "content": f"结构化请求字段校验失败: {exc.errors()}"
            })
            return
        except ValueError as exc:
            await websocket.send_json({
                "type": "error",
                "source": "System",
                "content": str(exc)
            })
            return

        result = await assistant.chat_stream(user_input, websocket)

        await websocket.send_json({
            "type": "done",
            "source": "System",
            "session_id": session_id,
            "conversation_status": result.conversation_status,
            "content": build_done_content(result.conversation_status)
        })
    except WebSocketDisconnect:
        print("WebSocket 已经断开连接")


@app.websocket("/ws/plan/feedback")
async def websocket_plan_feedback(websocket: WebSocket):
    """
    WebSocket 反馈端点：
    接收用户对当前方案的反馈，先交给 Reviewer 判断是否需要继续修改。
    """
    await websocket.accept()

    try:
        session_id = None
        raw_data = await websocket.receive_text()
        print(f"WebSocket 收到反馈原始数据: {raw_data[:200]}...")

        try:
            data = json.loads(raw_data)
            feedback_request = FeedbackRequest(**data)
            # 反馈接口必须提供 session_id，并且必须命中已经存在的会话。
            # 这是避免“页面 A 的反馈串到页面 B 的规划上下文”最核心的一步。
            session_id = resolve_session_id(feedback_request.session_id, allow_create=False)
            assistant = await get_session_assistant(session_id, allow_create=False)
            user_input = feedback_request.feedback.strip()
            print(f"\n解析为用户反馈，session_id={session_id}：\n{user_input}\n")
        except json.JSONDecodeError:
            await websocket.send_json({
                "type": "error",
                "source": "System",
                "content": "反馈请求必须使用 JSON，并携带 session_id 与 feedback 字段。"
            })
            return
        except ValidationError as exc:
            await websocket.send_json({
                "type": "error",
                "source": "System",
                "content": f"反馈字段校验失败: {exc.errors()}"
            })
            return
        except ValueError as exc:
            await websocket.send_json({
                "type": "error",
                "source": "System",
                "content": str(exc)
            })
            return
        except RuntimeError as exc:
            await websocket.send_json({
                "type": "error",
                "source": "System",
                "content": str(exc)
            })
            return

        if not user_input:
            await websocket.send_json({
                "type": "error",
                "source": "System",
                "content": "反馈内容不能为空。"
            })
            return

        try:
            result = await assistant.chat_stream(user_input, websocket, is_feedback=True)
        except RuntimeError as exc:
            await websocket.send_json({
                "type": "error",
                "source": "System",
                "content": str(exc)
            })
            return

        await websocket.send_json({
            "type": "done",
            "source": "System",
            "session_id": session_id,
            "conversation_status": result.conversation_status,
            "content": build_done_content(result.conversation_status)
        })
    except WebSocketDisconnect:
        print("反馈 WebSocket 已经断开连接")
