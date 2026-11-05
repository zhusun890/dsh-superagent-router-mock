"""The main Session's model loop: one persistent history, delivered JSON texts as user messages, tools until the turn ends.

DSH delivers candidate cards and relays with sessions.prompt(..., 'steer') (src/app/sessions.ts:162); here each one becomes a
user message whose content is the same JSON string. A turn runs model requests until a response has no tool calls.
"""
from __future__ import annotations

import json
from typing import Callable

from .tools import MainTools, openai_tools


def dumps(value: object) -> str:
    """JSON.stringify: no spaces, non-ASCII kept as is."""
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class RouterSession:
    def __init__(self, client, model: str, system: str, tools: MainTools, *, extra_body: dict | None = None,
                 temperature: float | None = None, max_steps: int = 12, echo: Callable[[str, object], None] = lambda kind, value: None):
        self.client = client
        self.model = model
        self.tools = tools
        self.extra_body = extra_body
        self.temperature = temperature
        self.max_steps = max_steps
        self.echo = echo
        self.messages: list[dict] = [{'role': 'system', 'content': system}]
        self.usage = {'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'cached_prompt_tokens': 0}

    def deliver(self, payload: dict) -> None:
        self.messages.append({'role': 'user', 'content': dumps(payload)})
        self.echo('deliver', payload)

    def request(self):
        options = {'model': self.model, 'messages': self.messages, 'tools': openai_tools()}
        if self.extra_body:
            options['extra_body'] = self.extra_body
        if self.temperature is not None:
            options['temperature'] = self.temperature
        response = self.client.chat.completions.create(**options)
        usage = response.usage
        if usage:
            self.usage['requests'] += 1
            self.usage['prompt_tokens'] += usage.prompt_tokens or 0
            self.usage['completion_tokens'] += usage.completion_tokens or 0
            details = getattr(usage, 'prompt_tokens_details', None)
            cached = getattr(details, 'cached_tokens', None) or (usage.model_extra or {}).get('prompt_cache_hit_tokens') or 0
            self.usage['cached_prompt_tokens'] += cached
        return response.choices[0].message

    def run_turn(self, between_rounds: Callable[[int], list[dict]] | None = None) -> bool:
        """Run one turn. `between_rounds(step)` may return payloads to deliver after a tool round, before the next request.
        Returns False when the turn hit max_steps without ending."""
        for step in range(self.max_steps):
            message = self.request()
            entry: dict = {'role': 'assistant', 'content': message.content or ''}
            reasoning = getattr(message, 'reasoning_content', None) or (message.model_extra or {}).get('reasoning_content')
            if reasoning:
                # DeepSeek's thinking mode expects reasoning_content back on assistant messages within a tool-using turn.
                entry['reasoning_content'] = reasoning
                self.echo('reasoning', reasoning)
            if message.content:
                self.echo('assistant', message.content)
            if message.tool_calls:
                entry['tool_calls'] = [{'id': call.id, 'type': 'function',
                                        'function': {'name': call.function.name, 'arguments': call.function.arguments}}
                                       for call in message.tool_calls]
            self.messages.append(entry)
            if not message.tool_calls:
                return True
            for call in message.tool_calls:
                self.echo('tool_call', {'name': call.function.name, 'arguments': call.function.arguments})
                result = self.tools.call(call.function.name, call.function.arguments)
                self.echo('tool_result', result)
                self.messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': dumps(result)})
            for payload in (between_rounds(step) if between_rounds else []):
                self.deliver(payload)
        return False


class ScriptedSession(RouterSession):
    """A stand-in model for --dry-run: accepts every explicit candidate. Exercises the engine and tools without an API."""

    def __init__(self, system: str, tools: MainTools, echo: Callable[[str, object], None]):
        super().__init__(None, 'scripted', system, tools, echo=echo)

    def request(self):
        # The candidates delivered since the scripted Router last routed anything.
        candidates = []
        for message in reversed(self.messages):
            if message['role'] == 'assistant' and message.get('tool_calls'):
                break
            if message['role'] == 'user':
                payload = json.loads(message['content'])
                if payload.get('kind') == 'candidate':
                    candidates.insert(0, payload)
        if not candidates:
            return _Message(content='(scripted) nothing to route', tool_calls=None)
        items = [{'kind': 'accept', 'trigger_id': payload['trigger_id'], 'title': payload['text'][:60],
                  'worktree_name': 'scripted-ticket', 'acknowledgement': '收到。'} for payload in candidates]
        return _Message(content='', tool_calls=[_Call(f'call_{len(self.messages)}', 'orchestrator_route', dumps({'decisions': items}))])


class _Function:
    def __init__(self, name: str, arguments: str):
        self.name, self.arguments = name, arguments


class _Call:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id, self.function = call_id, _Function(name, arguments)


class _Message:
    def __init__(self, content: str, tool_calls: list | None):
        self.content, self.tool_calls, self.model_extra = content, tool_calls, {}
