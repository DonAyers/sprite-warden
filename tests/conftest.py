"""Never let automatic telemetry tests mutate the developer's knowledge stores."""
import pytest


@pytest.fixture(autouse=True)
def isolated_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv('SPRITEWARDEN_EVENTS_DB', str(tmp_path / 'events.db'))
    monkeypatch.setenv('SPRITEWARDEN_LEARNING_DB', str(tmp_path / 'learning.db'))
    monkeypatch.setenv('SPRITEWARDEN_EVENT_SPOOL', str(tmp_path / 'spool'))
