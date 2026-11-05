"""A small in-memory stand-in for the dsh-superagent Engine, as far as the Router can observe it.

Covers Feishu triggers, threads and subscriptions, the admission gate, one Eval per thread with a pending batch, tickets,
decisions, fan-out of follow-ups to tickets, and the JSON the main Session receives: candidate cards (src/app/links.ts,
src/app/assessment.ts:99) and follow-up relays (src/app/notices.ts). Workers are not simulated: a ticket never reports.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ids import key
from .prompt import policy_version

TRIGGER_CHARS = 600

REASONS = {
    'noAdmissible': 'No admissible trigger',
    'startedTicket': 'Already started ticket {ticket}',
    'explicitRouted': 'Explicit request from an allowed sender routed by the orchestrator',
    'humanStop': 'Human asked to stop',
    'stopUnknown': 'Stop target is unknown or already ended',
    'stopNotExplicit': 'A stop needs an explicit request from a sender on the list',
    'observed': 'Observe mode: would accept ({text})',
    'senderNotMember': 'The sender is not on the Source\'s list',
}
USER_ERRORS = {
    'unknownTicket': 'Unknown ticket',
    'unknownTrigger': 'Unknown trigger',
    'notAdmissible': 'Trigger is not admissible: {reason}',
    'notExplicit': 'An accept through orchestrator_route is for explicit requests from allowed senders; assess proactive candidates with orchestrator_eval and orchestrator_eval_decide',
    'ticketStopped': 'A person stopped this ticket; new work needs an accept through orchestrator_route',
    'notRetryable': 'Only an open ticket can be retried, and only when its worker last reported failed or blocked and no person has sent a message since, or when delivering an Input to its worker failed',
    'handedBackHere': 'The worker of this ticket handed that trigger back as beyond it: accept it as new work, attach it to another ticket, or ignore it',
    'workerDecides': 'This message reached ticket {tickets} on its thread: its worker decides whether it belongs there and hands back what does not, which then arrives as a candidate. Only a stop goes through orchestrator_route, with kind stop',
    'notReady': 'Ticket is not ready for delivery. Keep progress internal and wait for a verified ready report; do not relabel progress as an answer or send it through another channel.',
    'notAttention': 'Attention requires the ticket\'s current attention question in human_action. Routine unfinished work stays internal.',
    'noAnswer': 'No worker answer is ready for this human trigger. Wait for the worker\'s report, or request one with orchestrator_ticket_request_status, which acts only on a worker that reported waiting; ordinary progress cannot be relabelled as an answer.',
}
FOLLOWUP_NOTE = ('Its ticket\'s worker has it and decides; act only if the person asks to stop the work '
                 '(orchestrator_route kind stop with this trigger_id).')
NEXT_EXPLICIT = 'Route it with orchestrator_route as dispatch says: accept, attach, stop or ignore.'
NEXT_INFERRED = ('Ignore it with orchestrator_route if the card shows it touches no enabled rule; else assess it with '
                 'orchestrator_eval, orchestrator_eval_read, then orchestrator_eval_decide.')


class UserError(Exception):
    def __init__(self, code: str, **values: str):
        super().__init__(USER_ERRORS[code].format(**values) if values else USER_ERRORS[code])
        self.code = code


def reason_text(code: str, **values: str) -> str:
    return REASONS[code].format(**values) if values else REASONS[code]


def preview(text: str) -> str:
    return re.sub(r'\s+', ' ', text)[:160]


@dataclass
class Trigger:
    id: str
    thread: str  # primary thread key, such as feishu:<chat>:<root message>
    actor: str
    human: bool
    at: str
    text: str
    url: str | None
    meta: dict
    addressed: str | None  # 'mention', 'direct' or None


@dataclass
class Eval:
    id: str
    thread: str
    trigger_ids: list[str]
    primary: str
    strength: str


@dataclass
class Thread:
    key: str
    created_at: str
    subscriptions: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    evaluating: Eval | None = None


@dataclass
class Ticket:
    id: str
    title: str
    strength: str
    created_at: str
    decision_id: str
    origin_trigger: str
    origin_triggers: list[str]
    worktree_name: str
    acknowledgement: str
    note: str | None
    threads: list[str] = field(default_factory=list)
    inputs: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    assignment: str = 'pending'  # the worker is never simulated, so the ticket stays working/queued
    ended: dict | None = None
    # Only seeded tickets set these: a card state other than working/queued, a bound PR, the latest report's summary.
    state: str | None = None
    pr_number: int | None = None
    report: str = ''


@dataclass
class Decision:
    id: str
    kind: str
    trigger_ids: list[str]
    eval_id: str
    thread: str
    at: str
    reason: str
    ticket_id: str | None = None
    by: str = 'router'  # 'gate' when no trigger was admissible and no Eval started


class Engine:
    def __init__(self, settings: dict):
        self.settings = settings
        self.now = '1970-01-01T00:00:00.000Z'
        self.triggers: dict[str, Trigger] = {}
        self.dispositions: dict[str, dict] = {}
        self.threads: dict[str, Thread] = {}
        self.tickets: dict[str, Ticket] = {}
        self.decisions: list[Decision] = []
        self.replies: list[dict] = []
        # JSON texts waiting to be delivered to the main Session, in order (candidate cards and relays).
        self.outbox: list[dict] = []

    # ---- intake -------------------------------------------------------------------------------------------------
    def thread(self, thread_key: str) -> Thread:
        if thread_key not in self.threads:
            self.threads[thread_key] = Thread(key=thread_key, created_at=self.now)
        return self.threads[thread_key]

    def scope(self, trigger: Trigger) -> dict:
        """The Feishu Source's scope (src/sources/feishu.ts:55): whether the chat is received at all, and whether every
        message in it is observed (listen = all) or only @-mentions. Mentions are judged later, by the gate."""
        if trigger.meta.get('direct'):
            return {'listened': True, 'observed': False} if self.settings['feishu_direct'] else {'listened': False, 'reason': 'direct_messages_not_allowed'}
        chat = self.settings['feishu_chats'].get(trigger.meta['chatId'])
        if chat:
            return {'listened': True, 'observed': chat == 'all'}
        if self.settings['feishu_other_chats'] == 'mention':
            return {'listened': True, 'observed': False}
        return {'listened': False, 'reason': 'chat_not_allowed'}

    def ingest(self, trigger: Trigger) -> str:
        """Record a trigger; fan it out to tickets subscribed to its thread, or queue it for an Eval (src/core/intake.ts)."""
        if trigger.id in self.triggers:
            return 'duplicate'
        self.triggers[trigger.id] = trigger
        disposition = {'fanout': [], 'at': self.now}
        self.dispositions[trigger.id] = disposition
        scope = self.scope(trigger)
        if not scope['listened']:
            disposition['filtered'] = scope['reason']
            return 'filtered'
        thread = self.thread(trigger.thread)
        reached = []
        for ticket_id in thread.subscriptions:
            ticket = self.tickets[ticket_id]
            resumes = trigger.human and (ticket.ended or {}).get('by') != 'human'
            if ticket.ended and not resumes:
                continue
            reached.append(ticket)
        if reached:
            for ticket in reached:
                ticket.inputs.append({'kind': 'trigger', 'trigger_id': trigger.id, 'at': self.now, 'human': trigger.human})
                ticket.ended = None
            disposition['fanout'] = [ticket.id for ticket in reached]
            if trigger.human:
                self.outbox.append(self.followup_relay(trigger, reached))
            return 'fanout'
        thread.pending.append(trigger.id)
        return 'queued'

    def gate(self, trigger: Trigger) -> tuple[str, str | None]:
        """Admission (src/policy.ts:191 gate): ('explicit' | 'inferred', None), or ('ignore', why it is not admissible)."""
        if not trigger.human:
            return 'ignore', 'machine'
        allowed = trigger.actor in self.settings['allowed']
        addressed = trigger.addressed is not None
        if allowed and addressed:
            return 'explicit', None
        scope = self.scope(trigger)
        # Unaddressed messages only use the admission table when the Source observes all messages in that scope.
        receivable = addressed or (scope['listened'] and scope['observed'])
        cell = ('listedUnaddressed' if allowed else 'unlistedAddressed' if addressed else 'unlistedUnaddressed')
        admission = self.settings['admission'][cell] if receivable else 'ignore'
        if admission == 'explicit':
            return 'explicit', None
        mode = self.settings['mode']
        # A thread with tickets of its own takes follow-ups that did not reach a worker (an ended ticket's thread, say).
        if (not (trigger.addressed == 'direct' and not allowed) and (allowed or mode != 'off')
                and self.thread(trigger.thread).subscriptions):
            return ('explicit' if allowed else 'inferred'), None
        if admission == 'inferred' and mode != 'off':
            return 'inferred', None
        return 'ignore', ('not_a_member' if not allowed else 'admission_ignored' if receivable and admission == 'ignore' else 'no_mention')

    def start_evals(self) -> None:
        """Start an Eval on every thread with queued triggers and none running; its candidate card goes to the outbox."""
        order = {'ignore': 0, 'inferred': 1, 'explicit': 2}
        for thread in self.threads.values():
            if thread.evaluating or not thread.pending:
                continue
            strongest: tuple[Trigger, str] | None = None
            reasons = []
            for trigger_id in thread.pending:
                trigger = self.triggers[trigger_id]
                strength, why = self.gate(trigger)
                if strength == 'ignore':
                    reasons.append(f'{trigger_id}: {why}')
                    continue
                if (strongest is None or order[strength] > order[strongest[1]]
                        or (strength == strongest[1] and trigger.at >= strongest[0].at)):
                    strongest = (trigger, strength)
            if strongest is None:
                # No Eval starts and the Router hears nothing of these triggers.
                eval_id = key('eval', thread.key, 'ignored', self.now, *thread.pending)
                self.record(thread, Eval(eval_id, thread.key, list(thread.pending), thread.pending[-1], 'inferred'),
                            'ignore', '; '.join(reasons) or reason_text('noAdmissible'), by='gate')
                thread.pending = []
                continue
            eval_id = key('eval', thread.key, policy_version(self.settings), self.now, *thread.pending)
            thread.evaluating = Eval(eval_id, thread.key, list(thread.pending), strongest[0].id, strongest[1])
            thread.pending = []
            for trigger_id in thread.evaluating.trigger_ids:
                self.dispositions[trigger_id]['evalId'] = eval_id
            self.outbox.append(self.candidate_card(thread))

    # ---- decisions ----------------------------------------------------------------------------------------------
    def holding(self, trigger_id: str) -> Thread | None:
        return next((thread for thread in self.threads.values()
                     if thread.evaluating and trigger_id in thread.evaluating.trigger_ids), None)

    def owners(self, trigger: Trigger) -> list[str]:
        """Live tickets the trigger reached on their threads, whose workers decide it (followupOwners)."""
        disposition = self.dispositions.get(trigger.id, {})
        handed_back = {item['ticket_id'] for item in disposition.get('handbacks', [])}
        return [ticket_id for ticket_id in disposition.get('fanout', [])
                if ticket_id not in handed_back and not self.tickets[ticket_id].ended]

    def left_to_workers(self, trigger: Trigger) -> None:
        owners = self.owners(trigger)
        if owners:
            raise UserError('workerDecides', tickets=', '.join(owners))

    def decide(self, trigger_id: str, kind: str, **outcome) -> Decision:
        """engine.decide for accept, stop and ignore (src/core/evals.ts decide)."""
        trigger = self.triggers.get(trigger_id)
        if trigger is None:
            raise UserError('unknownTrigger')
        held = self.holding(trigger_id)
        if kind == 'accept':
            earlier = next((decision for decision in self.decisions
                            if decision.kind == 'accept' and decision.ticket_id and trigger_id in decision.trigger_ids), None)
            if earlier:
                return earlier
        if kind != 'stop':
            self.left_to_workers(trigger)
        thread = held or self.thread(trigger.thread)
        evaluation = held.evaluating if held else self.direct_eval(thread, trigger, kind, outcome.get('ticket_id'))
        if kind == 'accept' and evaluation.strength != 'explicit':
            raise UserError('notExplicit')
        if kind == 'accept':
            ticket_id = key('ticket', evaluation.id)
            ticket = Ticket(id=ticket_id, title=outcome['title'], strength=evaluation.strength, created_at=self.now,
                            decision_id=key('decision', evaluation.id), origin_trigger=evaluation.primary,
                            origin_triggers=list(evaluation.trigger_ids), worktree_name=outcome['name'],
                            acknowledgement=outcome['acknowledgement'], note=outcome.get('note'))
            self.tickets[ticket_id] = ticket
            for member in evaluation.trigger_ids:
                self.subscribe(ticket, self.triggers[member].thread)
            if ticket.acknowledgement:
                self.replies.append({'kind': 'acknowledgement', 'ticket_id': ticket_id, 'reply_to': evaluation.primary,
                                     'text': ticket.acknowledgement, 'at': self.now})
            return self.record(thread, evaluation, 'accept', reason_text('explicitRouted'), ticket_id)
        if kind == 'stop':
            target = self.tickets.get(outcome.get('ticket_id') or '')
            if evaluation.strength != 'explicit':
                return self.record(thread, evaluation, 'ignore', reason_text('stopNotExplicit'))
            if target is None or target.ended:
                return self.record(thread, evaluation, 'ignore', reason_text('stopUnknown'))
            reason = outcome.get('reason') or reason_text('humanStop')
            target.ended = {'at': self.now, 'by': 'human', 'outcome': 'stopped', 'reason': reason}
            return self.record(thread, evaluation, 'stop', reason, target.id)
        return self.record(thread, evaluation, 'ignore', outcome['reason'])

    def direct_eval(self, thread: Thread, trigger: Trigger, kind: str, ticket_id: str | None) -> Eval:
        strength, why = self.gate(trigger)
        if strength == 'ignore' and kind != 'ignore':
            raise UserError('notAdmissible', reason=why or reason_text('noAdmissible'))
        eval_id = key('eval', thread.key, 'direct', trigger.id, kind, *([ticket_id] if ticket_id else []), self.now)
        return Eval(eval_id, thread.key, [trigger.id], trigger.id, strength)

    def record(self, thread: Thread, evaluation: Eval, kind: str, reason: str, ticket_id: str | None = None,
               by: str = 'router') -> Decision:
        decision = Decision(id=key('decision', evaluation.id), kind=kind, trigger_ids=list(evaluation.trigger_ids),
                            eval_id=evaluation.id, thread=thread.key, at=self.now, reason=reason, ticket_id=ticket_id, by=by)
        self.decisions.append(decision)
        if thread.evaluating and thread.evaluating.id == evaluation.id:
            thread.evaluating = None
        for trigger_id in evaluation.trigger_ids:
            self.dispositions.setdefault(trigger_id, {'fanout': [], 'at': self.now})['decisionId'] = decision.id
        return decision

    def subscribe(self, ticket: Ticket, thread_key: str) -> None:
        thread = self.thread(thread_key)
        if ticket.id not in thread.subscriptions:
            thread.subscriptions.append(ticket.id)
        if thread_key not in ticket.threads:
            ticket.threads.append(thread_key)

    def attach(self, ticket_id: str, trigger_id: str, note: str | None) -> Ticket:
        """engine.attach (src/core/engine.ts:109, src/core/tickets.ts attachable/attach)."""
        handbacks = self.dispositions.get(trigger_id, {}).get('handbacks', [])
        if any(item['ticket_id'] == ticket_id for item in handbacks):
            raise UserError('handedBackHere')
        trigger = self.triggers.get(trigger_id)
        if trigger is None:
            raise UserError('unknownTrigger')
        self.left_to_workers(trigger)
        ticket = self.tickets.get(ticket_id)
        if ticket is None:
            raise UserError('unknownTicket')
        if ticket.ended and ticket.ended.get('by') == 'human':
            raise UserError('ticketStopped')
        held = self.holding(trigger_id)
        if held:
            held.evaluating.trigger_ids.remove(trigger_id)
            if not held.evaluating.trigger_ids:
                held.evaluating = None
        self.subscribe(ticket, trigger.thread)
        disposition = self.dispositions[trigger_id]
        disposition['fanout'] = list(dict.fromkeys([*disposition['fanout'], ticket_id]))
        ticket.inputs.append({'kind': 'trigger', 'trigger_id': trigger_id, 'at': self.now, 'human': trigger.human,
                              **({'note': note} if note else {})})
        self.decisions.append(Decision(id=key('routed', trigger_id, ticket_id), kind='attach', trigger_ids=[trigger_id],
                                       eval_id='', thread=trigger.thread, at=self.now,
                                       reason=f'Routed to ticket {ticket_id}', ticket_id=ticket_id))
        ticket.ended = None
        return ticket

    # ---- what the main Session reads ----------------------------------------------------------------------------
    def card_state(self, ticket: Ticket) -> str:
        if ticket.ended:
            return ticket.ended['outcome']
        if ticket.state:
            return ticket.state
        return 'working/queued' if ticket.assignment == 'pending' else 'working'

    def ticket_card(self, ticket: Ticket) -> dict:
        card = {'ticket_id': ticket.id, 'title': ticket.title, 'state': self.card_state(ticket), 'strength': ticket.strength}
        if ticket.pr_number:
            card['pr_number'] = ticket.pr_number
        if ticket.ended:
            card['ended'] = {'at': ticket.ended['at'], 'by': ticket.ended['by'],
                             'reason': ticket.ended.get('reason') or ticket.ended['outcome']}
        card.update({'preview': preview(ticket.report), 'read': f'ticket/{ticket.id}'})
        return card

    def seed_ticket(self, seed: dict, actor: str) -> Ticket:
        """A ticket accepted before the scenario starts, so the Router's history does not hold its candidate card (as after a
        handoff). Its request is stored as its origin trigger; it follows that trigger's thread and its PR's thread."""
        trigger = Trigger(id=key('seed', seed['key']), thread=seed['thread'], actor=actor, human=True, at=seed['created_at'],
                          text=seed['request'], url=None, meta={}, addressed='mention')
        self.triggers[trigger.id] = trigger
        ticket = Ticket(id=key('ticket', 'seed', seed['key']), title=seed['title'], strength='explicit', created_at=seed['created_at'],
                        decision_id=key('decision', 'seed', seed['key']), origin_trigger=trigger.id, origin_triggers=[trigger.id],
                        worktree_name=seed.get('worktree_name', seed['key']), acknowledgement='', note=None,
                        state=seed.get('state'), pr_number=seed.get('pr_number'), report=seed.get('report', ''))
        self.tickets[ticket.id] = ticket
        self.subscribe(ticket, trigger.thread)
        if ticket.pr_number:
            self.subscribe(ticket, f'github:{ticket.pr_number}')
        self.decisions.append(Decision(id=ticket.decision_id, kind='accept', trigger_ids=[trigger.id], eval_id='', thread=trigger.thread,
                                       at=ticket.created_at, reason=reason_text('explicitRouted'), ticket_id=ticket.id, by='seed'))
        self.dispositions[trigger.id] = {'fanout': [], 'at': ticket.created_at, 'decisionId': ticket.decision_id}
        return ticket

    def trigger_card(self, trigger: Trigger, with_disposition: bool = False) -> dict:
        card = {'trigger_id': trigger.id, 'source': 'feishu', **({'source_url': trigger.url} if trigger.url else {}),
                'actor': trigger.actor, 'thread': trigger.thread, 'human': trigger.human, 'at': trigger.at,
                'preview': preview(trigger.text), 'chars': len(trigger.text), 'read': f'trigger/{trigger.id}'}
        if with_disposition:
            disposition = self.dispositions.get(trigger.id, {})
            card['disposition'] = {**({'filtered': disposition['filtered']} if 'filtered' in disposition else {}),
                                   **({'decision_id': disposition['decisionId']} if 'decisionId' in disposition else {}),
                                   'delivered_to': disposition.get('fanout', [])}
        return card

    def replied_to(self, trigger: Trigger) -> Trigger | None:
        parent = trigger.meta.get('parentId')
        return self.triggers.get(key('feishu', parent)) if parent else None

    def trigger_message(self, trigger: Trigger, link: dict, note: str | None = None) -> dict:
        """triggerMessage (src/executor/dsh.ts:121): the trigger as workers and the main Session read it."""
        text = trigger.text
        capped = ({'text': text} if len(text) <= TRIGGER_CHARS
                  else {'text': text[:TRIGGER_CHARS] + '…', 'chars': len(text), 'read': f'trigger/{trigger.id}'})
        message = {'trigger_id': trigger.id, 'from': trigger.actor, 'thread': trigger.thread, 'at': trigger.at, **capped, **link}
        if note:
            message['routing_note'] = note
        parent = self.replied_to(trigger)
        if parent:
            message['replies_to'] = {'trigger_id': parent.id, 'preview': preview(parent.text)}
        return message

    def tickets_on_thread(self, thread: Thread) -> list[Ticket]:
        return [self.tickets[ticket_id] for ticket_id in thread.subscriptions]

    def related_tickets(self, tickets: list[Ticket]) -> dict:
        ordered = sorted(tickets, key=lambda ticket: ticket.created_at, reverse=True)
        ordered = [ticket for ticket in ordered if not ticket.ended] + [ticket for ticket in ordered if ticket.ended]
        return {'related_tickets': [self.ticket_card(ticket) for ticket in ordered[:8]], 'tickets_total': len(tickets)}

    def referenced_tickets(self, trigger: Trigger, known: list[Ticket]) -> list[Ticket]:
        """Tickets subscribed to threads that `#12` or a URL in the text names; only the GitHub Source resolves any."""
        threads = []
        for reference in re.findall(r'#\d+|https?://[^\s)>\]]+', trigger.text):
            match = re.match(r'^#?(\d+)$', reference.strip()) or re.search(r'github\.com/[^/]+/[^/]+/(?:pull|issues)/(\d+)', reference)
            if match:
                threads.append(f'github:{match.group(1)}')
        ids = list(dict.fromkeys(ticket_id for thread in threads for ticket_id in self.threads.get(thread, Thread(thread, '')).subscriptions))
        known_ids = {ticket.id for ticket in known}
        return [self.tickets[ticket_id] for ticket_id in ids if ticket_id not in known_ids][:4]

    def candidate_card(self, thread: Thread) -> dict:
        """candidateCard (src/app/links.ts:55) as delivered by assessment.ts:99."""
        evaluation = thread.evaluating
        triggers = [self.triggers[trigger_id] for trigger_id in evaluation.trigger_ids]
        primary = triggers[-1]
        tickets = self.tickets_on_thread(thread)
        delivered = self.dispositions.get(primary.id, {}).get('fanout', [])
        referenced = self.referenced_tickets(primary, tickets)
        handed_back = [{'trigger_id': trigger.id, 'ticket_id': item['ticket_id'], 'reason': item['reason']}
                       for trigger in triggers for item in self.dispositions.get(trigger.id, {}).get('handbacks', [])]
        others = [trigger for trigger in triggers if trigger.id != primary.id]
        link = {'source_url': primary.url} if primary.url else {}
        card = {'kind': 'candidate', **self.trigger_message(primary, link), 'strength': evaluation.strength,
                'mode': self.settings['mode']}
        if others:
            card['batch'] = [self.trigger_card(trigger) for trigger in others]
        if handed_back:
            card['handed_back'] = handed_back
        card.update(self.related_tickets(tickets))
        if delivered:
            card['delivered_to'] = delivered
        if referenced:
            card['referenced_tickets'] = [self.ticket_card(ticket) for ticket in referenced]
        card['next_action'] = NEXT_EXPLICIT if evaluation.strength == 'explicit' else NEXT_INFERRED
        return card

    def followup_relay(self, trigger: Trigger, tickets: list[Ticket]) -> dict:
        """followupRelay (src/app/notices.ts:102): a person's message that reached tickets, which their workers decide."""
        return {'kind': 'follow_up', **self.trigger_message(trigger, {}),
                'delivered_to': [{'ticket_id': ticket.id, 'title': ticket.title, 'state': self.card_state(ticket)}
                                 for ticket in tickets],
                'note': FOLLOWUP_NOTE}
