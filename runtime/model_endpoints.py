"""Resolve a plan's `bound_model` to a real served endpoint.

The optimizer binds every semantic node to a model by its calibration.json KEY
(e.g. 'UNVERIFIED-sem-lightning-local'). The executor's first job is to turn that
key into an actual HTTP endpoint: base URL, served model id, and — the part that
governs correctness — the hard context budget that content must be truncated to.

This is where "which model" (a plan decision) becomes "which server" (an execution
fact), and the one place a wrong model string or an over-long prompt is caught.

Endpoints measured live on the GN100 (Acer Veriton GN100, GB10) 2026-08-16:
    Lightning  llama-server :8001  -c 32768 --parallel 4   -> 8192 tok / slot
    Omni       vLLM        :8005  --max-model-len 16384
    embed :8002  rerank :8003  parse :8004  asr :8006      Super: remote (build.nvidia.com)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# The box is reachable at this host. On the GN100 itself, export SPARK_HOST=localhost
# to skip the network hop; from another machine use the LAN IP (the repo's .env default).
_HOST = os.environ.get("SPARK_HOST", "172.16.94.53")
_REMOTE = os.environ.get("BQL_REMOTE_BASE_URL", "https://integrate.api.nvidia.com/v1")


@dataclass(frozen=True)
class Endpoint:
    calibration_key: str
    base_url: str
    model_id: str
    context_tokens: int          # usable context per request
    is_remote: bool
    serves: tuple[str, ...]      # predicate classes this endpoint can answer
    reserve_output_tokens: int = 256
    chars_per_token: float = 3.5  # conservative for dense legal text

    @property
    def max_input_chars(self) -> int:
        """The hard limit content is truncated to before this model sees it.
        Derived from the served context window, minus the judge prompt and the
        model's own answer -- not a magic number."""
        usable = self.context_tokens - self.reserve_output_tokens - 160
        return max(0, int(usable * self.chars_per_token))


# calibration.json key -> real endpoint. The keys are exactly the strings the
# optimizer writes into SemanticFilter.bound_model / Materialize.bound_model.
REGISTRY: dict[str, Endpoint] = {
    "UNVERIFIED-sem-lightning-local": Endpoint(
        "UNVERIFIED-sem-lightning-local", f"http://{_HOST}:8001/v1",
        "nvidia/nemotron-3.5-lightning", 8192, False, ("SEM", "AUDIO")),
    "UNVERIFIED-vlm-nano-local": Endpoint(
        "UNVERIFIED-vlm-nano-local", f"http://{_HOST}:8005/v1",
        "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4", 16384, False, ("VISUAL",)),
    "UNVERIFIED-embed-local": Endpoint(
        "UNVERIFIED-embed-local", f"http://{_HOST}:8002/v1",
        "nvidia/Nemotron-3-Embed-1B-BF16", 4096, False, ("SIM",)),
    "UNVERIFIED-rerank-local": Endpoint(
        "UNVERIFIED-rerank-local", f"http://{_HOST}:8003/v1",
        "nvidia/llama-nemotron-rerank-1b-v2", 4096, False, ("SIM",)),
    "UNVERIFIED-asr-local": Endpoint(
        "UNVERIFIED-asr-local", f"http://{_HOST}:8006/v1",
        "nvidia/nemotron-asr", 4096, False, ()),  # serves ASR derivation, not a predicate class
    "UNVERIFIED-docparse-local": Endpoint(
        "UNVERIFIED-docparse-local", f"http://{_HOST}:8004/v1",
        "nvidia/NVIDIA-Nemotron-Parse-v1.1", 8192, False, ()),  # DOC_PARSE derivation
    "UNVERIFIED-oracle-remote": Endpoint(
        "UNVERIFIED-oracle-remote", _REMOTE,
        "nvidia/nemotron-3-super-120b-a12b", 32768, True, ("SEM", "VISUAL", "AUDIO")),
}

# Deterministic (non-model) binders the optimizer may name, e.g. 'bespoke:pdf-raster'.
# These are handled by the bespoke backend, not by a model call.
DETERMINISTIC_PREFIX = "bespoke:"


def resolve(bound_model: str) -> Endpoint:
    """Look up the served endpoint for a plan's bound_model key. Raises loudly on
    an unknown key -- a silent fallback would run the wrong model."""
    ep = REGISTRY.get(bound_model)
    if ep is None:
        raise KeyError(
            f"bound_model {bound_model!r} is not a known served endpoint; "
            f"known keys: {sorted(REGISTRY)}"
        )
    return ep


def is_deterministic(bound_model: str) -> bool:
    return bound_model.startswith(DETERMINISTIC_PREFIX)


if __name__ == "__main__":
    for key, ep in REGISTRY.items():
        tag = "remote" if ep.is_remote else "local "
        print(f"{key:<32} {tag} {ep.base_url:<28} {ep.model_id:<48} "
              f"ctx={ep.context_tokens:<6} max_chars={ep.max_input_chars:<7} serves={ep.serves}")
