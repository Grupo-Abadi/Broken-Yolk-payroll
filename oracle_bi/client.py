"""Cliente de la Business Intelligence API de Oracle (Reporting and Analytics).

Viene de mealviolation (commit 575b084, oracle_bi/client.py), donde ya se probó
contra el enterprise de Broken Yolk: 8 tiendas y 2,941 tarjetas en un periodo.
Cambios desde ahí:
- Las consultas de datos se reintentan ante 429, 5xx y fallas de red. El inicio
  de sesión no: un código de autorización no se puede usar dos veces.
- get_employee_daily_totals, para los tips por persona y día, y
  get_job_code_daily_totals, para cuadrar las horas de las tarjetas.
- La contraseña y los tokens no salen en el repr: un log o un traceback que
  imprima la configuración ya no los enseña.
- Mensajes en español.

La autenticación es la de Oracle: OIDC Authorization Code con PKCE. Los tokens
viven solo en memoria; este cliente nunca los escribe a disco.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable
from urllib.parse import parse_qs, quote_plus, urlparse

import requests


class OracleBIError(RuntimeError):
    """Falló el inicio de sesión en Oracle BI o una consulta."""


class OracleBIAuthError(OracleBIError):
    """Oracle rechazó el inicio de sesión. Reintentar con la misma contraseña
    no sirve, y varios intentos fallidos bloquean la cuenta 30 minutos."""


@dataclass(frozen=True)
class OracleBIConfig:
    auth_server: str
    application_server: str
    org_identifier: str
    client_id: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    application_name: str = "Broken Yolk Payroll"
    timeout_seconds: int = 45
    verify_ssl: bool = True

    def __post_init__(self) -> None:
        required = {
            "auth_server": self.auth_server,
            "application_server": self.application_server,
            "org_identifier": self.org_identifier,
            "client_id": self.client_id,
            "username": self.username,
            "password": self.password,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError("Falta configuración de Oracle BI: " + ", ".join(missing))


@dataclass
class TokenBundle:
    id_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at_monotonic: float

    def is_valid(self, safety_seconds: int = 300) -> bool:
        return bool(self.id_token) and time.monotonic() + safety_seconds < self.expires_at_monotonic


# Respuestas que valen otro intento: 429 es "espera tantito" y los 5xx son
# fallas del lado de Oracle. Cualquier otro error es de la consulta misma, y
# repetirla no lo arregla.
REINTENTAR_STATUS = frozenset({429, 500, 502, 503, 504})
ESPERAS_SEGUNDOS = (2, 5, 10)
ESPERA_MAXIMA_SEGUNDOS = 60


class OracleBIClient:
    """Cliente de la Business Intelligence API de Oracle MICROS."""

    def __init__(self, config: OracleBIConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "BrokenYolkPayroll/0.1",
            }
        )
        self.tokens: TokenBundle | None = None
        # Las pruebas lo cambian para no esperar de verdad.
        self._dormir = time.sleep

    @property
    def authorize_url(self) -> str:
        return self._auth_url("/oidc-provider/v1/oauth2/authorize")

    @property
    def signin_url(self) -> str:
        return self._auth_url("/oidc-provider/v1/oauth2/signin")

    @property
    def token_url(self) -> str:
        return self._auth_url("/oidc-provider/v1/oauth2/token")

    def _auth_url(self, path: str) -> str:
        return self.config.auth_server.rstrip("/") + path

    def _api_url(self, endpoint: str) -> str:
        return (
            self.config.application_server.rstrip("/")
            + f"/bi/v1/{self.config.org_identifier}/{endpoint}"
        )

    @staticmethod
    def _new_pkce_pair() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(64)
        # RFC 7636 permite de 43 a 128 caracteres.
        verifier = verifier[:128]
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return verifier, challenge

    def _tachar(self, texto: Any) -> str:
        """Quita del texto la contraseña, el client_id y el usuario, tal cual o
        codificados para URL, por si Oracle los repite en un error."""
        texto = str(texto)
        for valor in (self.config.password, self.config.client_id, self.config.username):
            for forma in {valor, quote_plus(valor)}:
                if forma:
                    texto = texto.replace(forma, "***")
        return texto

    def _safe_error_message(self, response: requests.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = (payload.get("detail") or payload.get("message") or payload.get("title")
                          or payload.get("error_description") or payload.get("error"))
                code = payload.get("code") or payload.get("o:errorCode")
                pieces = [str(x) for x in (detail, code) if x not in (None, "")]
                if pieces:
                    return self._tachar(" · ".join(pieces))[:500]
        except ValueError:
            pass
        text = (response.text or "").strip().replace("\n", " ")
        return self._tachar(text)[:500] or "Oracle no dio detalles"

    def _pedir_sesion(self, metodo: str, url: str, paso: str, **kwargs: Any) -> requests.Response:
        """Una llamada del inicio de sesión, sin reintentos."""
        try:
            return getattr(self.session, metodo)(
                url,
                timeout=self.config.timeout_seconds,
                verify=self.config.verify_ssl,
                **kwargs,
            )
        except requests.RequestException as exc:
            # from None: el mensaje de requests trae la URL del authorize, con
            # el client_id en la query.
            raise OracleBIError(f"No se pudo conectar con Oracle ({paso}): {type(exc).__name__}") from None

    def authenticate(self, *, force_full: bool = False) -> TokenBundle:
        if not force_full and self.tokens and self.tokens.is_valid():
            return self.tokens

        if not force_full and self.tokens and self.tokens.refresh_token:
            try:
                return self._refresh_tokens(self.tokens.refresh_token)
            except OracleBIError:
                # Si se pasó la ventana del refresh o Oracle invalidó los tokens
                # tras una actualización, hay que volver a hacer el PKCE completo.
                self.tokens = None

        return self._full_pkce_authentication()

    def _full_pkce_authentication(self) -> TokenBundle:
        verifier, challenge = self._new_pkce_pair()
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "scope": "openid",
            "redirect_uri": "apiaccount://callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }

        response = self._pedir_sesion("get", self.authorize_url, "authorize", params=params)
        if not response.ok:
            raise OracleBIAuthError(
                f"Oracle rechazó la autorización (HTTP {response.status_code}): "
                f"{self._safe_error_message(response)}"
            )

        response = self._pedir_sesion(
            "post",
            self.signin_url,
            "signin",
            data={
                "username": self.config.username,
                "password": self.config.password,
                "orgname": self.config.org_identifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not response.ok:
            raise OracleBIAuthError(
                f"Oracle rechazó el inicio de sesión de la cuenta API (HTTP {response.status_code}): "
                f"{self._safe_error_message(response)}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise OracleBIError("El inicio de sesión de Oracle no respondió JSON.") from exc

        if not payload.get("success"):
            detail = payload.get("error") or payload.get("nextOp") or "el inicio de sesión no fue exitoso"
            raise OracleBIAuthError(
                f"Oracle rechazó el inicio de sesión de la cuenta API: {self._tachar(detail)}")

        redirect_url = str(payload.get("redirectUrl") or "")
        auth_code = parse_qs(urlparse(redirect_url).query).get("code", [""])[0]
        if not auth_code:
            raise OracleBIAuthError("El inicio de sesión de Oracle no regresó código de autorización.")

        response = self._pedir_sesion(
            "post",
            self.token_url,
            "token",
            data={
                "scope": "openid",
                "grant_type": "authorization_code",
                "client_id": self.config.client_id,
                "code_verifier": verifier,
                "code": auth_code,
                "redirect_uri": "apiaccount://callback",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            return self._consume_token_response(response)
        except OracleBIError as exc:
            raise OracleBIAuthError(str(exc)) from None

    def _refresh_tokens(self, refresh_token: str) -> TokenBundle:
        response = self._pedir_sesion(
            "post",
            self.token_url,
            "refresh",
            data={
                "scope": "openid",
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "refresh_token": refresh_token,
                "redirect_uri": "apiaccount://callback",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._consume_token_response(response)

    def _consume_token_response(self, response: requests.Response) -> TokenBundle:
        if not response.ok:
            raise OracleBIError(
                f"Oracle rechazó la petición del token (HTTP {response.status_code}): "
                f"{self._safe_error_message(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OracleBIError("Oracle respondió el token sin JSON.") from exc

        id_token = str(payload.get("id_token") or "")
        refresh_token = str(payload.get("refresh_token") or "")
        if not id_token:
            raise OracleBIError("La respuesta del token de Oracle no trae id_token.")

        try:
            expires_in = int(payload.get("expires_in", 1209600))
        except (TypeError, ValueError):
            expires_in = 1209600

        self.tokens = TokenBundle(
            id_token=id_token,
            refresh_token=refresh_token,
            expires_at_monotonic=time.monotonic() + max(60, expires_in),
        )
        return self.tokens

    def _consultar(self, endpoint: str, payload: dict[str, Any], id_token: str) -> requests.Response:
        """Un POST de datos, con reintentos ante 429, 5xx y fallas de red.

        Si se acaban los intentos con una respuesta de error, la regresa para
        que post() la reporte con su status. Si se acaban por fallas de red,
        levanta OracleBIError.
        """
        url = self._api_url(endpoint)
        headers = {
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
        }
        respuesta: requests.Response | None = None
        falla = ""
        for espera in (0, *ESPERAS_SEGUNDOS):
            if espera:
                self._dormir(min(ESPERA_MAXIMA_SEGUNDOS, max(espera, _retry_after(respuesta))))
            try:
                respuesta = self.session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self.config.timeout_seconds,
                    verify=self.config.verify_ssl,
                )
            except requests.exceptions.SSLError as exc:
                if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                    # Un certificado malo no se arregla esperando.
                    raise OracleBIError(f"Oracle {endpoint}: el certificado SSL no es válido") from None
                respuesta = None
                falla = type(exc).__name__
                continue
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as exc:
                # ChunkedEncodingError es la conexión cortada a media respuesta.
                respuesta = None
                falla = type(exc).__name__
                continue
            except requests.RequestException as exc:
                raise OracleBIError(f"Oracle {endpoint}: {type(exc).__name__}") from None
            if respuesta.status_code not in REINTENTAR_STATUS:
                return respuesta
        if respuesta is not None:
            return respuesta
        raise OracleBIError(
            f"Oracle {endpoint} no respondió después de {1 + len(ESPERAS_SEGUNDOS)} intentos ({falla})"
        )

    def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        tokens = self.authenticate()
        response = self._consultar(endpoint, payload, tokens.id_token)

        if response.status_code == 401:
            tokens = self.authenticate(force_full=True)
            response = self._consultar(endpoint, payload, tokens.id_token)

        if not response.ok:
            raise OracleBIError(
                f"Oracle {endpoint} falló (HTTP {response.status_code}): "
                f"{self._safe_error_message(response)}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise OracleBIError(f"Oracle {endpoint} no respondió JSON.") from exc
        if not isinstance(data, dict):
            raise OracleBIError(f"Oracle {endpoint} respondió algo que no es un objeto JSON.")
        return data

    def get_locations(self) -> dict[str, Any]:
        return self.post(
            "getLocationDimensions",
            {"applicationName": self.config.application_name},
        )

    def get_employees(self, loc_ref: str) -> dict[str, Any]:
        return self.post(
            "getEmployeeDimensions",
            {"locRef": str(loc_ref), "applicationName": self.config.application_name},
        )

    def get_job_codes(self, loc_ref: str) -> dict[str, Any]:
        return self.post(
            "getJobCodeDimensions",
            {"locRef": str(loc_ref), "applicationName": self.config.application_name},
        )

    def get_latest_business_date(self, loc_ref: str) -> dict[str, Any]:
        return self.post(
            "getLatestBusDt",
            {"locRef": str(loc_ref), "applicationName": self.config.application_name},
        )

    def get_timecards(
        self,
        loc_ref: str,
        business_date: date,
        *,
        include_adjustments: bool = True,
        changed_since_utc: str | None = None,
        emp_num: int | None = None,
        ext_payroll_id: str | None = None,
    ) -> dict[str, Any]:
        if emp_num is not None and ext_payroll_id:
            raise ValueError("Usa emp_num o ext_payroll_id, no los dos.")

        payload: dict[str, Any] = {
            "locRef": str(loc_ref),
            "busDt": business_date.isoformat(),
            "includeAdjustments": bool(include_adjustments),
            "applicationName": self.config.application_name,
        }
        if changed_since_utc:
            payload["changedSinceUTC"] = changed_since_utc
        if emp_num is not None:
            payload["empNum"] = int(emp_num)
        if ext_payroll_id:
            payload["extPayrollID"] = str(ext_payroll_id)
        data = self.post("getTimeCardDetails", payload)
        # Oracle normalmente regresa locRef y businessDates. Se guarda lo que se
        # pidió para que la cobertura y la revisión de ajustes sigan siendo
        # reproducibles aunque un gateway no regrese esos campos.
        data.setdefault("locRef", str(loc_ref))
        data["_requestedBusDt"] = business_date.isoformat()
        data["_includeAdjustmentsRequested"] = bool(include_adjustments)
        return data

    def get_employee_daily_totals(self, loc_ref: str, business_date: date) -> dict[str, Any]:
        """Totales por empleado y revenue center de un día: tips por tipo,
        service charges y ventas. No trae puesto, así que sirve para cuadrar,
        no para repartir."""
        data = self.post(
            "getEmployeeDailyTotals",
            {
                "locRef": str(loc_ref),
                "busDt": business_date.isoformat(),
                "applicationName": self.config.application_name,
            },
        )
        data.setdefault("locRef", str(loc_ref))
        data["_requestedBusDt"] = business_date.isoformat()
        return data

    def get_job_code_daily_totals(self, loc_ref: str, business_date: date) -> dict[str, Any]:
        """Horas y pago por revenue center y puesto de un día, sin persona. Es
        el único total de horas que Oracle da aparte de las tarjetas, así que
        sirve para cuadrarlas."""
        data = self.post(
            "getJobCodeDailyTotals",
            {
                "locRef": str(loc_ref),
                "busDt": business_date.isoformat(),
                "applicationName": self.config.application_name,
            },
        )
        data.setdefault("locRef", str(loc_ref))
        data["_requestedBusDt"] = business_date.isoformat()
        return data

    def get_timecards_range(
        self,
        loc_ref: str,
        start_date: date,
        end_date: date,
        *,
        include_adjustments: bool = True,
        maximum_days: int = 31,
    ) -> list[dict[str, Any]]:
        if end_date < start_date:
            raise ValueError("La fecha final no puede ser antes de la inicial.")
        day_count = (end_date - start_date).days + 1
        if day_count > maximum_days:
            raise ValueError(f"El rango no puede pasar de {maximum_days} días por corrida.")

        results: list[dict[str, Any]] = []
        current = start_date
        while current <= end_date:
            results.append(
                self.get_timecards(
                    loc_ref,
                    current,
                    include_adjustments=include_adjustments,
                )
            )
            current += timedelta(days=1)
        return results


def _retry_after(respuesta: requests.Response | None) -> float:
    """Los segundos que pide Oracle en Retry-After, o 0 si no pide."""
    if respuesta is None:
        return 0.0
    try:
        return max(0.0, float(respuesta.headers.get("Retry-After", 0)))
    except (TypeError, ValueError):
        return 0.0


def iter_timecards(payloads: Iterable[dict[str, Any]]) -> Iterable[tuple[str, str, dict[str, Any]]]:
    """Regresa (loc_ref, business_date, timecard) de las respuestas de Oracle.
    Lo que no tenga la forma esperada (una lista donde va una lista) se ignora;
    las horas que falten las detecta el cuadre contra el total por puesto."""
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        loc_ref = str(payload.get("locRef") or "")
        business_days = payload.get("businessDates")
        business_days = business_days if isinstance(business_days, list) else []
        if not business_days and isinstance(payload.get("timeCardDetails"), list):
            business_days = [{
                "busDt": payload.get("busDt") or payload.get("_requestedBusDt") or "",
                "timeCardDetails": payload.get("timeCardDetails", []),
            }]
        adjustments_requested = payload.get("_includeAdjustmentsRequested")
        for business_day in business_days:
            if not isinstance(business_day, dict):
                continue
            bus_dt = str(business_day.get("busDt") or payload.get("_requestedBusDt") or "")
            detalles = business_day.get("timeCardDetails")
            for timecard in detalles if isinstance(detalles, list) else []:
                if isinstance(timecard, dict):
                    if adjustments_requested is None:
                        yield loc_ref, bus_dt, timecard
                    else:
                        enriched = dict(timecard)
                        enriched["_adjustmentsRequested"] = adjustments_requested
                        yield loc_ref, bus_dt, enriched
