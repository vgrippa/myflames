"""Validated logical dump options. No arbitrary command or script arguments.

Source contracts: mysql-shell modules/util/dump/{dump_options,
ddl_dumper_options,dump_schemas_options}.{cc,h}; storage/compression/{gz_file,
zstd_file}.cc. Compression levels first appear in Shell 8.4.0 (c687dc69d).
SQL flags: mysql-server/client/mysqldump.cc and mariadb-server/client/mysqldump.cc.
Compression of the connection never means compression or encryption of files.
"""
import re

from .server_info import tool_version_tuple

ENCRYPTION_NOTE = ('These logical dump tools do not offer encryption of local backup files. '
                   'Connection TLS protects transport only. Use an encrypted destination volume '
                   'or encrypt the completed backup with a separate trusted tool.')
ENCRYPTION_SUMMARY = 'Backup files are not encrypted.'
#: The form's sections, in order. Each option names its group; a boolean also
#: names the row its checkbox sits in, so related checkboxes share one label.
GROUPS = (('performance', 'Performance'), ('contents', 'Contents'), ('output', 'SQL output'),
          ('compression', 'Compression'))
_BOOLS = ('triggers', 'routines', 'events', 'hex_blob', 'add_drop_table', 'network_compression', 'chunking')
_SHELL_NAMES = {'bytes_per_chunk': 'bytesPerChunk', 'max_rate': 'maxRate'}


def capability_catalog(tool_name, table_scope=False, tool_version=None):
    """Describe the bounded options exposed by myflames for this dump tool.

    ``group`` places an option in one of GROUPS; a boolean's ``row`` is the
    label of the row its checkbox shares with related ones."""
    if tool_name not in ('mysqlsh', 'mysqldump', 'mariadb-dump'):
        raise ValueError('This tool does not support logical dumps in myflames.')
    options = []

    def add(key, label, group, kind='boolean', default=True, **extra):
        options.append(dict(key=key, label=label, group=group, type=kind, default=default, **extra))

    if tool_name == 'mysqlsh':
        add('chunking', 'Split table data into chunks', 'performance', row='Chunking')
        add('bytes_per_chunk', 'Chunk size', 'performance', 'text', '', placeholder='64M',
            hint='Bytes, optionally k, M or G. Blank uses 64M; at least 128k.')
        add('max_rate', 'Rate limit', 'performance', 'text', '', placeholder='Unlimited', unit='per thread',
            hint='Bytes per second, optionally k, M or G. Blank or 0 means unlimited.')
    add('triggers', 'Triggers', 'contents', row='Include')
    if tool_name != 'mysqlsh' or not table_scope:
        add('routines', 'Stored routines', 'contents', row='Include')
        add('events', 'Events', 'contents', row='Include')
    if tool_name != 'mysqlsh':
        add('hex_blob', 'Binary values as hexadecimal', 'output', row='Write')
        add('add_drop_table', 'DROP TABLE before each CREATE TABLE', 'output', row='Write')
    if tool_name == 'mysqlsh':
        add('compression', 'Method', 'compression', 'choice', 'default',
            choices=[['default', 'Tool default (Zstandard)'], ['none', 'None'],
                     ['gzip', 'Gzip'], ['zstd', 'Zstandard']])
        if tool_version is None or tool_version_tuple(tool_version) >= (8, 4, 0):
            add('compression_level', 'Level', 'compression', 'integer', None, min=0, max=22,
                hint='Gzip 0 to 9, Zstandard 0 to 22; needs MySQL Shell 8.4 or newer. Blank uses the tool default.')
    else:
        add('network_compression', 'Compress traffic from the server', 'compression', default=False,
            row='Connection', hint='Only the network traffic; the backup file stays uncompressed.')
    return {'options': options, 'groups': [list(g) for g in GROUPS],
            'encryption': {'supported': False, 'summary': ENCRYPTION_SUMMARY, 'message': ENCRYPTION_NOTE}}


def _size(value, minimum, label):
    if not isinstance(value, str) or len(value) > 24:
        raise ValueError('%s must be a byte count, optionally followed by k, M or G.' % label)
    if not value:
        return ''
    match = re.fullmatch(r'(0|[1-9][0-9]{0,15})([kMG]?)', value)
    if not match:
        raise ValueError('%s must be a byte count, optionally followed by k, M or G.' % label)
    amount = int(match.group(1)) * {'': 1, 'k': 1000, 'M': 1000000, 'G': 1000000000}[match.group(2)]
    if not minimum <= amount <= 9223372036854775807:
        raise ValueError('%s is outside the supported range.' % label)
    return value


def normalize(options, tool, table_scope=False):
    """Return validated explicit options, retaining omitted tool defaults."""
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ValueError('Dump options must be an object.')
    name = tool['name']
    descriptors = capability_catalog(name, table_scope)['options']
    allowed = {item['key'] for item in descriptors} | {'encryption'}
    if any(not isinstance(key, str) or key not in allowed for key in options):
        raise ValueError('Unsupported dump option for the selected tool or scope.')
    result = {}
    for key, value in options.items():
        if key == 'encryption':
            if value != 'none':
                raise ValueError(ENCRYPTION_NOTE)
            continue
        if key in _BOOLS:
            if not isinstance(value, bool):
                raise ValueError('%s must be true or false.' % key)
        elif key == 'compression':
            if not isinstance(value, str) or value not in ('default', 'none', 'gzip', 'zstd'):
                raise ValueError('Compression must be default, none, gzip or zstd.')
        elif key == 'compression_level':
            if value is None:
                continue
            method = options.get('compression', 'default')
            if method not in ('gzip', 'zstd'):
                raise ValueError('Choose gzip or zstd before setting a compression level.')
            maximum = 9 if method == 'gzip' else 22
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError('Compression level must be 0 to %s for %s.' % (maximum, method))
            if tool_version_tuple(tool.get('version') or '') < (8, 4, 0):
                raise ValueError('Compression levels require MySQL Shell 8.4 or newer.')
        elif key == 'bytes_per_chunk':
            value = _size(value, 128000, 'Target chunk size')
            if value and options.get('chunking') is False:
                raise ValueError('Target chunk size requires chunking.')
        elif key == 'max_rate':
            value = _size(value, 0, 'Maximum rate')
        result[key] = value
    return result


def shell_options(options):
    result = {}
    for key, value in options.items():
        if key in ('triggers', 'routines', 'events', 'chunking'):
            result[key] = value
        elif key in _SHELL_NAMES and value:
            result[_SHELL_NAMES[key]] = value
    method = options.get('compression', 'default')
    if method != 'default':
        result['compression'] = method
        if 'compression_level' in options:
            result['compression'] += ';level=%s' % options['compression_level']
    return result


def sql_flags(options):
    result = []
    for key in ('routines', 'events', 'triggers', 'hex_blob', 'add_drop_table'):
        flag = key.replace('_', '-')
        result.append('--' + ('' if options.get(key, True) else 'skip-') + flag)
    if options.get('network_compression', False):
        result.append('--compress')
    return result
