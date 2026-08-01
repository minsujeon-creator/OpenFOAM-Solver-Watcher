from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from urllib.request import urlopen

from tests.helpers import TemporaryCase
from watcher.server import create_server
from watcher.snapshot import WatcherCollector


EXPECTED_VIEWS = {
    "overview-view",
    "meshing-view",
    "mesh-quality-view",
    "residuals-view",
    "transient-view",
    "physical-view",
    "stationarity-view",
    "diagnostics-view",
}

REQUIRED_IDS = EXPECTED_VIEWS | {
    "access-scope",
    "application-version",
    "case-name",
    "card-numerics",
    "card-physical",
    "card-process",
    "card-progress",
    "connection-state",
    "diagnostic-log-body",
    "diagnostic-notices",
    "last-refresh",
    "live-region",
    "mode-label",
    "meshing-current-work",
    "meshing-mesh-body",
    "meshing-progress",
    "meshing-settings",
    "meshing-stage",
    "meshing-summary",
    "meshing-warning-list",
    "layer-coverage-body",
    "layer-coverage-summary",
    "layer-coverage-advisory",
    "mesh-quality-summary",
    "mesh-quality-facts",
    "mesh-quality-metric-body",
    "mesh-quality-problem-body",
    "mesh-quality-diagnostics",
    "overview-quantity-body",
    "overview-residual-body",
    "physical-chart",
    "refresh-button",
    "residual-chart",
    "residual-table-body",
    "series-row-template",
    "series-table-body",
    "stationarity-chart",
    "stationarity-table-body",
    "transient-chart",
    "transient-table-body",
    "view-tabs",
}


class DashboardHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.external_assets: list[str] = []
        self.h1_count = 0
        self.live_regions = 0
        self.main_count = 0
        self.tablist_count = 0
        self.tabs: dict[str, dict[str, str | None]] = {}
        self.tabpanels: dict[str, dict[str, str | None]] = {}
        self.table_headers: set[str] = set()
        self.checkbox_count = 0
        self.unlabelled_checkboxes = 0
        self.labelled_forms = 0
        self.local_scripts: list[str] = []
        self.local_stylesheets: list[str] = []
        self.inline_script_count = 0
        self.inline_style_count = 0
        self._label_depth = 0
        self._current_header: list[str] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.ids.add(element_id)
        if tag == "h1":
            self.h1_count += 1
        if tag == "main":
            self.main_count += 1
        if values.get("aria-live") == "polite":
            self.live_regions += 1
        if values.get("role") == "tablist":
            self.tablist_count += 1
        if tag == "button" and values.get("role") == "tab" and element_id:
            self.tabs[element_id] = values
        if values.get("role") == "tabpanel" and element_id:
            self.tabpanels[element_id] = values
        if tag == "label":
            self._label_depth += 1
        if tag == "input" and values.get("type") == "checkbox":
            self.checkbox_count += 1
            if self._label_depth == 0 and not values.get("aria-label"):
                self.unlabelled_checkboxes += 1
        if tag == "form" and (
            values.get("aria-label") or values.get("aria-labelledby")
        ):
            self.labelled_forms += 1
        if tag == "th" and values.get("scope") == "col":
            self._current_header = []
        if tag == "script":
            source = values.get("src")
            if source:
                self.local_scripts.append(source)
            else:
                self.inline_script_count += 1
        if tag == "style":
            self.inline_style_count += 1
        if (
            tag == "link"
            and "stylesheet" in (values.get("rel") or "").split()
            and values.get("href")
        ):
            self.local_stylesheets.append(values["href"])

        source = values.get("src") or values.get("href")
        if source and source.startswith(("http://", "https://", "//")):
            self.external_assets.append(source)

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self._label_depth -= 1
        if tag == "th" and self._current_header is not None:
            label = " ".join("".join(self._current_header).split())
            if label:
                self.table_headers.add(label)
            self._current_header = None

    def handle_data(self, data: str) -> None:
        if self._current_header is not None:
            self._current_header.append(data)


class StaticContractTests(TestCase):
    def setUp(self) -> None:
        self.case = TemporaryCase()
        self.case.__enter__()
        self.case.write(
            "system/controlDict",
            "application pimpleFoam; startTime 0; endTime 1; deltaT 0.1;\n",
        )
        self.case.write("system/fvSolution", "PIMPLE { nOuterCorrectors 2; }\n")
        self.server = create_server(self.case.path, 0)
        self.port = self.server.server_address[1]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.case.__exit__()

    def get(self, path: str):
        return urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=2)

    def test_served_dashboard_has_accessible_views_and_local_assets(self) -> None:
        response = self.get("/")
        parser = DashboardHTMLParser()
        parser.feed(response.read().decode("utf-8"))

        self.assertEqual(set(parser.tabpanels), EXPECTED_VIEWS)
        self.assertTrue(REQUIRED_IDS.issubset(parser.ids))
        self.assertEqual(parser.h1_count, 1)
        self.assertEqual(parser.main_count, 1)
        self.assertEqual(parser.tablist_count, 1)
        self.assertGreaterEqual(parser.live_regions, 1)
        self.assertEqual(parser.external_assets, [])
        self.assertEqual(parser.local_scripts, ["app.js"])
        self.assertEqual(parser.local_stylesheets, ["styles.css"])
        self.assertEqual(parser.inline_script_count, 0)
        self.assertEqual(parser.inline_style_count, 0)
        self.assertEqual(
            response.headers["Content-Security-Policy"].split(";")[0],
            "default-src 'self'",
        )

    def test_tabs_and_tables_expose_semantic_control_relationships(self) -> None:
        parser = DashboardHTMLParser()
        parser.feed(self.get("/").read().decode("utf-8"))

        self.assertEqual(len(parser.tabs), 8)
        for tab_id, attributes in parser.tabs.items():
            controlled = attributes.get("aria-controls")
            self.assertIn(controlled, EXPECTED_VIEWS)
            self.assertEqual(
                parser.tabpanels[controlled].get("aria-labelledby"),
                tab_id,
            )
            self.assertIn(attributes.get("aria-selected"), {"true", "false"})

        self.assertGreaterEqual(parser.checkbox_count, 1)
        self.assertEqual(parser.unlabelled_checkboxes, 0)
        self.assertGreaterEqual(parser.labelled_forms, 1)
        self.assertTrue(
            {
                "Field",
                "Initial residual",
                "Time",
                "Courant max",
                "Quantity",
                "State",
                "Source",
                "Candidate",
                "Reason",
                "Cells",
                "Observed",
                "Requested layers",
                "Average layers",
            }.issubset(parser.table_headers)
        )

    def test_frontend_assets_are_served_with_expected_media_types(self) -> None:
        self.assertIn("text/css", self.get("/styles.css").headers["Content-Type"])
        self.assertIn("javascript", self.get("/app.js").headers["Content-Type"])

    def test_stylesheet_references_only_defined_custom_properties(self) -> None:
        stylesheet = (Path(__file__).parents[1] / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        definitions = set(re.findall(r"(--[a-z0-9-]+)\s*:", stylesheet, re.IGNORECASE))
        references = set(re.findall(r"var\((--[a-z0-9-]+)", stylesheet, re.IGNORECASE))

        self.assertEqual(references - definitions, set())

    def test_demo_case_exercises_every_dashboard_data_view(self) -> None:
        from tests.demo_case import generate_demo_case

        with TemporaryDirectory() as directory:
            case_dir = Path(directory) / "demo"
            generate_demo_case(case_dir)
            snapshot = WatcherCollector(case_dir).snapshot()

        self.assertEqual(snapshot["case"]["mode"], "transient_pimple")
        self.assertTrue(snapshot["solver"]["residuals"])
        self.assertTrue(snapshot["solver"]["timeSteps"])
        self.assertEqual(snapshot["numerics"]["kind"], "transient_health")
        self.assertGreaterEqual(len(snapshot["seriesCatalog"]), 3)
        self.assertTrue(snapshot["configuration"]["selectedSeries"])
        self.assertTrue(snapshot["physical"]["results"])
        self.assertTrue(snapshot["logSelection"]["candidates"])
        self.assertTrue(snapshot["notices"])
        json.dumps(snapshot, allow_nan=False)

    def test_snappy_demo_case_exercises_meshing_dashboard_model(self) -> None:
        from tests.demo_case import generate_snappy_demo_case

        with TemporaryDirectory() as directory:
            case_dir = Path(directory) / "snappy-demo"
            generate_snappy_demo_case(case_dir)
            snapshot = WatcherCollector(case_dir).snapshot()

        self.assertEqual(snapshot["workflow"]["kind"], "snappy_hex_mesh")
        self.assertEqual(snapshot["meshing"]["stage"], "snapping")
        self.assertEqual(snapshot["meshing"]["activeMorphIteration"], 14)
        self.assertEqual(snapshot["meshing"]["meshCells"], 8_903_300)
        self.assertTrue(snapshot["meshing"]["maxGlobalCellsReached"])
        self.assertEqual(snapshot["meshing"]["layerCoverage"]["reportedPatchCount"], 2)
        json.dumps(snapshot, allow_nan=False)


if __name__ == "__main__":
    import unittest

    unittest.main()
