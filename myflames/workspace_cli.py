"""CLI access to the same query capture and exploration used by the workspace."""
import argparse
import getpass
import json
import math
import sys

from . import __version__
from .live import LiveJob


def _bounded_integer(lower, upper):
    def parse(value):
        try:
            number = int(value)
        except (ValueError, TypeError):
            raise argparse.ArgumentTypeError('Choose an integer from %d to %d.' % (lower, upper))
        if not lower <= number <= upper:
            raise argparse.ArgumentTypeError('Choose an integer from %d to %d.' % (lower, upper))
        return number
    return parse


def _parameters(value):
    def finite_number(text):
        number = float(text)
        if not math.isfinite(number):
            raise ValueError('Numbers must be finite.')
        return number
    try:
        result = json.loads(value, parse_constant=finite_number, parse_float=finite_number)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('Pass --parameters as a valid JSON object with finite numbers.')
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError('Pass --parameters as a JSON object, for example \'{"id": 1}\'.')
    return result


def _fail(message, password=None):
    text = str(message)
    if password:
        text = text.replace(password, '[redacted]')
    sys.stderr.write('myflames: %s\n' % ' '.join(text.splitlines()))
    raise SystemExit(2)


def cmd_capture(argv):
    """Capture query evidence as JSON, with explicit execution opt-in."""
    from .cli import _write_output
    parser = argparse.ArgumentParser(
        prog='myflames capture', add_help=False,
        description='Capture a query plan, server context, and optional execution evidence as JSON.',
        epilog='Explain estimates the plan. --mode analyze executes the SELECT; --repeat repeats execution. Passwords are not saved in captures.',
    )
    parser.add_argument('--help', action='help', help='Show this help message and exit.')
    parser.add_argument('-e', '--execute', required=True, metavar='SQL', help='Capture one SELECT, optionally using :named parameters.')
    parser.add_argument('-h', '--host', default='127.0.0.1', help='Connect to this server (default: 127.0.0.1).')
    parser.add_argument('-P', '--port', type=_bounded_integer(1, 65535), default=3306, help='Connect to this port (default: 3306).')
    parser.add_argument('-u', '--user', help='Connect with this username.')
    parser.add_argument('-p', '--password', nargs='?', const='__PROMPT__', metavar='PASSWORD', help="Use '-p' alone to prompt for a password, or '-psecret' inline.")
    parser.add_argument('-D', '--database', help='Use this default database.')
    parser.add_argument('--ssl-mode', choices=('DISABLED', 'PREFERRED', 'REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY'), help='Choose the connection TLS mode.')
    parser.add_argument('--ssl-ca', metavar='PATH', help='Verify the server with this CA bundle.')
    parser.add_argument('--mode', choices=('explain', 'analyze'), default='explain', help='Estimate the plan, or execute it with analyze (default: explain).')
    parser.add_argument('--repeat', type=_bounded_integer(1, 10), default=1, metavar='N', help='Measure 1–10 executions in analyze mode (default: 1).')
    parser.add_argument('--timeout', type=_bounded_integer(1, 300), default=30, metavar='SECONDS', help='Cancel each run after 1–300 seconds (default: 30).')
    parser.add_argument('--trace', action='store_true', help='Collect the optimizer trace when the server permits it.')
    parser.add_argument('--parameters', type=_parameters, default={}, metavar='JSON', help='Bind :named values from a JSON object.')
    parser.add_argument('--conditions', default='', metavar='NOTE', help='Record execution conditions alongside the measurements.')
    parser.add_argument('-o', '--output', metavar='PATH', help='Write capture JSON to this file instead of stdout.')
    args = parser.parse_args(argv)
    password = args.password
    job = None
    try:
        if password == '__PROMPT__':
            password = getpass.getpass('Enter password: ')
        payload = {
            'connection': {key: value for key, value in {
                'host': args.host, 'port': args.port, 'user': args.user,
                'password': password, 'database': args.database,
                'ssl_mode': args.ssl_mode, 'ssl_ca': args.ssl_ca,
            }.items() if value is not None},
            'query': args.execute, 'mode': args.mode, 'repeat': args.repeat,
            'timeout': args.timeout, 'trace': args.trace, 'parameters': args.parameters,
            'conditions': args.conditions,
        }
        job = LiveJob(payload)
        job.run()
        if job.state != 'complete' or not isinstance(job.result, dict):
            _fail(job.error or 'Capture did not complete.', password)
        result = dict(job.result)
        result.setdefault('schema_version', 'capture-1.0')
        result.setdefault('myflames_version', __version__)
        _write_output(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + '\n', args.output)
    except KeyboardInterrupt:
        if job is not None:
            job.cancel()
        _fail('Capture cancelled.', password)
    except EOFError:
        _fail('Password prompt cancelled.', password)
    except (ValueError, TypeError, OSError) as exc:
        _fail(exc, password)


def cmd_explore(argv):
    """Export an interactive plan projection with stable operator identity."""
    from .cli import _read_explain_input, _parse_explain_input, _write_output
    from .exploration import VIEWS, METRICS, render_exploration
    from .parser import flatten_nodes
    parser = argparse.ArgumentParser(
        prog='myflames explore',
        description='Export an interactive plan with operator selection, branch focus, and a metric list.',
    )
    parser.add_argument('input', metavar='PLAN', help="Read EXPLAIN JSON from a file, or '-' for stdin.")
    parser.add_argument('--type', choices=VIEWS, default='workbench', help='Choose the chart (default: workbench / Visual Explain; diagram is an alias).')
    parser.add_argument('--metric', choices=tuple(METRICS), default='self_time', help='Choose the metric list; native chart encodings stay unchanged.')
    parser.add_argument('--focus', default='', metavar='NODE_ID', help='Show only this operator and its descendants.')
    parser.add_argument('--collapse', action='append', default=[], metavar='NODE_ID', help='Hide descendants of this operator; repeat for multiple branches.')
    parser.add_argument('--selected', default='', metavar='NODE_ID', help='Select this canonical operator ID when the report opens.')
    parser.add_argument('-o', '--output', metavar='PATH', help='Write interactive HTML to this file instead of stdout.')
    args = parser.parse_args(argv)
    root = _parse_explain_input(_read_explain_input(args.input))
    try:
        if args.selected and args.selected not in {node['node_id'] for node in flatten_nodes(root)}:
            raise ValueError('Unknown selected operator. Use a node_id from the plan sidecar.')
        output = render_exploration(root, args.type, selected_id=args.selected,
                                    focus_id=args.focus, collapsed_ids=args.collapse,
                                    metric=args.metric)
        _write_output(output, args.output)
    except (ValueError, TypeError, OSError) as exc:
        _fail(exc)
