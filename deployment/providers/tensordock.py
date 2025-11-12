"""Логика развёртывания инстансов TensorDock."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Protocol

from .. import Deployment
from ..cloud_init import render_cloud_init


class TensorDockAPI(Protocol):
    """Простейший интерфейс API TensorDock, необходимый для провижининга."""

    def create_instance(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Создаёт инстанс и возвращает словарь с информацией."""

    def get_instance(self, instance_id: str) -> Mapping[str, Any]:
        """Возвращает актуальную информацию об инстансе по его идентификатору."""


class ProvisioningError(RuntimeError):
    """Выбрасывается, когда API не вернул IP-адрес инстанса."""


@dataclass
class ProvisionResult:
    """Результат развёртывания инстанса TensorDock."""

    instance_data: Dict[str, Any]
    cloud_init: str
    config_env: str


class TensorDockProvider:
    """Провайдер, обеспечивающий получение IP перед генерацией cloud-init."""

    def __init__(
        self,
        api_client: TensorDockAPI,
        *,
        poll_interval: float = 2.0,
        poll_timeout: float = 120.0,
    ) -> None:
        self._api_client = api_client
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout

    def provision(self, deployment: Deployment, payload: Mapping[str, Any]) -> ProvisionResult:
        """Создаёт инстанс и формирует артефакты конфигурации.

        Сначала создаём инстанс и дожидаемся IP-адреса, затем генерируем
        cloud-init и config.env, где IP нужен для имени ключа.
        """

        instance_data = self._provision_on_host(payload)
        ip_address = _extract_ip(instance_data)
        if not ip_address:
            instance_id = _extract_instance_id(instance_data)
            if not instance_id:
                raise ProvisioningError("API TensorDock не вернул IP-адрес")
            ip_address = self._wait_for_ip(instance_id)
        if not ip_address:
            raise ProvisioningError("API TensorDock не вернул IP-адрес")

        deployment.set_ip(ip_address)
        cloud_init = self._generate_cloud_init_config(deployment)
        config_env = self._generate_config_env(deployment, payload)
        return ProvisionResult(
            instance_data=dict(instance_data),
            cloud_init=cloud_init,
            config_env=config_env,
        )

    def _provision_on_host(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Запрашивает создание инстанса и возвращает ответ API."""

        instance_data = self._api_client.create_instance(payload)
        # Гарантируем доступность ключа ipAddress на верхнем уровне.
        ip_address = _extract_ip(instance_data)
        if ip_address:
            # type: ignore[assignment]
            instance_data = dict(instance_data)
            instance_data["ipAddress"] = ip_address
        return instance_data

    def _wait_for_ip(self, instance_id: str) -> str | None:
        """Ожидает появления IP-адреса, опрашивая API TensorDock."""

        deadline = time.monotonic() + self._poll_timeout
        while time.monotonic() < deadline:
            time.sleep(self._poll_interval)
            details = self._api_client.get_instance(instance_id)
            ip_address = _extract_ip(details)
            if ip_address:
                return ip_address
        return None

    def _generate_cloud_init_config(self, deployment: Deployment) -> str:
        """Генерирует cloud-init уже после получения IP-адреса."""

        return render_cloud_init(deployment)

    def _generate_config_env(self, deployment: Deployment, payload: Mapping[str, Any]) -> str:
        """Формирует содержимое config.env с реальным IP в KEY_NAME."""

        env_vars = {k: str(v) for k, v in payload.items() if k.isupper()}
        env_vars["KEY_NAME"] = deployment.key_name
        return "\n".join(f"{key}={value}" for key, value in sorted(env_vars.items()))


def _extract_ip(instance_data: Mapping[str, Any]) -> str | None:
    """Ищет IP-адрес в различных местах ответа TensorDock."""

    if "ipAddress" in instance_data:
        return str(instance_data["ipAddress"])
    instance = instance_data.get("instance")
    if isinstance(instance, Mapping) and "ipAddress" in instance:
        return str(instance["ipAddress"])
    return None


def _extract_instance_id(instance_data: Mapping[str, Any]) -> str | None:
    """Пытается определить идентификатор инстанса из ответа API."""

    for key in ("instanceId", "id"):
        if key in instance_data:
            return str(instance_data[key])
    instance = instance_data.get("instance")
    if isinstance(instance, Mapping):
        for key in ("instanceId", "id"):
            if key in instance:
                return str(instance[key])
    return None
