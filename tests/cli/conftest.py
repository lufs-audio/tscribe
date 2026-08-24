import pytest


@pytest.fixture
def local_client_config(tmp_path):
    """A minimal, valid mode: client / backend: local tscribe.yaml — the
    config nearly every CLI test needs just to get past config loading."""
    cfg_path = tmp_path / "tscribe.yaml"
    cfg_path.write_text("mode: client\nbackend: local\n")
    return cfg_path
