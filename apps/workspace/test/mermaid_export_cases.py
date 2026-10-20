"""Print Mermaid export cases for the real-browser parser regression test."""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, ROOT)

from myflames import schema_diagram as d


def sql_case(name, text):
    model = d.import_sql(text)
    result = d.export_mermaid(model)
    return {'name': name, 'text': result['text'], 'warnings': result['warnings']}


def handmade_case(name, model):
    result = d.export_mermaid(model)
    return {'name': name, 'text': result['text'], 'warnings': result['warnings']}


def column(name, data_type='int', nullable=False, primary_key=False):
    return {'name': name, 'data_type': data_type, 'nullable': nullable, 'primary_key': primary_key}


def main():
    cases = [
        sql_case('types and nullability', '''
            CREATE TABLE types (
              id bigint unsigned NOT NULL,
              small_int int unsigned,
              amount decimal(15,5) NOT NULL,
              choice enum('a','b'),
              title varchar(255),
              happened_at datetime(6),
              payload json,
              UNIQUE KEY uk_title (title)
            );
        '''),
        sql_case('composite and self-referencing keys', '''
            CREATE TABLE parent (
              tenant int NOT NULL,
              id int NOT NULL,
              PRIMARY KEY (tenant, id)
            );
            CREATE TABLE child (
              id int NOT NULL PRIMARY KEY,
              tenant int NOT NULL,
              parent_id int NOT NULL,
              parent_self int,
              CONSTRAINT fk_parent FOREIGN KEY (tenant, parent_id)
                REFERENCES parent (tenant, id),
              CONSTRAINT fk_self FOREIGN KEY (parent_self)
                REFERENCES child (id)
            );
        '''),
        sql_case('unusual identifiers and relationship quotes', '''
            CREATE TABLE `order`.`123 weird.table-name` (
              `two words` int NOT NULL,
              `dot.name` varchar(20),
              `dash-name` int,
              `9lives` int,
              `café` int,
              `select` int,
              PRIMARY KEY (`two words`)
            );
            CREATE TABLE `order`.`child & co` (
              `parent key` int,
              CONSTRAINT `says "hi" & 'bye'` FOREIGN KEY (`parent key`)
                REFERENCES `order`.`123 weird.table-name` (`two words`)
            );
        '''),
        sql_case('two databases', '''
            CREATE TABLE db_one.accounts (id int NOT NULL PRIMARY KEY);
            CREATE TABLE db_two.events (
              account_id int NOT NULL,
              CONSTRAINT fk_account FOREIGN KEY (account_id)
                REFERENCES db_one.accounts (id)
            );
        '''),
        handmade_case('empty table and conceptual relationship', {
            'schema_version': d.SCHEMA_VERSION,
            'tables': [
                dict(id='empty', schema='', name='Empty table', columns=[], indexes=[]),
                dict(id='other', schema='', name='Other', columns=[column('id', primary_key=True)], indexes=[
                    {'name': 'PRIMARY', 'columns': ['id'], 'unique': True, 'primary': True},
                ]),
            ],
            'relationships': [{
                'id': 'conceptual', 'name': 'may include "quotes" & entities',
                'from_table': 'empty', 'from_columns': [], 'to_table': 'other', 'to_columns': [],
                'cardinality': {'from': 'one', 'to': 'zero-or-many'}, 'identifying': False,
            }],
            'warnings': [],
        }),
        handmade_case('unknown nullability and composite unique key', {
            'schema_version': d.SCHEMA_VERSION,
            'tables': [dict(id='keys', schema='', name='keys', columns=[
                column('part_a', nullable=None), column('part_b', 'decimal(15,5)', nullable=None),
            ], indexes=[{'name': 'uk_parts', 'columns': ['part_a', 'part_b'], 'unique': True}])],
            'relationships': [], 'warnings': [],
        }),
    ]
    tables = []
    for i in range(60):
        tables.append('CREATE TABLE table_%02d (id int NOT NULL PRIMARY KEY, previous_id int NULL%s);' % (
            i, ', CONSTRAINT fk_%02d FOREIGN KEY (previous_id) REFERENCES table_%02d (id)' % (i, i - 1) if i else ''))
    cases.append(sql_case('sixty tables', '\n'.join(tables)))
    print(json.dumps(cases, ensure_ascii=False))


if __name__ == '__main__':
    main()
