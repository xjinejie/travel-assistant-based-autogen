from dataclasses import dataclass
from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_ext.tools.mcp import StdioServerParams, mcp_server_tools
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_agentchat.teams import Swarm,RoundRobinGroupChat
from autogen_agentchat.ui import Console
from autogen_agentchat.conditions import (
    HandoffTermination,
    TextMentionTermination,
    MaxMessageTermination
)
from autogen_agentchat.messages import HandoffMessage, ThoughtEvent, TextMessage
from autogen_agentchat.messages import ModelClientStreamingChunkEvent, ToolCallSummaryMessage
import dotenv
import os
import asyncio

dotenv.load_dotenv()

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

agent_1 = AssistantAgent(
    name="chater_1",
    model_client=deepseek_client,
    system_message="你是一个激进的人",
)

agent_2= AssistantAgent(
    name="chater_2",
    model_client=deepseek_client,
    system_message="你是一个温和的人",
)

termination = MaxMessageTermination(4)

team = RoundRobinGroupChat(
    participants=[agent_1,agent_2],
    termination_condition=termination
)

async def main():
    task = "小学的儿子被别人的爸爸欺负了该揍他还是和他说说理？"
    async for msg in team.run_stream(task=task):
        if isinstance(msg, ThoughtEvent | TextMessage):
            print(msg)

asyncio.run(main())