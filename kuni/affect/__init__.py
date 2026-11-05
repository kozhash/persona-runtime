"""Аффект: моноамины, PAD, гомеостаз, безопасность, модуляция.

Публичный API модуля. Внутренние детали — отдельные файлы.
"""
from __future__ import annotations

from .engine import EngineConfig, NeuromodulatoryEngine, project_pad
from .modulation import InferenceModulator, ModulationConfig
from .safety import BreakerConfig, BreakerDecision, MetacognitiveCircuitBreaker
from .service import AffectConfig, AffectService
from .types import (OBSERVATION, SELF_INFERENCE, TEACHING, AffectSnapshot,
                    AppraisalImpulse, Homeostasis, Monoamines, PAD,
                    ProvenanceClass, clamp)

__all__ = [
    "AffectConfig",
    "AffectService",
    "AffectSnapshot",
    "AppraisalImpulse",
    "BreakerConfig",
    "BreakerDecision",
    "EngineConfig",
    "Homeostasis",
    "InferenceModulator",
    "MetacognitiveCircuitBreaker",
    "ModulationConfig",
    "Monoamines",
    "NeuromodulatoryEngine",
    "PAD",
    "ProvenanceClass",
    "OBSERVATION",
    "TEACHING",
    "SELF_INFERENCE",
    "clamp",
    "project_pad",
]
