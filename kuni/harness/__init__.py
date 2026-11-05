"""Гейт одобрений: то, что модель не имеет права решать сама.

Идея простая: инструмент не выполняет опасное действие, а возвращает
`PendingApproval`. Дальше `ApprovalManager` шлёт владельцу сообщение с
точным текстом команды и двумя кнопками. Пока владелец не нажал
«разрешить» — ничего не происходит. Таймаут = отказ, не разрешение.

Гейт живёт в коде kuni, а не в промпте: содержимое репозитория или
веб-страницы не может его обойти, потому что решение принимает человек.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Сколько живёт запрос. Дольше — уже не помню, о чём он был.
APPROVAL_TTL = 600.0

#: Действия, которые нельзя одобрить вообще никогда, даже кнопкой.
#: Список намеренно короткий: это не «опасное», это «необратимое без
#: возможности откатиться».
NEVER_ALLOWED = (
    "shutdown", "restart-computer", "format", "diskpart", "bcdedit",
    "reg delete", "net user", "cipher /w", "vssadmin delete",
)


@dataclass
class Approval:
    id: str
    kind: str                 # shell / fs / code_task
    summary: str              # что именно произойдёт, человеческим языком
    detail: str               # точная команда или путь
    chat_id: int
    created_at: float = field(default_factory=time.time)
    future: asyncio.Future | None = None
    message_id: int = 0
    resolved: bool = False
    allowed: bool = False


class ApprovalManager:
    """Одноразовые разрешения, привязанные к владельцу и чату."""

    def __init__(self, telegram: Any, owner_chat_id: int, bus: Any = None) -> None:
        self.telegram = telegram
        self.owner_chat_id = owner_chat_id
        self.bus = bus
        self._pending: dict[str, Approval] = {}

    # ------------------------------------------------------------------ запрос

    async def request(self, kind: str, summary: str, detail: str,
                      chat_id: int = 0) -> dict[str, Any]:
        """Спросить владельца. Возвращает {"allowed": bool, "reason": str}."""
        chat_id = chat_id or self.owner_chat_id
        if not chat_id:
            return {"allowed": False, "reason": "нет chat_id владельца для одобрения"}

        lowered = detail.lower()
        for banned in NEVER_ALLOWED:
            if banned in lowered:
                log.warning("одобрение отклонено политикой: %s", banned)
                return {"allowed": False,
                        "reason": f"действие «{banned}» запрещено политикой безусловно"}

        token = secrets.token_hex(6)
        approval = Approval(id=token, kind=kind, summary=summary,
                            detail=detail[:1500], chat_id=chat_id)
        approval.future = asyncio.get_running_loop().create_future()
        self._pending[token] = approval

        text = (f"🔐 нужно разрешение\n\n"
                f"что: {summary}\n"
                f"тип: {kind}\n\n"
                f"```\n{detail[:1200]}\n```\n\n"
                f"живёт {int(APPROVAL_TTL // 60)} мин, потом отказ")
        try:
            approval.message_id = await self.telegram.send_text(
                chat_id, text,
                buttons=[[("✅ Разрешить", f"approve:{token}"),
                          ("⛔ Отклонить", f"deny:{token}")]],
            )
        except Exception as e:
            self._pending.pop(token, None)
            log.warning("не смогла спросить разрешение: %s", e)
            return {"allowed": False, "reason": f"не удалось отправить запрос: {e}"}

        try:
            allowed = await asyncio.wait_for(approval.future, timeout=APPROVAL_TTL)
        except asyncio.TimeoutError:
            allowed = False
            approval.resolved = True
            if self.bus is not None:
                self.bus.emit("approval.timeout", kind=kind, id=token)
        finally:
            self._pending.pop(token, None)

        approval.allowed = bool(allowed)
        return {"allowed": bool(allowed),
                "reason": "разрешено владельцем" if allowed else "отклонено или истекло"}

    # --------------------------------------------------------------- ответ

    def resolve(self, chat_id: int, data: str, sender_user_id: int = 0) -> str:
        """Обработать нажатие кнопки. Возвращает текст для правки сообщения."""
        if ":" not in (data or ""):
            return ""
        action, token = data.split(":", 1)
        approval = self._pending.get(token)
        if approval is None:
            return "⌛ запрос уже неактуален"
        # нажимать может только владелец в своём чате
        if self.owner_chat_id and chat_id != self.owner_chat_id:
            log.warning("кнопка нажата из чужого чата %s — игнорирую", chat_id)
            return ""
        if approval.future is not None and not approval.future.done():
            approval.future.set_result(action == "approve")
        approval.resolved = True
        verdict = "✅ разрешено" if action == "approve" else "⛔ отклонено"
        if self.bus is not None:
            self.bus.emit("approval.resolved", kind=approval.kind,
                          id=token, allowed=(action == "approve"))
        return f"{verdict}\n\n{approval.summary}"

    def pending_count(self) -> int:
        return len(self._pending)

    def drop_all(self) -> None:
        """При остановке все ожидания = отказ, чтобы никто не завис."""
        for approval in list(self._pending.values()):
            if approval.future is not None and not approval.future.done():
                approval.future.set_result(False)
        self._pending.clear()
