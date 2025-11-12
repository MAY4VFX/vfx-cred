"""Утилиты для управления развёртыванием вспомогательных сервисов."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Deployment:
    """Сведения о создаваемом инстансе TensorDock.

    Атрибут ``ip_address`` заполняется после успешного создания инстанса
    провайдером и далее используется при генерации cloud-init и конфигурации
    окружения.
    """

    name: str
    ip_address: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def set_ip(self, ip_address: str) -> None:
        """Сохраняет IP-адрес и одновременно пишет его в метаданные."""

        self.ip_address = ip_address
        self.metadata["ipAddress"] = ip_address

    @property
    def key_name(self) -> str:
        """Формирует имя SSH-ключа, зависящее от IP-адреса."""

        if not self.ip_address:
            raise ValueError("IP-адрес ещё не присвоен инстансу")
        return f"node-{self.name}-{self.ip_address}"
