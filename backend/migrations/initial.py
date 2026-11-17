from sqlalchemy import inspect, text
from src.config.database import Base, engine
from src import models

# Columns added after the first release. Kept as explicit, idempotent ALTERs so
# an existing database keeps its history when the dispatcher is upgraded.
_ADDED_COLUMNS = {
    'jobs': {
        'round_no': 'INTEGER',
        'round_pos': 'INTEGER',
        'dispatch_seq': 'INTEGER',
        'executor': 'VARCHAR(80)',
        'lease_expires_at': 'DOUBLE PRECISION',
        'dispatched_at': 'DOUBLE PRECISION',
        'attempts': 'INTEGER DEFAULT 0',
        'recovery_count': 'INTEGER DEFAULT 0',
        'claim_token': 'VARCHAR(64)',
    },
}


def _upgrade_columns(connection):
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    dialect = connection.dialect.name
    for table, columns in _ADDED_COLUMNS.items():
        if table not in existing_tables:
            continue
        present = {c['name'] for c in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name in present:
                continue
            if dialect == 'sqlite':
                ddl = ddl.replace('DOUBLE PRECISION', 'FLOAT')
            connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))


def migrate():
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        _upgrade_columns(connection)


if __name__ == '__main__':
    migrate()
