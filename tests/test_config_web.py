"""Roundtrip веб-редактора: config -> dict -> toml -> config."""
from pathlib import Path

from kuni.core.config import (config_to_dict, dict_to_toml_text,
                              load_config, validate_config_dict)


def test_roundtrip_preserves_values(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[general]\ncharacter_name = "Ақерке"\ncharacter_nickname = "Ако"\n'
        'papik_name = "Влад"\ntelegram_api_id = 21149731\n\n'
        '[llm]\nbase_url = "http://x:1/v1"\nmodel = "m"\ntemperature = 0.2\n'
        'fallback_models = ["a", "b"]\n\n'
        '[llm.embedding]\nbase_url = "http://e:2/v1"\napi_key = "k"\nmodel = "e8"\n\n'
        '[proactivity]\nenabled = true\nimpulse_interval_minutes = 45\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    data = config_to_dict(cfg)
    # вложенная секция на месте
    assert data["llm"]["embedding"]["model"] == "e8"
    assert data["llm"]["fallback_models"] == ["a", "b"]
    assert data["general"]["character_name"] == "Ақерке"

    cfg2, text = validate_config_dict(data)
    assert cfg2 is not None, text
    assert cfg2.general.character_name == "Ақерке"
    assert cfg2.llm.embedding.model == "e8"
    assert cfg2.llm.fallback_models == ["a", "b"]
    assert cfg2.llm.temperature == 0.2
    assert cfg2.proactivity.impulse_interval_minutes == 45


def test_validate_rejects_garbage():
    bad = {"general": {"character_name": 'хуй"с кавычками и \\nпереносом'}}
    cfg, _ = validate_config_dict(bad)   # сериализуется корректно (экранирование)
    assert cfg is not None
    assert cfg.general.character_name == 'хуй"с кавычками и \\nпереносом'
