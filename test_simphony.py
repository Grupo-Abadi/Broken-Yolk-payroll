"""Pruebas del cliente de la BI API y del volcado. No se conectan a Oracle.

    python -m unittest test_simphony

Usan los JSON de fixtures/, que son inventados pero tienen la forma que da
Oracle de verdad (volcado de BYC301, 12 nov 2026): el shiftType viene en el
tramo de trabajo que termina yéndose a break, y los tips vienen en los totales
diarios, no en las tarjetas.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

import requests

from oracle_bi.client import (
    ESPERA_MAXIMA_SEGUNDOS,
    OracleBIClient,
    OracleBIError,
    TokenBundle,
    iter_timecards,
)
from oracle_bi.settings import config_from_secret_mapping, config_from_toml_file
import volcado_simphony

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONTRASENA = "contrasena-de-prueba-123"


def fixture(nombre: str) -> dict:
    return json.loads((FIXTURES / nombre).read_text(encoding="utf-8"))


def seccion(**cambios) -> dict:
    base = {
        "auth_server": "https://auth.ejemplo.test",
        "application_server": "https://app.ejemplo.test",
        "org_identifier": "BYC",
        "client_id": "cliente-prueba",
        "username": "usuario-prueba",
        "password": CONTRASENA,
    }
    base.update(cambios)
    return {"oracle_bi": base}


class Respuesta:
    """Lo mínimo de requests.Response que usa el cliente."""

    def __init__(self, status: int = 200, datos=None, texto: str = "", headers=None):
        self.status_code = status
        self._datos = datos
        self.text = texto if texto else (json.dumps(datos) if datos is not None else "")
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self):
        if self._datos is None:
            raise ValueError("no es JSON")
        return self._datos


class SesionFalsa:
    """Contesta el inicio de sesión de Oracle y, para los datos, lo que se le
    ponga en la fila: una Respuesta o una excepción por cada llamada."""

    def __init__(self, fila_de_datos=None, signin=None):
        self.headers: dict = {}
        self.fila = list(fila_de_datos or [])
        self.signin = signin or Respuesta(
            200, {"success": True, "redirectUrl": "apiaccount://callback?code=codigo-1"}
        )
        self.llamadas: list[tuple[str, str, dict]] = []
        self.inicios_de_sesion = 0

    def get(self, url, **kwargs):
        self.llamadas.append(("get", url, kwargs))
        return Respuesta(200, {})

    def post(self, url, **kwargs):
        self.llamadas.append(("post", url, kwargs))
        if url.endswith("/oauth2/signin"):
            self.inicios_de_sesion += 1
            return self.signin
        if url.endswith("/oauth2/token"):
            return Respuesta(200, {"id_token": f"token-{self.inicios_de_sesion}",
                                   "refresh_token": "refresh-1", "expires_in": 1209600})
        siguiente = self.fila.pop(0)
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente

    def consultas(self):
        return [ll for ll in self.llamadas if "/bi/v1/" in ll[1]]


def cliente(sesion: SesionFalsa) -> tuple[OracleBIClient, list[float]]:
    c = OracleBIClient(config_from_secret_mapping(seccion()))
    c.session = sesion
    esperas: list[float] = []
    c._dormir = esperas.append
    return c, esperas


class TestConfiguracion(unittest.TestCase):
    def test_verify_ssl_false_en_texto_si_apaga_la_verificacion(self):
        for valor, esperado in (("false", False), ("False", False), (False, False), ("0", False),
                                ("true", True), (True, True), ("1", True)):
            with self.subTest(valor=valor):
                config = config_from_secret_mapping(seccion(verify_ssl=valor))
                self.assertIs(config.verify_ssl, esperado)

    def test_verify_ssl_raro_no_se_adivina(self):
        with self.assertRaisesRegex(ValueError, "verify_ssl"):
            config_from_secret_mapping(seccion(verify_ssl="quizas"))

    def test_los_valores_de_ejemplo_cuentan_como_faltantes(self):
        ejemplo = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml.example"
        with self.assertRaises(ValueError) as error:
            config_from_toml_file(ejemplo)
        for llave in ("auth_server", "application_server", "org_identifier",
                      "client_id", "username", "password"):
            self.assertIn(llave, str(error.exception))

    def test_acepta_la_seccion_vieja_de_mealviolation(self):
        config = config_from_secret_mapping({"oracle": seccion()["oracle_bi"]})
        self.assertEqual(config.org_identifier, "BYC")

    def test_timeout_que_no_es_numero(self):
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            config_from_secret_mapping(seccion(timeout_seconds="mucho"))

    def test_la_contrasena_no_sale_al_imprimir_la_configuracion(self):
        config = config_from_secret_mapping(seccion())
        self.assertNotIn(CONTRASENA, repr(config))
        self.assertNotIn(CONTRASENA, str(config))

    def test_los_tokens_no_salen_al_imprimirlos(self):
        tokens = TokenBundle(id_token="secreto-id", refresh_token="secreto-refresh",
                             expires_at_monotonic=1.0)
        self.assertNotIn("secreto", repr(tokens))

    def test_falta_la_seccion(self):
        with self.assertRaisesRegex(ValueError, r"\[oracle_bi\]"):
            config_from_secret_mapping({})


class TestCliente(unittest.TestCase):
    def test_reintenta_un_503_y_sigue(self):
        sesion = SesionFalsa([Respuesta(503, texto="ocupado"), Respuesta(200, {"locations": []})])
        c, esperas = cliente(sesion)
        self.assertEqual(c.get_locations(), {"locations": []})
        self.assertEqual(len(sesion.consultas()), 2)
        self.assertEqual(esperas, [2])

    def test_un_400_no_se_reintenta(self):
        sesion = SesionFalsa([Respuesta(400, {"detail": "locRef inválido"})])
        c, esperas = cliente(sesion)
        with self.assertRaisesRegex(OracleBIError, "HTTP 400.*locRef inválido"):
            c.get_employees("BYC999")
        self.assertEqual(len(sesion.consultas()), 1)
        self.assertEqual(esperas, [])

    def test_respeta_retry_after_con_tope(self):
        sesion = SesionFalsa([Respuesta(429, texto="espera", headers={"Retry-After": "7"}),
                              Respuesta(429, texto="espera", headers={"Retry-After": "600"}),
                              Respuesta(200, {"ok": True})])
        c, esperas = cliente(sesion)
        c.get_locations()
        self.assertEqual(esperas, [7.0, ESPERA_MAXIMA_SEGUNDOS])

    def test_se_rinde_tras_fallas_de_red(self):
        sesion = SesionFalsa([requests.ConnectionError("sin red")] * 4)
        c, esperas = cliente(sesion)
        with self.assertRaisesRegex(OracleBIError, "4 intentos.*ConnectionError"):
            c.get_locations()
        self.assertEqual(esperas, [2, 5, 10])

    def test_un_503_que_no_se_quita_se_reporta_con_su_status(self):
        sesion = SesionFalsa([Respuesta(503, texto="ocupado")] * 4)
        c, _ = cliente(sesion)
        with self.assertRaisesRegex(OracleBIError, "HTTP 503"):
            c.get_locations()

    def test_un_401_vuelve_a_iniciar_sesion_una_vez(self):
        sesion = SesionFalsa([Respuesta(401, texto="token vencido"), Respuesta(200, {"ok": True})])
        c, _ = cliente(sesion)
        self.assertEqual(c.get_locations(), {"ok": True})
        self.assertEqual(sesion.inicios_de_sesion, 2)
        tokens_usados = [ll[2]["headers"]["Authorization"] for ll in sesion.consultas()]
        self.assertEqual(tokens_usados, ["Bearer token-1", "Bearer token-2"])

    def test_el_error_de_inicio_de_sesion_no_trae_la_contrasena(self):
        sesion = SesionFalsa(signin=Respuesta(200, {"success": False, "error": "credenciales"}))
        c, _ = cliente(sesion)
        with self.assertRaises(OracleBIError) as error:
            c.get_locations()
        self.assertNotIn(CONTRASENA, str(error.exception))

    def test_totales_diarios_pide_tienda_y_dia(self):
        sesion = SesionFalsa([Respuesta(200, fixture("totales_diarios.json"))])
        c, _ = cliente(sesion)
        datos = c.get_employee_daily_totals("BYC999", date(2026, 11, 12))
        _, url, kwargs = sesion.consultas()[0]
        self.assertTrue(url.endswith("/bi/v1/BYC/getEmployeeDailyTotals"))
        self.assertEqual(kwargs["json"]["locRef"], "BYC999")
        self.assertEqual(kwargs["json"]["busDt"], "2026-11-12")
        self.assertEqual(kwargs["json"]["applicationName"], "Broken Yolk Payroll")
        self.assertEqual(datos["_requestedBusDt"], "2026-11-12")

    def test_tarjetas_piden_ajustes_y_guardan_lo_pedido(self):
        sesion = SesionFalsa([Respuesta(200, fixture("tarjetas_con_break.json"))])
        c, _ = cliente(sesion)
        datos = c.get_timecards("BYC999", date(2026, 11, 12))
        self.assertIs(sesion.consultas()[0][2]["json"]["includeAdjustments"], True)
        tarjetas = list(iter_timecards([datos]))
        self.assertEqual(len(tarjetas), 3)
        self.assertEqual({t[0] for t in tarjetas}, {"BYC999"})
        self.assertTrue(all(t[2]["_adjustmentsRequested"] for t in tarjetas))


class ClienteFalso:
    """Lo que el volcado le pide a OracleBIClient, con datos inventados."""

    def __init__(self, falla_en: str = ""):
        self.falla_en = falla_en

    def _quizas_falla(self, endpoint: str):
        if endpoint == self.falla_en:
            raise OracleBIError(f"Oracle {endpoint} falló (HTTP 403): sin permiso")

    def get_locations(self):
        return {"locations": [{"locRef": "BYC999", "name": "Tienda Ejemplo", "active": True},
                              {"locRef": "BYC998", "name": "Cerrada", "active": False}]}

    def get_employees(self, ref):
        return {"locRef": ref, "employees": [
            {"num": 9001, "fName": "Ana", "lName": "Ejemplo", "payrollId": "EJ9001"},
            {"num": 9002, "fName": "Beto", "lName": "Ejemplo", "payrollId": "EJ9002"}]}

    def get_job_codes(self, ref):
        return {"locRef": ref, "jobCodes": [{"num": 10, "name": "Server"}]}

    def get_latest_business_date(self, ref):
        return {"locRef": ref, "latestBusDt": "2026-11-12"}

    def get_timecards(self, ref, dia):
        return fixture("tarjetas_con_break.json")

    def get_employee_daily_totals(self, ref, dia):
        self._quizas_falla("getEmployeeDailyTotals")
        return fixture("totales_diarios.json")


def volcar(cliente_falso, dias: int = 2) -> tuple[dict, str, Path]:
    carpeta = Path(tempfile.mkdtemp()) / "volcado"
    lineas: list[str] = []
    fin = date(2026, 11, 11 + dias)
    resultado = volcado_simphony.correr(cliente_falso, date(2026, 11, 12), fin, [], carpeta,
                                        lineas.append)
    return resultado, "\n".join(lineas), carpeta


class TestVolcado(unittest.TestCase):
    def test_guarda_cada_respuesta_y_solo_pide_tiendas_activas(self):
        _, _, carpeta = volcar(ClienteFalso())
        archivos = sorted(p.name for p in carpeta.iterdir())
        self.assertIn("timecards_BYC999_2026-11-12.json", archivos)
        self.assertIn("employee_daily_totals_BYC999_2026-11-13.json", archivos)
        self.assertIn("employees_BYC999.json", archivos)
        self.assertFalse([a for a in archivos if "BYC998" in a])

    def test_en_pantalla_no_sale_ningun_dato_de_una_persona(self):
        _, texto, carpeta = volcar(ClienteFalso())
        inventario = (carpeta / "inventario.txt").read_text(encoding="utf-8")
        for dato in ("Ana", "Beto", "EJ9001", "EJ9002", "9001", "Gerente Ejemplo",
                     "Olvido checar", "07:00", "17.0", "55.5", "12.0"):
            with self.subTest(dato=dato):
                self.assertNotIn(dato, texto)
                self.assertNotIn(dato, inventario)

    def test_dice_que_campos_vienen_y_en_cuantos_registros(self):
        _, texto, _ = volcar(ClienteFalso())
        self.assertRegex(texto, r"shiftType\s+viene en\s+2\s+con valor en\s+2")
        self.assertRegex(texto, r"chrgTipsTtl\s+viene en\s+4\s+con valor en\s+4\s+float\s+suma 135\.00")
        self.assertRegex(texto, r"ovt1Hrs\s+viene en\s+2\s+con valor en\s+2\s+float\s+suma sin enseñar")
        self.assertIn("BYC999: 6 tarjetas de 2 empleado(s) en 2 días (0 sin tarjetas), 0 error(es)", texto)

    def test_una_consulta_que_falla_no_tumba_el_volcado(self):
        resultado, texto, carpeta = volcar(ClienteFalso(falla_en="getEmployeeDailyTotals"))
        self.assertEqual(len(resultado["errores"]), 2)
        self.assertIn("2 error(es)", texto)
        self.assertTrue((carpeta / "timecards_BYC999_2026-11-13.json").exists())
        errores = json.loads((carpeta / "errores.json").read_text(encoding="utf-8"))
        self.assertEqual(errores[0]["consulta"], "getEmployeeDailyTotals BYC999 2026-11-12")

    def test_las_llaves_que_parecen_datos_no_se_ensenan(self):
        inventario = volcado_simphony.Inventario()
        inventario.agregar({"porEmpleado": {"Ana Ejemplo": 1, "9001": 2, "ok_campo": 3}})
        texto = inventario.formatear("prueba")
        self.assertNotIn("Ana Ejemplo", texto)
        self.assertNotIn("9001", texto)
        self.assertIn("ok_campo", texto)
        self.assertIn("2 llave(s) que no parecen nombre de campo", texto)

    def test_sin_credenciales_no_corre_y_no_ensena_nada(self):
        salida = io.StringIO()
        with redirect_stdout(salida):
            codigo = volcado_simphony.main(["--desde", "2026-11-12", "--hasta", "2026-11-12",
                                            "--secrets", str(FIXTURES / "no-existe.toml")])
        self.assertEqual(codigo, 2)
        self.assertIn("No se pudieron leer las credenciales", salida.getvalue())

    def test_rango_mayor_a_31_dias_no_se_acepta(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            volcado_simphony.main(["--desde", "2026-10-01", "--hasta", "2026-11-12"])


if __name__ == "__main__":
    unittest.main()
