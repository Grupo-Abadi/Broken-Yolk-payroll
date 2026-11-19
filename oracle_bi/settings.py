"""Configuración de la BI API, de los Secrets de Streamlit o de un TOML local.

Viene de mealviolation (commit 575b084, oracle_bi/settings.py). Cambios:
- verify_ssl = "false" ahora sí apaga la verificación; antes bool("false")
  era True.
- Los valores de ejemplo (YOUR_..., TU_..., REPLACE_WITH_..., https://YOUR-...)
  cuentan como faltantes.
- Mensajes en español.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover - Python < 3.11
    raise RuntimeError("Hace falta Python 3.11 o más nuevo.") from exc

from oracle_bi.client import OracleBIConfig

REQUERIDOS = (
    "auth_server",
    "application_server",
    "org_identifier",
    "client_id",
    "username",
    "password",
)
_VERDADERO = {"true", "1", "yes", "si", "sí", "on"}
_FALSO = {"false", "0", "no", "off"}


def _es_de_ejemplo(valor: str) -> bool:
    texto = valor.strip().upper()
    return texto.startswith(("YOUR_", "TU_", "REPLACE_WITH_")) or "://YOUR-" in texto


def _a_bool(valor: Any, nombre: str) -> bool:
    if isinstance(valor, bool):
        return valor
    texto = str(valor).strip().lower()
    if texto in _VERDADERO:
        return True
    if texto in _FALSO:
        return False
    raise ValueError(f"{nombre} tiene que ser true o false, no {valor!r}.")


def config_from_secret_mapping(secrets: Mapping[str, Any]) -> OracleBIConfig:
    """Arma la configuración de la BI desde secrets estilo Streamlit.

    La sección es [oracle_bi]. [oracle] se acepta igual, que es como la traen
    las versiones viejas de mealviolation. Las credenciales de Labor no se leen.
    """
    section = secrets.get("oracle_bi")
    if not isinstance(section, Mapping):
        legacy = secrets.get("oracle")
        section = legacy if isinstance(legacy, Mapping) else None
    if not isinstance(section, Mapping):
        raise ValueError(
            "No están configurados los secrets de la BI API: "
            "falta la sección [oracle_bi]."
        )

    missing = [
        key
        for key in REQUERIDOS
        if not str(section.get(key, "")).strip() or _es_de_ejemplo(str(section.get(key, "")))
    ]
    if missing:
        raise ValueError("Faltan secrets de la BI API: " + ", ".join(missing))

    try:
        timeout = int(section.get("timeout_seconds", 45))
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds tiene que ser un número entero de segundos.") from exc

    return OracleBIConfig(
        auth_server=str(section["auth_server"]).strip(),
        application_server=str(section["application_server"]).strip(),
        org_identifier=str(section["org_identifier"]).strip(),
        client_id=str(section["client_id"]).strip(),
        username=str(section["username"]).strip(),
        password=str(section["password"]),
        application_name=str(
            section.get("application_name", "Broken Yolk Payroll")
        ).strip()
        or "Broken Yolk Payroll",
        timeout_seconds=timeout,
        verify_ssl=_a_bool(section.get("verify_ssl", True), "verify_ssl"),
    )


def config_from_toml_file(path: str | Path) -> OracleBIConfig:
    """Lee un TOML local de secrets, como .streamlit/secrets.toml."""
    file_path = Path(path)
    if not file_path.exists():
        raise ValueError(f"No existe el archivo de secrets: {file_path}")
    try:
        with file_path.open("rb") as fh:
            payload = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"El TOML de secrets no es válido: {exc}") from exc
    return config_from_secret_mapping(payload)
