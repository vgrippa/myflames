"""Workspace route tables shared by ui.py and the feature API modules.

ui.py needs every route's path at import time (dispatch and the write-path
allowlist that guards server writes), but the handler modules pull in heavy
dependencies. The tuples live here so ui.py imports only this module at
startup; each feature module imports its own tuples from here, so there is
one source of truth. test_startup_imports.py checks both sides agree and
that the write-path classification matches the base release exactly.
"""

#: Schema diagram (schema_diagram_api.py).
DIAGRAM_READ_PATH = '/api/schema-diagram/read'
DIAGRAM_PATHS = (DIAGRAM_READ_PATH, '/api/schema-diagram/import', '/api/schema-diagram/export')
#: Schema versions (schema_versions_api.py).
SCHEMA_VERSION_PATHS = ('/api/schema-versions/list', '/api/schema-versions/get', '/api/schema-versions/save',
                        '/api/schema-versions/update', '/api/schema-versions/delete', '/api/schema-versions/export',
                        '/api/schema-versions/import', '/api/schema-versions/compare', '/api/schema-versions/sync',
                        '/api/schema-versions/lint', '/api/schema-versions/export-sql')
#: A snapshot import can be larger than the workspace's usual request limit.
SCHEMA_VERSION_LARGE_PATHS = ('/api/schema-versions/import',)
#: Compare servers and config snapshots (compare_api.py).
COMPARE_PATHS = ('/api/config/snapshots/list', '/api/config/snapshots/save', '/api/config/snapshots/get',
                 '/api/config/snapshots/delete', '/api/config/compare', '/api/accounts/compare',
                 '/api/accounts/export')
#: Baselines (baselines.py).
BASELINE_PATHS = ('/api/baselines/list', '/api/baselines/save', '/api/baselines/get', '/api/baselines/delete')
#: Privileges and Users and roles (accounts_api.py).
ACCOUNT_READ_PATHS = ('/api/live/privileges', '/api/accounts/list', '/api/accounts/read', '/api/accounts/plan',
                      '/api/accounts/scope-objects')
ACCOUNT_WRITE_PATHS = ('/api/accounts/apply',)
#: Import CSV (csv_import_api.py).
CSV_IMPORT_PATHS = ('/api/import/csv/choose', '/api/import/csv/open-path', '/api/import/csv/inspect',
                    '/api/import/csv/plan', '/api/import/csv/apply', '/api/import/csv/progress',
                    '/api/import/csv/stop')
CSV_IMPORT_WRITE_PATHS = ('/api/import/csv/apply',)
#: Slow log files (slowlog_api.py).
SLOW_LOG_PATHS = ('/api/slow-log/choose', '/api/slow-log/open-path', '/api/slow-log/read',
                  '/api/slow-log/progress', '/api/slow-log/stop')

#: (module, attribute, tuple) per feature route table: the handler module
#: must publish the same tuple and answer each of its paths through its
#: response() entry point (checked by test_startup_imports.py).
FEATURE_ROUTES = (
    ('schema_diagram_api', 'PATHS', DIAGRAM_PATHS),
    ('schema_diagram_api', 'READ_PATH', DIAGRAM_READ_PATH),
    ('schema_versions_api', 'PATHS', SCHEMA_VERSION_PATHS),
    ('schema_versions_api', 'LARGE_PATHS', SCHEMA_VERSION_LARGE_PATHS),
    ('compare_api', 'PATHS', COMPARE_PATHS),
    ('baselines', 'PATHS', BASELINE_PATHS),
    ('accounts_api', 'READ_PATHS', ACCOUNT_READ_PATHS),
    ('accounts_api', 'WRITE_PATHS', ACCOUNT_WRITE_PATHS),
    ('csv_import_api', 'PATHS', CSV_IMPORT_PATHS),
    ('csv_import_api', 'WRITE_PATHS', CSV_IMPORT_WRITE_PATHS),
    ('slowlog_api', 'PATHS', SLOW_LOG_PATHS),
)
