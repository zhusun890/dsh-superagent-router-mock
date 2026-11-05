"""Router (orchestrator) system prompt, composed the way dsh-superagent does it.

Mirrors src/app/skill-files.ts (promptFile), src/app/context.ts, src/app/prompts.ts (promptParts, environmentSection,
policySection) and src/app/sessions.ts:244 (the revision header). Prompt texts are copied from the prototype under prompts/.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .ids import key

PROMPTS = Path(__file__).resolve().parent.parent / 'prompts'


def prompt_file(name: str) -> str:
    """promptFile: trim, split on blank lines, join the lines inside each paragraph with spaces, paragraphs with '\\n'."""
    text = (PROMPTS / f'{name}.md').read_text(encoding='utf-8').strip()
    paragraphs = re.split(r'\n(?:[ \t]*\n)+', text)
    return '\n'.join(' '.join(line.strip() for line in paragraph.split('\n')) for paragraph in paragraphs)


TEAM_HEADINGS = {'communication': 'Communication', 'scope': 'Ticket scope', 'delivery': 'Delivery requirements'}
ENVIRONMENT_HEADING = ('Execution environment (from this installation\'s settings; it takes precedence over any repository or '
                       'account named elsewhere in these instructions; an empty value means that part is not configured here). '
                       'The prepare command of a ticket directory reads the same variables:')
POLICY_HEAD = ('## Policy\nPolicy version {version}; accept mode {mode}. The accept mode and the rules decide proactive candidates '
               'only: an explicit request is routed as dispatch says, in any mode and whatever the rules.')

# src/policy.ts DEFAULT_RULES
DEFAULT_RULES = [
    {'id': 'copy', 'name': 'Copy fixes', 'enabled': True,
     'instruction': 'Explicit requests to correct a typo, wording, punctuation or existing translation. Preserve product promises and business meaning.'},
    {'id': 'visual', 'name': 'Visual fixes', 'enabled': True,
     'instruction': 'Explicit implementation requests for local spacing, alignment, overflow or styling, with a specified target or an objectively established existing standard/correct state to restore. Locating an element or calling it inconsistent, unattractive or hard to understand does not establish a target. Do not invent visual style, information hierarchy, rendering or interaction decisions. No redesign, new interaction or behavior. A known spacing defect does not authorize adjacent undefined restyling; assess only a clearly separable authorized scope. If direction or scope is unsettled, observe only: no acknowledgement, worker or unsolicited question.'},
]


def environment_variables(deployment: dict) -> list[tuple[str, str, str]]:
    """Deployment-level variables the main Session sees (environmentOf without a ticket or trigger): executor, GitHub, Feishu."""
    return [
        ('DSH_SUPERAGENT_BLOCKED_COMMANDS', '', 'Commands blocked in this execution environment, separated by spaces; never run them'),
        ('DSH_SUPERAGENT_GITHUB_REPOSITORY', deployment['repository'], 'The repository this Source watches, as owner/name. A PR the agent opens here from a branch whose last path segment is DSH_SUPERAGENT_NAME (for example agent/$DSH_SUPERAGENT_NAME) binds to the ticket'),
        ('DSH_SUPERAGENT_GITHUB_ACCOUNT', deployment['github_login'], 'The agent\'s GitHub account. gh is signed in to it; use gh for every GitHub operation except auto-merge, and never another account or credential'),
        ('DSH_SUPERAGENT_GITHUB_COMMIT_EMAIL', deployment['github_noreply'], 'Commit email of the agent\'s account. git has no identity here: commit as the author these instructions name, or as the agent\'s account with this email when they name none'),
        ('DSH_SUPERAGENT_GITHUB_NUMBER', '', 'Number of the Issue or PR that started the ticket; empty when the ticket did not start on GitHub'),
        ('DSH_SUPERAGENT_GITHUB_PR_HEAD', '', 'Head ref refs/pull/<number>/head of the PR that started the ticket; empty when it was not a PR'),
        ('DSH_SUPERAGENT_FEISHU_BOT', deployment['feishu_bot'], 'The Feishu bot; lark-cli acts as it'),
        ('DSH_SUPERAGENT_FEISHU_CHAT', '', 'chat_id of the Feishu chat that started the ticket; empty when the ticket did not start on Feishu'),
    ]


def environment_section(deployment: dict) -> str:
    lines = '\n'.join(f'- {name} = {json.dumps(value, ensure_ascii=False)}: {description}.'
                      for name, value, description in environment_variables(deployment))
    return f'{ENVIRONMENT_HEADING}\n{lines}'


def policy_version(settings: dict) -> str:
    return key('policy', json.dumps({'rules': settings['rules'], 'mode': settings['mode']}, ensure_ascii=False, separators=(',', ':')))


def policy_section(settings: dict) -> str:
    def line(text: str) -> str:
        return re.sub(r'\s*\n\s*', ' ', text)
    rules = [f"- {line(rule['name'])} ({rule['id']}): {line(rule['instruction'])}" for rule in settings['rules'] if rule['enabled']]
    head = POLICY_HEAD.format(version=policy_version(settings), mode=settings['mode'])
    if not rules:
        return f'{head} No rule is enabled: a proactive candidate can only be ignored.'
    return f'{head} The enabled rules, by name and id:\n' + '\n'.join(rules)


def prompt_parts(deployment: dict, settings: dict) -> list[tuple[str, str]]:
    """promptParts(config, 'orchestrator', environment, policy) with the built-in prompt configuration (revision 0)."""
    parts = [
        ('runtime/shared', prompt_file('shared')),
        ('runtime/orchestrator', prompt_file('orchestrator')),
        *[(f'team/{name}', f'## {heading}\n{prompt_file(f"team/{name}")}') for name, heading in TEAM_HEADINGS.items()],
        ('runtime/subscriptions', f"## subscriptions\n{prompt_file('subscriptions')}"),
        ('runtime/replies', f"## replies\n{prompt_file('replies')}"),
        ('runtime/dispatch', f"## dispatch\n{prompt_file('dispatch')}"),
        ('runtime/admission', prompt_file('admission')),
        ('runtime/handoff', f"{prompt_file('handoff')}\n{prompt_file('handoff-orchestrator')}"),
        ('runtime/environment', environment_section(deployment)),
        ('runtime/policy', policy_section(settings)),
    ]
    return [(name, text) for name, text in parts if text]


def system_prompt(deployment: dict, settings: dict, revision: int = 0) -> str:
    """The main Session's dsh-superagent section: revision header, composed prompt, and no mandate (the master has no ticket)."""
    composed = '\n\n'.join(text for _, text in prompt_parts(deployment, settings))
    return f'[dsh-superagent prompt revision {revision}]\n{composed}\n'
