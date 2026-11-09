"""Workspace adapter for schema diagrams; imports and exports stay offline."""
from . import schema_diagram
from .connector import ConnectorError, MySQLConnection
from .live import connection_options

READ_PATH = '/api/schema-diagram/read'
PATHS = (READ_PATH, '/api/schema-diagram/import', '/api/schema-diagram/export')


def response(path, payload, with_saved_password=None):
    if not isinstance(payload, dict):
        raise ValueError('Expected a diagram request object.')
    if path == '/api/schema-diagram/import':
        return schema_diagram.import_diagram(payload.get('format'), payload.get('text'))
    if path == '/api/schema-diagram/export':
        return schema_diagram.export_diagram(payload.get('format'), payload.get('diagram'))
    if path != READ_PATH:
        raise ValueError('Unknown schema diagram operation.')
    database = schema_diagram._text(payload.get('database'), 'database name', 64)
    schema_diagram.check_schema(database)
    connection = payload.get('connection')
    if not isinstance(connection, dict) or len(connection) > 40:
        raise ValueError('Invalid diagram connection.')
    for key, value in connection.items():
        if not isinstance(key, str) or len(key) > 64 or type(value) not in (str, int, bool, type(None)):
            raise ValueError('Invalid diagram connection field.')
        if isinstance(value, str):
            schema_diagram._text(value, 'connection field', 4096, empty=True)
        if type(value) is int and value.bit_length() > 64:
            raise ValueError('Invalid diagram connection number.')
    if with_saved_password is None:
        raise ValueError('Reading a database needs a running workspace.')
    try:
        filled = with_saved_password({'connection': connection})
        options = connection_options(filled.get('connection'))
        options['query_timeout'] = schema_diagram.READ_SECONDS
        with MySQLConnection(**options) as conn:
            return schema_diagram.read_diagram(conn, database)
    except ConnectorError:
        raise ValueError('Could not read schema metadata; check the connection and metadata privileges.') from None
