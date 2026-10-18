"""Integration test: docs/erd.md must match the deployed schema exactly.

Rubric C2 asks for an ERD "matching the deployed schema exactly". This test reads
from the real database every table, column, type and length, which columns may
be empty, the primary keys, foreign keys and unique rules, and compares them
with the UML diagram, so the two can never drift apart. It also checks that
every foreign key is drawn as a relationship with the right multiplicity.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ERD_FILE = Path(__file__).resolve().parents[2] / "docs" / "erd.md"

# PostgreSQL internal type name -> the name used in the diagram
TYPE_NAMES = {
    "varchar": "varchar", "bpchar": "char", "numeric": "numeric", "timestamptz": "timestamptz",
    "int2": "smallint", "int8": "bigint", "date": "date",
}
ATTRIBUTE = re.compile(
    r"^(?:«(?P<key>PK|FK)» )?(?P<name>[a-z_]+) : (?P<type>[a-z]+(?:\(\d+(?:,\d+)?\))?)"
    r"(?P<nullable> \[0\.\.1\])?(?P<unique> \{unique\})?$"
)
RELATIONSHIP = re.compile(r'^\s*([a-z_]+) "([^"]+)" -- "([^"]+)" ([a-z_]+) : (.+)$', re.M)


def diagram_text():
    text = ERD_FILE.read_text()
    start = text.index("```mermaid")
    return text[start:text.index("```", start + 3)]


def erd_entities():
    """The classDiagram as {table: {column: (type, key, nullable, unique)}}, in drawing order."""
    entities = {}
    for name, body in re.findall(r"^\s*class ([a-z_]+) \{\n(.*?)^\s*\}", diagram_text(), flags=re.M | re.S):
        columns = {}
        for line in body.strip().splitlines():
            line = line.strip()
            if line == "<<table>>":
                continue
            match = ATTRIBUTE.match(line)
            assert match, f"cannot read the line '{line}' in table {name}"
            columns[match["name"]] = (match["type"], match["key"] or "", bool(match["nullable"]), bool(match["unique"]))
        entities[name] = columns
    return entities


def erd_relationships():
    """{(parent, child): (parent multiplicity, child multiplicity)}"""
    return {(parent, child): (one, many) for parent, one, many, child, _label in RELATIONSHIP.findall(diagram_text())}


def _type(udt, length, precision, scale):
    name = TYPE_NAMES[udt]
    if udt in ("varchar", "bpchar"):
        return f"{name}({length})"
    if udt == "numeric":
        return f"{name}({precision},{scale})"
    return name


def database_entities(admin):
    with admin.cursor() as cursor:
        cursor.execute("""
            SELECT c.table_name, c.column_name, c.udt_name, c.character_maximum_length,
                   c.numeric_precision, c.numeric_scale, c.is_nullable = 'YES'
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'   -- views are not entities
            ORDER BY c.table_name, c.ordinal_position
        """)
        columns = cursor.fetchall()
        cursor.execute("""
            SELECT c.conrelid::regclass::text, a.attname, c.contype
            FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
            WHERE c.contype IN ('p', 'f', 'u') AND c.connamespace = 'public'::regnamespace
              AND cardinality(c.conkey) = 1
        """)
        constraints = cursor.fetchall()
    keys = {(table, column): {"p": "PK", "f": "FK"}[kind] for table, column, kind in constraints if kind in "pf"}
    unique = {(table, column) for table, column, kind in constraints if kind == "u"}
    entities = {}
    for table, column, udt, length, precision, scale, nullable in columns:
        entities.setdefault(table, {})[column] = (
            _type(udt, length, precision, scale), keys.get((table, column), ""), nullable, (table, column) in unique)
    return entities


def database_relationships(admin):
    """Every foreign key as (parent, child) with the multiplicities it implies."""
    with admin.cursor() as cursor:
        cursor.execute("""
            SELECT c.confrelid::regclass::text, c.conrelid::regclass::text, a.attname, a.attnotnull
            FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
            WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace
        """)
        foreign_keys = cursor.fetchall()
    found = {}
    entities = database_entities(admin)
    for parent, child, column, not_null in foreign_keys:
        is_unique = entities[child][column][3]
        found[(parent, child)] = ("1" if not_null else "0..1", "0..1" if is_unique else "0..*")
    return found


def test_erd_has_exactly_the_deployed_tables(admin):
    assert set(erd_entities()) == set(database_entities(admin))


@pytest.mark.parametrize("table", ["customers", "accounts", "transactions", "risk_events", "alerts"])
def test_erd_columns_types_keys_and_nullability_match(admin, table):
    assert erd_entities()[table] == database_entities(admin)[table]


def test_erd_columns_are_in_schema_order(admin):
    diagram, database = erd_entities(), database_entities(admin)
    for table in database:
        assert list(diagram[table]) == list(database[table])


def test_every_foreign_key_is_a_relationship_with_the_right_multiplicity(admin):
    assert erd_relationships() == database_relationships(admin)
