"""Scenarios: Feishu messages from people in one group chat, in order, with tickets that already exist when they start.

A scenario is a JSON file under scenarios/ (see README.md). Each message reaches the Router after its turn for the previous
message ended (timing 'after-turn'), after the first tool round of that turn ('mid-turn'), or together with the previous
message, before the Router starts on it ('together').
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .engine import Engine, Trigger
from .ids import key
from .prompt import DEFAULT_RULES

SCENARIOS = Path(__file__).resolve().parent.parent / 'scenarios'
BOT = 'DSH'
TIMINGS = ('after-turn', 'mid-turn', 'together')


@dataclass
class Message:
    id: str
    sender: str
    at: str
    text: str
    mention: bool = True
    reply_to: str | None = None
    timing: str = 'after-turn'


@dataclass
class Scenario:
    name: str
    description: str
    repository: str
    chat: str
    chat_mode: str
    people: dict[str, str]  # Feishu open_id -> display name; all of them are on the allowed list
    messages: list[Message]
    tickets: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, name_or_path: str) -> 'Scenario':
        path = Path(name_or_path)
        if not path.exists():
            path = SCENARIOS / f'{name_or_path}.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        by_name = {name: open_id for open_id, name in data['people'].items()}
        messages = [Message(id=item['id'], sender=by_name.get(item['from'], item['from']), at=item['at'], text=item['text'],
                            mention=item.get('mention', True), reply_to=item.get('reply_to'), timing=item.get('timing', 'after-turn'))
                    for item in data['messages']]
        tickets = [{**seed, 'from': by_name.get(seed['from'], seed['from'])} for seed in data.get('tickets', [])]
        return cls(name=data['name'], description=data.get('description', ''), repository=data.get('repository', 'acme/web-app'),
                   chat=data.get('chat', 'oc_4d2f8a91'), chat_mode=data.get('chat_mode', 'mention'), people=data['people'],
                   messages=messages, tickets=tickets)

    def override(self, timing: str | None, reply: bool, no_mention: bool, chat_mode: str | None) -> None:
        """Command-line overrides for every message after the first."""
        for message in self.messages[1:]:
            if timing:
                message.timing = timing
            if reply:
                message.reply_to = self.messages[0].id
            if no_mention:
                message.mention = False
        if chat_mode:
            self.chat_mode = chat_mode

    def deployment(self) -> dict:
        login = 'acme-dsh-bot'
        return {'repository': self.repository, 'github_login': login, 'github_noreply': f'91827364+{login}@users.noreply.github.com',
                'feishu_bot': BOT}

    def settings(self) -> dict:
        return {'mode': 'observe', 'rules': DEFAULT_RULES, 'allowed': list(self.people),
                'admission': {'listedUnaddressed': 'inferred', 'unlistedAddressed': 'ignore', 'unlistedUnaddressed': 'ignore'},
                'feishu_chats': {self.chat: self.chat_mode}, 'feishu_other_chats': 'mention', 'feishu_direct': True}

    def root_of(self, message_id: str) -> str:
        """A Feishu reply's root_id is the root of the chain it replies into."""
        message = next((item for item in self.messages if item.id == message_id), None)
        return self.root_of(message.reply_to) if message and message.reply_to else message_id

    def trigger(self, message: Message) -> Trigger:
        """feishuTrigger (src/sources/feishu-trigger.ts): thread = chat + (root_id ?? message_id); mentions render as @name."""
        root = self.root_of(message.reply_to) if message.reply_to else None
        meta = {'chatId': self.chat, 'messageId': message.id, 'direct': False, 'mentioned': message.mention,
                **({'rootId': root, 'parentId': message.reply_to} if message.reply_to else {})}
        return Trigger(id=key('feishu', message.id), thread=f'feishu:{self.chat}:{root or message.id}', actor=message.sender, human=True,
                       at=message.at, text=f'@{BOT} {message.text}' if message.mention else message.text,
                       url=f'https://applink.feishu.cn/client/message/link/open?token=mock_{message.id}',
                       meta=meta, addressed='mention' if message.mention else None)

    def seed(self, engine: Engine) -> None:
        for seed in self.tickets:
            engine.seed_ticket(seed, seed['from'])


def flush(engine: Engine) -> list[dict]:
    engine.start_evals()
    payloads, engine.outbox = engine.outbox, []
    return payloads


def run(scenario: Scenario, engine: Engine, session, echo, max_turns: int = 20) -> dict:
    """Play the scenario against `session`; returns what happened to each message and the tickets."""
    scenario.seed(engine)
    names = {open_id: name for open_id, name in scenario.people.items()}
    notes = []
    turns = 0

    def ingest(message: Message) -> None:
        trigger = scenario.trigger(message)
        engine.now = trigger.at
        intake = engine.ingest(trigger)
        echo('arrive', {'trigger_id': trigger.id, 'thread': trigger.thread, 'text': f'{names[message.sender]}: {trigger.text}', 'intake': intake})

    def deliver_all() -> None:
        for payload in flush(engine):
            session.deliver(payload)

    def turn(between=None) -> None:
        nonlocal turns
        turns += 1
        if not session.run_turn(between):
            notes.append(f'turn {turns} hit max_steps')

    pending = list(scenario.messages)
    while pending:
        group = [pending.pop(0)]
        while pending and pending[0].timing == 'together':
            group.append(pending.pop(0))
        for message in group:
            ingest(message)
        deliver_all()
        mid = []
        while pending and pending[0].timing == 'mid-turn':
            mid.append(pending.pop(0))
        if session.messages[-1]['role'] != 'user':
            # Nothing reached the Router (filtered, not admitted, or a follow-up that only went to a worker).
            for message in mid:
                ingest(message)
            deliver_all()
            if session.messages[-1]['role'] == 'user':
                turn()
            continue
        injected = []

        def between(step: int) -> list[dict]:
            if injected or not mid:
                return []
            injected.append(step)
            for message in mid:
                ingest(message)
            return flush(engine)
        turn(between)
        if mid and not injected:
            notes.append('a mid-turn message arrived after the turn ended: the turn made no tool call')
            for message in mid:
                ingest(message)
            deliver_all()
            if session.messages[-1]['role'] == 'user':
                turn()
        # Anything the Engine queued meanwhile (a pending trigger on a thread whose Eval ended) gets its own turn.
        while turns < max_turns:
            payloads = flush(engine)
            if not payloads:
                break
            for payload in payloads:
                session.deliver(payload)
            turn()
    return outcome(scenario, engine, notes)


def outcome(scenario: Scenario, engine: Engine, notes: list[str]) -> dict:
    """What happened to each message, named so runs can be compared: S:<key> for seeded tickets, T1, T2… for new ones."""
    names = {engine.tickets[key('ticket', 'seed', seed['key'])].id: f"S:{seed['key']}" for seed in scenario.tickets}
    for ticket in engine.tickets.values():
        names.setdefault(ticket.id, f'T{sum(1 for name in names.values() if name.startswith("T")) + 1}')
    fates = []
    for index, message in enumerate(scenario.messages, 1):
        trigger_id = key('feishu', message.id)
        disposition = engine.dispositions.get(trigger_id, {})
        decisions = [decision for decision in engine.decisions if trigger_id in decision.trigger_ids]
        if 'filtered' in disposition:
            fate = f"filtered ({disposition['filtered']})"
        elif decisions and decisions[-1].by == 'gate':
            fate = 'not admitted'
        elif decisions:
            decision = decisions[-1]
            fate = decision.kind + (f'→{names[decision.ticket_id]}' if decision.ticket_id else '')
        elif disposition.get('fanout'):
            fate = 'follow-up→' + ','.join(names[ticket_id] for ticket_id in disposition['fanout'])
        else:
            fate = 'undecided'
        fates.append({'message': f'm{index}', 'sender': scenario.people[message.sender], 'fate': fate})
    ticket_notes = [{'ticket': names[ticket.id], 'text': text} for ticket in engine.tickets.values() for text in ticket.notes]
    routing_notes = ([{'ticket': names[ticket.id], 'on': 'accept', 'text': ticket.note} for ticket in engine.tickets.values() if ticket.note]
                     + [{'ticket': names[ticket.id], 'on': 'attach', 'text': item['note']}
                        for ticket in engine.tickets.values() for item in ticket.inputs if item.get('note')])
    signature = (' | '.join(f"{item['message']}:{item['fate']}" for item in fates)
                 + f' | routing_notes:{len(routing_notes)} | ticket_notes:{len(ticket_notes)}')
    return {'label': signature, 'messages': fates, 'ticket_notes': ticket_notes, 'routing_notes': routing_notes, 'notes': notes,
            'tickets': [{'name': names[ticket.id], 'ticket_id': ticket.id, 'title': ticket.title, 'worktree_name': ticket.worktree_name,
                         'acknowledgement': ticket.acknowledgement, 'threads': ticket.threads, 'inputs': ticket.inputs}
                        for ticket in engine.tickets.values()]}
