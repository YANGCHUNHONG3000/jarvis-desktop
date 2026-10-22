"""Open Jarvis's selected-window computer panel; optional read-only CLI."""
import argparse
import json
from agent_service.computer import ComputerSession


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--list-windows', action='store_true')
    args = parser.parse_args(argv)
    if args.list_windows:
        session = ComputerSession.create()
        print(json.dumps({'session': str(session.root), 'windows': session.windows()}, ensure_ascii=False))
        return 0
    from agent_service.computer_panel import main as show
    show()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
