"""Генерация cloud-init конфигурации с учётом выделенного IP."""

from __future__ import annotations

from string import Template
from typing import Dict

from . import Deployment


DEFAULT_TEMPLATE = Template("""#cloud-config
write_files:
  - path: /etc/node-ip
    permissions: '0644'
    content: '${ip_address}'
""")


def render_cloud_init(deployment: Deployment, variables: Dict[str, str] | None = None) -> str:
    """Подставляет IP-адрес инстанса в cloud-init шаблон.

    Если IP ещё не известен, будет использовано значение ``auto``. Это позволяет
    повторно вызвать функцию, когда IP появится, и получить окончательную
    конфигурацию.
    """

    context = {"ip_address": deployment.ip_address or "auto"}
    if variables:
        context.update(variables)
    return DEFAULT_TEMPLATE.safe_substitute(context)
