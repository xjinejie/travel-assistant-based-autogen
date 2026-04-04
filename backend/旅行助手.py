"""
智能旅行助手 ── 基于 AutoGen Swarm 的多 Agent 协作系统

架构说明：
    本脚本使用 Swarm 团队模式，通过 HandoffMessage 显式控制 Agent 之间的对话流转。
    相比 RoundRobinGroupChat 的固定轮转，Swarm 让每个 Agent 自主决定"下一步交给谁"，
    流程更灵活、可控。

Agent 角色：
    1. Agent_Planner  ── 旅行规划师，调用高德 MCP 工具生成方案，完成后 handoff 给 Reviewer
    2. Agent_Reviewer ── 方案审核员（纯文本审核，不调工具），审核通过 handoff 给 user，不通过 handoff 回 Planner
    3. user           ── UserProxyAgent，代表人类用户，既可提供初始需求，也可对最终方案给出反馈

对话流程：
    首轮规划：
        用户输入 → Planner(生成方案) → Reviewer(审核)
                      ↑                    ↓
                      ← REVISE(打回修改) ←─┤
                                           ↓
                                    APPROVE → handoff 给 user

    反馈阶段：
        user → Reviewer(判断反馈)
                     ├─ 无需修改 → TERMINATE
                     └─ 需要修改 → Planner → Reviewer → user
"""

from dataclasses import dataclass
from autogen_core import CancellationToken
from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_ext.tools.mcp import StdioServerParams, mcp_server_tools
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_agentchat.teams import Swarm
from autogen_agentchat.ui import Console
from autogen_agentchat.conditions import (
    HandoffTermination,
    TextMentionTermination,
)
from autogen_agentchat.messages import HandoffMessage, ThoughtEvent, TextMessage
from autogen_agentchat.messages import ModelClientStreamingChunkEvent, ToolCallSummaryMessage
import dotenv
import os
import asyncio

dotenv.load_dotenv()

# ──────────────────────────────────────
# 模型客户端配置
# ──────────────────────────────────────

gemini_client = OpenAIChatCompletionClient(
    model=os.getenv("GEMINI_MODEL_NAME"),
    base_url=os.getenv("GEMINI_BASE_URL"),
    api_key=os.getenv("GEMINI_API_KEY"),
    parallel_tool_calls=False,
    model_info={
        "vision": False,
        "function_calling": True,
        "json_output": True,
        "family": "gemini",
        "structured_output": True,
    },
)

deepseek_client = OpenAIChatCompletionClient(
    model=os.getenv("DEEPSEEK_MODEL_NAME"),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    model_info={
        "vision": False,
        "function_calling": True,
        "json_output": True,
        "family": "deepseek",
        "structured_output": True,
    },
)

# ──────────────────────────────────────
# 高德地图 MCP Server 参数
# ──────────────────────────────────────

mcp_server_params = StdioServerParams(
    command="npx",
    args=["-y", "@amap/amap-maps-mcp-server"],
    env={"AMAP_MAPS_API_KEY": os.getenv("AMAP_MAPS_API_KEY")},
    read_timeout_seconds=60,
)

# ──────────────────────────────────────
# Agent 系统提示词
# ──────────────────────────────────────

# ====== Planner prompt ======
planner_sys_msg = """你是资深旅行规划师 Agent_Planner。

## 核心规则（最重要，必须遵守）
你有一个工具叫 transfer_to_Agent_Reviewer。
无论你是在生成首版方案，还是在根据 Reviewer 转述的用户反馈修改方案，
当你完成方案输出后，你的最后一个动作必须是调用 transfer_to_Agent_Reviewer。
不调用此工具 = 流程卡死。每次回复都必须以调用该工具结束。

## 任务
基于用户需求，调用高德地图 MCP 工具生成可执行的旅行方案。
如果 Reviewer 转达了用户新增需求、删减要求、预算变化、偏好调整或其他修改意见，
你需要在保留合理内容的前提下继续修改当前方案，而不是从零开始无视上下文重写。

## 工作流程
1. 先判断当前是“首次规划”还是“根据反馈改稿”
2. 用地图工具查询 POI、路线、距离等事实信息
3. 根据查询结果撰写完整方案；若是改稿，必须显式回应用户反馈
4. 调用 transfer_to_Agent_Reviewer（必须！）

## 工作原则
1. 善用工具：优先使用地图/POI/路线/时长相关工具做事实核验，不要臆造路程、营业时间、票价。
2. 结果导向：输出必须能直接执行，包含时间、地点、交通方式、费用区间与备选方案。
3. 风险意识：识别闭馆、高峰拥堵、天气、跨城中转、儿童/老人友好性等风险并给出规避建议。
4. 中文输出：默认使用简体中文，结构清晰、简洁专业。
5. 反馈对齐：若本轮是修改稿，优先响应 Reviewer 转述的用户反馈，并说明哪些部分已调整。

## 输出格式（严格遵守）
【本轮任务类型】
- 首次规划 / 根据反馈修改

【反馈响应】
- 如果本轮是根据反馈修改：列出 2-5 条已响应的用户反馈
- 如果本轮是首次规划：写“首版方案，无需反馈响应”

【需求理解】
- 用 3-5 条总结用户目标与约束

【行程总览】
- 总天数、城市/区域、节奏（紧凑/适中/轻松）

【详细行程】
- Day 1/Day 2 ...
- 每天按"上午/中午/下午/晚上"给出：地点、建议停留时长、交通方式、预计耗时

【预算估算】
- 交通、住宿、餐饮、门票、机动费用（给出区间）

【预订与准备清单】
- 必须提前预约/购票项
- 出发前准备项（证件、衣物、药品等）

## 禁止事项
- 不要输出 APPROVE
- 不要在没调用 transfer_to_Agent_Reviewer 的情况下结束回复
"""

reviewer_sys_msg = """你是严谨的旅行方案审核专家 Agent_Reviewer。

## 核心规则（最重要，必须遵守）
你有两个工具：transfer_to_Agent_Planner 和 transfer_to_user。
你会收到两类输入：
1. 来自 Agent_Planner 的旅行方案
2. 来自 user 的反馈

除“用户明确接受当前方案、不再需要修改”这一种情况外，
你每次回复结束时都必须调用一个工具：
- 方案不合格，或用户提出了需要改动方案的新反馈 → 调用 transfer_to_Agent_Planner
- 方案合格，且需要把结果交给用户确认或收集反馈 → 调用 transfer_to_user
- 只有当用户明确表示“同意/满意/无需修改/结束本轮”等情况时，才允许直接输出 TERMINATE，并且不要再调用任何工具

## 任务
审查 Agent_Planner 的旅行方案是否真实、可执行、符合用户约束；
同时在收到 user 反馈时，判断该反馈是否需要继续修改旅行计划。

## 审核清单（用于审查 Planner 方案）
1. 约束匹配：是否满足用户的预算、时间、偏好、人群特征。
2. 事实一致性：地点顺序是否合理，交通时间是否可行，行程是否过于压缩。
3. 完整性：是否包含总览、分日安排、预算、准备清单。
4. 风险控制：是否覆盖天气、拥堵、闭馆、排队等风险及应对。
5. 可执行性：是否给出清晰的时间段、交通方式、时长和关键预订事项。

## 用户反馈判定规则（用于处理 user 反馈）
以下情况视为“需要修改”：
- 用户提出新增景点、删减安排、调整预算、变更日期、增加同行人限制、改变住宿/交通偏好
- 用户认为当前方案不满意，希望优化节奏、顺序、成本、餐饮、住宿等
- 用户补充了 Planner 尚未覆盖的明确约束

以下情况视为“无需修改”：
- 用户明确表示同意当前方案
- 用户明确表示没有其他补充、可以按此执行、结束本轮

## 输出规则
### 情况 A：你收到的是 Agent_Planner 的方案
若不通过：
1. 输出 REVISE
2. 列出"必须修改"清单（最多 5 条）
3. 列出"可选优化"清单（最多 3 条）
4. 调用 transfer_to_Agent_Planner（必须！）

若通过：
1. 简要说明通过理由（1-3 条）
2. 输出 APPROVE
3. 明确提示用户：可以直接确认方案，也可以补充新的修改意见
4. 调用 transfer_to_user（必须！）

### 情况 B：你收到的是 user 的反馈
若反馈“无需修改”：
1. 用 1-3 句确认当前方案已被用户接受
2. 单独输出一行 TERMINATE
3. 不要调用任何 handoff 工具

若反馈“需要修改”：
1. 输出 REVISE
2. 用 2-5 条列出“用户新增/修改需求”
3. 必要时补一句提醒 Planner 优先保留原方案中仍然有效的安排
4. 调用 transfer_to_Agent_Planner（必须！）

## 禁止事项
- 不要重写整份方案，只指出缺陷和修正方向
- 不要在未满足上面规则时擅自输出 TERMINATE
- 不要把“用户只是表达感谢或同意”误判成需要改稿
"""


@dataclass
class AssistantRunResult:
    reply: str
    conversation_status: str


class TravelAssistantService:
    def __init__(self):
        """初始化旅行助手服务的运行状态。"""
        self.team = None
        self.awaiting_user_feedback = False
        self._active_cancellation_token: CancellationToken | None = None

    def cancel_active_run(self):
        """取消当前正在执行的团队运行。"""
        if self._active_cancellation_token is not None:
            self._active_cancellation_token.cancel()
            self._active_cancellation_token = None

    async def dispose(self):
        """
        释放当前 service 持有的团队状态，便于 session 被移除后尽快让对象可回收。
        """
        self.cancel_active_run()

        if self.team is not None:
            try:
                await self.team.reset()
            except RuntimeError:
                # 如果团队仍处于运行中，reset 会失败；此时直接丢弃引用，避免 session 长驻内存。
                pass

        self.team = None
        self.awaiting_user_feedback = False

    async def initialize(self):
        """初始化 MCP 工具并构建团队"""
        if not os.getenv("AMAP_MAPS_API_KEY"):
            raise RuntimeError("缺少环境变量 AMAP_MAPS_API_KEY，请先在 .env 中配置后再运行。")

        mcp_tools = await mcp_server_tools(mcp_server_params)

        agent_planner = AssistantAgent(
            name="Agent_Planner",
            model_client=deepseek_client,
            system_message=planner_sys_msg,
            tools=mcp_tools,
            handoffs=["Agent_Reviewer"],

        )

        agent_reviewer = AssistantAgent(
            name="Agent_Reviewer",
            model_client=deepseek_client,
            system_message=reviewer_sys_msg,
            handoffs=["Agent_Planner", "user"],
        )

        user_proxy = UserProxyAgent(
            name="user",
            description="人类用户，既可以提供初始旅行需求，也可以在方案通过审核后确认接受或继续补充反馈。",
        )

        # AutoGen 官方推荐的人类反馈模式是：
        # handoff 到 user 时先把控制权交回应用层，等用户真正输入反馈后，
        # 再以 HandoffMessage(source='user', target=原请求者, ...) 继续团队运行。
        # 因此这里保留 user handoff 作为“暂停点”，同时引入 TERMINATE 作为“完成点”。
        termination_condition = (
            HandoffTermination(target="user")
            | TextMentionTermination("TERMINATE")
        )

        self.team = Swarm(
            participants=[agent_planner, agent_reviewer, user_proxy],
            termination_condition=termination_condition,
        )

    def _build_task(self, user_input: str, is_feedback: bool):
        """根据当前轮次类型构造传给 Swarm 的任务对象。"""
        if is_feedback:
            return HandoffMessage(
                source="user",
                target="Agent_Reviewer",
                content=user_input,
            )
        return user_input

    async def _reset_for_new_conversation(self):
        """
        新的首轮规划必须清空上一轮团队上下文。
        否则 Swarm 会沿用历史消息，把上一次已结束的对话继续带进新任务。
        """
        if self.team is None:
            await self.initialize()
            return

        await self.team.reset()
        self.awaiting_user_feedback = False

    @staticmethod
    def _extract_latest_text(messages, source: str) -> str:
        """从消息列表中提取指定来源最近一条有效文本内容。"""
        for msg in reversed(messages):
            if isinstance(msg, (TextMessage, ThoughtEvent)) and getattr(msg, "source", "") == source:
                content = getattr(msg, "content", "")
                if isinstance(content, str) and content.strip():
                    return content
        return ""

    @staticmethod
    def _is_waiting_for_user(messages) -> bool:
        """判断当前对话是否停在等待用户反馈的 handoff 状态。"""
        if not messages:
            return False
        last_message = messages[-1]
        return isinstance(last_message, HandoffMessage) and last_message.target == "user"

    def _build_run_result(self, messages) -> AssistantRunResult:
        """根据本轮消息历史整理前端需要展示的回复与会话状态。"""
        planner_reply = self._extract_latest_text(messages, "Agent_Planner")
        reviewer_reply = self._extract_latest_text(messages, "Agent_Reviewer")

        if self._is_waiting_for_user(messages):
            self.awaiting_user_feedback = True
            return AssistantRunResult(
                reply=planner_reply or reviewer_reply or "本轮运行已结束，等待用户反馈。",
                conversation_status="awaiting_feedback",
            )

        if reviewer_reply and "TERMINATE" in reviewer_reply:
            self.awaiting_user_feedback = False
            return AssistantRunResult(
                reply=reviewer_reply,
                conversation_status="terminated",
            )

        if planner_reply:
            self.awaiting_user_feedback = False
            return AssistantRunResult(
                reply=planner_reply,
                conversation_status="terminated",
            )

        if reviewer_reply:
            self.awaiting_user_feedback = False
            return AssistantRunResult(
                reply=reviewer_reply,
                conversation_status="terminated",
            )

        self.awaiting_user_feedback = False
        print("\n[Debug] 提取失败！当前对话历史结构如下：")
        for i, msg in enumerate(messages):
            content_preview = str(getattr(msg, "content", ""))[:40].replace("\n", " ")
            print(
                f"  [{i}] Type: {type(msg).__name__}, "
                f"Source: {getattr(msg, 'source', 'N/A')}, "
                f"Content: '{content_preview}...'"
            )
        return AssistantRunResult(
            reply="未获取到有效回复，请查看终端运行日志排查原因。",
            conversation_status="terminated",
        )

    async def chat(self, user_input: str, is_feedback: bool = False) -> AssistantRunResult:
        """运行一轮团队对话，并返回当前轮次最需要展示给前端的结果。"""
        if self.team is None:
            await self.initialize()

        if not is_feedback:
            await self._reset_for_new_conversation()

        if is_feedback and not self.awaiting_user_feedback:
            raise RuntimeError("当前没有待处理的方案反馈，请先生成并审核通过一版旅行方案。")

        cancellation_token = CancellationToken()
        self._active_cancellation_token = cancellation_token
        task = self._build_task(user_input, is_feedback) # 通过task区分首轮规划和反馈阶段的改稿，交给 Swarm 内部的 Agent 处理
        try:
            result = await Console(self.team.run_stream(task=task, cancellation_token=cancellation_token))
            async for msg in self.team.run_stream(task=task, cancellation_token=cancellation_token):
                print(msg.type)
        finally:
            if self._active_cancellation_token is cancellation_token:
                self._active_cancellation_token = None
        messages = getattr(result, "messages", [])
        run_result = self._build_run_result(messages)
        print(f"\n\n本轮返回结果（{run_result.conversation_status}）\n", run_result.reply)
        return run_result
    

    async def test_type(self,user_input: str):
        async for msg in self.team.run_stream(task=user_input):
            if  isinstance(msg,ThoughtEvent | ToolCallSummaryMessage | TextMessage):
                print(msg)


    async def chat_stream(self, user_input: str, websocket, is_feedback: bool = False) -> AssistantRunResult:
        """流式处理版本，直接通过 WebSocket 向前端推送 Agent 的每一条思考和发信。"""

        from autogen_agentchat.messages import ModelClientStreamingChunkEvent, ToolCallRequestEvent
        if self.team is None:
            await self.initialize()

        if not is_feedback:
            await self._reset_for_new_conversation()

        if is_feedback and not self.awaiting_user_feedback:
            raise RuntimeError("当前没有待处理的方案反馈，请先生成并审核通过一版旅行方案。")

        task = self._build_task(user_input, is_feedback)
        run_messages = []
        cancellation_token = CancellationToken()
        self._active_cancellation_token = cancellation_token

        # 使用 async for 获取实时的流式反馈，让网页端实现真实"打字"和"思考"的过程
        try:
            async for msg in self.team.run_stream(task=task, cancellation_token=cancellation_token):
                source = getattr(msg, 'source', 'System')

                # 拦截流式文本碎片
                # 如果你的前端不需要“一个字一个字蹦出来”的打字机效果，必须在这里拦截掉！
                # 否则它们全都会掉进 else 分支，导致前端收到几千条 [系统流转中] 消息。
                if isinstance(msg, ModelClientStreamingChunkEvent):
                    continue  # 直接跳过，等待最后的完整 TextMessage/ThoughtEvent

                if hasattr(msg, "messages"):
                    run_messages = getattr(msg, "messages", []) or run_messages
                    continue

                run_messages.append(msg)

                # TextMessage 和 ThoughtEvent
                # 只要是有实质性内容的文本（无论是普通对话还是大模型的“思考”出的方案），都发给前端
                if isinstance(msg, (TextMessage, ThoughtEvent)):
                    content = getattr(msg, 'content', '')

                    # 增加判空：过滤掉纯粹为了调用工具而产生的“空文本消息”
                    if isinstance(content, str) and content.strip():
                        await websocket.send_json({
                            "type": "message",
                            "source": source,
                            "content": content
                        })
                        
                # 【维持原样】处理工具调用请求
                elif isinstance(msg, ToolCallRequestEvent):
                    # 调用了工具（例如正在查高德地图！）
                    tools_used = ", ".join([call.name for call in msg.content])
                    await websocket.send_json({
                        "type": "tool_call",
                        "source": source,
                        "content": f"[系统日志] 正在调用外部工具: {tools_used} ..."
                    })

                elif isinstance(msg, HandoffMessage):
                    await websocket.send_json({
                        "type": "info",
                        "source": source,
                        "content": f"[系统流转中] 已移交给 {msg.target}"
                    })

                # 【处理剩余的系统事件】
                # 这里现在只会捕获 ToolCallExecutionEvent(工具执行结果) 等
                else:
                    await websocket.send_json({
                        "type": "info",
                        "source": source,
                        "content": f"[系统流转中] 动作: {type(msg).__name__}"
                    })
        except Exception:
            self.cancel_active_run()
            raise
        finally:
            if self._active_cancellation_token is cancellation_token:
                self._active_cancellation_token = None

        return self._build_run_result(run_messages)

async def main():
    """保留原有 CLI 交互模式，作为测试入口"""
    print("=" * 60)
    print("  欢迎使用智能旅行助手！(后台类版本测试)")
    print("  输入你的旅行需求开始规划，输入 'exit' 退出。")
    print("=" * 60)

    assistant = TravelAssistantService()

    user_input = input("\n请输入你的旅行需求：\n> ").strip()
    if not user_input or user_input.lower() == "exit":
        return

    print("\n规划中，这可能需要几十秒...")
    result = await assistant.chat(user_input)
    print("\n" + result.reply)

    while True:
        user_input = input("\n对方案有什么意见？（输入 'exit' 退出）\n> ").strip()
        if not user_input or user_input.lower() == "exit":
            break

        print("\n重新规划中...")
        result = await assistant.chat(user_input, is_feedback=True)
        print("\n" + result.reply)


async def test_type():
    assistant = TravelAssistantService()
    await assistant.initialize()
    await assistant.test_type("4月4日上海一日游，7点到上海站，多去著名景点")

if __name__ == "__main__":
    asyncio.run(main())
