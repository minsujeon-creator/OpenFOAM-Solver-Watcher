from __future__ import annotations

import os
from unittest import TestCase

from tests.helpers import TemporaryCase
from watcher.case_config import inspect_case
from watcher.postprocessing import discover_series, parse_numeric_table


class PostProcessingDiscoveryTests(TestCase):
    def test_partial_row_is_ignored_and_later_restart_wins(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "postProcessing/roomAverage/0/volFieldValue.dat",
                "# Time volAverage(T)\n0 300\n1 302\n2\n",
            )
            case.write(
                "postProcessing/roomAverage/1/volFieldValue.dat",
                "# Time volAverage(T)\n1 305\n2 306\n",
            )
            found = discover_series(inspect_case(case.path), now=0)

        temperature = next(item for item in found.values() if item.field == "T")
        self.assertEqual(temperature.times, (0.0, 1.0, 2.0))
        self.assertEqual(temperature.values, (300.0, 305.0, 306.0))

    def test_unknown_vector_table_produces_components_and_magnitude(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "postProcessing/customProbe/0/data.dat",
                "# Time customVector\n0 (1 2 2)\n1 (2 3 6)\n",
            )
            found = discover_series(inspect_case(case.path), now=0)

        components = {item.component: item for item in found.values()}
        self.assertEqual(components["x"].values, (1.0, 2.0))
        self.assertEqual(components["magnitude"].values, (3.0, 7.0))

    def test_composite_scalar_and_vector_headers_keep_both_fields(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "postProcessing/composite/0/data.dat",
                "# Time p U\n0 1 (2 3 6)\n",
            )
            found = tuple(discover_series(inspect_case(case.path), now=0).values())

        series = {(item.field, item.component): item for item in found}
        self.assertEqual(series[("p", None)].values, (1.0,))
        self.assertEqual(series[("U", "x")].values, (2.0,))
        self.assertEqual(series[("U", "magnitude")].values, (7.0,))

    def test_force_and_moment_layout_produces_named_component_series(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "postProcessing/loads/0/forces.dat",
                "# Time forces(pressure viscous porous) moments(pressure viscous porous)\n"
                "0 ((1 2 3) (4 5 6) (7 8 9)) ((10 11 12) (13 14 15) (16 17 18))\n",
            )
            found = tuple(discover_series(inspect_case(case.path), now=0).values())

        series = {(item.field, item.component): item for item in found}
        self.assertEqual(series[("force pressure", "x")].values, (1.0,))
        self.assertEqual(series[("force porous", "z")].values, (9.0,))
        self.assertEqual(series[("moment pressure", "x")].values, (10.0,))
        self.assertEqual(series[("moment porous", "z")].values, (18.0,))

    def test_incompatible_restart_table_cannot_overwrite_vector_series(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "postProcessing/velocity/0/data.dat",
                "# Time U\n0 (1 2 2)\n1 (2 3 6)\n",
            )
            case.write("postProcessing/velocity/1/data.dat", "# Time U\n1 99\n")
            found = tuple(discover_series(inspect_case(case.path), now=0).values())

        series = {item.component: item for item in found}
        self.assertEqual(series["x"].values, (1.0, 2.0))
        self.assertEqual(series["magnitude"].values, (3.0, 7.0))

    def test_wider_later_restart_shape_supersedes_initial_truncated_segment(self) -> None:
        with TemporaryCase() as case:
            case.write("postProcessing/velocity/0/data.dat", "# Time U\n0 99\n")
            case.write(
                "postProcessing/velocity/1/data.dat",
                "# Time U\n1 (1 2 2)\n2 (2 3 6)\n",
            )
            found = tuple(discover_series(inspect_case(case.path), now=0).values())

        series = {item.component: item for item in found}
        self.assertEqual(series["x"].times, (1.0, 2.0))
        self.assertEqual(series["x"].values, (1.0, 2.0))
        self.assertEqual(series["magnitude"].values, (3.0, 7.0))

    def test_force_and_coefficient_layouts_are_high_priority_candidates(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "system/controlDict",
                "functions { loads { type forces; } aero { type forceCoeffs; } }",
            )
            case.write(
                "postProcessing/loads/0/forces.dat",
                "# Time forces\n0 ((1 2 3) (4 5 6) (7 8 9))\n",
            )
            case.write(
                "postProcessing/aero/0/coefficient.dat",
                "# Time Cd Cl CmPitch\n0 0.1 0.2 0.3\n",
            )
            found = tuple(discover_series(inspect_case(case.path), now=0).values())

        self.assertTrue(any(item.function_name == "loads" and item.component == "x" for item in found))
        coefficient = next(item for item in found if item.field == "Cd")
        self.assertEqual(coefficient.values, (0.1,))
        self.assertTrue(coefficient.candidate.recommended)
        self.assertEqual(coefficient.candidate.score, 80)

    def test_generic_force_header_uses_force_vector_layout(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "postProcessing/loads/0/forces.dat",
                "# Time forces(pressure viscous porous)\n0 ((1 2 3) (4 5 6) (7 8 9))\n",
            )
            found = tuple(discover_series(inspect_case(case.path), now=0).values())

        first_force = next(item for item in found if item.field == "force pressure" and item.component == "x")
        self.assertEqual(first_force.values, (1.0,))
        self.assertTrue(first_force.candidate.recommended)

    def test_probe_values_at_several_locations_remain_separate(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "functions { sample { type probes; fields (p); } }")
            case.write(
                "postProcessing/sample/0/p",
                "# Probe 0 (0 0 0)\n# Probe 1 (1 0 0)\n# Time p\n0 10 20\n1 11 21\n",
            )
            found = tuple(discover_series(inspect_case(case.path), now=0).values())

        probes = [item for item in found if item.field == "p"]
        self.assertEqual(len(probes), 2)
        self.assertEqual({item.component for item in probes}, {"probe 0", "probe 1"})
        self.assertTrue(all(item.candidate.score == 60 for item in probes))

    def test_surface_and_volume_headers_extract_operation_and_quoted_field(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "postProcessing/fields/0/value.dat",
                '# Time "areaAverage(p)" "volIntegrate(T)"\n0 101325 600\n',
            )
            found = tuple(discover_series(inspect_case(case.path), now=0).values())

        metadata = {(item.field, item.operation): item.values for item in found}
        self.assertEqual(metadata[("p", "areaAverage")], (101325.0,))
        self.assertEqual(metadata[("T", "volIntegrate")], (600.0,))

    def test_nonfinite_rows_are_removed_and_tensor_components_are_named(self) -> None:
        with TemporaryCase() as case:
            table = case.write(
                "postProcessing/stress/0/data.dat",
                "# Time stress\n0 (1 2 3 4 5 6 7 8 9)\n1 (nan 2 3 4 5 6 7 8 9)\n2 (1 2 inf 4 5 6 7 8 9)\n",
            )
            parsed = parse_numeric_table(table)
            found = tuple(discover_series(inspect_case(case.path), now=0).values())

        self.assertEqual(parsed.times, (0.0,))
        tensor = {item.component: item for item in found}
        self.assertEqual(tensor["xx"].values, (1.0,))
        self.assertEqual(tensor["zy"].values, (8.0,))

    def test_multi_region_path_and_staleness_are_reported(self) -> None:
        with TemporaryCase() as case:
            path = case.write("postProcessing/fluid/thermal/0/value.dat", "# Time volAverage(T)\n0 300\n")
            old_ns = 1_000_000_000
            os.utime(path, ns=(old_ns, old_ns))
            found = tuple(discover_series(inspect_case(case.path), now=3_000.0).values())

        item = found[0]
        self.assertEqual(item.region, "fluid")
        self.assertTrue(item.stale)
        self.assertEqual(item.candidate.score, 50)
        self.assertIn("stale", item.candidate.explanation.lower())

    def test_series_identifiers_are_stable_across_restart_directories(self) -> None:
        with TemporaryCase() as case:
            case.write("postProcessing/average/0/value.dat", "# Time volAverage(T)\n0 1\n")
            first = next(iter(discover_series(inspect_case(case.path), now=0).values()))
            case.write("postProcessing/average/2/value.dat", "# Time volAverage(T)\n2 3\n")
            second = next(iter(discover_series(inspect_case(case.path), now=0).values()))

        self.assertEqual(first.series_id, second.series_id)
        self.assertEqual(second.times, (0.0, 2.0))

    def test_generic_table_without_a_time_directory_keeps_its_filename_in_identity(self) -> None:
        with TemporaryCase() as case:
            case.write("postProcessing/summary.dat", "# Time power\n0 4\n")
            item = next(iter(discover_series(inspect_case(case.path), now=0).values()))

        self.assertEqual(item.source_relative, "postProcessing/summary.dat")
        self.assertEqual(item.function_name, "summary")

    def test_ranking_adds_sample_and_constant_factors(self) -> None:
        with TemporaryCase() as case:
            rows = "\n".join(f"{index} 1" for index in range(20))
            case.write("postProcessing/monitor/0/value.dat", f"# Time flowRate\n{rows}\n")
            item = next(iter(discover_series(inspect_case(case.path), now=0).values()))

        self.assertEqual(item.candidate.score, 70)
        self.assertTrue(item.candidate.recommended)
        self.assertIn("20 finite samples", item.candidate.explanation)
        self.assertIn("constant", item.candidate.explanation.lower())
