from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("owner_extract.py")
SPEC = importlib.util.spec_from_file_location("owner_extract", MODULE_PATH)
assert SPEC and SPEC.loader
owner_extract = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = owner_extract
SPEC.loader.exec_module(owner_extract)

try:
    from permuter import farm_config
except ImportError:  # run directly from this directory
    import farm_config


def tool(name: str) -> Path | None:
    """A PowerPC binutils program: ``tools.ppc_binutils`` / ``DECOMP_PPC_BINUTILS``, the
    project's ``build/binutils``, then PATH."""
    path = farm_config.binutil(farm_config.binutils_dir(), name)
    if path.is_file():
        return path
    discovered = shutil.which(f"powerpc-eabi-{name}")
    return Path(discovered) if discovered else None


def symlink(link: Path, target: Path, test: unittest.TestCase) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        test.skipTest("symbolic links are not permitted on this host")


class OwnerExtractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.assembler = tool("as")
        cls.objcopy = tool("objcopy")
        cls.readelf = tool("readelf")
        if not all((cls.assembler, cls.objcopy, cls.readelf)):
            raise unittest.SkipTest("PowerPC binutils are unavailable")

    def assemble(self, root: Path, name: str, source: str) -> Path:
        source_path = root / f"{name}.s"
        object_path = root / f"{name}.o"
        source_path.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [
                str(self.assembler),
                "-mgekko",
                "-mregnames",
                "-be",
                "-o",
                str(object_path),
                str(source_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return object_path

    @staticmethod
    def owner_source(
        *,
        target_nops: int = 0,
        before_value: int = 1,
        after_call: str = "ext_after",
        compiler_local: str = "@12",
        data_value: int = 0,
    ) -> str:
        nops = "\n".join("  nop" for _ in range(target_nops))
        return f"""
.text
.globl before
.type before,@function
before:
  li 3,{before_value}
  blr
.size before,.-before

.globl target
.type target,@function
target:
  bl ext_target
{nops}
  blr
.size target,.-target

.globl after
.type after,@function
after:
  bl {after_call}
  blr
.size after,.-after

.data
.local "{compiler_local}"
.type "{compiler_local}",@object
"{compiler_local}":
  .long {data_value}
.size "{compiler_local}",4

.globl owner_data
.type owner_data,@object
owner_data:
  .long target
  .long "{compiler_local}"
.size owner_data,8
"""

    def test_extracts_one_function_and_preserves_target_relocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self.assemble(root, "baseline", self.owner_source())
            candidate = self.assemble(
                root, "candidate", self.owner_source(target_nops=1)
            )
            output = root / "target.o"
            report = owner_extract.extract(
                baseline_path=baseline,
                candidate_path=candidate,
                function="target",
                output_path=output,
                objcopy=self.objcopy,
                readelf=self.readelf,
                expected_baseline_fingerprint=owner_extract.target_record(
                    owner_extract.read_elf(baseline),
                    owner_extract.find_target(owner_extract.read_elf(baseline), "target"),
                )["fingerprint_sha256"],
            )
            emitted = owner_extract.read_elf(output)
            target = owner_extract.find_target(emitted, "target")
            functions = [s.name for s in emitted.symbols if s.type == owner_extract.STT_FUNC]
            self.assertEqual(functions, ["target"])
            self.assertEqual(target.symbol.size, 12)
            relocations = owner_extract.target_relocations(emitted, target)
            self.assertEqual(len(relocations), 1)
            self.assertEqual(emitted.symbols[relocations[0].symbol_index].name, "ext_target")
            self.assertEqual(report["sibling_audit"]["post_target_shift"], 4)
            self.assertEqual(
                report["candidate"]["fingerprint_sha256"],
                report["output"]["fingerprint_sha256"],
            )
            self.assertEqual(
                report["candidate"]["topology"], report["output"]["topology"]
            )

    def test_rejects_sibling_text_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self.assemble(root, "baseline", self.owner_source())
            candidate = self.assemble(
                root, "candidate", self.owner_source(before_value=2)
            )
            with self.assertRaisesRegex(owner_extract.ExtractError, "text before"):
                owner_extract.extract(
                    baseline_path=baseline,
                    candidate_path=candidate,
                    function="target",
                    output_path=root / "target.o",
                    objcopy=self.objcopy,
                    readelf=self.readelf,
                )

    def test_rejects_sibling_relocation_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self.assemble(root, "baseline", self.owner_source())
            candidate = self.assemble(
                root, "candidate", self.owner_source(after_call="other_after")
            )
            with self.assertRaisesRegex(owner_extract.ExtractError, "sibling relocation"):
                owner_extract.extract(
                    baseline_path=baseline,
                    candidate_path=candidate,
                    function="target",
                    output_path=root / "target.o",
                    objcopy=self.objcopy,
                    readelf=self.readelf,
                )

    def test_compiler_local_renumber_uses_stable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self.assemble(
                root, "baseline", self.owner_source(compiler_local="@12")
            )
            candidate = self.assemble(
                root, "candidate", self.owner_source(compiler_local="@34")
            )
            report = owner_extract.extract(
                baseline_path=baseline,
                candidate_path=candidate,
                function="target",
                output_path=root / "target.o",
                objcopy=self.objcopy,
                readelf=self.readelf,
            )
            self.assertEqual(report["sibling_audit"]["state"], "passed")

    def test_non_ascii_digits_are_named_symbols_not_compiler_locals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self.assemble(
                root, "baseline", self.owner_source(compiler_local="@١")
            )
            candidate = self.assemble(
                root, "candidate", self.owner_source(compiler_local="@２")
            )
            with self.assertRaises(owner_extract.ExtractError):
                owner_extract.extract(
                    baseline_path=baseline,
                    candidate_path=candidate,
                    function="target",
                    output_path=root / "target.o",
                    objcopy=self.objcopy,
                    readelf=self.readelf,
                )

    def test_rejects_allocatable_data_state_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self.assemble(root, "baseline", self.owner_source(data_value=0))
            candidate = self.assemble(root, "candidate", self.owner_source(data_value=1))
            with self.assertRaisesRegex(owner_extract.ExtractError, "allocatable section"):
                owner_extract.extract(
                    baseline_path=baseline,
                    candidate_path=candidate,
                    function="target",
                    output_path=root / "target.o",
                    objcopy=self.objcopy,
                    readelf=self.readelf,
                )

    def test_rejects_target_relocation_to_sibling_definition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.owner_source().replace("bl ext_target", ".long after")
            owner = self.assemble(root, "owner", source)
            with self.assertRaisesRegex(
                owner_extract.ExtractError, "definition outside the target"
            ):
                owner_extract.extract(
                    baseline_path=owner,
                    candidate_path=owner,
                    function="target",
                    output_path=root / "target.o",
                    objcopy=self.objcopy,
                    readelf=self.readelf,
                )

    def test_rejects_target_symbol_topology_drift_without_sibling_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = """
.text
.globl before
.type before,@function
before:
  blr
.size before,.-before

.globl target
.type target,@function
target:
  bl ext_target
  blr
.size target,.-target

.globl after
.type after,@function
after:
  blr
.size after,.-after
"""
            baseline = self.assemble(root, "baseline", source)
            baseline_owner = owner_extract.read_elf(baseline)
            baseline_target = owner_extract.find_target(baseline_owner, "target")
            target_relocations = owner_extract.target_relocations(
                baseline_owner, baseline_target
            )
            self.assertEqual(len(target_relocations), 1)
            self.assertEqual(
                [
                    relocation
                    for relocation in baseline_owner.relocations
                    if relocation not in target_relocations
                ],
                [],
            )
            candidates = {
                "binding": source.replace(
                    ".globl target\n.type target,@function",
                    ".local target\n.type target,@function",
                ),
                "visibility": source.replace(
                    ".globl target\n.type target,@function",
                    ".globl target\n.hidden target\n.type target,@function",
                ),
            }
            for label, candidate_source in candidates.items():
                with self.subTest(label=label):
                    candidate = self.assemble(root, f"candidate-{label}", candidate_source)
                    with self.assertRaisesRegex(
                        owner_extract.ExtractError, "target symbol topology drifted"
                    ):
                        owner_extract.extract(
                            baseline_path=baseline,
                            candidate_path=candidate,
                            function="target",
                            output_path=root / f"target-{label}.o",
                            objcopy=self.objcopy,
                            readelf=self.readelf,
                        )

    def test_rejects_target_text_section_topology_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.owner_source().replace("  .long target\n", "  .long 0\n")
            path = self.assemble(root, "owner", source)
            baseline = owner_extract.read_elf(path)
            baseline_target = owner_extract.find_target(baseline, "target")
            changes = {
                "flags": {"flags": baseline_target.section.flags ^ 1},
                "address": {"addr": baseline_target.section.addr + 4},
                "alignment": {
                    "addralign": 8 if baseline_target.section.addralign != 8 else 4
                },
            }
            for label, values in changes.items():
                with self.subTest(label=label):
                    sections = list(baseline.sections)
                    sections[baseline_target.section.index] = replace(
                        baseline_target.section, **values
                    )
                    candidate = replace(baseline, sections=tuple(sections))
                    candidate_target = owner_extract.Target(
                        baseline_target.symbol,
                        candidate.sections[baseline_target.section.index],
                    )
                    with self.assertRaisesRegex(
                        owner_extract.ExtractError,
                        "target text section topology drifted",
                    ):
                        owner_extract.audit_siblings(
                            baseline,
                            candidate,
                            baseline_target,
                            candidate_target,
                        )

    def test_report_hashes_parsed_owners_across_symlink_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audited_baseline = self.assemble(root, "baseline", self.owner_source())
            replacement_baseline = self.assemble(
                root,
                "replacement-baseline",
                self.owner_source(data_value=1),
            )
            audited = self.assemble(
                root,
                "audited-candidate",
                self.owner_source(target_nops=1),
            )
            replacement = self.assemble(
                root,
                "replacement-candidate",
                self.owner_source(target_nops=1, data_value=1),
            )
            baseline = root / "baseline-link.o"
            candidate = root / "candidate-link.o"
            symlink(baseline, audited_baseline, self)
            symlink(candidate, audited, self)
            audited_baseline_sha256 = hashlib.sha256(
                audited_baseline.read_bytes()
            ).hexdigest()
            replacement_baseline_sha256 = hashlib.sha256(
                replacement_baseline.read_bytes()
            ).hexdigest()
            audited_sha256 = hashlib.sha256(audited.read_bytes()).hexdigest()
            replacement_sha256 = hashlib.sha256(replacement.read_bytes()).hexdigest()
            self.assertNotEqual(
                audited_baseline_sha256, replacement_baseline_sha256
            )
            self.assertNotEqual(audited_sha256, replacement_sha256)

            real_run_tool = owner_extract._run_tool
            swapped = False

            def swap_after_objcopy(argv: list[str], label: str) -> None:
                nonlocal swapped
                real_run_tool(argv, label)
                if label == "powerpc-eabi-objcopy" and not swapped:
                    baseline.unlink()
                    baseline.symlink_to(replacement_baseline)
                    candidate.unlink()
                    candidate.symlink_to(replacement)
                    swapped = True

            with mock.patch.object(
                owner_extract, "_run_tool", side_effect=swap_after_objcopy
            ):
                report = owner_extract.extract(
                    baseline_path=baseline,
                    candidate_path=candidate,
                    function="target",
                    output_path=root / "target.o",
                    objcopy=self.objcopy,
                    readelf=self.readelf,
                )

            self.assertTrue(swapped)
            self.assertEqual(
                report["baseline_owner_sha256"], audited_baseline_sha256
            )
            self.assertEqual(report["candidate_owner_sha256"], audited_sha256)
            self.assertEqual(
                owner_extract.file_sha256(baseline), replacement_baseline_sha256
            )
            self.assertEqual(owner_extract.file_sha256(candidate), replacement_sha256)

    def test_report_hashes_parsed_output_across_path_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self.assemble(root, "baseline", self.owner_source())
            candidate = self.assemble(
                root, "candidate", self.owner_source(target_nops=1)
            )
            replacement = self.assemble(
                root, "replacement", self.owner_source(data_value=1)
            )
            replacement_sha256 = hashlib.sha256(replacement.read_bytes()).hexdigest()
            symlink(root / "symlink-probe", replacement, self)
            output = root / "target.o"
            real_read_elf = owner_extract.read_elf
            audited_output_sha256: str | None = None

            def swap_after_output_parse(path: Path) -> owner_extract.ElfObject:
                nonlocal audited_output_sha256
                parsed = real_read_elf(path)
                if path == output and audited_output_sha256 is None:
                    audited_output_sha256 = hashlib.sha256(parsed.data).hexdigest()
                    output.unlink()
                    output.symlink_to(replacement)
                return parsed

            with mock.patch.object(
                owner_extract, "read_elf", side_effect=swap_after_output_parse
            ):
                report = owner_extract.extract(
                    baseline_path=baseline,
                    candidate_path=candidate,
                    function="target",
                    output_path=output,
                    objcopy=self.objcopy,
                    readelf=self.readelf,
                )

            self.assertIsNotNone(audited_output_sha256)
            self.assertNotEqual(audited_output_sha256, replacement_sha256)
            self.assertEqual(report["output"]["elf_sha256"], audited_output_sha256)
            self.assertEqual(owner_extract.file_sha256(output), replacement_sha256)

    def test_expected_baseline_fingerprint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = self.assemble(root, "owner", self.owner_source())
            with self.assertRaisesRegex(owner_extract.ExtractError, "fingerprint drifted"):
                owner_extract.extract(
                    baseline_path=owner,
                    candidate_path=owner,
                    function="target",
                    output_path=root / "target.o",
                    objcopy=self.objcopy,
                    readelf=self.readelf,
                    expected_baseline_fingerprint="0" * 64,
                )


if __name__ == "__main__":
    unittest.main()
