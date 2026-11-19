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
from unittest import mock
from urllib.parse import quote_plus

import requests

from oracle_bi.client import (
    ESPERA_MAXIMA_SEGUNDOS,
    OracleBIAuthError,
    OracleBIClient,
    OracleBIError,
    TokenBundle,
    iter_timecards,
)
from oracle_bi.settings import config_from_secret_mapping, config_from_toml_file
import simphony
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

    def test_oracle_bi_le_gana_a_la_seccion_vieja(self):
        vieja = {**seccion()["oracle_bi"], "org_identifier": "VIEJA"}
        config = config_from_secret_mapping({**seccion(), "oracle": vieja})
        self.assertEqual(config.org_identifier, "BYC")

    def test_las_credenciales_de_labor_nunca_se_usan_para_la_bi(self):
        with self.assertRaisesRegex(ValueError, r"\[oracle_bi\]"):
            config_from_secret_mapping({"oracle_labor": seccion()["oracle_bi"]})

    def test_timeout_que_no_es_numero(self):
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            config_from_secret_mapping(seccion(timeout_seconds="mucho"))

    def test_las_credenciales_no_salen_al_imprimir_la_configuracion(self):
        config = config_from_secret_mapping(seccion())
        for secreto in (CONTRASENA, "cliente-prueba", "usuario-prueba"):
            with self.subTest(secreto=secreto):
                self.assertNotIn(secreto, repr(config))
                self.assertNotIn(secreto, str(config))

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

    def test_si_oracle_repite_las_credenciales_en_un_error_se_tachan(self):
        eco = f"usuario usuario-prueba con clave {CONTRASENA} y cliente cliente-prueba"
        for respuesta in (Respuesta(400, {"error_description": eco}), Respuesta(400, texto=eco)):
            with self.subTest(respuesta=respuesta.text[:20]):
                c, _ = cliente(SesionFalsa([respuesta]))
                with self.assertRaises(OracleBIError) as error:
                    c.get_locations()
                for secreto in (CONTRASENA, "usuario-prueba", "cliente-prueba"):
                    self.assertNotIn(secreto, str(error.exception))
                self.assertIn("***", str(error.exception))

    def test_un_error_de_conexion_al_iniciar_sesion_no_encadena_la_url(self):
        class SesionCaida(SesionFalsa):
            def get(self, url, **kwargs):
                raise requests.ConnectionError(f"no se pudo abrir {url}?client_id=cliente-prueba")
        c, _ = cliente(SesionCaida())
        with self.assertRaises(OracleBIError) as error:
            c.get_locations()
        self.assertIsNone(error.exception.__cause__)
        self.assertTrue(error.exception.__suppress_context__)
        self.assertNotIn("cliente-prueba", str(error.exception))

    def test_verify_ssl_llega_a_todas_las_llamadas(self):
        for valor in (True, False):
            with self.subTest(verify_ssl=valor):
                sesion = SesionFalsa([Respuesta(200, {"ok": True})])
                c = OracleBIClient(config_from_secret_mapping(seccion(verify_ssl=valor)))
                c.session = sesion
                c.get_locations()
                self.assertEqual([ll[2]["verify"] for ll in sesion.llamadas], [valor] * 4)

    def test_un_certificado_invalido_no_se_reintenta(self):
        sesion = SesionFalsa([requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")])
        c, esperas = cliente(sesion)
        with self.assertRaisesRegex(OracleBIError, "certificado"):
            c.get_locations()
        self.assertEqual((len(sesion.consultas()), esperas), (1, []))

    def test_otras_fallas_de_red_si_se_reintentan(self):
        for falla in (requests.exceptions.SSLError("EOF occurred in violation of protocol"),
                      requests.exceptions.ChunkedEncodingError("IncompleteRead"),
                      requests.Timeout("lento")):
            with self.subTest(falla=type(falla).__name__):
                sesion = SesionFalsa([falla, Respuesta(200, {"ok": True})])
                c, esperas = cliente(sesion)
                self.assertEqual(c.get_locations(), {"ok": True})
                self.assertEqual(esperas, [2])

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

    def test_totales_por_puesto_pide_tienda_y_dia(self):
        sesion = SesionFalsa([Respuesta(200, {"busDt": "2026-11-12", "revenueCenters": []})])
        c, _ = cliente(sesion)
        datos = c.get_job_code_daily_totals("BYC999", date(2026, 11, 12))
        _, url, kwargs = sesion.consultas()[0]
        self.assertTrue(url.endswith("/bi/v1/BYC/getJobCodeDailyTotals"))
        self.assertEqual((kwargs["json"]["locRef"], kwargs["json"]["busDt"]), ("BYC999", "2026-11-12"))
        self.assertEqual((datos["locRef"], datos["_requestedBusDt"]), ("BYC999", "2026-11-12"))

    def test_un_rechazo_del_inicio_de_sesion_es_oracle_bi_auth_error(self):
        class AutorizacionRechazada(SesionFalsa):
            def get(self, url, **kwargs):
                self.llamadas.append(("get", url, kwargs))
                return Respuesta(400, {"error": "invalid_client"})

        class TokenRechazado(SesionFalsa):
            def post(self, url, **kwargs):
                if url.endswith("/oauth2/token"):
                    self.llamadas.append(("post", url, kwargs))
                    return Respuesta(400, {"error": "invalid_grant"})
                return super().post(url, **kwargs)

        casos = {
            "authorize": AutorizacionRechazada(),
            "signin HTTP 401": SesionFalsa(signin=Respuesta(401, {"error": "credenciales"})),
            "signin sin éxito": SesionFalsa(signin=Respuesta(200, {"success": False, "error": "credenciales"})),
            "signin sin código": SesionFalsa(signin=Respuesta(200, {"success": True,
                                                                    "redirectUrl": "apiaccount://callback"})),
            "token": TokenRechazado(),
        }
        for caso, sesion in casos.items():
            with self.subTest(caso=caso):
                c, _ = cliente(sesion)
                with self.assertRaises(OracleBIAuthError):
                    c.get_locations()

    def test_la_contrasena_codificada_para_url_tambien_se_tacha(self):
        clave = "clave de prueba&1"
        c = OracleBIClient(config_from_secret_mapping(seccion(password=clave)))
        c.session = SesionFalsa([Respuesta(400, texto=f"cuerpo: password={quote_plus(clave)}")])
        c._dormir = lambda segundos: None
        with self.assertRaises(OracleBIError) as error:
            c.get_locations()
        self.assertNotIn(quote_plus(clave), str(error.exception))

    def test_el_inicio_de_sesion_no_se_reintenta(self):
        # Un código de autorización no se puede usar dos veces: si falla el
        # signin o el token, se reporta y no se repite.
        class SesionTokenCaido(SesionFalsa):
            def post(self, url, **kwargs):
                if url.endswith("/oauth2/token"):
                    self.llamadas.append(("post", url, kwargs))
                    raise requests.ConnectionError("caído")
                return super().post(url, **kwargs)

        casos = {
            "signin": SesionFalsa(signin=Respuesta(503, texto="ocupado")),
            "token": SesionTokenCaido(),
        }
        for paso, sesion in casos.items():
            with self.subTest(paso=paso):
                c, esperas = cliente(sesion)
                with self.assertRaises(OracleBIError):
                    c.get_locations()
                self.assertEqual(sum(1 for ll in sesion.llamadas if ll[1].endswith(f"/oauth2/{paso}")), 1)
                self.assertEqual((sesion.consultas(), esperas), ([], []))

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
    """Lo que el volcado le pide a OracleBIClient, con datos inventados.
    falla_en: un endpoint que siempre falla, o '*' para que falle todo menos
    getLocationDimensions."""

    def __init__(self, falla_en: str = ""):
        self.falla_en = falla_en
        self.llamadas = 0

    def _quizas_falla(self, endpoint: str):
        self.llamadas += 1
        if endpoint == self.falla_en or self.falla_en == "*":
            raise OracleBIError(f"Oracle {endpoint} falló (HTTP 403): sin permiso")

    def get_locations(self):
        return {"locations": [{"locRef": "BYC999", "name": "Tienda Ejemplo", "active": True},
                              {"locRef": "BYC998", "name": "Cerrada", "active": False}]}

    def get_employees(self, ref):
        self._quizas_falla("getEmployeeDimensions")
        return {"locRef": ref, "employees": [
            {"num": 9001, "fName": "Ana", "lName": "Ejemplo", "payrollId": "EJ9001"},
            {"num": 9002, "fName": "Beto", "lName": "Ejemplo", "payrollId": "EJ9002"}]}

    def get_job_codes(self, ref):
        self._quizas_falla("getJobCodeDimensions")
        return {"locRef": ref, "jobCodes": [{"num": 10, "name": "Server"}]}

    def get_latest_business_date(self, ref):
        self._quizas_falla("getLatestBusDt")
        return {"locRef": ref, "latestBusDt": "2026-11-12"}

    def get_timecards(self, ref, dia):
        self._quizas_falla("getTimeCardDetails")
        return fixture("tarjetas_con_break.json")

    def get_employee_daily_totals(self, ref, dia):
        self._quizas_falla("getEmployeeDailyTotals")
        return fixture("totales_diarios.json")

    def get_job_code_daily_totals(self, ref, dia):
        self._quizas_falla("getJobCodeDailyTotals")
        return {"locRef": ref, "busDt": dia.isoformat(), "revenueCenters": [
            {"rvcNum": 1, "jobCodes": [{"jcNum": 10, "regHrs": 6.62}, {"jcNum": 20, "regHrs": 8.0, "ovtHrs": 1.0}]}]}


def volcar(cliente_falso, dias: int = 2) -> tuple[dict, str, Path]:
    carpeta = Path(tempfile.mkdtemp()) / "volcado"
    lineas: list[str] = []
    fin = date(2026, 11, 11 + dias)
    resultado = volcado_simphony.correr(cliente_falso, date(2026, 11, 12), fin, [], carpeta,
                                        lineas.append)
    return resultado, "\n".join(lineas), carpeta


class TestVolcado(unittest.TestCase):
    def setUp(self):
        # Que ninguna prueba pueda llegar a las credenciales reales, a
        # reference/ ni a Oracle, aunque una validación falle.
        self.referencia = Path(tempfile.mkdtemp())

        class SinOracle:
            def __init__(self, *args, **kwargs):
                raise AssertionError("una prueba intentó conectarse a Oracle")

        for nombre, valor in (("SECRETS", FIXTURES / "no-existe.toml"),
                              ("REFERENCIA", self.referencia),
                              ("OracleBIClient", SinOracle)):
            parche = mock.patch.object(volcado_simphony, nombre, valor)
            parche.start()
            self.addCleanup(parche.stop)

    def secrets_inventados(self) -> Path:
        ruta = self.referencia / "secrets_inventados.toml"
        datos = seccion()["oracle_bi"]
        ruta.write_text("[oracle_bi]\n" + "".join(f'{k} = "{v}"\n' for k, v in datos.items()),
                        encoding="utf-8")
        return ruta

    def test_guarda_cada_respuesta_y_solo_pide_tiendas_activas(self):
        _, _, carpeta = volcar(ClienteFalso())
        archivos = sorted(p.name for p in carpeta.iterdir())
        self.assertIn("timecards_BYC999_2026-11-12.json", archivos)
        self.assertIn("employee_daily_totals_BYC999_2026-11-13.json", archivos)
        self.assertIn("job_code_daily_totals_BYC999_2026-11-13.json", archivos)
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
        self.assertRegex(texto, r"shiftType\s+viene en\s+6\s+con valor en\s+2")
        self.assertRegex(texto, r"adjustments\s+viene en\s+6\s+con valor en\s+2\s")
        self.assertRegex(texto, r"chrgTipsTtl\s+viene en\s+6\s+con valor en\s+6\s+float\s+suma 150\.00")
        self.assertIn("BYC999: 6 tarjetas de 2 empleado(s) en 2 días (0 sin tarjetas), 0 error(es)", texto)

    def test_una_suma_de_menos_de_3_personas_no_se_ensena_aunque_tenga_muchos_registros(self):
        # ovt1Hrs es de una sola persona, en dos días; regHrs es de dos
        # personas, en seis tarjetas.
        _, texto, _ = volcar(ClienteFalso())
        self.assertRegex(texto, r"ovt1Hrs\s+viene en\s+2\s+con valor en\s+2\s+float\s+suma sin enseñar")
        self.assertRegex(texto, r"regHrs\s+viene en\s+6\s+con valor en\s+6\s+float\s+suma sin enseñar")

    def test_una_consulta_que_falla_no_tumba_el_volcado(self):
        resultado, texto, carpeta = volcar(ClienteFalso(falla_en="getEmployeeDailyTotals"))
        self.assertEqual(len(resultado["errores"]), 2)
        self.assertIn("2 error(es)", texto)
        self.assertTrue((carpeta / "timecards_BYC999_2026-11-13.json").exists())
        errores = json.loads((carpeta / "errores.json").read_text(encoding="utf-8"))
        self.assertEqual(errores[0]["consulta"], "getEmployeeDailyTotals BYC999 2026-11-12")

    def test_las_llaves_que_parecen_datos_no_se_ensenan(self):
        inventario = volcado_simphony.Inventario()
        inventario.agregar({"porEmpleado": {"Ana Ejemplo": 1, "9001": 2, "Ana": 3, "EJ9001": 4,
                                            "okCampo": 5, "_requestedBusDt": 6}})
        texto = inventario.formatear("prueba")
        for dato in ("Ana", "9001", "EJ9001"):
            self.assertNotIn(dato, texto)
        self.assertIn("okCampo", texto)
        self.assertIn("_requestedBusDt", texto)
        self.assertIn("4 llave(s) que no parecen nombre de campo", texto)

    def test_si_oracle_rechaza_la_contrasena_se_corta_al_primer_intento(self):
        class Rechaza(ClienteFalso):
            def get_employees(self, ref):
                self.llamadas += 1
                raise OracleBIAuthError("Oracle rechazó el inicio de sesión de la cuenta API")
        falso = Rechaza()
        resultado, texto, _ = volcar(falso)
        self.assertEqual(falso.llamadas, 1)
        self.assertEqual(resultado["errores"][-1], {"consulta": "volcado",
                                                    "error": "Oracle no dejó iniciar sesión"})
        self.assertIn("ya no se pide nada más", texto)

    def test_un_archivo_danado_del_volcado_falla_como_una_consulta(self):
        carpeta = Path(tempfile.mkdtemp())
        (carpeta / "employees_BYC999.json").write_text("{ esto no es json", encoding="utf-8")
        (carpeta / "job_codes_BYC999.json").write_text("[1, 2]", encoding="utf-8")
        fuente = simphony.VolcadoFuente(carpeta)
        for metodo, mensaje in ((fuente.get_employees, "no se pudo leer"),
                                (fuente.get_job_codes, "no es un objeto"),
                                (fuente.get_latest_business_date, "no está en el volcado")):
            with self.subTest(metodo=metodo.__name__), self.assertRaisesRegex(OracleBIError, mensaje):
                metodo("BYC999")

    def test_se_corta_tras_tres_fallas_seguidas(self):
        falso = ClienteFalso(falla_en="*")
        resultado, texto, _ = volcar(falso)
        self.assertEqual(falso.llamadas, 3)
        self.assertEqual(resultado["errores"][-1]["consulta"], "volcado")
        self.assertIn("ya no se pide nada más", texto)

    def test_main_baja_a_una_carpeta_nueva_por_corrida(self):
        with mock.patch.object(volcado_simphony, "OracleBIClient", lambda config: ClienteFalso()), \
                redirect_stdout(io.StringIO()):
            argumentos = ["--desde", "2026-11-12", "--hasta", "2026-11-12", "--tiendas", "BYC999",
                          "--secrets", str(self.secrets_inventados())]
            self.assertEqual(volcado_simphony.main(argumentos), 0)
        carpetas = [p for p in self.referencia.iterdir() if p.is_dir()]
        self.assertEqual(len(carpetas), 1)
        self.assertTrue(carpetas[0].name.startswith("volcado_2026-11-12_2026-11-12_"))
        self.assertTrue((carpetas[0] / "timecards_BYC999_2026-11-12.json").exists())

    def test_main_con_errores_regresa_1(self):
        with mock.patch.object(volcado_simphony, "OracleBIClient",
                               lambda config: ClienteFalso(falla_en="getEmployeeDailyTotals")), \
                redirect_stdout(io.StringIO()):
            argumentos = ["--desde", "2026-11-12", "--hasta", "2026-11-12",
                          "--secrets", str(self.secrets_inventados())]
            self.assertEqual(volcado_simphony.main(argumentos), 1)

    def test_sin_credenciales_no_corre_y_no_ensena_nada(self):
        salida = io.StringIO()
        with redirect_stdout(salida):
            codigo = volcado_simphony.main(["--desde", "2026-11-12", "--hasta", "2026-11-12"])
        self.assertEqual(codigo, 2)
        self.assertIn("No se pudieron leer las credenciales", salida.getvalue())

    def test_rango_mayor_a_31_dias_no_se_acepta(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            volcado_simphony.main(["--desde", "2026-10-01", "--hasta", "2026-11-12",
                                   "--secrets", str(self.secrets_inventados())])


if __name__ == "__main__":
    unittest.main()
