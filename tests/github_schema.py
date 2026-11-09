"""Download GitHub's GraphQL schema into `fixtures/github-schema.graphql`
(`make schema`), fixed to be a valid schema.

GitHub deprecates some fields on a type but not on the interface it
implements (`Project.id`, not `Node.id`). graphql-core and graphql-js 17 (the
GraphQL LSP) reject such a schema; the LSP then reports nothing at all. The
fix drops those deprecations, the only change to GitHub's file.
"""

from dataclasses import replace
from pathlib import Path

import httpx
from graphql import (
    DocumentNode,
    FieldDefinitionNode,
    InterfaceTypeDefinitionNode,
    ObjectTypeDefinitionNode,
    build_ast_schema,
    parse,
    print_ast,
)
from graphql.type import assert_valid_schema

URL = "https://docs.github.com/public/fpt/schema.docs.graphql"
OUT = Path(__file__).parent / "fixtures" / "github-schema.graphql"

type TypeNode = ObjectTypeDefinitionNode | InterfaceTypeDefinitionNode


def deprecated(field: FieldDefinitionNode) -> bool:
    return any(d.name.value == "deprecated" for d in field.directives or ())


def fix(doc: DocumentNode) -> tuple[DocumentNode, list[str]]:
    """Drop each deprecation its interface doesn't share; also the fields fixed."""
    live = {
        d.name.value: {f.name.value for f in d.fields or () if not deprecated(f)}
        for d in doc.definitions
        if isinstance(d, InterfaceTypeDefinitionNode)
    }
    fixed: list[str] = []

    def field(t: TypeNode, f: FieldDefinitionNode) -> FieldDefinitionNode:
        name = f.name.value
        if not deprecated(f) or not any(
            name in live.get(i.name.value, ()) for i in t.interfaces or ()
        ):
            return f
        fixed.append(f"{t.name.value}.{name}")
        kept = tuple(d for d in f.directives or () if d.name.value != "deprecated")
        return replace(f, directives=kept)

    definitions = tuple(
        replace(d, fields=tuple(field(d, f) for f in d.fields or ()))
        if isinstance(d, ObjectTypeDefinitionNode | InterfaceTypeDefinitionNode)
        else d
        for d in doc.definitions
    )
    return replace(doc, definitions=definitions), fixed


def main() -> None:
    resp = httpx.get(URL, follow_redirects=True, timeout=60)
    resp.raise_for_status()
    doc, fixed = fix(parse(resp.text))
    assert_valid_schema(build_ast_schema(doc))
    OUT.write_text(print_ast(doc) + "\n")
    print(f"{OUT.name}: deprecation dropped on {', '.join(fixed) or 'nothing'}")


if __name__ == "__main__":
    main()
