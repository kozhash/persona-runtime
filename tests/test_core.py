"""Тесты ядра: шина событий, конфиг, промпты."""
import time
from pathlib import Path

from kuni.core.bus import Bus
from kuni.core.config import ConfigService, load_config
from kuni.character.prompts import PromptLibrary


def test_bus_subscribe_and_emit():
    bus = Bus()
    seen = []
    bus.subscribe("telegram.*", lambda p: seen.append(p))
    bus.emit("telegram.message", chat_id=1, text="привет")
    bus.emit("memory.х", text="мимо")
    assert len(seen) == 1
    assert seen[0]["text"] == "привет"
    assert bus.history[-1].topic == "memory.х"


def test_bus_unsubscribe():
    bus = Bus()
    seen = []
    unsub = bus.subscribe("*", lambda p: seen.append(1))
    bus.emit("a")
    unsub()
    bus.emit("b")
    assert len(seen) == 1


def test_config_load_and_hot_reload(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[general]\ncharacter_name = "Тест"\ntelegram_api_id = 123\n'
        '[telegram]\necho_mode = true\nreply_delay_min_ms = 100\n',
        encoding="utf-8",
    )
    svc = ConfigService(cfg_file)
    assert svc.current.general.character_name == "Тест"
    assert svc.current.telegram.echo_mode is True
    assert svc.current.telegram.reply_delay_min_ms == 100
    # значения по умолчанию не тронуты
    assert svc.current.llm.model == "qwen2.5:14b"

    # правим файл — mtime меняется — перезагрузка
    time.sleep(0.02)
    cfg_file.write_text('[general]\ncharacter_name = "Другая"\n', encoding="utf-8")
    assert svc.maybe_reload(force=True)
    assert svc.current.general.character_name == "Другая"
    assert svc.current.telegram.echo_mode is False  # сбросились к дефолту


def test_config_broken_file_keeps_old(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[general]\ncharacter_name = "OK"\n', encoding="utf-8")
    svc = ConfigService(cfg_file)
    time.sleep(0.02)
    cfg_file.write_text("это не toml {{{", encoding="utf-8")
    assert svc.maybe_reload(force=True) is False
    assert svc.current.general.character_name == "OK"


def test_prompts_hot_reload_and_vars(tmp_path: Path):
    f = tmp_path / "character_base.md"
    f.write_text("Привет, {name}! {unknown_var}", encoding="utf-8")
    lib = PromptLibrary(tmp_path)
    text = lib.get("character_base", name="Куни")
    assert "Привет, Куни!" in text
    assert "{unknown_var}" in text  # несуществующая переменная не ломает промпт

    # правим файл на лету
    time.sleep(0.02)
    f.write_text("Теперь по-другому", encoding="utf-8")
    assert lib.get("character_base") == "Теперь по-другому"


def test_prompts_missing_returns_empty(tmp_path: Path):
    lib = PromptLibrary(tmp_path)
    assert lib.get("нет_такого") == ""
