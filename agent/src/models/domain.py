"""Validation and pipeline output models - canonical definitions live in common."""

# ValidationStatus is a StrEnum so its values are wire-compatible with the str
# typed fields in the common dataclasses (e.g. ValidationStatus.PASS == "PASS").
from ms_duke_je_common.assembly_gate.models import CheckResult, ProcessingResult

__all__ = ["CheckResult", "ProcessingResult"]