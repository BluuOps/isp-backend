from __future__ import annotations

import ipaddress
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import settings


class DisconnectError(RuntimeError):
    retryable = False


class RetryableDisconnectError(DisconnectError):
    retryable = True


class TerminalDisconnectError(DisconnectError):
    retryable = False


@dataclass(frozen=True)
class DisconnectRequest:
    username: str
    session_identity: str
    accounting_session_id: str
    nas_ip_address: str
    framed_ip_address: str | None = None


@dataclass(frozen=True)
class DisconnectResult:
    acknowledged: bool
    detail: str


class DisconnectAdapter(Protocol):
    def disconnect(self, request: DisconnectRequest) -> DisconnectResult: ...


class DisabledDisconnectAdapter:
    def disconnect(self, request: DisconnectRequest) -> DisconnectResult:
        del request
        raise TerminalDisconnectError("disconnect_adapter_disabled")


class MockDisconnectAdapter:
    def __init__(self, outcomes: list[DisconnectResult | Exception] | None = None):
        self.outcomes = list(outcomes or [DisconnectResult(True, "mock_disconnect_ack")])
        self.requests: list[DisconnectRequest] = []

    def disconnect(self, request: DisconnectRequest) -> DisconnectResult:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else DisconnectResult(True, "mock_disconnect_ack")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RadclientDisconnectAdapter:
    def __init__(self) -> None:
        if not settings.radius_coa_enabled or settings.radius_disconnect_mode != "real":
            raise TerminalDisconnectError("real_disconnect_not_enabled")
        if not settings.radius_disconnect_nas_allowlist:
            raise TerminalDisconnectError("nas_allowlist_empty")

    def _validate_target(self, value: str) -> None:
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise TerminalDisconnectError("malformed_nas_address") from exc
        if value not in settings.radius_disconnect_nas_allowlist:
            raise TerminalDisconnectError("nas_not_allowlisted")

    def disconnect(self, request: DisconnectRequest) -> DisconnectResult:
        self._validate_target(request.nas_ip_address)
        executable = Path(settings.radclient_bin)
        secret = Path(settings.coa_secret_path)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise TerminalDisconnectError("radclient_unavailable")
        if not secret.is_file() or not os.access(secret, os.R_OK):
            raise TerminalDisconnectError("coa_secret_unavailable")

        attributes = [
            f'User-Name = "{request.username}"',
            f'Acct-Session-Id = "{request.accounting_session_id}"',
            f"NAS-IP-Address = {request.nas_ip_address}",
        ]
        if request.framed_ip_address:
            attributes.append(f"Framed-IP-Address = {request.framed_ip_address}")
        try:
            result = subprocess.run(
                [
                    str(executable), "-S", str(secret), "-x",
                    f"{request.nas_ip_address}:{settings.coa_port}", "disconnect",
                ],
                input="\n".join(attributes) + "\n",
                text=True,
                capture_output=True,
                timeout=settings.radius_disconnect_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RetryableDisconnectError("disconnect_timeout") from exc
        except OSError as exc:
            raise RetryableDisconnectError("disconnect_process_unavailable") from exc
        output = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0 or "Disconnect-ACK" not in output:
            raise RetryableDisconnectError("disconnect_not_acknowledged")
        return DisconnectResult(True, "disconnect_acknowledged")


def configured_disconnect_adapter() -> DisconnectAdapter:
    if settings.radius_disconnect_mode == "real":
        return RadclientDisconnectAdapter()
    return DisabledDisconnectAdapter()
