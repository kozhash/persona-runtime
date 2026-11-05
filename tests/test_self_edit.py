"""Тесты скилла самоправки: броня, бэкап, автооткат, приёмка зелёных правок."""
import pytest

from kuni.skills.self_edit import SelfEditSkill


def make_skill(tmp_path, sentinel_content: str, tests_should_pass: bool):
    """Скилл с фейковой тест-командой: тест 'проходит', если в файле-сигнале 'OK'."""
    root = tmp_path / "proj"
    (root / "prompts").mkdir(parents=True)
    sentinel = root / "sentinel.txt"
    sentinel.write_text(sentinel_content, encoding="utf-8")

    def fake_cmd():
        # «pytest»: exit-код зависит от содержимого sentinel-файла
        code = (
            "import sys; from pathlib import Path;"
            "sys.exit(0 if Path('sentinel.txt').read_text(encoding='utf-8')=='OK' else 1)"
        )
        return [skill.python, "-c", code]

    skill = SelfEditSkill(root, test_cmd=None)
    skill._test_cmd = fake_cmd   # подменяем после создания
    return skill, root, sentinel


@pytest.mark.asyncio
async def test_protected_paths_rejected(tmp_path):
    skill, root, _ = make_skill(tmp_path, "OK", True)
    result = await skill({"action": "write", "path": "kuni/skills/self_edit.py",
                          "content": "print('pwned')"})
    assert not result.ok and "защищён" in result.output
    result = await skill({"action": "write", "path": "data/kuni.db", "content": "x"})
    assert not result.ok
    result = await skill({"action": "write", "path": "../outside.py", "content": "x"})
    assert not result.ok


@pytest.mark.asyncio
async def test_green_edit_applied(tmp_path):
    skill, root, _ = make_skill(tmp_path, "OK", True)
    result = await skill({"action": "write", "path": "prompts/character_base.md",
                          "content": "# новое я"})
    assert result.ok and "тесты зелёные" in result.output
    assert (root / "prompts" / "character_base.md").read_text(encoding="utf-8") == "# новое я"
    assert list((root / "data" / "self_edits").glob("*.bak")), "бэкап не создан"


@pytest.mark.asyncio
async def test_failing_tests_rollback(tmp_path):
    # sentinel != OK -> «тесты падают» -> правка обязана откатиться
    skill, root, _ = make_skill(tmp_path, "NOT-OK", False)
    target = root / "prompts" / "character_base.md"
    result = await skill({"action": "write", "path": "prompts/character_base.md",
                          "content": "# поломанное я"})
    assert not result.ok and "ОТКАЧЕНА" in result.output
    assert not target.exists(), "сломанная правка осталась на месте!"


@pytest.mark.asyncio
async def test_read_and_history(tmp_path):
    skill, root, _ = make_skill(tmp_path, "OK", True)
    (root / "prompts" / "x.md").write_text("содержимое", encoding="utf-8")
    result = await skill({"action": "read", "path": "prompts/x.md"})
    assert result.ok and "содержимое" in result.output
    result = await skill({"action": "read", "path": "prompts/nope.md"})
    assert not result.ok
