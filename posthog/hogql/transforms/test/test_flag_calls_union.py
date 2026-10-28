import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from posthog.test.base import (
    APIBaseTest,
    BaseTest,
    ClickhouseTestMixin,
    _create_event,
    _create_flag_evaluations,
    _create_person,
    cleanup_materialized_columns,
    flush_persons_and_events,
)
from unittest.mock import patch

from django.test import SimpleTestCase

from parameterized import param, parameterized

from posthog.schema import (
    DateRange,
    EventPropertyFilter,
    EventsNode,
    FunnelsQuery,
    HogQLQueryModifiers,
    PersonsOnEventsMode,
    PropertyOperator,
)

from posthog.hogql import ast
from posthog.hogql.context import HogQLContext
from posthog.hogql.database.database import Database
from posthog.hogql.database.models import DatabaseField, FieldTraverser, LazyJoin, StringJSONDatabaseField, VirtualTable
from posthog.hogql.errors import QueryError
from posthog.hogql.functions.mapping import HOGQL_CLICKHOUSE_FUNCTIONS
from posthog.hogql.parser import parse_select
from posthog.hogql.printer import prepare_and_print_ast
from posthog.hogql.query import HogQLQueryExecutor, execute_hogql_query
from posthog.hogql.transforms.flag_calls_union import (
    ROW_DEPENDENT_FUNCTIONS,
    rename_flag_call_scans_to_union,
    union_refusals,
)
from posthog.hogql.visitor import TraversingVisitor

from posthog.clickhouse.client import sync_execute
from posthog.clickhouse.client.escape import substitute_params
from posthog.constants import AvailableFeature
from posthog.test.persons import create_group_type_mapping

from products.access_control.backend.models.property_access_control import PropertyAccessControl
from products.access_control.backend.property_access_control import PropertyAccessLevel
from products.data_tools.backend.models.join import DataWarehouseJoin
from products.event_definitions.backend.models.property_definition import PropertyDefinition
from products.feature_flags.backend.facade.enums import FlagEvaluationsMode
from products.feature_flags.backend.models.organization_feature_flags_config import OrganizationFeatureFlagsConfig
from products.product_analytics.backend.hogql_queries.funnels.funnels_query_runner import FunnelsQueryRunner

from ee.clickhouse.materialized_columns.columns import materialize

MIXED = "event IN ('$pageview', '$feature_flag_called')"
E_MIXED = "e.event IN ('$pageview', '$feature_flag_called')"
UNION = "posthog.events_with_flag_calls"
READ = FlagEvaluationsMode.READ_FLAG_EVALUATIONS
ONLY = FlagEvaluationsMode.FLAG_EVALUATIONS_ONLY


class _TableCollector(TraversingVisitor):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[str] = []

    def visit_join_expr(self, node: ast.JoinExpr) -> None:
        if isinstance(node.table, ast.Field):
            self.tables.append(".".join(str(part) for part in node.table.chain))
        super().visit_join_expr(node)


def _tables_after_rename(query: str) -> list[str]:
    select = parse_select(query)
    rename_flag_call_scans_to_union(select, flag_key_compares_as_string=True)
    collector = _TableCollector()
    collector.visit(select)
    return collector.tables


def _set_mode(organization, mode: FlagEvaluationsMode) -> None:
    OrganizationFeatureFlagsConfig.objects.update_or_create(
        organization=organization, defaults={"flag_evaluations_mode": mode}
    )


def _enabled_flags(union: bool):
    enabled = {"hogql-flag-calls-from-flag-evaluations", *(["hogql-flag-calls-union"] if union else [])}
    return patch(
        "posthog.hogql.database.database.feature_enabled_or_false",
        side_effect=lambda key, *args, **kwargs: key in enabled,
    )


def _restrict(organization, team, name: str) -> None:
    organization.available_product_features = [
        {"name": AvailableFeature.PROPERTY_ACCESS_CONTROL, "key": AvailableFeature.PROPERTY_ACCESS_CONTROL}
    ]
    organization.save()
    definition = PropertyDefinition.objects.create(
        team=team, name=name, property_type="String", type=PropertyDefinition.Type.EVENT
    )
    PropertyAccessControl.objects.create(
        team=team, property_definition=definition, access_level=PropertyAccessLevel.NONE.value
    )


class TestRenameFlagCallScansToUnion(SimpleTestCase):
    @parameterized.expand(
        [
            ("event_in_a_list_with_other_events", f"SELECT count() FROM events WHERE {MIXED}", [UNION], []),
            ("paths_v2_step_sources", f"SELECT e.event FROM events AS e WHERE {E_MIXED}", [UNION], []),
            (
                "funnel_step_column",
                "SELECT if(event = '$feature_flag_called' AND properties.$feature_flag = 'f', 1, 0) AS step_1 "
                "FROM events",
                [UNION],
                [],
            ),
            (
                "or_of_event_comparisons",
                "SELECT count() FROM events WHERE event = '$pageview' OR equals(event, '$feature_flag_called')",
                [UNION],
                [],
            ),
            # Phase 1 swaps this scan when it can, and otherwise the union reads it. Phase 1 refuses it on mode 2.
            ("only_flag_calls", "SELECT count() FROM events WHERE event = '$feature_flag_called'", [UNION], []),
            (
                "only_flag_calls_with_sample",
                "SELECT count() FROM events SAMPLE 0.1 WHERE event = '$feature_flag_called'",
                ["events"],
                [],
            ),
            (
                "left_join_after_the_scan",
                f"SELECT count() FROM events AS e LEFT JOIN persons AS p ON e.person_id = p.id WHERE {E_MIXED}",
                [UNION, "persons"],
                [],
            ),
            (
                "inner_join_after_the_scan",
                f"SELECT count() FROM events AS e INNER JOIN persons AS p ON e.person_id = p.id WHERE {E_MIXED}",
                [UNION, "persons"],
                [],
            ),
            ("sample_one", f"SELECT count() FROM events SAMPLE 1 WHERE {MIXED}", [UNION], []),
            ("scan_in_a_subquery", f"SELECT count() FROM (SELECT event FROM events WHERE {MIXED})", [UNION], []),
            (
                "comparison_only_in_a_subquery",
                "SELECT count() FROM events WHERE distinct_id IN "
                "(SELECT distinct_id FROM events WHERE event = '$feature_flag_called')",
                ["events", UNION],
                [],
            ),
            (
                "no_flag_call_comparison",
                "SELECT count() FROM events SAMPLE 0.1 WHERE event = '$pageview'",
                ["events"],
                [],
            ),
            ("not_equals", "SELECT count() FROM events WHERE event != '$feature_flag_called'", ["events"], []),
            (
                "constant_on_another_column",
                "SELECT count() FROM events WHERE distinct_id = '$feature_flag_called'",
                ["events"],
                [],
            ),
            (
                "scan_after_another_table",
                f"SELECT count() FROM persons AS p JOIN events AS e ON p.id = e.person_id WHERE {E_MIXED}",
                ["persons", "events"],
                ["it joins events after another table"],
            ),
            (
                "scan_of_only_flag_calls_after_another_table",
                "SELECT count() FROM persons AS p JOIN events AS e ON p.id = e.person_id "
                "WHERE e.event = '$feature_flag_called'",
                ["persons", "events"],
                ["it joins events after another table"],
            ),
            (
                "right_join_after_the_scan",
                f"SELECT count() FROM events AS e RIGHT JOIN persons AS p ON e.person_id = p.id WHERE {E_MIXED}",
                ["events", "persons"],
                ["it joins another table to events with RIGHT JOIN"],
            ),
            (
                "final",
                f"SELECT count() FROM events FINAL WHERE {MIXED}",
                ["events"],
                ["it uses FINAL or table arguments on events"],
            ),
            (
                "column_aliases",
                f"SELECT count() FROM events AS e (uuid, event, properties, timestamp, distinct_id, x) WHERE {E_MIXED}",
                ["events"],
                ["it names the events columns with column aliases"],
            ),
            ("sample_ratio", f"SELECT count() FROM events SAMPLE 0.1 WHERE {MIXED}", ["events"], ["it uses SAMPLE"]),
            ("select_star", f"SELECT * FROM events WHERE {MIXED}", ["events"], ["it selects * or COLUMNS(...)"]),
            (
                "cte_named_events",
                f"WITH events AS (SELECT '$pageview' AS event) SELECT count() FROM events WHERE {MIXED}",
                ["events"],
                [],
            ),
        ]
    )
    def test_renames_the_scans_the_union_reads_and_names_why_it_cannot_read_the_rest(
        self, _name: str, query: str, expected_tables: list[str], expected_refusals: list[str]
    ):
        assert _tables_after_rename(query) == expected_tables
        assert union_refusals(parse_select(query), flag_key_compares_as_string=True) == expected_refusals

    def test_row_dependent_functions_cover_every_hogql_function_in_their_families(self):
        families = re.compile(
            r"^(rand|random|rowNumber|blockNumber|generateUUID|generateULID|runningDifference|neighbor)"
        )

        assert {name for name in HOGQL_CLICKHOUSE_FUNCTIONS if families.match(name)} <= ROW_DEPENDENT_FUNCTIONS


class TestReadFlagCallsWithUnion(BaseTest):
    def _sql(
        self,
        query: str | ast.SelectQuery,
        *,
        mode: FlagEvaluationsMode = READ,
        union: bool = True,
        persons_on_events: PersonsOnEventsMode | None = None,
    ) -> str:
        _set_mode(self.organization, mode)
        with _enabled_flags(union=union):
            sql, context = HogQLQueryExecutor(
                query=query,
                team=self.team,
                modifiers=HogQLQueryModifiers(personsOnEventsMode=persons_on_events),
                pretty=False,
            ).generate_clickhouse_sql()
        return substitute_params(sql, context.values)

    @staticmethod
    def _branches(sql: str) -> list[str]:
        start = sql.index("UNION ALL")
        union_start = sql.rindex("(SELECT", 0, sql.rindex("FROM events", 0, start))
        union_end = sql.index(") AS ", sql.rindex("FROM flag_evaluations"))
        return sql[union_start:union_end].split("UNION ALL")

    @parameterized.expand(
        [
            ("mixed_query", f"SELECT count() FROM events WHERE {MIXED}", True, True),
            ("mixed_query_with_the_flag_off", f"SELECT count() FROM events WHERE {MIXED}", False, False),
            (
                "query_phase_1_swaps",
                "SELECT count() FROM events WHERE event = '$feature_flag_called' "
                "AND properties.$feature_flag = 'f' AND timestamp >= now() - INTERVAL 7 DAY",
                True,
                False,
            ),
            # The CTE shadows events, so the query reads the CTE on every mode.
            (
                "cte_named_events",
                f"WITH events AS (SELECT '$pageview' AS event) SELECT count() FROM events WHERE {MIXED}",
                True,
                False,
            ),
            param(
                "cte_named_events_on_mode_2",
                f"WITH events AS (SELECT '$pageview' AS event) SELECT count() FROM events WHERE {MIXED}",
                True,
                False,
                mode=ONLY,
            ),
        ]
    )
    def test_union_reads_the_scans_phase_1_leaves_on_events(
        self, _name: str, query: str, union: bool, expected_union: bool, mode: FlagEvaluationsMode = READ
    ):
        sql = self._sql(query, union=union, mode=mode)

        assert ("UNION ALL" in sql) is expected_union
        if expected_union:
            assert re.search(r"FROM flag_evaluations\b", sql) is not None

    @parameterized.expand(
        [
            (
                "right_join",
                f"SELECT count() FROM events AS e RIGHT JOIN persons AS p ON e.person_id = p.id WHERE {E_MIXED}",
                "RIGHT JOIN",
            ),
            (
                "field_flag_evaluations_lacks",
                f"SELECT goe_0.properties.plan FROM events WHERE {MIXED}",
                "goe_0.properties",
            ),
            param(
                "join_whose_on_clause_reads_another_field",
                f"SELECT probe_join.key FROM events WHERE {MIXED}",
                "probe_join",
                warehouse_join=True,
            ),
        ]
    )
    def test_mode_1_reads_events_and_mode_2_raises_when_the_union_cannot_read_a_mixed_scan(
        self, _name: str, query: str, reason: str, warehouse_join: bool = False
    ):
        if warehouse_join:
            # Joined on persons, it reaches events as a join that compares `person.properties.email`, not its key.
            DataWarehouseJoin.objects.create(
                team=self.team,
                source_table_name="persons",
                source_table_key="properties.email",
                joining_table_name="groups",
                joining_table_key="key",
                field_name="probe_join",
            )
        persons_on_events = PersonsOnEventsMode.PERSON_ID_OVERRIDE_PROPERTIES_ON_EVENTS

        assert "UNION ALL" not in self._sql(query, persons_on_events=persons_on_events)
        with pytest.raises(QueryError, match=reason):
            self._sql(query, mode=ONLY, persons_on_events=persons_on_events)

    def test_query_cannot_name_the_union_table(self):
        with pytest.raises(QueryError, match="Unknown table `posthog.events_with_flag_calls`"):
            self._sql(f"SELECT count() FROM {UNION}")

    @parameterized.expand(
        [
            ("mode_1", READ, False),
            # Mode 2 reads every flag call from flag_evaluations, so it never asks events for the flag key.
            ("mode_2", ONLY, True),
        ]
    )
    def test_restricted_flag_key_keeps_mode_1_on_events(self, _name: str, mode: FlagEvaluationsMode, expected: bool):
        _restrict(self.organization, self.team, "$feature_flag")

        assert ("UNION ALL" in self._sql(f"SELECT count() FROM events WHERE {MIXED}", mode=mode)) is expected

    def test_funnel_step_on_one_flag_key_reads_only_that_key_and_skips_keyless_calls(self):
        query = FunnelsQuery(
            dateRange=DateRange(date_from="2026-10-01", date_to="2026-10-20"),
            series=[
                EventsNode(event="$pageview"),
                EventsNode(
                    event="$feature_flag_called",
                    properties=[
                        EventPropertyFilter(key="$feature_flag", value="probe-flag", operator=PropertyOperator.EXACT)
                    ],
                ),
            ],
        )
        sql = self._sql(FunnelsQueryRunner(query=query, team=self.team).to_query())

        events, flag_evaluations = self._branches(sql)
        for branch in (events, flag_evaluations):
            assert "2026-10-01" in branch and "2026-10-20" in branch
        assert "in(flag_evaluations.flag_key, tuple('probe-flag'))" in flag_evaluations
        assert "in(events.event, tuple('$feature_flag_called', '$pageview'))" in events

    @parameterized.expand(
        [
            ("no_flag_key_filter", "", None),
            # A Boolean type converts the key before the comparison, so 'TRUE' also matches the JSON value true.
            ("boolean_flag_key", " AND properties.$feature_flag = 'TRUE'", "Boolean"),
        ]
    )
    def test_query_without_a_string_flag_key_reads_keyless_calls_from_events(
        self, _name: str, flag_key_filter: str, flag_key_type: str | None
    ):
        if flag_key_type is not None:
            PropertyDefinition.objects.create(
                team=self.team, name="$feature_flag", property_type=flag_key_type, type=PropertyDefinition.Type.EVENT
            )

        events, keyless, flag_evaluations = self._branches(
            self._sql(f"SELECT count() FROM events WHERE {MIXED} AND distinct_id = 'probe'{flag_key_filter}")
        )

        for branch in (events, keyless):
            assert "equals(events.distinct_id, 'probe')" in branch
        assert "equals(flag_evaluations.distinct_id, 'probe')" in flag_evaluations
        assert "flag_key" not in flag_evaluations

    @parameterized.expand(
        [
            ("array_join", "arrayJoin(['a', 'b']) = 'a'", "arrayJoin"),
            ("random", "rand() % 10 = 0", "rand"),
            ("row_number", "rowNumberInBlock() < 5", "rowNumberInBlock"),
        ]
    )
    def test_filter_that_depends_on_each_read_stays_out_of_the_branches(self, _name: str, condition: str, name: str):
        sql = self._sql(f"SELECT count() FROM events WHERE {MIXED} AND {condition}")

        assert all(name not in branch for branch in self._branches(sql))
        assert name in sql

    def test_sample_one_adds_no_warning(self):
        _set_mode(self.organization, READ)
        with _enabled_flags(union=True):
            _, context = HogQLQueryExecutor(
                query=f"SELECT count() FROM events SAMPLE 1 WHERE {MIXED}", team=self.team
            ).generate_clickhouse_sql()

        assert context.warnings == []

    def test_sessions_join_keeps_its_time_bound(self):
        sql = self._sql(
            f"SELECT session.$session_duration FROM events WHERE {MIXED} "
            "AND timestamp >= toDateTime('2026-10-01') AND timestamp < toDateTime('2026-10-08')"
        )

        sessions = sql[sql.index("FROM raw_sessions") : sql.index("AS events__session")]
        assert "toDateTime('2026-10-01', 'UTC')" in sessions and "toDateTime('2026-10-08', 'UTC')" in sessions

    def test_persons_join_runs_one_override_join(self):
        sql = self._sql(
            f"SELECT person_id, person.properties.email FROM events WHERE {MIXED}",
            persons_on_events=PersonsOnEventsMode.PERSON_ID_OVERRIDE_PROPERTIES_JOINED,
        )

        assert sql.count("FROM person_distinct_id_overrides") == 2  # one inner, one outer for `person_id`


class TestFlagCallsUnionRows(ClickhouseTestMixin, APIBaseTest):
    def setUp(self):
        super().setUp()
        now = datetime.now(UTC)
        self.recent = _create_person(distinct_ids=["recent"], team_id=self.team.pk, properties={"email": "r@x.com"})
        self.old = _create_person(distinct_ids=["old"], team_id=self.team.pk, properties={"email": "o@x.com"})
        for distinct_id, person, days_ago in (("recent", self.recent, 3), ("old", self.old, 100)):
            _create_event(
                event="$pageview",
                distinct_id=distinct_id,
                person_id=person.uuid,
                person_properties={"email": person.properties["email"]},
                team=self.team,
                timestamp=now - timedelta(days=days_ago),
            )
        # Ingestion writes a keyed flag call to both tables on mode 1. Only flag_evaluations has the stale person,
        # which an override corrects.
        flag_call_uuid = uuid.uuid4()
        _create_event(
            event_uuid=flag_call_uuid,
            event="$feature_flag_called",
            distinct_id="recent",
            person_id=self.recent.uuid,
            person_properties={"email": "r@x.com"},
            team=self.team,
            timestamp=now - timedelta(days=2),
            properties={"$feature_flag": "probe-flag", "source": "events"},
        )
        _create_flag_evaluations(
            self.team.pk,
            "probe-flag",
            timestamp=now - timedelta(days=2),
            distinct_id="recent",
            person_id=uuid.uuid4(),
            person_properties={"email": "r@x.com"},
            properties={"source": "flag_evaluations"},
        )
        sync_execute(
            "INSERT INTO person_distinct_id_overrides (team_id, distinct_id, person_id, version) VALUES",
            [(self.team.pk, "recent", str(self.recent.uuid), 1)],
        )
        _create_event(
            event="$feature_flag_called",
            distinct_id="old",
            person_id=self.old.uuid,
            team=self.team,
            timestamp=now - timedelta(days=99),
            properties={"$feature_flag": "probe-flag", "source": "events"},
        )
        for distinct_id, flag_key in (("missing", None), ("empty", ""), ("number", 100)):
            _create_event(
                event="$feature_flag_called",
                distinct_id=distinct_id,
                team=self.team,
                timestamp=now - timedelta(days=1),
                properties={"source": "events", **({} if flag_key is None else {"$feature_flag": flag_key})},
            )
        flush_persons_and_events()

    def _run(self, query: str, mode: FlagEvaluationsMode, persons_on_events: PersonsOnEventsMode | None = None):
        _set_mode(self.organization, mode)
        with _enabled_flags(union=True):
            return execute_hogql_query(
                query, team=self.team, modifiers=HogQLQueryModifiers(personsOnEventsMode=persons_on_events)
            )

    @parameterized.expand(
        [
            (f"{mode.name.lower()}_{poe.name.lower()}", mode, poe)
            for mode in (FlagEvaluationsMode.READ_FLAG_EVALUATIONS, FlagEvaluationsMode.FLAG_EVALUATIONS_ONLY)
            for poe in (
                PersonsOnEventsMode.PERSON_ID_OVERRIDE_PROPERTIES_ON_EVENTS,
                PersonsOnEventsMode.PERSON_ID_OVERRIDE_PROPERTIES_JOINED,
            )
        ]
    )
    def test_each_flag_call_comes_from_one_table(self, _name: str, mode: FlagEvaluationsMode, poe: PersonsOnEventsMode):
        response = self._run(
            f"""
            SELECT distinct_id, event, properties.source, person_id, person.properties.email
            FROM events
            WHERE {MIXED} AND timestamp >= now() - INTERVAL 120 DAY
            ORDER BY timestamp, distinct_id
            """,
            mode,
            poe,
        )

        recent_id, old_id = str(self.recent.uuid), str(self.old.uuid)
        history = [
            ("old", "$pageview", None, old_id, "o@x.com"),
            ("old", "$feature_flag_called", "events", old_id, "o@x.com"),
        ]
        recent = [
            ("recent", "$pageview", None, recent_id, "r@x.com"),
            ("recent", "$feature_flag_called", "flag_evaluations", recent_id, "r@x.com"),
        ]
        keyless = [(distinct_id, "$feature_flag_called", "events") for distinct_id in ("empty", "missing", "number")]
        rows = [
            tuple(str(value) if isinstance(value, uuid.UUID) else value for value in row) for row in response.results
        ]
        if mode == FlagEvaluationsMode.FLAG_EVALUATIONS_ONLY:
            assert rows == [history[0], *recent]
        else:
            assert rows[:4] == [*history, *recent]
            assert [row[:3] for row in rows[4:]] == keyless

    def test_prewhere_reads_both_tables(self):
        response = self._run(
            f"SELECT count() FROM events PREWHERE {MIXED} WHERE timestamp >= now() - INTERVAL 120 DAY",
            FlagEvaluationsMode.READ_FLAG_EVALUATIONS,
        )

        assert response.results == [(7,)]

    @parameterized.expand([("json",), ("materialized",)])
    def test_keyless_flag_calls_come_from_events(self, read: str):
        # A materialized $feature_flag column reads an empty string as missing.
        if read == "materialized":
            self.addCleanup(cleanup_materialized_columns)
            materialize("events", "$feature_flag")
        response = self._run(
            f"SELECT distinct_id FROM events WHERE {MIXED} AND event = '$feature_flag_called' "
            "AND timestamp >= now() - INTERVAL 5 DAY ORDER BY distinct_id",
            FlagEvaluationsMode.READ_FLAG_EVALUATIONS,
        )

        assert response.results == [("empty",), ("missing",), ("number",), ("recent",)]

    def test_query_on_one_flag_key_keeps_every_call_of_it(self):
        response = self._run(
            "SELECT distinct_id, event, properties.source FROM events WHERE event = '$pageview' "
            "OR (event = '$feature_flag_called' AND properties.$feature_flag = 'probe-flag') ORDER BY timestamp",
            FlagEvaluationsMode.READ_FLAG_EVALUATIONS,
        )

        assert response.results == [
            ("old", "$pageview", None),
            ("old", "$feature_flag_called", "events"),
            ("recent", "$pageview", None),
            ("recent", "$feature_flag_called", "flag_evaluations"),
        ]

    def test_restricted_property_is_hidden_in_every_branch(self):
        _restrict(self.organization, self.team, "source")
        query = f"SELECT properties.source FROM events WHERE {MIXED}"

        assert {row[0] for row in self._run(query, FlagEvaluationsMode.READ_FLAG_EVALUATIONS).results} == {None}
        assert (
            self._run(f"{query} AND properties.source = 'events'", FlagEvaluationsMode.READ_FLAG_EVALUATIONS).results
            == []
        )

    @parameterized.expand(
        [
            (f"{poe.name.lower()}_{'events_json' if new_schema else 'events'}", poe, new_schema)
            for poe in (
                PersonsOnEventsMode.PERSON_ID_NO_OVERRIDE_PROPERTIES_ON_EVENTS,
                PersonsOnEventsMode.PERSON_ID_OVERRIDE_PROPERTIES_JOINED,
            )
            for new_schema in (False, True)
        ]
    )
    def test_every_events_field_reads_the_same_type_or_keeps_events(
        self, _name: str, poe: PersonsOnEventsMode, new_schema: bool
    ):
        _set_mode(self.organization, READ)
        create_group_type_mapping(team=self.team, group_type="organization", group_type_index=0)
        # A typed property reads as a number, which the union keeps only when it types each branch.
        for property_type in (PropertyDefinition.Type.EVENT, PropertyDefinition.Type.PERSON):
            PropertyDefinition.objects.create(team=self.team, name="probe", property_type="Numeric", type=property_type)
        modifiers = HogQLQueryModifiers(personsOnEventsMode=poe, useNewEventsSchema=new_schema)
        with _enabled_flags(union=True):
            union_database = Database.create_for(team=self.team, modifiers=modifiers)
        with _enabled_flags(union=False):
            events_database = Database.create_for(team=self.team, modifiers=modifiers)

        def sql(query: str, database: Database) -> str:
            context = HogQLContext(
                team_id=self.team.pk, team=self.team, database=database, modifiers=modifiers, enable_select_queries=True
            )
            return prepare_and_print_ast(parse_select(query), context, "clickhouse")[0]

        accepted = []
        for chain in _readable_chains(union_database):
            name = ".".join(f"`{part}`" for part in chain)
            try:
                sql(f"SELECT {name} FROM events", events_database)
            except QueryError:
                continue
            # A field the union cannot read keeps the query on events instead of failing it.
            if "UNION ALL" in sql(f"SELECT {name} FROM events WHERE {MIXED}", union_database):
                accepted.append(name)

        assert {
            "`timestamp`",
            "`properties`.`probe`",
            "`person_id`",
            "`person`.`properties`.`probe`",
            "`$session_id`",
            "`elements_chain_texts`",
            "`$group_0`",
            # The sessions join reads `$session_id_uuid`, which is not its key.
            "`session`.`id`",
        } <= set(accepted)
        types: dict[bool, dict[str, str]] = {False: {}, True: {}}
        # One query per few fields keeps each union within the memory of a local ClickHouse.
        for offset in range(0, len(accepted), 8):
            names = accepted[offset : offset + 8]
            types_query = f"SELECT {', '.join(f'toTypeName(any({name}))' for name in names)} FROM events WHERE {MIXED}"
            for union in (False, True):
                with _enabled_flags(union=union):
                    response = execute_hogql_query(types_query, team=self.team, modifiers=modifiers)
                types[union].update(zip(names, response.results[0]))

        # flag_evaluations has no elements, and its empty Array(String) widens the union of the events Enum8 array.
        # Comparisons with string literals behave the same on both types.
        widened = {"`elements_chain_elements`": "Array(String)"}
        assert types[True] == {**types[False], **widened}


def _readable_chains(database: Database) -> list[list[str | int]]:
    """A chain to select for each events field, and for one field behind each of its joins."""
    events = database.get_table("events")
    chains: list[list[str | int]] = []
    for name, field in events.fields.items():
        if isinstance(field, (LazyJoin, FieldTraverser)):
            chains.extend([[name, "id"], [name, "properties", "probe"], [name, "person_id"]])
            continue
        inner_fields = field.fields.items() if isinstance(field, VirtualTable) else [(None, field)]
        for inner_name, inner in inner_fields:
            chain: list[str | int] = [name] if inner_name is None else [name, inner_name]
            if isinstance(inner, StringJSONDatabaseField):
                chains.append([*chain, "probe"])
            elif isinstance(inner, DatabaseField):
                chains.append(chain)
    return chains
