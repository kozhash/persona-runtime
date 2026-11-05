"""Dev-мост: «модель — это ты».

FileBridgeLLM реализует тот же интерфейс, что LLMClient, но вместо
HTTP кладёт запрос в data/devbrain/inbox/<id>.json и ждёт ответ в
data/devbrain/outbox/<id>.json. Ответ пишешь ты (или я в сессии) —
можно как обычный текст, так и tool_calls. Эмбеддинги — детерминированный
хэш, сеть не нужна вообще.
"""
from .bridge import FileBridgeLLM

__all__ = ["FileBridgeLLM"]
