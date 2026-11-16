"""Location of the shared contract files (openapi.json, fixtures) for the tests – independent of the contract
generator, which needs the own plant's mapping (plan hub-publish P4)."""

from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
