from __future__ import annotations

import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from tests.helpers import TemporaryCase
from watcher.case_config import inspect_case, parse_foam_file


class CaseInspectionTests(TestCase):
    def test_detects_simple_case_and_residual_control(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "system/controlDict",
                "application simpleFoam;\nstartTime 0;\nendTime 500;\ndeltaT 1;\n",
            )
            case.write(
                "system/fvSolution",
                """
                SIMPLE
                {
                    nNonOrthogonalCorrectors 1;
                    residualControl
                    {
                        U 1e-5;
                        "(k|omega)" 1e-4;
                    }
                }
                """,
            )
            result = inspect_case(case.path)

        self.assertEqual(result.application, "simpleFoam")
        self.assertEqual(result.mode, "steady_simple")
        self.assertEqual(result.mode_confidence, "high")
        self.assertEqual(result.end_time, 500.0)
        self.assertEqual(
            [(item.pattern, item.threshold) for item in result.residual_targets],
            [("U", 1e-5), ("(k|omega)", 1e-4)],
        )

    def test_reads_dictionary_form_residual_tolerance(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            case.write(
                "system/fvSolution",
                "SIMPLE { residualControl { U { tolerance 1e-5; relTol 0; } } }\n",
            )
            result = inspect_case(case.path)

        self.assertEqual(
            [(item.pattern, item.threshold) for item in result.residual_targets],
            [("U", 1e-5)],
        )

    def test_detects_transient_pimple_and_included_functions(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "system/controlDict",
                """
                application pimpleFoam;
                endTime 20;
                deltaT 0.01;
                adjustTimeStep yes;
                maxCo 0.8;
                #include "functions.inc"
                """,
            )
            case.write(
                "system/functions.inc",
                "functions { lift { type forceCoeffs; patches (body); } }\n",
            )
            case.write("system/fvSolution", "PIMPLE { nOuterCorrectors 3; }\n")
            result = inspect_case(case.path)

        self.assertEqual(result.mode, "transient_pimple")
        self.assertTrue(result.adjust_time_step)
        self.assertEqual(result.max_co, 0.8)
        self.assertEqual(result.function_objects["lift"].type_name, "forceCoeffs")
        self.assertEqual(result.function_objects["lift"].fields, ())

    def test_strips_comments_without_changing_quoted_regex_keys(self) -> None:
        with TemporaryCase() as case:
            config = case.write(
                "system/controlDict",
                """
                // leading comment
                application simpleFoam; /* end-of-line comment */
                "a//b" retained; // this comment must disappear
                "(p|U)" { value 1; }
                """,
            )
            parsed, notices = parse_foam_file(config, case.path)

        self.assertEqual(notices, ())
        self.assertEqual(parsed["application"], "simpleFoam")
        self.assertEqual(parsed["a//b"], "retained")
        self.assertEqual(parsed["(p|U)"], {"value": 1.0})

    def test_preserves_token_boundary_across_block_comment(self) -> None:
        with TemporaryCase() as case:
            config = case.write(
                "system/controlDict",
                "application/* a comment */simpleFoam;\n",
            )
            parsed, _ = parse_foam_file(config, case.path)

        self.assertEqual(parsed["application"], "simpleFoam")

    def test_keeps_double_slash_after_an_escaped_quote(self) -> None:
        with TemporaryCase() as case:
            config = case.write(
                "system/controlDict",
                'label "a\\"//b";\nafter retained;\n',
            )
            parsed, _ = parse_foam_file(config, case.path)

        self.assertEqual(parsed["after"], "retained")

    def test_does_not_follow_dynamic_code_directives(self) -> None:
        with TemporaryCase() as case:
            config = case.write(
                "system/controlDict",
                "application simpleFoam;\n#codeStream { code #{ ignored; #}; }\nendTime 4;\n",
            )
            parsed, notices = parse_foam_file(config, case.path)

        self.assertEqual(parsed["application"], "simpleFoam")
        self.assertEqual(parsed["endTime"], 4.0)
        self.assertTrue(any("#codeStream" in notice for notice in notices))

    def test_refuses_include_outside_case_and_project_directory(self) -> None:
        with TemporaryCase() as case:
            outside = case.path.parent / "outside.inc"
            outside.write_text("leaked yes;", encoding="utf-8")
            config = case.write("system/controlDict", '#include "../../outside.inc"\n')
            with patch.dict(os.environ, {}, clear=True):
                parsed, notices = parse_foam_file(config, case.path)

        self.assertNotIn("leaked", parsed)
        self.assertTrue(any("outside" in notice.lower() for notice in notices))

    def test_counts_parallel_regions_and_detects_piso(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "application pisoFoam;\n")
            case.write("system/fvSolution", "PISO { nCorrectors 2; }\n")
            case.write("constant/regionProperties", "regions (fluid solid);\n")
            case.mkdir("processor0")
            case.mkdir("processor3")
            case.mkdir("processorA")
            result = inspect_case(case.path)

        self.assertEqual(result.mode, "transient_piso")
        self.assertEqual(result.parallel_ranks, 2)
        self.assertTrue(result.multi_region)

    def test_does_not_classify_one_typed_region_as_multi_region(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            case.write("constant/regionProperties", "regions ( fluid (fluid) );\n")
            result = inspect_case(case.path)

        self.assertFalse(result.multi_region)

    def test_uses_pseudo_transient_for_simple_with_transient_marker(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\ndeltaT 0.01;\n")
            case.write("system/fvSolution", "SIMPLE { nNonOrthogonalCorrectors 1; }\n")
            case.write("system/fvSchemes", "ddtSchemes { default Euler; }\n")
            result = inspect_case(case.path)

        self.assertEqual(result.mode, "pseudo_transient")

    def test_reports_malformed_input_without_crashing(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "application pimpleFoam;\nendTime bad-number;\n")
            case.write("system/fvSolution", "PIMPLE { residualControl { U 1e-5; ")
            result = inspect_case(case.path)

        self.assertEqual(result.application, "pimpleFoam")
        self.assertEqual(result.mode, "transient_pimple")
        self.assertIsNone(result.end_time)
        self.assertTrue(result.notices)

    def test_returns_an_immutable_function_object_mapping(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "functions { probe { type probes; } }\n")
            result = inspect_case(case.path)

        with self.assertRaises(TypeError):
            result.function_objects["other"] = result.function_objects["probe"]  # type: ignore[index]
