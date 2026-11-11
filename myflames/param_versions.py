"""Parameter version catalogs: compare two releases, trace one parameter.

Answers come from the generated catalog modules ``sysvar_history_mysql`` and
``sysvar_history_mariadb`` (scripts/gen_sysvar_history.py, committed to this
package), never from a running server. The catalog modules are imported with
plain imports inside the functions so PyInstaller sees them; a build without a
catalog raises CatalogError, and catalog_index reports that engine as
unavailable instead of failing. Every sentence is written here, in Python.
"""
import difflib
import re

from .server_info import PRODUCT_NAMES

CATALOG_SCHEMA = 'parameter-version-catalog-1.0'
COMPARE_SCHEMA = 'parameter-version-compare-1.0'
HISTORY_SCHEMA = 'parameter-history-1.0'
NAMES_SCHEMA = 'param-names-1.0'
ENGINES = ('mysql', 'mariadb')
FIELDS = ('scope', 'type', 'dynamic', 'default', 'default_note', 'min', 'max',
          'values', 'deprecated', 'replacement', 'alias_of', 'plugin')
KINDS = ('added', 'removed', 'deprecated', 'undeprecated', 'default', 'dynamic',
         'scope', 'type', 'values', 'range')
MAX_SUGGESTIONS = 8
MAX_NAME_LENGTH = 64
MAX_VERSION_LENGTH = 32
_FIELD_INDEX = dict((name, i) for i, name in enumerate(FIELDS))
#: A version text starts with or contains the numeric release ("8.4.6-6",
#: "11.4.5-MariaDB-log", a tag such as "mysql-8.0.30").
_VERSION_TEXT = re.compile(r'(\d+)\.(\d+)\.(\d+)')
_PERCONA_NOTE = ('Percona Server for MySQL uses the MySQL catalog; variables '
                 'that only Percona adds are not cataloged.')


class CatalogError(ValueError):
    """The generated catalog of one engine is missing or unreadable."""


class Catalog(object):
    """One generated catalog module: versions in numeric order, lineage and
    the run-length per-parameter history, indexed for direct answers."""

    def __init__(self, module):
        engine = getattr(module, 'ENGINE', None)
        versions = getattr(module, 'VERSIONS', None)
        fields = getattr(module, 'FIELDS', None)
        history = getattr(module, 'HISTORY', None)
        if engine not in ENGINES or not versions or tuple(fields or ()) != FIELDS \
                or not isinstance(history, dict):
            raise CatalogError('The %s parameter catalog does not have the expected format.'
                               % (engine or 'chosen engine'))
        self.engine = engine
        self.product = PRODUCT_NAMES[engine]
        self.generated = dict(getattr(module, 'GENERATED', None) or {})
        self.versions = []
        self.by_version = {}
        for i, row in enumerate(versions):
            try:
                version, tag, maturity, date, previous, method = row
            except (TypeError, ValueError):
                raise CatalogError('The %s parameter catalog does not have the expected format.'
                                   % engine) from None
            if version in self.by_version or previous is not None and not 0 <= previous < i:
                raise CatalogError('The %s parameter catalog does not have the expected format.'
                                   % engine)
            self.by_version[version] = i
            self.versions.append({'version': version, 'tag': tag, 'maturity': maturity,
                                  'date': date, 'previous': previous, 'method': method,
                                  'series': '.'.join(version.split('.')[:2])})
        # Names whose presence follows build options (innodb_numa_interleave):
        # their appearing or disappearing is never an added or removed change.
        self.build_only = dict(getattr(module, 'BUILD_ONLY', None) or {})
        self.skipped = []
        self.skipped_dates = {}
        for entry in getattr(module, 'SKIPPED', ()):
            # (version, reason) or (version, reason, tag date)
            if not isinstance(entry, tuple) or len(entry) not in (2, 3):
                raise CatalogError('The %s parameter catalog does not have the expected format.' % engine)
            self.skipped.append({'version': entry[0], 'reason': entry[1]})
            self.skipped_dates[entry[0]] = entry[2] if len(entry) > 2 else None
        # name -> ((first version, last version or None, sentence), ...): how
        # the server sets a default it computes at startup (reviewed source).
        self.default_rules = dict(getattr(module, 'DEFAULT_RULES', None) or {})
        self.history = history
        self.names = sorted(history)

    def index(self, version):
        return self.by_version.get(version)

    def row(self, name, index):
        """The raw facts tuple of *name* at VERSIONS[index], None when absent."""
        entries = self.history.get(name)
        if entries is None or index is None:
            return None
        facts = None
        for first, value in entries:
            if first > index:
                break
            facts = value
        return facts

    def facts(self, name, index):
        row = self.row(name, index)
        return facts_dict(row) if row is not None else None

    def chain(self, index):
        """VERSIONS[index] and its lineage back to the first cataloged release."""
        out = []
        while index is not None:
            out.append(index)
            index = self.versions[index]['previous']
        return out

    def since_index(self, name, start, kind, stop=None):
        """Walk the lineage back from VERSIONS[start] while the value the kind
        compares keeps its value there, never reaching VERSIONS[stop]; the
        index of the earliest such version."""
        key = _kind_key(kind, self.row(name, start))
        current = start
        while True:
            previous = self.versions[current]['previous']
            if previous is None or previous == stop:
                break
            if _kind_key(kind, self.row(name, previous)) != key:
                break
            current = previous
        return current

    def since(self, name, start, kind, stop=None):
        return self.versions[self.since_index(name, start, kind, stop)]['version']

    def unread_before(self, index):
        """Skipped releases (no record) that sit on the lineage step into
        VERSIONS[index]: a change seen there may have come with one of them."""
        previous = self.versions[index]['previous']
        if previous is None:
            return []
        here, before = self.versions[index], self.versions[previous]
        key, low = _version_key(here['version']), _version_key(before['version'])
        out = []
        for entry in self.skipped:
            version = entry['version']
            series = '.'.join(version.split('.')[:2])
            if series == here['series'] and low < _version_key(version) < key:
                out.append(version)
            elif series == before['series'] != here['series'] and _version_key(version) > low:
                date = self.skipped_dates.get(version)
                if date and date <= here['date']:
                    out.append(version)
        return sorted(out, key=_version_key)

    def when(self, index, subject=False):
        """VERSIONS[index] as a sentence names it: "10.6.7", or "10.6.6 or
        10.6.7 (10.6.6 could not be read)" when the step into it skips an
        unread release; as the subject of a sentence, "10.6.7 (or 10.6.6,
        which could not be read)"."""
        version = self.versions[index]['version']
        unread = self.unread_before(index)
        if not unread:
            return version
        if subject:
            return '%s (or %s, which could not be read)' % (
                version, ' or '.join(unread) if len(unread) < 3 else ', '.join(unread[:-1]) + ' or ' + unread[-1])
        names = unread + [version]
        listed = '%s or %s' % (', '.join(names[:-1]), names[-1])
        return '%s%s (%s could not be read)' % (
            '' if len(names) == 2 else 'one of ', listed,
            ' and '.join([', '.join(unread[:-1]), unread[-1]] if len(unread) > 1 else unread))

    def on_line(self, index, series):
        """'8.0.34 on the 8.0 line', or '8.4.1 or 8.4.3 on the 8.4 line; 8.4.1
        could not be read' when the step into it skips unread releases."""
        names = self.unread_before(index) + [self.versions[index]['version']]
        listed = names[0] if len(names) == 1 else '%s or %s' % (', '.join(names[:-1]), names[-1])
        text = '%s on the %s line' % (listed, series)
        if len(names) > 1:
            text += '; %s could not be read' % ' and '.join(names[:-1])
        return text

    def rule(self, name, index):
        """The reviewed sentence for how the server sets this default at
        VERSIONS[index], or None."""
        key = _version_key(self.versions[index]['version'])
        for first, last, sentence in self.default_rules.get(name, ()):
            if _version_key(first) <= key and (last is None or key <= _version_key(last)):
                return sentence
        return None

    def readded(self, name, first, last):
        """(removed in, added again in) when To's lineage, after it meets
        From's, lost the parameter and got it back; else None. Walks back from
        To: the earliest present release after a gap is where it returned, the
        earliest absent release of that gap is where it was removed."""
        reached = set(self.chain(first))
        returned = removed = None
        for index in self.chain(last):
            present = self.row(name, index) is not None
            if present and removed is not None:
                return self.when(removed), self.when(returned)
            if index in reached:
                return None
            if present:
                returned = index
            elif returned is None:
                return None  # absent at To itself: not a return
            else:
                removed = index
        return None

    def from_line_twin(self, name, first, last, kind):
        """A release after VERSIONS[first] in From's own series (not on To's
        lineage) that already has To's value of this kind: 8.0.34 deprecated
        binlog_format on the 8.0 line while 8.4 got it through 8.1.0."""
        series = self.versions[first]['series']
        if series == self.versions[last]['series']:
            return None
        wanted = _kind_key(kind, self.row(name, last))
        chain = self.chain(last)
        on_to_line = set(chain)
        # The last release of From's series on To's line: when it already has
        # To's value, the change came before the lines split and since names it.
        split = next((i for i in chain if self.versions[i]['series'] == series), None)
        if split is not None and _kind_key(kind, self.row(name, split)) == wanted:
            return None
        for index in range(first + 1, len(self.versions)):
            if self.versions[index]['series'] != series:
                break
            if index not in on_to_line and _kind_key(kind, self.row(name, index)) == wanted:
                return self.versions[index]['version']
        return None

    def where(self, name, first, last, kind):
        """Which line made the difference of one kind between VERSIONS[first]
        and VERSIONS[last]: ('to', version) when To's own line changed it after
        the two lines met, ('from', version) when From's line did (a downgrade,
        or a patch of an older series that a newer series does not have), and
        ('both', None) when both lines changed it since they met. The second
        item is a VERSIONS index."""
        reached = set(self.chain(first))
        meet = next((i for i in self.chain(last) if i in reached), None)
        if meet is not None:
            at_meet = _kind_key(kind, self.row(name, meet))
            if at_meet == _kind_key(kind, self.row(name, first)):
                return 'to', self.since_index(name, last, kind, meet)
            if at_meet == _kind_key(kind, self.row(name, last)):
                return 'from', self.since_index(name, first, kind, meet)
            # Both lines moved since they met, but one of them passed through
            # the other side's value: that line alone made the difference.
            for line, start, other in (('to', last, first), ('from', first, last)):
                at = self.since_index(name, start, kind, meet)
                previous = self.versions[at]['previous']
                if previous is not None and _kind_key(kind, self.row(name, previous)) == \
                        _kind_key(kind, self.row(name, other)):
                    return line, at
        return 'both', None


def facts_dict(row):
    """A facts tuple in FIELDS order as the JSON-ready dict of the contracts."""
    facts = dict(zip(FIELDS, row))
    if facts['values'] is not None:
        facts['values'] = list(facts['values'])
    if facts['deprecated'] is not None:
        facts['deprecated'] = sorted(facts['deprecated'])
    return facts


def _load_module(engine):
    if engine == 'mysql':
        try:
            from . import sysvar_history_mysql as module
        except ImportError:
            raise CatalogError('The MySQL parameter version catalog is not available in this build.') from None
    else:
        try:
            from . import sysvar_history_mariadb as module
        except ImportError:
            raise CatalogError('The MariaDB parameter version catalog is not available in this build.') from None
    return module


def load(engine, module=None):
    """The Catalog of one engine; tests pass the fixture *module* directly."""
    if engine not in ENGINES:
        raise ValueError('Choose engine mysql or mariadb.')
    catalog = Catalog(module if module is not None else _load_module(engine))
    if catalog.engine != engine:
        raise CatalogError('The %s parameter catalog does not have the expected format.' % engine)
    return catalog


def catalog_index():
    """parameter-version-catalog-1.0: every engine, available or not."""
    engines = []
    for engine in ENGINES:
        try:
            catalog = load(engine)
        except CatalogError as exc:
            engines.append({'engine': engine, 'product': PRODUCT_NAMES[engine], 'available': False,
                            'versions': [], 'skipped': [], 'names': [], 'generated': {},
                            'notes': [str(exc)]})
            continue
        notes = []
        if engine == 'mysql':
            notes.append(_PERCONA_NOTE)
        if catalog.skipped:
            notes.append('%d tagged release%s could not be started; see skipped.'
                         % (len(catalog.skipped), '' if len(catalog.skipped) == 1 else 's'))
        engines.append({'engine': engine, 'product': catalog.product, 'available': True,
                        'versions': [dict((k, v[k]) for k in ('version', 'series', 'maturity', 'date'))
                                     for v in catalog.versions],
                        'skipped': catalog.skipped, 'names': catalog.names,
                        'generated': catalog.generated, 'notes': notes})
    return {'schema': CATALOG_SCHEMA, 'engines': engines}


def resolve_version(engine, text):
    """The cataloged release inside *text* ("8.4.6-6" -> "8.4.6"), else None."""
    catalog = load(engine)
    if not isinstance(text, str):
        return None
    match = _VERSION_TEXT.search(text.strip())
    if match is None:
        return None
    version = match.group(0)
    return version if version in catalog.by_version else None


def names(engine, version):
    """param-names-1.0: the system variables of one release with scope, type,
    dynamic and deprecated, for the editor's completion and warnings.

    An uncataloged patch or a Percona build ("8.0.36-28") resolves to the
    nearest earlier cataloged release of the same major.minor series with
    exact false; a series the catalog does not have (or one whose releases
    are all later) answers an empty names list with release null. ``type``
    is null where the catalogs do not record it (MySQL releases before 9.0),
    and ``dynamic`` is null where they do not record it, never false for
    "unknown"."""
    catalog = load(engine)
    if not isinstance(version, str) or not version.strip() or len(version) > MAX_VERSION_LENGTH:
        raise ValueError('Choose a cataloged version of at most %d characters.' % MAX_VERSION_LENGTH)
    match = _VERSION_TEXT.search(version.strip())
    parsed = match.group(0) if match else None
    index = catalog.by_version.get(parsed) if parsed is not None else None
    exact = index is not None
    if parsed is not None and index is None:
        series = '.'.join(parsed.split('.')[:2])
        key = _version_key(parsed)
        earlier = [i for i, entry in enumerate(catalog.versions)
                   if entry['series'] == series and _version_key(entry['version']) < key]
        index = earlier[-1] if earlier else None
    if index is None:
        return {'schema_version': NAMES_SCHEMA, 'engine': catalog.engine, 'requested': version,
                'release': None, 'exact': False, 'names': []}
    found = []
    for name in catalog.names:
        row = catalog.row(name, index)
        if row is None:
            continue
        found.append({'name': name,
                      'scope': row[_FIELD_INDEX['scope']],
                      'type': row[_FIELD_INDEX['type']],
                      # None where the catalogs do not record it; the editor
                      # must not read "unknown" as "not dynamic".
                      'dynamic': row[_FIELD_INDEX['dynamic']],
                      'deprecated': bool(row[_FIELD_INDEX['deprecated']])})
    return {'schema_version': NAMES_SCHEMA, 'engine': catalog.engine, 'requested': version,
            'release': catalog.versions[index]['version'], 'exact': exact, 'names': found}


def _version_index(catalog, text):
    if not isinstance(text, str) or not text or len(text) > MAX_VERSION_LENGTH:
        raise ValueError('Choose a cataloged version of at most %d characters.' % MAX_VERSION_LENGTH)
    match = _VERSION_TEXT.search(text.strip())
    version = match.group(0) if match else None
    if version not in catalog.by_version:
        raise ValueError('%s is not a cataloged %s release; list them with: myflames config versions list.'
                         % (text, catalog.product))
    return catalog.by_version[version]


def _version_key(version):
    return tuple(int(part) for part in version.split('.'))


def _kind_key(kind, row):
    """The value *kind* compares, at one version; None (absent) is distinct
    from every value, so the lineage walk stops where the parameter is absent."""
    if kind in ('added', 'removed'):
        return row is not None
    if row is None:
        return None
    if kind in ('deprecated', 'undeprecated'):
        return bool(row[_FIELD_INDEX['deprecated']])
    if kind == 'default':
        note = row[_FIELD_INDEX['default_note']]
        if note in ('host', 'release', 'build', 'state'):
            return (None, note)  # the lab's sample is never compared
        return (row[_FIELD_INDEX['default']], note)
    if kind == 'range':
        return (row[_FIELD_INDEX['min']], row[_FIELD_INDEX['max']])
    return row[_FIELD_INDEX[kind]]


def _kinds(before, after):
    """The kinds of change between two raw facts rows, in KINDS order. A value
    that is unknown on either side is never a change of that kind."""
    if before is None and after is None:
        return (), False
    if before is None:
        # A parameter that arrives deprecated (an alias or a stopgap) says so.
        return (('added', 'deprecated') if after[_FIELD_INDEX['deprecated']] else ('added',)), False
    if after is None:
        return ('removed',), False
    kinds = []
    was = bool(before[_FIELD_INDEX['deprecated']])
    now = bool(after[_FIELD_INDEX['deprecated']])
    if now and not was:
        kinds.append('deprecated')
    elif was and not now:
        kinds.append('undeprecated')
    unjudged_default = False
    notes = (before[_FIELD_INDEX['default_note']], after[_FIELD_INDEX['default_note']])
    if notes == ('host', 'host') or {'release', 'build', 'state'} & set(notes):
        # Both values were sampled on one lab host, the value names the
        # release or build itself (version, build_id), or it follows the
        # build's installation layout (paths): a difference says nothing
        # about a default.
        pass
    elif before[_FIELD_INDEX['default']] != after[_FIELD_INDEX['default']] or notes[0] != notes[1]:
        if 'unknown' in notes:
            unjudged_default = True
        else:
            kinds.append('default')
    for kind in ('dynamic', 'scope', 'type'):
        first, second = before[_FIELD_INDEX[kind]], after[_FIELD_INDEX[kind]]
        if first != second and first is not None and second is not None:
            if kind == 'type' and 'boolean' in (first, second) and _on_off(before) and _on_off(after):
                # An ON/OFF variable declared again as an enum of ON and OFF.
                continue
            kinds.append(kind)
    first, second = _values(before), _values(after)
    if first != second and first is not None and second is not None:
        kinds.append('values')
    first = (before[_FIELD_INDEX['min']], before[_FIELD_INDEX['max']])
    second = (after[_FIELD_INDEX['min']], after[_FIELD_INDEX['max']])
    if first != second and None not in first and None not in second and not _rounded(first, second):
        kinds.append('range')
    return tuple(kinds), unjudged_default


def _values(row):
    """Allowed values as compared: a boolean takes OFF and ON."""
    values = row[_FIELD_INDEX['values']]
    if values is None and row[_FIELD_INDEX['type']] == 'boolean':
        return ('OFF', 'ON')
    return values


def _on_off(row):
    """An ON/OFF value whose allowed values, when known, are only ON and OFF."""
    values = row[_FIELD_INDEX['values']]
    return (row[_FIELD_INDEX['default']] in ('ON', 'OFF')
            and (values is None or set(v.upper() for v in values) <= {'ON', 'OFF'}))


def _rounded(first, second):
    """Only the maximum moved, by less than a block: MySQL 8.0.26 started
    reporting maxima rounded down to the variable's block size."""
    if first[0] != second[0]:
        return False
    try:
        a, b = int(first[1]), int(second[1])
    except (TypeError, ValueError):
        return False
    return min(a, b) >= 2 ** 31 and abs(a - b) < 65536


def _default_text(row):
    value = row[_FIELD_INDEX['default']]
    if value == '':
        return 'an empty string'
    if value is not None:
        return value
    note = row[_FIELD_INDEX['default_note']]
    if note == 'null':
        return 'NULL'
    if note == 'host':
        return 'depends on the host'
    if note == 'release':
        return 'the release itself'
    if note == 'build':
        return 'depends on the build'
    if note == 'state':
        return 'set by the server'
    return 'unknown'


def _range_parts(old, new):
    parts = []
    for field, label in (('min', 'minimum'), ('max', 'maximum')):
        first, second = old[_FIELD_INDEX[field]], new[_FIELD_INDEX[field]]
        if first != second:
            parts.append('%s %s to %s' % (label, first, second))
    return ', '.join(parts)


def _same_charset(a, b):
    """utf8 and utf8mb3 name one character set (MySQL 8.0.24 began printing the full name)."""
    return (isinstance(a, str) and isinstance(b, str) and a != b
            and a.replace('utf8mb3', 'utf8') == b.replace('utf8mb3', 'utf8'))


def _lower_first(text):
    return text[0].lower() + text[1:] if text[:1].isupper() and not text[1:2].isupper() else text


def _use_clause(after, use_note):
    replacement = after[_FIELD_INDEX['replacement']]
    return '; use %s instead%s' % (replacement, use_note) if replacement else ''


def _computed(row, rule):
    """A default computed at startup: (text inside the sentence, sentence after
    it). With a reviewed rule the rule follows as its own sentence and carries
    what a lab server got; without one, the lab value goes in parentheses."""
    lab = row[_FIELD_INDEX['default']]
    got = 'a lab server got %s' % lab if lab is not None else ''
    if rule:
        return '', ' %s%s.' % (rule.rstrip('.'), ' (%s)' % got if got else '')
    return (' (%s)' % got if got else ''), ''


def _sentence(kind, before, after, since, use=None, use_note='', rule=None, also=None, rule_before=None):
    """The one sentence of a change kind made on To's own line. *since* names
    the release ("8.4.0", or "10.6.6 or 10.6.7 (10.6.6 could not be read)");
    None (both lines changed it) leaves the release out. *use* is the
    successor of a removed parameter, *use_note* says the replacement is
    itself deprecated, *rule* how a computed default is set, *also* the
    release of From's own series that made the same change. Values print as
    the server shows them."""
    at = ' in %s' % since if since else ''
    if also:
        at += ' (%s)' % also
    if kind == 'added':
        return 'Added%s.' % at
    if kind == 'removed':
        return 'Removed%s%s.' % (at, '; use %s instead' % use if use else '')
    if kind == 'deprecated':
        replacement = after[_FIELD_INDEX['replacement']]
        if replacement:
            return 'Deprecated%s; use %s instead%s.' % (at, replacement, use_note)
        return 'Deprecated%s.' % at
    if kind == 'undeprecated':
        return 'No longer deprecated%s.' % at
    if kind == 'default':
        was, now = before[_FIELD_INDEX['default_note']], after[_FIELD_INDEX['default_note']]
        when = 'From %s the' % since if since else 'The'
        if now == 'host' and was != 'host':
            inline, after_text = _computed(after, rule)
            return ('%s server computes the default at startup%s; before, it was %s.%s'
                    % (when, inline, _default_text(before), after_text))
        if was == 'host' and now != 'host':
            return ('%s default is %s; before, the server computed it at startup.%s'
                    % (when, _default_text(after), ' Before then: %s' % _lower_first(rule_before) if rule_before else ''))
        old, new = before[_FIELD_INDEX['default']], after[_FIELD_INDEX['default']]
        if _same_charset(old, new):
            return ('The default reads %s instead of %s%s: the same character set under its full name.'
                    % (new, old, at))
        return 'Default changed from %s to %s%s.' % (_default_text(before), _default_text(after), at)
    if kind == 'dynamic':
        verb = 'Can be changed at runtime' if after[_FIELD_INDEX['dynamic']] else 'Can no longer be changed at runtime'
        return '%s%s.' % (verb, at.replace(' in ', ' since ', 1) if at else '')
    if kind in ('scope', 'type'):
        return '%s changed from %s to %s%s.' % (kind.capitalize(), before[_FIELD_INDEX[kind]],
                                                after[_FIELD_INDEX[kind]], at)
    if kind == 'values':
        return 'Allowed values changed%s.' % at
    return 'Range changed%s: %s.' % (at, _range_parts(before, after))


def _from_line_sentence(kind, before, after, version, target, rule=None, rule_to=None):
    """The sentence of a difference that From's own line made (a downgrade, or
    a patch of an older series the newer one does not have): *version* on
    From's line changed the value *after* (To's, unchanged since the lines met)
    into *before*."""
    if kind == 'added':
        return '%s removed it; %s still has it.' % (version, target)
    if kind == 'removed':
        return '%s added it; %s does not have it.' % (version, target)
    if kind == 'deprecated':
        return '%s no longer marks it deprecated; %s still does.' % (version, target)
    if kind == 'undeprecated':
        replacement = before[_FIELD_INDEX['replacement']]
        return '%s deprecated it%s; %s does not.' % (
            version, ' (use %s instead)' % replacement if replacement else '', target)
    if kind == 'default':
        was, now = after[_FIELD_INDEX['default_note']], before[_FIELD_INDEX['default_note']]
        if now == 'host' and was != 'host':
            inline, after_text = _computed(before, rule)
            return ('From %s the server computes the default at startup%s; %s keeps %s.%s'
                    % (version, inline, target, _default_text(after), after_text))
        if was == 'host' and now != 'host':
            return ('%s fixed the default at %s; in %s the server computes it at startup.%s'
                    % (version, _default_text(before), target, ' %s' % rule_to if rule_to else ''))
        if _same_charset(after[_FIELD_INDEX['default']], before[_FIELD_INDEX['default']]):
            return ('%s reads the default as %s, %s as %s: the same character set under two names.'
                    % (version, before[_FIELD_INDEX['default']], target, after[_FIELD_INDEX['default']]))
        return '%s changed the default from %s to %s; %s keeps %s.' % (
            version, _default_text(after), _default_text(before), target, _default_text(after))
    if kind == 'dynamic':
        if before[_FIELD_INDEX['dynamic']]:
            return '%s made it changeable at runtime; in %s it cannot be changed at runtime.' % (version, target)
        return '%s made it unchangeable at runtime; in %s it can be changed at runtime.' % (version, target)
    if kind in ('scope', 'type'):
        return '%s changed its %s from %s to %s; %s keeps %s.' % (
            version, kind, after[_FIELD_INDEX[kind]], before[_FIELD_INDEX[kind]], target, after[_FIELD_INDEX[kind]])
    if kind == 'values':
        return '%s changed the allowed values; %s keeps the earlier ones.' % (version, target)
    return '%s changed the range (%s); %s keeps the earlier range.' % (version, _range_parts(after, before), target)


_SUMMARY_LABELS = {'added': ('added', 'added'), 'removed': ('removed', 'removed'),
                   'deprecated': ('deprecated', 'deprecated'), 'undeprecated': ('undeprecated', 'undeprecated'),
                   'default': ('default change', 'default changes'), 'dynamic': ('dynamic change', 'dynamic changes'),
                   'scope': ('scope change', 'scope changes'), 'type': ('type change', 'type changes'),
                   'values': ('values change', 'values changes'), 'range': ('range change', 'range changes')}


def _cap120(text):
    return text if len(text) <= 120 else text[:117].rstrip() + '...'


def _compare_summary(product, source, target, direction, counts):
    head = '%s %s to %s' % (product, source, target)
    if direction == 'same':
        return _cap120(head + ': the same version; no changes.')
    if not counts['changed']:
        return _cap120(head + ': no parameter changes; %d unchanged.' % counts['unchanged'])
    parts = []
    for kind in KINDS:
        if counts[kind]:
            singular, plural = _SUMMARY_LABELS[kind]
            parts.append('%d %s' % (counts[kind], singular if counts[kind] == 1 else plural))
    text = head + ': ' + ', '.join(parts) + '; %d unchanged.' % counts['unchanged']
    if len(text) > 120:
        text = head + ': %d changed, %d unchanged.' % (counts['changed'], counts['unchanged'])
    return _cap120(text)


def _notes(engine, unjudged_defaults=0):
    notes = []
    if engine == 'mysql':
        notes.append(_PERCONA_NOTE)
    if unjudged_defaults:
        notes.append('The default of %d parameter%s was not compared because it is unknown on '
                     'one side; not counted as a default change.'
                     % (unjudged_defaults, '' if unjudged_defaults == 1 else 's'))
    return notes


def _successor(catalog, before, index):
    """The replacement a removed parameter named, when it exists at VERSIONS[index]."""
    replacement = before[_FIELD_INDEX['replacement']] or before[_FIELD_INDEX['alias_of']]
    return replacement if replacement and catalog.row(replacement, index) is not None else None


def _replacement_note(catalog, after, index):
    """' (X is itself deprecated since V)' when the suggested replacement is
    deprecated at VERSIONS[index] too."""
    replacement = after[_FIELD_INDEX['replacement']]
    row = catalog.row(replacement, index) if replacement else None
    if row is None or not row[_FIELD_INDEX['deprecated']]:
        return ''
    return ' (%s is itself deprecated since %s)' % (replacement, catalog.since(replacement, index, 'deprecated'))


def _build_only_notes(catalog, names):
    """One note per build-only sentence, naming every parameter it covers."""
    grouped = {}
    for name in names:
        grouped.setdefault(catalog.build_only[name], []).append(name)
    return [('%s: %s It is not counted as added or removed.' % (members[0], sentence)) if len(members) == 1 else
            ('%s: each %s They are not counted as added or removed.' % (', '.join(sorted(members)), _lower_first(sentence)))
            for sentence, members in sorted(grouped.items(), key=lambda item: sorted(item[1]))]


def compare(engine, from_version, to_version):
    """parameter-version-compare-1.0: the parameter changes from one cataloged
    release to another. Upgrade, downgrade and the same version all answer."""
    catalog = load(engine)
    first = _version_index(catalog, from_version)
    last = _version_index(catalog, to_version)
    source = catalog.versions[first]['version']
    target = catalog.versions[last]['version']
    source_series = catalog.versions[first]['series']
    direction = 'same' if first == last else ('upgrade' if last > first else 'downgrade')
    counts = dict((kind, 0) for kind in KINDS + ('changed', 'unchanged'))
    changes = []
    unjudged = 0
    build_only_skipped = []
    for name in catalog.names:
        before, after = catalog.row(name, first), catalog.row(name, last)
        if before is None and after is None:
            continue
        kinds, unjudged_default = _kinds(before, after)
        if name in catalog.build_only and ('added' in kinds or 'removed' in kinds):
            # Its presence follows the build, not the server: no change.
            build_only_skipped.append(name)
            counts['unchanged'] += 1
            continue
        if unjudged_default:
            unjudged += 1
        if not kinds:
            counts['unchanged'] += 1
            continue
        counts['changed'] += 1
        since = {}
        text = []
        for kind in kinds:
            counts[kind] += 1
            if kind == 'deprecated' and before is None:
                # From does not have it: say when To's own line deprecated it.
                at = catalog.since_index(name, last, 'deprecated')
                since[kind] = catalog.versions[at]['version']
                if catalog.versions[at]['previous'] is None:
                    # Deprecated in the first cataloged release: maybe long before.
                    text.append('Deprecated since %s (the first cataloged release) or earlier%s.' % (
                        since[kind], _use_clause(after, _replacement_note(catalog, after, last))))
                else:
                    text.append(_sentence(kind, before, after, catalog.when(at),
                                          use_note=_replacement_note(catalog, after, last)))
                continue
            # since names a release only for a change made on To's own line.
            line, at = catalog.where(name, first, last, kind)
            since[kind] = catalog.versions[at]['version'] if line == 'to' else None
            if line == 'from':
                text.append(_from_line_sentence(kind, before, after, catalog.when(at, subject=True), target,
                                                rule=catalog.rule(name, first), rule_to=catalog.rule(name, last)))
                continue
            if line == 'both' and kind in ('deprecated', 'undeprecated'):
                marked, unmarked = (target, source) if kind == 'deprecated' else (source, target)
                text.append('%s marks it deprecated; %s does not.' % (marked, unmarked))
                continue
            twin = catalog.from_line_twin(name, first, last, kind) if line == 'to' else None
            text.append(_sentence(kind, before, after, catalog.when(at) if line == 'to' else None,
                                  use=_successor(catalog, before, last) if kind == 'removed' else None,
                                  use_note=_replacement_note(catalog, after, last) if kind == 'deprecated' else '',
                                  rule=catalog.rule(name, last), rule_before=catalog.rule(name, first),
                                  also=catalog.on_line(catalog.index(twin), source_series) if twin else None))
        readded = catalog.readded(name, first, last) if before is not None and after is not None else None
        if readded:
            text.append('It was removed in %s and added again in %s.' % readded)
        if unjudged_default:
            versions = []
            for row, version in ((before, source), (after, target)):
                if row[_FIELD_INDEX['default_note']] == 'unknown':
                    versions.append(version)
            text.append('The default is not compared: it is unknown in %s.' % ' and '.join(versions))
        replacement = None
        for row in (after, before):
            if row is not None and row[_FIELD_INDEX['replacement']]:
                replacement = row[_FIELD_INDEX['replacement']]
                break
        changes.append({'name': name, 'kinds': list(kinds), 'text': text,
                        'from': facts_dict(before) if before is not None else None,
                        'to': facts_dict(after) if after is not None else None,
                        'since': since, 'replacement': replacement})
    return {'schema': COMPARE_SCHEMA, 'engine': catalog.engine, 'product': catalog.product,
            'from': source, 'to': target, 'direction': direction,
            'summary': _compare_summary(catalog.product, source, target, direction, counts),
            'counts': counts, 'changes': changes,
            'notes': _notes(catalog.engine, unjudged) + _build_only_notes(catalog, build_only_skipped)}


def _history_series(catalog, name):
    """Per series, the maximal runs of identical facts (or absence)."""
    series = []
    entry = None
    run_row = run_from = run_to = None
    for i, version in enumerate(catalog.versions):
        row = catalog.row(name, i)
        if entry is None or version['series'] != entry['series'] or row != run_row:
            if entry is not None:
                entry['ranges'].append({'from': run_from, 'to': run_to,
                                        'facts': facts_dict(run_row) if run_row is not None else None})
            if entry is None or version['series'] != entry['series']:
                entry = {'series': version['series'], 'ranges': []}
                series.append(entry)
            run_row, run_from = row, version['version']
        run_to = version['version']
    entry['ranges'].append({'from': run_from, 'to': run_to,
                            'facts': facts_dict(run_row) if run_row is not None else None})
    return series


def _history_summary(product, name, catalog, first, latest, events, removed_in):
    if first == 0:
        first_text = 'present since the first cataloged release (%s)' % catalog.versions[first]['version']
    else:
        first_text = 'added in %s' % catalog.versions[first]['version']
    if removed_in:
        state = 'removed in %s' % removed_in
    elif latest == len(catalog.versions) - 1:
        state = 'latest: %s' % catalog.versions[latest]['version']
    else:
        state = 'last present in %s' % catalog.versions[latest]['version']
    count = len(events)
    changes = '%d change%s' % (count, '' if count == 1 else 's') if count else 'no changes'
    text = '%s %s: %s; %s; %s.' % (product, name, first_text, state, changes)
    if len(text) > 120:
        # Long names (group_replication_...) keep every fact in fewer words.
        first_text = 'since %s' % catalog.versions[first]['version'] if first == 0 else first_text
        text = '%s: %s; %s; %s.' % (name, first_text, state, changes)
    return _cap120(text)


def history(engine, name):
    """parameter-history-1.0: every cataloged change of one parameter, oldest
    first. An unknown name answers found false with close names of the engine."""
    catalog = load(engine)
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LENGTH:
        raise ValueError('Choose a parameter name of at most %d characters.' % MAX_NAME_LENGTH)
    # Server variable names are case-insensitive; the catalogs keep them in lowercase.
    name = name.strip().lower()
    if name not in catalog.history:
        suggestions = difflib.get_close_matches(name, catalog.names, MAX_SUGGESTIONS, 0.6)
        return {'schema': HISTORY_SCHEMA, 'engine': catalog.engine, 'product': catalog.product,
                'name': name, 'found': False,
                'summary': _cap120('No parameter named %s is cataloged for %s.' % (name, catalog.product)),
                'first': None, 'latest': None, 'current': None, 'series': [], 'events': [],
                'suggestions': suggestions, 'build_only': None, 'default_rule': None, 'aliases': [],
                'notes': _notes(catalog.engine)}
    present = [i for i in range(len(catalog.versions)) if catalog.row(name, i) is not None]
    if not present:
        raise CatalogError('The %s parameter catalog does not have the expected format.' % catalog.engine)
    first, latest = present[0], present[-1]
    events = []
    removed_in = None
    for i in range(1, len(catalog.versions)):
        previous = catalog.versions[i]['previous']
        if previous is None:
            continue
        before, after = catalog.row(name, previous), catalog.row(name, i)
        kinds, _ = _kinds(before, after)
        if name in catalog.build_only:
            kinds = tuple(kind for kind in kinds if kind not in ('added', 'removed'))
        if not kinds:
            continue
        version = catalog.versions[i]['version']
        if 'removed' in kinds and i > latest and removed_in is None:
            removed_in = version
        events.append({'version': version, 'previous': catalog.versions[previous]['version'],
                       'series': catalog.versions[i]['series'], 'kinds': list(kinds),
                       'text': [_sentence(kind, before, after, catalog.when(i),
                                          use=_successor(catalog, before, i) if kind == 'removed' else None,
                                          use_note=_replacement_note(catalog, after, i) if kind == 'deprecated' else '',
                                          rule=catalog.rule(name, i))
                                for kind in kinds],
                       'before': facts_dict(before) if before is not None else None,
                       'after': facts_dict(after) if after is not None else None})
    return {'schema': HISTORY_SCHEMA, 'engine': catalog.engine, 'product': catalog.product,
            'name': name, 'found': True,
            'summary': _history_summary(catalog.product, name, catalog, first, latest, events, removed_in),
            'first': catalog.versions[first]['version'], 'latest': catalog.versions[latest]['version'],
            'current': catalog.facts(name, latest), 'series': _history_series(catalog, name),
            'events': events, 'suggestions': [], 'build_only': catalog.build_only.get(name),
            'default_rule': catalog.rule(name, latest),
            'aliases': sorted(other for other in catalog.names
                              if (catalog.row(other, latest) or (None,) * len(FIELDS))[_FIELD_INDEX['alias_of']] == name),
            'notes': _notes(catalog.engine) + ([catalog.build_only[name]] if name in catalog.build_only else [])}
