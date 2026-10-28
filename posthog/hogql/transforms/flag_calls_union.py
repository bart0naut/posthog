"""Read flag calls from `events` and `posthog.flag_evaluations` together when a query reads them with other events.

Phase 1 swaps a scan that reads only flag calls. This transform renames each remaining `events` scan that names
`$feature_flag_called` to `posthog.events_with_flag_calls`, a lazy table with the events fields. Lazy table
resolution replaces it with a union of the two tables, split at the coverage start.

The rename runs before type resolution, so every rule here is syntactic. The union returns the right rows for any
scan, so the rules decide which scans pay for a second table, and which shapes the union cannot read yet.
"""

from typing import TYPE_CHECKING

from posthog.hogql import ast
from posthog.hogql.constants import FLAG_CALLED_EVENT
from posthog.hogql.context import HogQLContext
from posthog.hogql.database.lazy_join_tags import (
    EVENTS_TO_SESSIONS_V1,
    EVENTS_TO_SESSIONS_V2,
    EVENTS_TO_SESSIONS_V3,
    FOREIGN_KEY,
    GROUP_N,
    PERSON_DISTINCT_ID_OVERRIDES,
    PERSON_DISTINCT_IDS,
    PERSONS,
)
from posthog.hogql.database.schema.events_with_flag_calls import (
    EVENTS_WITH_FLAG_CALLS_CHAIN,
    EVENTS_WITH_FLAG_CALLS_TABLE,
    EventsWithFlagCallsTable,
    FlagEvaluationsColumns,
)
from posthog.hogql.database.schema.util.where_clause_extractor import (
    contains_in_cohort,
    contains_row_multiplying_function,
)
from posthog.hogql.errors import QueryError
from posthog.hogql.restricted_properties import key_read_overlaps_restriction, restricted_property_keys_for_table_type
from posthog.hogql.transforms.events_predicate_pushdown import PREDICATE_SAFE_JOIN_TYPES, SelectAliasInliner
from posthog.hogql.transforms.flag_calls_from_flag_evaluations import (
    EventsScan,
    ScanVisitor,
    SyntaxVisitor,
    boolean_operands,
    compared_values,
    comparison_of,
    filter_conjuncts,
    flag_key_predicate,
)
from posthog.hogql.visitor import CloningVisitor

from posthog.dataclasses import frozen

if TYPE_CHECKING:
    from posthog.hogql.database.database import Database

# The joins whose resolver compares only the join's key and `extra_from_fields` on the source table. Lazy table
# resolution projects exactly those from the union, so a join that reads anything else would name a missing column.
_UNION_JOIN_RESOLVERS = frozenset(
    {
        EVENTS_TO_SESSIONS_V1,
        EVENTS_TO_SESSIONS_V2,
        EVENTS_TO_SESSIONS_V3,
        FOREIGN_KEY,
        GROUP_N,
        PERSON_DISTINCT_ID_OVERRIDES,
        PERSON_DISTINCT_IDS,
        PERSONS,
    }
)

# Functions whose value changes from row to row or from one read to the next. A filter with one of them would filter
# again in each branch, so the union keeps it out of the branches.
ROW_DEPENDENT_FUNCTIONS = frozenset({"rand", "rowNumberInAllBlocks", "rowNumberInBlock"})


def refuse_union_table_by_name(node: ast.AST, context: HogQLContext) -> None:
    """Raises when `node` names the union table. The table exists only for the rename to point at, so a query that
    names it would depend on an internal table."""
    if context.database is None or not context.database.has_table(EVENTS_WITH_FLAG_CALLS_CHAIN):
        return
    finder = _UnionTableFinder()
    finder.visit(node)
    if finder.found:
        raise QueryError(f"Unknown table `posthog.{EVENTS_WITH_FLAG_CALLS_TABLE}`.")


def read_flag_calls_with_union(node: ast.AST, context: HogQLContext, flag_key_compares_as_string: bool) -> bool:
    """Points each mixed flag-call scan in `node` at the union, in place, when the union reads this query's scans.
    Returns whether any scan changed."""
    database = _union_database(context)
    if database is None:
        return False
    if not database.flag_calls_only_in_flag_evaluations and _flag_key_is_restricted(database, context):
        # The union tells the flag calls that only events holds by their `$feature_flag`, and a restricted property
        # reads as missing. Each flag call since the cutoff would then come from both tables.
        return False
    return rename_flag_call_scans_to_union(node, flag_key_compares_as_string)


def flag_call_union_refusals(node: ast.AST, context: HogQLContext, flag_key_compares_as_string: bool) -> list[str]:
    """Why each scan that reads flag calls with other events cannot read the union, for an organization whose flag
    calls are only in `flag_evaluations`. Empty for every other organization, which keeps reading `events`."""
    database = _union_database(context)
    if database is None or not database.flag_calls_only_in_flag_evaluations:
        return []
    return union_refusals(node, flag_key_compares_as_string)


def union_unreadable_reason(node: ast.AST, context: HogQLContext) -> str | None:
    """Why the union cannot read a scan in the resolved `node`, or None when it can read them all.

    Lazy table resolution builds the union after this check, from these same fields, the keys of the joins that start
    from the scan, and the other fields those joins declare that they read.
    """
    database = _union_database(context)
    if database is None:
        return None
    collector = _UnionChainCollector()
    collector.visit(node)
    if collector.unreadable_joins:
        return f"it reads {collector.unreadable_joins[0]}, a join that the union can't read yet"
    columns = FlagEvaluationsColumns(
        database.get_table(EVENTS_WITH_FLAG_CALLS_CHAIN), database.get_table(["posthog", "flag_evaluations"])
    )
    for chain in dict.fromkeys(tuple(chain) for chain in collector.chains):
        if columns.chain(list(chain)) is None:
            return f"it reads {'.'.join(str(part) for part in chain)}, which that table doesn't have"
    return None


@frozen
class UnionBranchFilters:
    """The predicates that a union scan's branches copy from the query that reads the scan.

    Each is a set of conjuncts from that query's WHERE and PREWHERE, so every row the query keeps passes it.
    """

    events: ast.Expr | None
    # For the branch that reads flag_evaluations. A read of `event` is `$feature_flag_called` there.
    flag_evaluations: ast.Expr | None
    # False when the query pins `$feature_flag` to keys that only a call with a non-empty string key matches.
    keyless_flag_calls_can_match: bool


def union_branch_filters(
    node: ast.SelectQuery,
    union: EventsWithFlagCallsTable,
    columns: FlagEvaluationsColumns,
    flag_key_compares_as_string: bool,
) -> UnionBranchFilters:
    """The filters for the union scan that `node` reads, from the resolved WHERE and PREWHERE of `node`."""
    extracted = _scan_conjuncts(node, union)
    flag_call_conjuncts = [_fold(_EventAsFlagCall().visit(conjunct)) for conjunct in extracted]
    # Phase 1 accepts a key only when it is a non-empty string that no other JSON value prints as. A flag call that
    # ingestion left in events, with a missing, empty or non-string key, cannot match one.
    keys = EventsScan.of_scan_chains(flag_key_compares_as_string).flag_keys(
        [_StepTests().visit(conjunct) for conjunct in flag_call_conjuncts]
    )
    flag_evaluations_conjuncts = [
        mapped
        for conjunct in flag_call_conjuncts
        if _truth(conjunct) is not True and (mapped := _FlagEvaluationsReads(columns).copy(conjunct)) is not None
    ]
    return UnionBranchFilters(
        events=_events_filter(extracted),
        flag_evaluations=and_conditions([*flag_evaluations_conjuncts, flag_key_predicate("flag_evaluations", keys)]),
        keyless_flag_calls_can_match=not keys,
    )


def union_events_filter(node: ast.SelectQuery, union: EventsWithFlagCallsTable) -> ast.Expr | None:
    """The filter that a scan of `events` copies from `node`, as `union_branch_filters` builds it."""
    return _events_filter(_scan_conjuncts(node, union))


def and_conditions(conditions: list[ast.Expr | None]) -> ast.Expr | None:
    present = [condition for condition in conditions if condition is not None]
    if not present:
        return None
    return present[0] if len(present) == 1 else ast.And(exprs=present)


def rename_flag_call_scans_to_union(node: ast.AST, flag_key_compares_as_string: bool) -> bool:
    """Point each `events` scan that the union can read, and whose query names `$feature_flag_called`, at the union.

    A scan qualifies when all of these hold:

    1. Its query compares the scan's `event` with `$feature_flag_called` by `=` or `IN`, in one of the query's own
       clauses. A subquery's comparison does not count. A funnel step column such as
       `if(event = '$feature_flag_called' AND ..., 1, 0)` counts.
    2. The scan is the first table in FROM, and every join after it is INNER, LEFT or CROSS.
    3. The scan has no FINAL, table arguments, column aliases or SAMPLE other than 1, and its query selects no `*`
       or `COLUMNS(...)`.

    A scan that phase 1 swapped reads `posthog.flag_evaluations` and is not an `events` scan any more. Returns whether
    any scan changed.
    """
    renamer = _UnionRenamer(flag_key_compares_as_string)
    renamer.visit(node)
    return renamer.renamed


def union_refusals(node: ast.AST, flag_key_compares_as_string: bool) -> list[str]:
    """Why the union cannot read each scan that names `$feature_flag_called` with other events.

    A first scan that reads only flag calls is phase 1's, which swaps or refuses it.
    """
    collector = _UnionRefusalCollector(flag_key_compares_as_string)
    collector.visit(node)
    return collector.reasons


def _union_database(context: HogQLContext) -> "Database | None":
    """The database when the union reads this query's mixed flag-call scans, and None otherwise."""
    if (
        not context.rewrite_flag_calls_to_flag_evaluations
        or context.database is None
        or not context.database.read_flag_calls_with_union
    ):
        return None
    return context.database


def _flag_key_is_restricted(database: "Database", context: HogQLContext) -> bool:
    restricted = restricted_property_keys_for_table_type(ast.TableType(table=database.get_table("events")), context)
    return key_read_overlaps_restriction("$feature_flag", restricted)


def _union_table_of(table_type: ast.Type | None) -> EventsWithFlagCallsTable | None:
    while isinstance(table_type, (ast.TableAliasType, ast.VirtualTableType)):
        table_type = table_type.table_type
    if isinstance(table_type, ast.LazyTableType) and isinstance(table_type.table, EventsWithFlagCallsTable):
        return table_type.table
    return None


def _union_chain(field_type: ast.Type | None) -> tuple[EventsWithFlagCallsTable, list[str | int]] | None:
    """The union scan that a resolved field reads directly, and its chain there, or None."""
    property_chain: list[str | int] = []
    if isinstance(field_type, ast.PropertyType):
        property_chain = list(field_type.chain)
        field_type = field_type.field_type
    if not isinstance(field_type, ast.FieldType):
        return None
    chain: list[str | int] = [field_type.name, *property_chain]
    if isinstance(field_type.table_type, ast.VirtualTableType):
        chain = [field_type.table_type.field, *chain]
    union = _union_table_of(field_type.table_type)
    return (union, chain) if union is not None else None


def _scan_conjuncts(node: ast.SelectQuery, union: EventsWithFlagCallsTable) -> list[ast.Expr]:
    """The parts of the WHERE and PREWHERE of `node` that read only the union scan, with reads as chains on it."""
    inliner = SelectAliasInliner({expr.alias: expr.expr for expr in node.select if isinstance(expr, ast.Alias)})
    extracted = [_scan_predicate(inliner.visit(conjunct), union) for conjunct in filter_conjuncts(node)]
    return [conjunct for conjunct in extracted if conjunct is not None]


def _scan_predicate(expr: ast.Expr, union: EventsWithFlagCallsTable) -> ast.Expr | None:
    """`expr` with each read as a chain on the scan, or the conjuncts of it that read only the scan, or None.

    An OR counts only when every branch of it does, so every row that `expr` accepts passes the result.
    """
    if (parts := boolean_operands(expr, ast.And)) is not None:
        return and_conditions([_scan_predicate(part, union) for part in parts])
    if (branches := boolean_operands(expr, ast.Or)) is not None:
        extracted = [_scan_predicate(branch, union) for branch in branches]
        kept = [branch for branch in extracted if branch is not None]
        return ast.Or(exprs=kept) if kept and len(kept) == len(extracted) else None
    # A copy in each branch would multiply rows, or would move a cohort join that the printer adds later.
    if contains_row_multiplying_function(expr) or contains_in_cohort(expr):
        return None
    copier = _ScanReadCopier(union)
    copied = copier.visit(expr)
    return None if copier.failed else copied


def _events_filter(scan_conjuncts: list[ast.Expr]) -> ast.Expr | None:
    return and_conditions([_Qualified("events").visit(conjunct) for conjunct in scan_conjuncts])


def _truth(expr: ast.Expr) -> bool | None:
    """The truth value of a constant false, true, 0 or 1, or None for any other expression."""
    if isinstance(expr, ast.Constant) and expr.value in (0, 1):
        return bool(expr.value)
    return None


# Whether a comparison of constants passes when the left value is among the right values.
_MATCH_PASSES = {
    ast.CompareOperationOp.Eq: True,
    ast.CompareOperationOp.In: True,
    ast.CompareOperationOp.NotEq: False,
    ast.CompareOperationOp.NotIn: False,
}


def _fold(expr: ast.Expr) -> ast.Expr:
    """`expr` with comparisons of constants, and the AND, OR, NOT and `if` they decide, replaced by their value."""
    if (parts := boolean_operands(expr, ast.And)) is not None:
        folded = [_fold(part) for part in parts]
        if any(_truth(part) is False for part in folded):
            return ast.Constant(value=False)
        return and_conditions([part for part in folded if _truth(part) is not True]) or ast.Constant(value=True)
    if (parts := boolean_operands(expr, ast.Or)) is not None:
        folded = [_fold(part) for part in parts]
        if any(_truth(part) is True for part in folded):
            return ast.Constant(value=True)
        kept = [part for part in folded if _truth(part) is not False]
        if not kept:
            return ast.Constant(value=False)
        return kept[0] if len(kept) == 1 else ast.Or(exprs=kept)
    if isinstance(expr, ast.Not) or (isinstance(expr, ast.Call) and expr.name == "not" and len(expr.args) == 1):
        inner = _fold(expr.expr if isinstance(expr, ast.Not) else expr.args[0])
        truth = _truth(inner)
        return ast.Constant(value=not truth) if truth is not None else ast.Not(expr=inner)
    if isinstance(expr, ast.Call) and expr.name == "if" and len(expr.args) == 3:
        args = [_fold(arg) for arg in expr.args]
        truth = _truth(args[0])
        if truth is not None:
            return args[1] if truth else args[2]
        return ast.Call(name="if", args=args)
    compared = comparison_of(expr)
    if compared is None:
        return expr
    op, left, right = compared.op, _fold(compared.left), _fold(compared.right)
    values = right.exprs if isinstance(right, (ast.Tuple, ast.Array)) else [right]
    constants = [value.value for value in values if isinstance(value, ast.Constant) and value.value is not None]
    if (
        op in _MATCH_PASSES
        and isinstance(left, ast.Constant)
        and left.value is not None
        and len(constants) == len(values)
        and (len(values) == 1 or op in (ast.CompareOperationOp.In, ast.CompareOperationOp.NotIn))
    ):
        return ast.Constant(value=(left.value in constants) == _MATCH_PASSES[op])
    return ast.CompareOperation(op=op, left=left, right=right)


def _step_condition(expr: ast.Expr) -> ast.Expr | None:
    """`c` for a funnel step test such as `if(c, 1, 0) = 1`, or None for any other expression."""
    compared = comparison_of(expr)
    if compared is None or compared.op != ast.CompareOperationOp.Eq:
        return None
    for step, value in ((compared.left, compared.right), (compared.right, compared.left)):
        if (
            isinstance(step, ast.Call)
            and step.name == "if"
            and len(step.args) == 3
            and _truth(step.args[1]) is True
            and _truth(step.args[2]) is False
            and _truth(value) is True
        ):
            return step.args[0]
    return None


def _union_refusal(node: ast.SelectQuery, scan: EventsScan, position: int) -> str | None:
    """Why the union cannot stand in for `scan`, the scan at `position` in the FROM chain of `node`, or None."""
    if position > 0:
        return "it joins events after another table"
    refusal = scan.shape_refusal(node)
    if refusal is not None:
        return refusal
    join = scan.join.next_join
    while join is not None:
        # A predicate moves into the union's branches only across these joins.
        if join.join_type not in PREDICATE_SAFE_JOIN_TYPES:
            return f"it joins another table to events with {join.join_type or 'an unknown join type'}"
        join = join.next_join
    return None


def _flag_call_scans(node: ast.SelectQuery, flag_key_compares_as_string: bool) -> list[tuple[int, EventsScan]]:
    """Each `events` scan in the FROM chain of `node` whose query names `$feature_flag_called`, with its position."""
    scans: list[tuple[int, EventsScan]] = []
    join = node.select_from
    position = 0
    while join is not None:
        if isinstance(join.table, ast.Field) and join.table.chain == ["events"]:
            # Only the first scan can read unqualified names, and EventsScan.of decides when it can.
            scan = (
                EventsScan.of(node, flag_key_compares_as_string)
                if position == 0
                else EventsScan(join, join.alias or "events", True, flag_key_compares_as_string)
            )
            if scan is not None and _names_flag_calls(node, scan):
                scans.append((position, scan))
        join = join.next_join
        position += 1
    return scans


def _names_flag_calls(node: ast.SelectQuery, scan: EventsScan) -> bool:
    finder = _FlagCallComparisonFinder(node, scan)
    finder.visit(node)
    return finder.found


class _ScanReadCopier(CloningVisitor):
    """Copies an expression that reads only one union scan, with each read as its chain on the scan."""

    def __init__(self, union: EventsWithFlagCallsTable) -> None:
        super().__init__(clear_types=True, clear_locations=True)
        self.union = union
        self.failed = False

    def visit_field(self, node: ast.Field):
        read = _union_chain(node.type)
        if read is None or read[0] is not self.union:
            self.failed = True
            return super().visit_field(node)
        return ast.Field(chain=read[1])

    def visit_alias(self, node: ast.Alias):
        # A hidden alias wraps an expression field, such as `person_id`, that the resolver expanded in place.
        if not node.hidden:
            self.failed = True
        return self.visit(node.expr)

    def visit_call(self, node: ast.Call):
        if node.name in ROW_DEPENDENT_FUNCTIONS:
            self.failed = True
        return super().visit_call(node)

    def _refuse(self, node):
        self.failed = True
        return node

    visit_select_query = visit_select_set_query = visit_lambda = _refuse


class _EventAsFlagCall(CloningVisitor):
    def visit_field(self, node: ast.Field):
        if node.chain == ["event"]:
            return ast.Constant(value=FLAG_CALLED_EVENT)
        return super().visit_field(node)


class _StepTests(CloningVisitor):
    """Reads each funnel step test such as `if(c, 1, 0) = 1` as `c`, to find the keys a predicate pins.

    The copy is not a filter: for a NULL `c` the test is false, and `c` is NULL.
    """

    def visit_compare_operation(self, node: ast.CompareOperation):
        condition = _step_condition(node)
        return self.visit(condition) if condition is not None else super().visit_compare_operation(node)

    def visit_call(self, node: ast.Call):
        condition = _step_condition(node)
        return self.visit(condition) if condition is not None else super().visit_call(node)


class _Qualified(CloningVisitor):
    def __init__(self, table: str) -> None:
        super().__init__()
        self.table = table

    def visit_field(self, node: ast.Field):
        return ast.Field(chain=[self.table, *node.chain])


class _FlagEvaluationsReads(CloningVisitor):
    def __init__(self, columns: FlagEvaluationsColumns) -> None:
        super().__init__()
        self.columns = columns
        self.failed = False

    def copy(self, expr: ast.Expr) -> ast.Expr | None:
        copied = self.visit(expr)
        return None if self.failed else copied

    def visit_field(self, node: ast.Field):
        chain = self.columns.chain(node.chain)
        if chain is None:
            self.failed = True
            return super().visit_field(node)
        return ast.Field(chain=["flag_evaluations", *chain])


class _UnionChainCollector(SyntaxVisitor):
    """Collects the chain each resolved field reads on a union scan, as lazy table resolution will request it."""

    def __init__(self) -> None:
        super().__init__()
        self.chains: list[list[str | int]] = []
        self.unreadable_joins: list[str] = []

    def visit_field(self, node: ast.Field) -> None:
        read = _union_chain(node.type)
        if read is not None:
            self.chains.append(read[1])
            return
        field_type = node.type.field_type if isinstance(node.type, ast.PropertyType) else node.type
        if not isinstance(field_type, ast.FieldType):
            return
        table_type: ast.Type = field_type.table_type
        while isinstance(table_type, ast.VirtualTableType):
            table_type = table_type.table_type
        # A join that starts from the scan, such as `override` or `person`, reads its key from the union.
        while isinstance(table_type, ast.LazyJoinType):
            source = table_type.table_type
            while isinstance(source, ast.TableAliasType):
                source = source.table_type
            if _union_table_of(source) is not None:
                lazy_join = table_type.lazy_join
                if isinstance(source, ast.VirtualTableType) or lazy_join.resolver not in _UNION_JOIN_RESOLVERS:
                    self.unreadable_joins.append(table_type.field)
                else:
                    self.chains.extend([list(lazy_join.from_field), *lazy_join.extra_from_fields])
                return
            table_type = table_type.table_type


class _UnionTableFinder(SyntaxVisitor):
    def __init__(self) -> None:
        super().__init__()
        self.found = False

    def visit_join_expr(self, node: ast.JoinExpr) -> None:
        if isinstance(node.table, ast.Field) and node.table.chain == EVENTS_WITH_FLAG_CALLS_CHAIN:
            self.found = True
        super().visit_join_expr(node)


class _FlagCallComparisonFinder(SyntaxVisitor):
    """Finds `event = '$feature_flag_called'` or an `IN` list with it, on the scan, in its own query's clauses."""

    def __init__(self, root: ast.SelectQuery, scan: EventsScan) -> None:
        super().__init__()
        self.root = root
        self.scan = scan
        self.found = False

    def visit_select_query(self, node: ast.SelectQuery) -> None:
        if node is self.root:
            super().visit_select_query(node)

    def visit_select_set_query(self, node: ast.SelectSetQuery) -> None:
        pass

    def visit_compare_operation(self, node: ast.CompareOperation) -> None:
        self._check(node)
        super().visit_compare_operation(node)

    def visit_call(self, node: ast.Call) -> None:
        self._check(node)
        super().visit_call(node)

    def _check(self, expr: ast.Expr) -> None:
        values = compared_values(expr, lambda read: self.scan.column(read) == ["event"])
        if values and any(isinstance(value, ast.Constant) and value.value == FLAG_CALLED_EVENT for value in values):
            self.found = True


class _UnionRenamer(ScanVisitor):
    def __init__(self, flag_key_compares_as_string: bool) -> None:
        super().__init__()
        self.flag_key_compares_as_string = flag_key_compares_as_string
        self.renamed = False

    def visit_scan_query(self, node: ast.SelectQuery) -> None:
        for position, scan in _flag_call_scans(node, self.flag_key_compares_as_string):
            if _union_refusal(node, scan, position) is None:
                # The old alias keeps qualified reads such as `events.timestamp` resolving.
                scan.join.alias = scan.alias
                scan.join.table = ast.Field(chain=list(EVENTS_WITH_FLAG_CALLS_CHAIN))
                # A lazy table cannot be sampled, and SAMPLE 1 keeps every row anyway.
                scan.join.sample = None
                # A query that reads a subquery cannot have a PREWHERE, and WHERE selects the same rows.
                node.where = and_conditions([node.prewhere, node.where])
                node.prewhere = None
                self.renamed = True


class _UnionRefusalCollector(ScanVisitor):
    def __init__(self, flag_key_compares_as_string: bool) -> None:
        super().__init__()
        self.flag_key_compares_as_string = flag_key_compares_as_string
        self.reasons: list[str] = []

    def visit_scan_query(self, node: ast.SelectQuery) -> None:
        for position, scan in _flag_call_scans(node, self.flag_key_compares_as_string):
            # Phase 1 owns a first scan that reads only flag calls, and refuses it there when it must.
            if position == 0 and scan.pins_flag_calls(filter_conjuncts(node), needs_flag_key=False):
                continue
            reason = _union_refusal(node, scan, position)
            if reason is not None:
                self.reasons.append(reason)
