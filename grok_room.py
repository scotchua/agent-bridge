"""Run via Grok Bot's explicitly approved local computer tool.

The helper only reads and writes the private Agent Room queue. It never calls
an xAI API, starts a shell, installs a routine, or enables unattended work.
"""
import argparse
import json
from pathlib import Path
import sys

for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from agent_bridge.chat.grok import receive, respond  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    nxt = sub.add_parser('next')
    nxt.add_argument('--wait', type=int, default=45, help='Seconds to wait (0 = check once)')
    nxt.add_argument('--queue-dir', '--state-dir', dest='state_dir', type=Path, default=Path.home() / '.agent-bridge' / 'grok')
    reply = sub.add_parser('reply')
    reply.add_argument('job_id')
    reply.add_argument('--queue-dir', '--state-dir', dest='state_dir', type=Path, default=Path.home() / '.agent-bridge' / 'grok')
    args = parser.parse_args()
    root = args.state_dir.resolve()
    if args.command == 'next':
        print(json.dumps(receive(root, args.wait) or {'status': 'idle', 'note': 'No addressed request yet'}, ensure_ascii=False))
    else:
        text = sys.stdin.read(100002)
        respond(root, args.job_id, text)
        print(json.dumps({'status': 'reply_saved'}))


if __name__ == '__main__':
    main()
