import asyncio
from tests.fakes import ToolCallingLlm
from src.agent import build_agent, build_runner, _event_text
from src.store import InMemoryOrderStore
from src.seed_data import CONFIG
from google.adk.sessions import InMemorySessionService
from google.genai import types

store = InMemoryOrderStore(config={**CONFIG, "cutoff_time": "23:59"})
agent = build_agent(model=ToolCallingLlm(), store=store)
runner = build_runner(agent, InMemorySessionService())

content = types.Content(role="user", parts=[types.Part(text="2 tons of acid")])
for event in runner.run(user_id="+919812345001", session_id="+919812345001", new_message=content):
    if event.content and event.content.parts:
        for p in event.content.parts:
            if p.function_response and p.function_response.response:
                resp = p.function_response.response
                if isinstance(resp, dict):
                    print(f"fn_resp keys={list(resp.keys())} has_reply_hint={'reply_hint' in resp}")
                    if "reply_hint" in resp:
                        print(f"  reply_hint={resp['reply_hint']!r}")
                else:
                    print(f"fn_resp type={type(resp).__name__} not a dict")
    text = _event_text(event)
    print(f"  final={event.is_final_response()} text={text!r}")
