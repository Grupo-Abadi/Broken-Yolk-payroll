"""Pruebas de la pantalla con streamlit.testing. No se conectan a Oracle.

    python -m unittest test_app

La app lee volcados inventados (PAYROLL_VOLCADO), armados con
simphony_falso.Escenario dentro del último periodo cerrado. Los secrets de la
prueba siempre se dan completos, para que la app nunca lea el
.streamlit/secrets.toml real.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

import motor
import simphony
from simphony_falso import TIENDA, Escenario

CLAVE = "clave-de-prueba"
APP = str(Path(__file__).resolve().parent / "app.py")


def escenario(descuadre: bool = False) -> tuple[Escenario, str]:
    inicio, fin = motor.ultimo_periodo_cerrado()
    esc = Escenario()
    esc.tiendas_inactivas.append("BYC998")
    ana = esc.empleado("Ejemplo", "Ana")
    beto = esc.empleado("Ejemplo", "Beto", puesto=30, nombre_puesto="Cook", clase="Clock-In Only")
    caro = esc.empleado("Ejemplo", "Caro")
    dia = (inicio + timedelta(days=2)).isoformat()
    esc.turno(ana, dia, "07:00", "11:00", meal=31, hasta="14:30")
    esc.tramo(beto, dia, "05:00", "10:00", salida=simphony.SALIDA_A_BREAK)
    esc.tramo(beto, dia, "10:30", "18:30", ovt=1.0)        # 13 h: 4 de OT y 1 doble
    esc.turno(caro, dia, "07:00", "14:00")                  # 7 h sin meal
    esc.tips(ana, dia, tarjeta=80.0, pagados=60.0)
    esc.tips(esc.cuenta("Online"), dia, tarjeta=5.0)
    if descuadre:
        esc.descuadre(dia, 7.5)
    return esc, dia


def volcado(descuadre: bool = False) -> Path:
    esc, _ = escenario(descuadre)
    desde, hasta = motor.dias_a_pedir(*motor.ultimo_periodo_cerrado())
    return esc.guardar_volcado(Path(tempfile.mkdtemp()) / "volcado", desde, hasta)


class TestPantalla(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.carpeta = volcado()
        cls.carpeta_descuadrada = volcado(descuadre=True)

    def setUp(self):
        self.antes = {k: os.environ.get(k) for k in ("PAYROLL_VOLCADO", "APP_PASSWORD")}
        os.environ["PAYROLL_VOLCADO"] = str(self.carpeta)
        os.environ.pop("APP_PASSWORD", None)

    def tearDown(self):
        for k, v in self.antes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def app(self, clave: str | None = CLAVE) -> AppTest:
        at = AppTest.from_file(APP, default_timeout=60)
        # AppTest solo reemplaza st.secrets si se le da algo; si no, lee el
        # archivo real. Siempre se le da algo.
        at.secrets["PRUEBA"] = "1"
        if clave is not None:
            at.secrets["APP_PASSWORD"] = clave
        return at

    def entrar(self) -> AppTest:
        at = self.app().run()
        at.text_input[0].input(CLAVE)
        at.button[0].click().run()
        self.assertFalse(at.exception)
        return at

    def generar(self) -> AppTest:
        at = self.entrar()
        at.sidebar.button[0].click().run()
        self.assertFalse(at.exception)
        return at

    def test_sin_contrasena_configurada_no_abre(self):
        for clave in (None, "", "   ", "YOUR_APP_PASSWORD", "TU_CONTRASENA", "your_app_password"):
            with self.subTest(clave=clave):
                at = self.app(clave=clave).run()
                self.assertIn("no tiene contraseña", at.error[0].value)
                self.assertEqual(len(at.sidebar.button), 0)
                self.assertEqual(len(at.text_input), 0)

    def test_con_contrasena_incorrecta_no_entra(self):
        at = self.app().run()
        at.text_input[0].input("otra")
        at.button[0].click().run()
        self.assertIn("incorrecta", at.error[0].value)
        self.assertEqual(len(at.sidebar.button), 0)

    def test_una_contrasena_con_espacios_de_mas_si_entra(self):
        at = self.app(clave=CLAVE + " ").run()
        at.text_input[0].input(" " + CLAVE).run()
        at.button[0].click().run()
        self.assertEqual(len(at.error), 0)
        self.assertEqual(len(at.sidebar.button), 1)

    def test_una_tienda_sin_el_campo_active_no_se_ofrece(self):
        # Oracle omite los campos que valen false.
        carpeta = volcado()
        ruta = carpeta / "locations.json"
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        for tienda in datos["locations"]:
            if tienda["locRef"] == TIENDA:
                del tienda["active"]
        ruta.write_text(json.dumps(datos), encoding="utf-8")
        os.environ["PAYROLL_VOLCADO"] = str(carpeta)
        at = self.entrar()
        self.assertEqual(at.sidebar.multiselect[0].value, [])
        self.assertTrue(at.sidebar.button[0].disabled)

    def test_avisa_que_usa_un_volcado_y_solo_ofrece_tiendas_activas(self):
        at = self.entrar()
        self.assertIn("volcado local", at.sidebar.warning[0].value)
        tiendas = at.sidebar.multiselect[0]
        self.assertEqual(tiendas.value, [TIENDA])
        self.assertFalse([o for o in tiendas.options if "BYC998" in o])

    def test_genera_y_ensena_horas_primas_y_tips(self):
        at = self.generar()
        self.assertIn("cuadran con Oracle", at.success[0].value)
        tabla = at.dataframe[0].value.set_index("Empleado")
        beto, ana, caro = tabla.loc["Ejemplo, Beto"], tabla.loc["Ejemplo, Ana"], tabla.loc["Ejemplo, Caro"]
        self.assertEqual((beto["Regular"], beto["Overtime"], beto["Doble"], beto["OT de Oracle"]),
                         (8.0, 4.0, 1.0, 1.0))
        self.assertEqual((beto["Prima meal (h)"], caro["Prima meal (h)"], ana["Prima meal (h)"]), (1.0, 1.0, 0.0))
        self.assertEqual((ana["Tips tarjeta"], ana["Tips pagados"]), (80.0, 60.0))
        metricas = {m.label: m.value for m in at.metric}
        self.assertEqual((metricas["Personas"], metricas["Horas"], metricas["Overtime"], metricas["Doble"],
                          metricas["Horas de prima"], metricas["Tips con tarjeta"]),
                         ("3", "26.98", "4.00", "1.00", "2", "$85.00"))
        self.assertEqual(len(at.get("download_button")), 1)
        self.assertEqual(len(at.table), 0)
        self.assertTrue(any("SuperUser" in w.value for w in at.warning))

    def test_ensena_el_detalle_de_violaciones_y_los_tips_retenidos(self):
        at = self.generar()
        violaciones = at.dataframe[1].value.set_index("Empleado")
        self.assertEqual(sorted(violaciones.index), ["Ejemplo, Beto", "Ejemplo, Caro"])
        self.assertEqual(violaciones.loc["Ejemplo, Caro", "Violación"], simphony.SIN_MEAL)
        self.assertEqual(violaciones.loc["Ejemplo, Beto", "Violación"], simphony.SIN_SEGUNDO_MEAL)
        retenidos = at.dataframe[2].value.set_index("Clase de cuenta")
        self.assertEqual((retenidos.loc["SuperUser", "Tips tarjeta"], retenidos.loc["SuperUser", "Días con tips"]),
                         (5.0, 1))

    def test_un_resultado_que_bloquea_no_se_puede_descargar(self):
        os.environ["PAYROLL_VOLCADO"] = str(self.carpeta_descuadrada)
        at = self.generar()
        self.assertIn("No se puede concluir", at.error[0].value)
        self.assertIn("total por puesto", at.error[0].value)
        self.assertEqual(len(at.success), 0)
        self.assertEqual(len(at.get("download_button")), 0)
        self.assertTrue(any("solo para revisar" in c.value for c in at.caption))
        # Sin st.dataframe, que trae su propio "Download as CSV": van en st.table.
        self.assertEqual(len(at.dataframe), 0)
        self.assertGreaterEqual(len(at.table), 1)

    def test_cambiar_las_tiendas_pide_volver_a_generar(self):
        at = self.generar()
        at.sidebar.multiselect[0].unselect(TIENDA).run()
        self.assertIn("Cambiaste el periodo o las tiendas", at.info[0].value)
        self.assertEqual(len(at.dataframe), 0)
        self.assertTrue(at.sidebar.button[0].disabled)

    def test_cambiar_el_periodo_pide_volver_a_generar(self):
        at = self.generar()
        at.sidebar.selectbox[0].select(motor.periodos_recientes(8)[1]).run()
        self.assertIn("Cambiaste el periodo o las tiendas", at.info[0].value)
        self.assertEqual(len(at.dataframe), 0)

    def test_si_generar_truena_no_se_queda_el_resultado_anterior(self):
        at = self.generar()
        with mock.patch.object(motor, "calcular", side_effect=RuntimeError("se cayó a media corrida")):
            at.sidebar.button[0].click().run()
        self.assertTrue(at.exception)
        at.run()
        self.assertEqual(len(at.dataframe), 0)

    def test_un_resultado_de_otra_version_no_se_ensena(self):
        at = self.generar()
        at.session_state["resultado"] = {**at.session_state["resultado"], "version": "otra"}
        at.run()
        self.assertIn("se actualizó", at.info[0].value)
        self.assertEqual(len(at.dataframe), 0)


if __name__ == "__main__":
    unittest.main()
