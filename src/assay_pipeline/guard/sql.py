"""The SQL guard (Tech Spec §6.1, D-5): default deny over a sqlglot AST.

A statement is allowed only when it is one plain SELECT (or a set operation of SELECTs) built
from an allow-listed set of syntax nodes, every table it reads resolves (after CTE scoping) to an
approved view, and every function is on the allow-list. The SQL that runs is re-rendered from
the checked AST, never the string the model wrote, and re-checked once more.
"""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import version
from typing import Any

import sqlglot
from sqlglot import Expr, TokenType, exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError, TokenError
from sqlglot.optimizer.scope import traverse_scope

from assay_pipeline.config import ViewsConfig
from assay_pipeline.schemas import GuardDecision

DIALECT = "duckdb"

# The only functions a query may call: allow-list name -> sqlglot expression class.
FUNCTION_CLASSES: dict[str, type[exp.Func]] = {
    "count": exp.Count,
    "sum": exp.Sum,
    "avg": exp.Avg,
    "median": exp.Median,
    "min": exp.Min,
    "max": exp.Max,
    "quantile_cont": exp.PercentileCont,
    "stddev_samp": exp.StddevSamp,
    "abs": exp.Abs,
    "round": exp.Round,
    "log10": exp.Log,
    "ln": exp.Ln,
    "exp": exp.Exp,
    "power": exp.Pow,
    "sqrt": exp.Sqrt,
    "lower": exp.Lower,
    "upper": exp.Upper,
    "length": exp.Length,
    "trim": exp.Trim,
    "contains": exp.Contains,
    "starts_with": exp.StartsWith,
    "coalesce": exp.Coalesce,
    "nullif": exp.Nullif,
    "cast": exp.Cast,
}

# Plain-SELECT syntax. Any node outside this set (PIVOT, window frames, lambdas, table
# functions, ...) makes the statement "not a plain SELECT".
ALLOWED_NODES: tuple[type[Expr], ...] = (
    exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery, exp.With, exp.CTE,
    exp.From, exp.Join, exp.Where, exp.Group, exp.Having, exp.Order, exp.Ordered,
    exp.Limit, exp.Offset, exp.Distinct, exp.Table, exp.TableAlias, exp.Alias, exp.Column,
    exp.Identifier, exp.Star, exp.Literal, exp.Null, exp.Boolean, exp.Paren, exp.Tuple,
    exp.Neg, exp.Not, exp.And, exp.Or, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Is, exp.In, exp.Between, exp.Like,
    exp.ILike, exp.Case, exp.If, exp.DataType, exp.DataTypeParam, exp.Var, exp.Dot,
    exp.NullSafeEQ, exp.NullSafeNEQ, exp.DPipe,
)  # fmt: skip
# Syntax that sqlglot models as function classes but is plain SELECT grammar, not a call.
SYNTAX_FUNCS: tuple[type[Expr], ...] = (exp.Case, exp.If, exp.Exists, exp.And, exp.Or)
# DuckDB keyword functions that parse as bare column names (`SELECT user`, `"current_catalog"`).
KEYWORD_FUNCTIONS = frozenset(
    {"user", "current_user", "session_user", "current_role", "current_schema", "current_schemas",
     "current_catalog", "current_database", "current_query", "current_date", "current_time",
     "current_timestamp", "current_localtime", "current_localtimestamp", "localtime",
     "localtimestamp", "now", "today", "version"}
)  # fmt: skip
# Complexity bounds (D-28): DuckDB plans a CTE once per reference, so a chain of CTEs each
# used twice grows exponentially in bind/plan time and memory, where neither the interrupt nor
# memory_limit reaches. Statements over these bounds are refused before they reach DuckDB.
MAX_NODES = 2_000
MAX_DEPTH = 64
MAX_EXPANDED_TABLES = 16
MAX_DERIVED_DEPTH = 4  # nested subqueries in FROM
MAX_CONCAT_OPERANDS = 8
MAX_GROWTH = 64  # how many source values one output value may be built from (D-28)
SCALAR_TYPES = frozenset(
    {"INT", "BIGINT", "SMALLINT", "TINYINT", "DOUBLE", "FLOAT", "DECIMAL", "VARCHAR", "TEXT",
     "CHAR", "BOOLEAN", "DATE"}
)  # fmt: skip
CATALOG_SCHEMAS = frozenset({"information_schema", "pg_catalog", "duckdb", "system", "temp"})


def _blocked(
    reason: str, tables: tuple[str, ...] = (), funcs: tuple[str, ...] = ()
) -> GuardDecision:
    return GuardDecision(
        decision="blocked",
        reason=reason,  # type: ignore[arg-type]
        rendered_sql=None,
        tables=tables,
        functions=funcs,
        limit_applied=None,
    )


class SqlGuard:
    def __init__(self, views: ViewsConfig) -> None:
        unknown = [f for f in views.functions if f not in FUNCTION_CLASSES]
        if unknown:
            raise ValueError(f"views config allows functions the guard cannot map: {unknown}")
        self.views = frozenset(v.lower() for v in views.view_names)
        self.view_columns = frozenset(c.lower() for v in views.views for c in v.columns)
        self.functions = {FUNCTION_CLASSES[f]: f for f in views.functions}
        self.max_rows = views.limits.max_rows_executed
        policy = {
            "views": sorted(self.views),
            "functions": sorted(views.functions),
            "max_rows": self.max_rows,
            "nodes": sorted(c.__name__ for c in ALLOWED_NODES),
            "sqlglot": version("sqlglot"),
        }
        self.version = hashlib.sha256(json.dumps(policy).encode()).hexdigest()[:16]

    # ---- the checks of Tech Spec §6.1, in order ----

    def check(self, sql: str) -> GuardDecision:
        """Never raises: any unexpected failure inside the checks is a block."""
        try:
            return self._check_twice(sql)
        except Exception:  # a guard that crashes must fail closed
            return _blocked("parse_error")

    def _check_twice(self, sql: str) -> GuardDecision:
        first = self._check(sql)
        if first.decision == "blocked" or first.rendered_sql is None:
            return first
        # The rendered SQL is what runs: it must pass the same checks, unchanged.
        second = self._check(first.rendered_sql)
        if second.decision != "allowed" or second.rendered_sql != first.rendered_sql:
            return _blocked("parse_error", first.tables, first.functions)
        return first

    def _check(self, sql: str) -> GuardDecision:
        try:
            parsed = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
            string_tokens = {
                t.text.lower()
                for t in sqlglot.tokenize(sql, read=DIALECT)
                if t.token_type in (TokenType.STRING, TokenType.NATIONAL_STRING)
            }
        except (ParseError, TokenError, SqlglotError, ValueError, RecursionError):
            return _blocked("parse_error")
        if not parsed:
            return _blocked("parse_error")
        if len(parsed) > 1:
            return _blocked("multiple_statements")
        ast = parsed[0]
        if not isinstance(ast, exp.Select | exp.Union | exp.Intersect | exp.Except):
            return _blocked("not_select")

        tables = tuple(sorted({t.name.lower() for t in ast.find_all(exp.Table) if t.name}))
        funcs = tuple(sorted({type(f).__name__.lower() for f in ast.find_all(exp.Func)}))
        if _too_big(ast):
            return _blocked("too_complex", tables, funcs)  # node count or depth (D-28)

        for w in ast.find_all(exp.With):
            if w.args.get("recursive"):
                return _blocked("recursive_cte", tables, funcs)

        # A CTE may never reuse an approved view's name, wherever it is defined.
        for cte in ast.find_all(exp.CTE):
            if cte.alias_or_name.lower() in self.views:
                return _blocked("cte_shadows_view", tables, funcs)
        # Table references, resolved per scope so a CTE is only visible where SQL says it is.
        for table in ast.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                return _blocked("function_not_allowed", tables, funcs)  # table function
            schema = (table.args.get("db") or exp.Identifier(this="")).name.lower()
            catalog = (table.args.get("catalog") or exp.Identifier(this="")).name.lower()
            if schema in CATALOG_SCHEMAS or catalog in CATALOG_SCHEMAS:
                return _blocked("catalog_access", tables, funcs)
            if table.name.lower() in string_tokens and table.this.args.get("quoted"):
                return _blocked("file_reference", tables, funcs)
            if schema or catalog:
                return _blocked("table_not_allowed", tables, funcs)
        try:
            scopes = list(traverse_scope(ast))
        except (ValueError, RecursionError, OptimizeError, SqlglotError):
            return _blocked("parse_error", tables, funcs)
        views_read: set[str] = set()
        for scope in scopes:
            for table in scope.tables:
                if table.name in scope.cte_sources:
                    continue
                if table.name.lower() not in self.views:
                    if table.name.lower().startswith(("duckdb_", "pg_", "sqlite_")):
                        return _blocked("catalog_access", tables, funcs)
                    return _blocked("table_not_allowed", tables, funcs)
                views_read.add(table.name.lower())
        tables = tuple(sorted(views_read))  # only the approved views actually read
        if scopes and _expanded_tables(scopes[-1], {}) > MAX_EXPANDED_TABLES:
            return _blocked("too_complex", tables, funcs)  # CTE fan-out (D-28)

        for col in ast.find_all(exp.Column):
            if not col.table and col.name.lower() in KEYWORD_FUNCTIONS:
                return _blocked("function_not_allowed", tables, funcs)
            owner = col.find_ancestor(exp.Select)
            if (
                owner is not None
                and owner.args.get("from_") is None
                and owner.args.get("from") is None
            ):
                return _blocked("too_complex", tables, funcs)  # nothing to come from (D-28)
        if (
            _lateral_alias(ast)
            or _too_many_concat_operands(ast)
            or (scopes and _derived_depth(scopes[-1]) > MAX_DERIVED_DEPTH)
            or any(_max_growth(s, {}) > MAX_GROWTH for s in scopes)
        ):
            return _blocked("too_complex", tables, funcs)  # bind/plan or memory blow-up (D-28)

        allowed_funcs: list[str] = []
        for f in ast.find_all(exp.Func):
            if isinstance(f, SYNTAX_FUNCS):
                continue
            name = self.functions.get(type(f))
            if name is None:
                if type(f).__name__.lower().startswith(("duckdb", "pg")):
                    return _blocked("catalog_access", tables, funcs)
                return _blocked("function_not_allowed", tables, funcs)
            allowed_funcs.append(name)

        for node in ast.walk():
            if isinstance(node, exp.Func):
                continue
            if not isinstance(node, ALLOWED_NODES):
                return _blocked("not_select", tables, funcs)
            if isinstance(node, exp.DataType) and node.this.value not in SCALAR_TYPES:
                return _blocked("not_select", tables, funcs)  # nested and exotic types
            # Renaming outside the select list (column lists, * REPLACE / * RENAME) would hide
            # where an output column comes from; refused so growth tracing stays exact (D-28).
            if isinstance(node, exp.TableAlias) and node.args.get("columns"):
                return _blocked("too_complex", tables, funcs)
            if isinstance(node, exp.Star) and (node.args.get("replace") or node.args.get("rename")):
                return _blocked("too_complex", tables, funcs)

        for clause in ast.find_all(exp.Limit, exp.Offset):
            value = clause.expression
            positive = isinstance(clause, exp.Limit)
            if isinstance(value, exp.Literal) and value.is_int:
                if int(value.this) < (1 if positive else 0):
                    return _blocked("parse_error", tables, funcs)
            elif not (
                positive and isinstance(value, exp.Subquery) and clause is ast.args.get("limit")
            ):
                return _blocked("parse_error", tables, funcs)  # NULL, decimals, negatives
        limit = self._apply_limit(ast)
        rendered = ast.sql(dialect=DIALECT, comments=False)  # the model's comments never run
        return GuardDecision(
            decision="allowed",
            reason=None,
            rendered_sql=rendered,
            tables=tables,
            functions=tuple(sorted(set(allowed_funcs))),
            limit_applied=limit,
        )

    def _apply_limit(self, ast: Expr) -> int:
        cap = self.max_rows
        current = ast.args.get("limit")
        value = cap
        if isinstance(current, exp.Limit):
            lit = current.expression
            if isinstance(lit, exp.Literal) and lit.is_int:
                value = min(int(lit.this), cap)
        ast.set("limit", exp.Limit(expression=exp.Literal.number(value)))
        return value


def _too_big(ast: Expr) -> bool:
    """More than MAX_NODES nodes, or nested deeper than MAX_DEPTH (checked iteratively)."""
    count = 0
    stack: list[tuple[Expr, int]] = [(ast, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            return True
        for child in node.iter_expressions():
            stack.append((child, depth + 1))
    return False


def _expanded_tables(scope: Any, memo: dict[int, int]) -> int:
    """Table references after inlining every CTE: what DuckDB actually binds and plans."""
    key = id(scope)
    if key in memo:
        return memo[key]
    memo[key] = MAX_EXPANDED_TABLES + 1  # a cycle can only mean trouble
    total = 0
    for table in scope.tables:
        cte = scope.cte_sources.get(table.name)
        total += _expanded_tables(cte, memo) if cte is not None else 1
        if total > MAX_EXPANDED_TABLES:
            break
    children = (*scope.subquery_scopes, *scope.derived_table_scopes, *scope.set_operation_scopes)
    for child in children:
        total += _expanded_tables(child, memo)
        if total > MAX_EXPANDED_TABLES:
            break
    memo[key] = total
    return total


def _lateral_alias(ast: Expr) -> bool:
    """DuckDB lets one select expression reuse another's alias and copies the aliased expression
    at each reference, so `a1 = a0 + a0, a2 = a1 + a1, ...` binds exponentially. Refuse any
    unqualified reference, inside one select expression, to an alias defined by a different
    expression of the same SELECT, whatever the name (a self-named `x * 2 AS x` reads the source
    column, and one-step uses in WHERE, GROUP BY, HAVING and ORDER BY cannot chain)."""
    for select in ast.find_all(exp.Select):
        defined = [
            (i, e.alias.lower())
            for i, e in enumerate(select.expressions)
            if isinstance(e, exp.Alias) and e.alias
        ]
        if not defined:
            continue
        for i, expr in enumerate(select.expressions):
            others = {name for j, name in defined if j != i}
            for col in expr.find_all(exp.Column):
                if col.table or col.find_ancestor(exp.Select) is not select:
                    continue
                if col.name.lower() in others:
                    return True
    return False


def _too_many_concat_operands(ast: Expr) -> bool:
    for node in ast.find_all(exp.DPipe):
        if isinstance(node.parent, exp.DPipe):
            continue  # judge each chain once, from its top
        count, stack = 0, [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, exp.DPipe):
                stack.extend((cur.this, cur.expression))
            else:
                count += 1
        if count > MAX_CONCAT_OPERANDS:
            return True
    return False


def _source_growth(scope: Any, col: Any, memo: dict[tuple[int, str], int]) -> int:
    """How many base values the column reference `col` stands for in `scope`."""
    name = col.name.lower()
    candidates = [scope.sources.get(col.table)] if col.table else list(scope.sources.values())
    best = 1
    for src in candidates:
        if src is None or isinstance(src, exp.Table):
            continue
        best = max(best, _output_growth(src, name, memo))
    return best


def _output_growth(scope: Any, name: str, memo: dict[tuple[int, str], int]) -> int:
    """Growth of the output column `name` of a derived table, CTE or set operation."""
    key = (id(scope), name)
    if key in memo:
        return memo[key]
    memo[key] = MAX_GROWTH + 1  # a cycle can only mean trouble
    if scope.set_operation_scopes:
        # branches line up by position, not name: take the largest growth of any branch column
        value = max(_max_output_growth(s, memo) for s in scope.set_operation_scopes)
    else:
        value = 1
        matched = False
        select = scope.expression
        for e in getattr(select, "expressions", []):
            if isinstance(e, exp.Star) or (
                isinstance(e, exp.Column) and isinstance(e.this, exp.Star)
            ):
                matched = True
                value = max(value, _source_growth(scope, exp.column(name), memo))
            elif e.alias_or_name.lower() == name:
                matched = True
                value = max(value, _expression_growth(scope, e, memo))
        if not matched:
            # a name the select list does not spell out (DuckDB's automatic names such as
            # "(a || a)"): assume the worst output of this scope
            value = max(value, _max_growth(scope, memo))
    memo[key] = value
    return value


def _max_output_growth(scope: Any, memo: dict[tuple[int, str], int]) -> int:
    if scope.set_operation_scopes:
        return max(_max_output_growth(s, memo) for s in scope.set_operation_scopes)
    return _max_growth(scope, memo)


def _inside_subquery(node: Any, top: Any) -> bool:
    """Is there a Subquery between `node` and `top` (the expression being measured)?

    Only ancestors within the expression count: a derived table's own Subquery wrapper sits
    above the whole SELECT and must not hide its columns.
    """
    cur = node.parent
    while cur is not None and cur is not top:
        if isinstance(cur, exp.Subquery):
            return True
        cur = cur.parent
    return False


def _expression_growth(scope: Any, expr: Any, memo: dict[tuple[int, str], int]) -> int:
    total = 0
    for col in expr.find_all(exp.Column):
        if isinstance(col.this, exp.Star) or _inside_subquery(col, expr):
            continue  # counted below, through the subquery's own scope
        total += _source_growth(scope, col, memo)
        if total > MAX_GROWTH:
            return total
    # a scalar subquery yields one value, but that value can itself be a grown string
    for sub in expr.find_all(exp.Subquery):
        if _inside_subquery(sub, expr):
            continue  # nested deeper: counted through its parent subquery's scope
        inner = sub.unnest()
        child = next((s for s in scope.subquery_scopes if s.expression is inner), None)
        total += _max_output_growth(child, memo) if child is not None else MAX_GROWTH + 1
        if total > MAX_GROWTH:
            return total
    return max(total, 1)


def _max_growth(scope: Any, memo: dict[tuple[int, str], int]) -> int:
    select = scope.expression
    return max(
        (_expression_growth(scope, e, memo) for e in getattr(select, "expressions", [])),
        default=1,
    )


def _derived_depth(scope: Any) -> int:
    children = (*scope.derived_table_scopes, *scope.subquery_scopes, *scope.set_operation_scopes)
    return 1 + max((_derived_depth(c) for c in children), default=0)
