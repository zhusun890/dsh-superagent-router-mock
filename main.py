"""Replay a Feishu group-chat scenario against a model acting as the dsh-superagent Router (main Session).

Examples:
  uv run main.py --list-scenarios
  uv run main.py --dry-run --scenario preference-storage
  DEEPSEEK_API_KEY=... uv run main.py --model <model id> --scenario theme-followup --runs 5
  uv run main.py --model <model id> --scenario theme-followup --timing together
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import textwrap
from datetime import datetime
from pathlib import Path

from router_mock.engine import Engine
from router_mock.prompt import system_prompt
from router_mock.scenario import SCENARIOS, TIMINGS, Scenario, run
from router_mock.session import RouterSession, ScriptedSession, dumps
from router_mock.tools import MainTools, openai_tools


def parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--scenario', default='theme-followup', help='a name under scenarios/ or a path to a scenario JSON file')
    parser.add_argument('--list-scenarios', action='store_true')
    parser.add_argument('--model', default=os.environ.get('ROUTER_MODEL'), help='model id (or ROUTER_MODEL)')
    parser.add_argument('--base-url', default=os.environ.get('ROUTER_BASE_URL', 'https://api.deepseek.com'),
                        help='OpenAI-compatible endpoint (or ROUTER_BASE_URL); default https://api.deepseek.com')
    parser.add_argument('--api-key-env', default='DEEPSEEK_API_KEY', help='environment variable holding the API key')
    parser.add_argument('--timing', choices=TIMINGS, help='override for every message after the first: it reaches the Router after '
                        'the previous turn ended (after-turn), after that turn\'s first tool round (mid-turn), or together with the '
                        'previous message before the Router starts (together)')
    parser.add_argument('--reply', action='store_true', help='every message after the first is a Feishu reply to the first')
    parser.add_argument('--no-mention', action='store_true', help='messages after the first do not @ the bot')
    parser.add_argument('--chat-mode', choices=('mention', 'all'), help='the group\'s listen setting: @ only, or all messages')
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--temperature', type=float)
    parser.add_argument('--extra-body', type=json.loads, help='JSON merged into the request body, e.g. \'{"thinking": {"type": "enabled"}}\'')
    parser.add_argument('--max-steps', type=int, default=12, help='model requests per turn at most')
    parser.add_argument('--dry-run', action='store_true', help='no API: a scripted Router accepts every candidate')
    parser.add_argument('--show-prompt', action='store_true', help='print the system prompt and tool names, then exit')
    parser.add_argument('--quiet', action='store_true', help='print only the summary of each run')
    parser.add_argument('--out', default='runs', help='directory for JSON logs')
    return parser.parse_args()


def printer(quiet: bool):
    def echo(kind: str, value: object) -> None:
        if quiet:
            return
        if kind == 'arrive':
            print(f"\n=== message arrives ({value['intake']}): {value['text']}\n    thread {value['thread']}")
        elif kind == 'deliver':
            print(f"\n--> to Router ({value.get('kind')}):\n{textwrap.indent(json.dumps(value, ensure_ascii=False, indent=2), '    ')}")
        elif kind == 'reasoning':
            print(f'\n[reasoning]\n{textwrap.indent(str(value).strip(), "    ")}')
        elif kind == 'assistant':
            print(f'\n[Router says]\n{textwrap.indent(str(value).strip(), "    ")}')
        elif kind == 'tool_call':
            try:
                arguments = json.dumps(json.loads(value['arguments']), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                arguments = value['arguments']
            print(f"\n[tool call] {value['name']}\n{textwrap.indent(arguments, '    ')}")
        elif kind == 'tool_result':
            print(f"[tool result]\n{textwrap.indent(json.dumps(value, ensure_ascii=False, indent=2), '    ')}")
    return echo


def report(index: int, result: dict, usage: dict) -> str:
    lines = [f'---- run {index}: ' + ', '.join(f"{item['message']} ({item['sender']}) {item['fate']}" for item in result['messages'])]
    lines += [f"     {ticket['name']}: {ticket['title']} ({ticket['worktree_name']})" for ticket in result['tickets']]
    lines += [f"     ticket_note to {note['ticket']}: {note['text']}" for note in result['ticket_notes']]
    lines += [f"     routing note on {note['on']} {note['ticket']}: {note['text']}" for note in result['routing_notes']]
    if result['notes']:
        lines.append(f"     notes: {'; '.join(result['notes'])}")
    lines.append(f'     usage: {dumps(usage)}')
    return '\n'.join(lines)


def main() -> int:
    args = parse()
    if args.list_scenarios:
        for path in sorted(SCENARIOS.glob('*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            print(f"{path.stem}: {data.get('description', '')}")
        return 0
    scenario = Scenario.load(args.scenario)
    scenario.override(args.timing, args.reply, args.no_mention, args.chat_mode)
    config = scenario.settings()
    system = system_prompt(scenario.deployment(), config)
    if args.show_prompt:
        print(system)
        print('tools:', ', '.join(tool['function']['name'] for tool in openai_tools()))
        return 0
    client = None
    if not args.dry_run:
        if not args.model:
            print('Set --model or ROUTER_MODEL (or use --dry-run).', file=sys.stderr)
            return 2
        api_key = os.environ.get(args.api_key_env) or os.environ.get('OPENAI_API_KEY')
        if not api_key:
            print(f'Set {args.api_key_env} (or OPENAI_API_KEY) to your API key.', file=sys.stderr)
            return 2
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=args.base_url)

    out = Path(args.out) / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{scenario.name}"
    out.mkdir(parents=True, exist_ok=True)
    (out / 'system_prompt.txt').write_text(system, encoding='utf-8')
    (out / 'tools.json').write_text(json.dumps(openai_tools(), ensure_ascii=False, indent=2), encoding='utf-8')
    labels = collections.Counter()
    for index in range(1, args.runs + 1):
        print(f'\n################ {scenario.name}: run {index}/{args.runs} ################')
        scenario = Scenario.load(args.scenario)
        scenario.override(args.timing, args.reply, args.no_mention, args.chat_mode)
        engine = Engine(config)
        tools = MainTools(engine)
        echo = printer(args.quiet)
        if args.dry_run:
            session = ScriptedSession(system, tools, echo)
        else:
            session = RouterSession(client, args.model, system, tools, extra_body=args.extra_body, temperature=args.temperature,
                                    max_steps=args.max_steps, echo=echo)
        result = run(scenario, engine, session, echo)
        labels[result['label']] += 1
        log = {'scenario': args.scenario, 'overrides': {'timing': args.timing, 'reply': args.reply, 'no_mention': args.no_mention,
                                                        'chat_mode': args.chat_mode},
               'messages_played': [vars(message) for message in scenario.messages],
               'model': args.model if not args.dry_run else 'scripted', 'base_url': args.base_url,
               'result': result, 'usage': session.usage, 'messages': session.messages,
               'decisions': [vars(decision) for decision in engine.decisions], 'replies': engine.replies}
        (out / f'run-{index}.json').write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding='utf-8')
        print('\n' + report(index, result, session.usage))
    print(f'\n==== summary over {args.runs} run(s) of {scenario.name}:')
    for label, count in labels.most_common():
        print(f'     {count} × {label}')
    print(f'logs: {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
