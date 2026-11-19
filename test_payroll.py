"""Pruebas del lector de Simphony, el motor y el calendario. No se conectan a Oracle.

    python -m unittest test_payroll

Los escenarios se arman con simphony_falso.Escenario, que da tarjetas y tips
inventados con la forma real de Oracle.
"""
from __future__ import annotations

import io
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import ca_overtime
import motor
import simphony
from oracle_bi import client as cliente_oracle
from oracle_bi.client import OracleBIAuthError
from simphony_falso import TIENDA, Escenario

INICIO, FIN = date(2026, 10, 31), date(2026, 11, 13)   # sábado a viernes
LUNES = "2026-11-02"


def persona(resultado: dict, nombre: str) -> dict:
    return next(p for p in resultado["personas"] if p["nombre"].startswith(nombre))


def tipos(resultado: dict, nivel: str | None = None) -> list[str]:
    return [a["tipo"] for a in resultado["avisos"] if nivel is None or a["nivel"] == nivel]


def texto_de(resultado: dict, tipo: str) -> str:
    return next(a["texto"] for a in resultado["avisos"] if a["tipo"] == tipo)


def jornada_de(esc: Escenario, dia: str = LUNES) -> simphony.Jornada:
    datos = simphony.jalar(esc.fuente(), esc.tiendas, date.fromisoformat(dia), date.fromisoformat(dia))
    tramos, _ = simphony.tramos_de(datos)
    jornadas = list(simphony.jornadas_de(tramos).values())
    assert len(jornadas) == 1, jornadas
    return jornadas[0]


def una(esc_configurar) -> simphony.Jornada:
    esc = Escenario()
    ana = esc.empleado("Ejemplo", "Ana")
    esc_configurar(esc, ana)
    return jornada_de(esc)


class TestOvertime(unittest.TestCase):
    def reparto(self, horas_por_dia: dict, inicio_semana: int = ca_overtime.WORKWEEK_START_WEEKDAY):
        return ca_overtime.compute_overtime({"p": horas_por_dia}, inicio_semana)["p"]

    def semana(self, horas: list[float], desde: date = date(2026, 10, 31)) -> dict:
        return {desde + timedelta(days=i): h for i, h in enumerate(horas)}

    def test_mas_de_8_es_overtime_y_mas_de_12_es_doble(self):
        dias = self.reparto({date(2026, 11, 2): 10.0, date(2026, 11, 3): 13.0})["days"]
        self.assertEqual((dias[date(2026, 11, 2)]["regular"], dias[date(2026, 11, 2)]["overtime"]), (8.0, 2.0))
        self.assertEqual((dias[date(2026, 11, 3)]["overtime"], dias[date(2026, 11, 3)]["double"]), (4.0, 1.0))

    def test_mas_de_40_a_la_semana(self):
        totales = self.reparto(self.semana([7.5] * 6))["totals"]
        self.assertEqual((totales["regular"], totales["overtime"]), (40.0, 5.0))

    def test_no_se_piramida(self):
        # 5 días de 10 h: 10 h de overtime diario, y el tiempo recto (40) no
        # pasa de 40, así que no hay más overtime semanal.
        totales = self.reparto(self.semana([10.0] * 5))["totals"]
        self.assertEqual((totales["regular"], totales["overtime"], totales["double"]), (40.0, 10.0, 0.0))

    def test_septimo_dia_de_la_semana_laboral(self):
        viernes = self.reparto(self.semana([6.0] * 7))["days"][date(2026, 11, 6)]
        self.assertEqual((viernes["regular"], viernes["overtime"]), (0.0, 6.0))
        self.assertEqual(viernes["rule"], "7mo dia consecutivo")

    def test_el_septimo_dia_arriba_de_8_es_doble(self):
        viernes = self.reparto(self.semana([6.0] * 6 + [10.0]))["days"][date(2026, 11, 6)]
        self.assertEqual((viernes["regular"], viernes["overtime"], viernes["double"]), (0.0, 8.0, 2.0))

    def test_solo_el_septimo_dia_es_septimo_y_seis_dias_no_alcanzan(self):
        completa = self.reparto(self.semana([6.0] * 7))["days"]
        self.assertEqual(completa[date(2026, 11, 5)]["rule"], "recto")
        seis = self.reparto(self.semana([6.0] * 6))["days"]
        self.assertTrue(all(d["rule"] == "recto" for d in seis.values()))

    def test_un_dia_en_cero_no_cuenta_para_el_septimo_dia(self):
        # Para ca_overtime, un día con 0 h no es un día trabajado.
        viernes = self.reparto(self.semana([6.0, 6.0, 6.0, 6.0, 0.0, 6.0, 6.0]))["days"][date(2026, 11, 6)]
        self.assertEqual(viernes["rule"], "recto")

    def test_si_descansa_el_miercoles_el_viernes_no_es_septimo_dia(self):
        semana = self.semana([6.0] * 7)
        del semana[date(2026, 11, 4)]
        self.assertEqual(self.reparto(semana)["days"][date(2026, 11, 6)]["rule"], "recto")

    def test_el_septimo_dia_respeta_el_inicio_de_semana_que_se_pasa(self):
        # Domingo a sábado: con semana de domingo es un 7mo día; con la de
        # sábado son dos semanas y no lo es.
        corrida = self.semana([6.0] * 7, desde=date(2026, 11, 1))
        sabado = date(2026, 11, 7)
        self.assertEqual(self.reparto(corrida, inicio_semana=6)["days"][sabado]["rule"], "7mo dia consecutivo")
        self.assertEqual(self.reparto(corrida, inicio_semana=5)["days"][sabado]["rule"], "recto")

    def test_acepta_fechas_de_simphony_y_de_toast(self):
        self.assertEqual(ca_overtime.parse_business_date("2026-11-02"), date(2026, 11, 2))
        self.assertEqual(ca_overtime.parse_business_date("20261102"), date(2026, 11, 2))


class TestMeals(unittest.TestCase):
    def test_meal_completo_a_tiempo(self):
        j = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "11:00", meal=31, hasta="14:30"))
        self.assertEqual(simphony.auditar(j), [])
        self.assertTrue(j.meals[0].declarado)
        self.assertAlmostEqual(j.meals[0].horas_antes, 4.0)

    def test_un_meal_de_30_exactos_cuenta_y_uno_de_29_y_medio_ya_no(self):
        # Sin tolerancia: con el redondeo de Oracle un meal de 30 min mide 30:00
        # o más, y en California los meals no se redondean.
        bien = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:00"))
        self.assertEqual(simphony.auditar(bien), [])
        corto = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "11:00", meal=29.5, hasta="14:00"))
        self.assertFalse(corto.meals[0].completo)
        self.assertEqual(simphony.auditar(corto), [simphony.SIN_MEAL])

    def test_seis_horas_sin_meal_viola_la_estricta_y_no_con_waivers(self):
        j = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "13:00"))
        self.assertEqual(simphony.auditar(j, simphony.ESTRICTA), [simphony.SIN_MEAL])
        self.assertEqual(simphony.auditar(j, simphony.CON_WAIVERS), [])

    def test_el_primer_meal_se_debe_pasando_de_5_horas(self):
        exactas = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "12:00"))
        self.assertEqual(simphony.auditar(exactas, simphony.ESTRICTA), [])
        un_minuto_mas = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "12:01"))
        self.assertEqual(simphony.auditar(un_minuto_mas, simphony.ESTRICTA), [simphony.SIN_MEAL])

    def test_el_primer_meal_empieza_a_mas_tardar_al_terminar_la_hora_5(self):
        a_tiempo = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "12:00", meal=30, hasta="14:00"))
        self.assertEqual(simphony.auditar(a_tiempo, simphony.ESTRICTA), [])
        tarde = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "12:01", meal=30, hasta="14:00"))
        self.assertEqual(simphony.auditar(tarde, simphony.ESTRICTA), [simphony.MEAL_TARDE])
        # Con waivers el límite de la hora 5 es el mismo.
        self.assertEqual(simphony.auditar(tarde, simphony.CON_WAIVERS), [simphony.MEAL_TARDE])

    def test_con_waivers_el_primer_meal_se_debe_pasando_de_6_horas(self):
        seis = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "13:00"))
        self.assertEqual(simphony.auditar(seis, simphony.CON_WAIVERS), [])
        seis_y_uno = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "13:01"))
        self.assertEqual(simphony.auditar(seis_y_uno, simphony.CON_WAIVERS), [simphony.SIN_MEAL])

    def test_el_segundo_meal_se_debe_pasando_de_10_horas(self):
        diez = una(lambda esc, ana: esc.turno(ana, LUNES, "06:00", "10:00", meal=30, hasta="16:30"))
        self.assertEqual(simphony.auditar(diez, simphony.ESTRICTA), [])
        diez_y_uno = una(lambda esc, ana: esc.turno(ana, LUNES, "06:00", "10:00", meal=30, hasta="16:31"))
        self.assertEqual(simphony.auditar(diez_y_uno, simphony.ESTRICTA), [simphony.SIN_SEGUNDO_MEAL])

    def test_con_waivers_el_segundo_meal_se_debe_pasando_de_12_horas(self):
        doce = una(lambda esc, ana: esc.turno(ana, LUNES, "06:00", "10:00", meal=30, hasta="18:30"))
        self.assertEqual(simphony.auditar(doce, simphony.CON_WAIVERS), [])
        self.assertEqual(simphony.auditar(doce, simphony.ESTRICTA), [simphony.SIN_SEGUNDO_MEAL])
        doce_y_uno = una(lambda esc, ana: esc.turno(ana, LUNES, "06:00", "10:00", meal=30, hasta="18:31"))
        self.assertEqual(simphony.auditar(doce_y_uno, simphony.CON_WAIVERS), [simphony.SIN_SEGUNDO_MEAL])

    def segundo_meal(self, horas_antes_del_segundo: float) -> simphony.Jornada:
        """06:00-10:00, meal de 30 min, un segundo tramo hasta que lleve esas
        horas trabajadas, otro meal de 30 y 2 h más: siempre pasa de 10 h."""
        sale = datetime(2026, 11, 2, 10, 30) + timedelta(hours=horas_antes_del_segundo - 4.0)
        regresa = sale + timedelta(minutes=30)

        def armar(esc, ana):
            esc.tramo(ana, LUNES, "06:00", "10:00", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "10:30", sale.strftime("%H:%M"), salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, regresa.strftime("%H:%M"), (regresa + timedelta(hours=2)).strftime("%H:%M"))
        j = una(armar)
        assert j.horas_trabajadas > 10, j.horas_trabajadas
        return j

    def test_dos_meals_a_tiempo(self):
        j = self.segundo_meal(9.0)
        self.assertEqual([round(m.horas_antes, 2) for m in j.meals], [4.0, 9.0])
        self.assertEqual(simphony.auditar(j, simphony.ESTRICTA), [])

    def test_el_segundo_meal_justo_a_las_10_horas_esta_a_tiempo(self):
        self.assertEqual(simphony.auditar(self.segundo_meal(10.0), simphony.ESTRICTA), [])

    def test_el_segundo_meal_despues_de_la_hora_10_es_tarde(self):
        j = self.segundo_meal(10.5)
        self.assertEqual(simphony.auditar(j, simphony.ESTRICTA), [simphony.SEGUNDO_MEAL_TARDE])

    def test_el_segundo_meal_un_minuto_despues_de_la_hora_10_es_tarde(self):
        j = self.segundo_meal(10 + 1 / 60)
        for politica in (simphony.ESTRICTA, simphony.CON_WAIVERS):
            with self.subTest(politica=politica.nombre):
                self.assertEqual(simphony.auditar(j, politica), [simphony.SEGUNDO_MEAL_TARDE])

    def test_el_segundo_meal_es_el_segundo_meal_completo(self):
        def tres_completos(esc, ana):
            # Meals completos a las 4, 9 y 10.5 h: el segundo (a las 9) va a tiempo.
            esc.tramo(ana, LUNES, "06:00", "10:00", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "10:30", "15:30", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "16:00", "17:30", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "18:00", "19:00")

        def corto_antes(esc, ana):
            # Break de 10 min a las 2 h y meals a las 3.83 y a las 10.33 h: el segundo es tarde.
            esc.tramo(ana, LUNES, "06:00", "08:00", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "08:10", "10:00", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "10:30", "17:00", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "17:30", "18:30")
        self.assertEqual(simphony.auditar(una(tres_completos)), [])
        self.assertEqual(simphony.auditar(una(corto_antes)), [simphony.SEGUNDO_MEAL_TARDE])

    def test_un_break_corto_no_cuenta_como_segundo_meal(self):
        def armar(esc, ana):
            esc.tramo(ana, LUNES, "06:00", "10:00", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "10:30", "13:00", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "13:10", "17:00")
        self.assertEqual(simphony.auditar(una(armar)), [simphony.SIN_SEGUNDO_MEAL])

    def test_un_meal_de_29_59_ya_no_cuenta(self):
        def armar(esc, ana):
            esc.tramo(ana, LUNES, "07:00", "11:00", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "11:29:59", "14:00")
        j = una(armar)
        self.assertFalse(j.meals[0].completo)
        self.assertEqual(simphony.auditar(j), [simphony.SIN_MEAL])

    def test_las_horas_exactas_no_se_pasan_por_el_float(self):
        # Son 5 o 10 h exactas, pero en float se pasan por un pelo: con sum(),
        # 2 + 35 + 263 min dan 5.000000000000001 y 4 + 70 + 526, 10.000000000000002;
        # con el += de jornadas_de, 8 + 237 + 55 dan 5.000000000000001 y
        # 180 + 310 + 110, 10.000000000000002.
        casos = {
            "pasa de 5 h": ([("07:00", "07:02", 84), ("07:02", "07:37", 84), ("07:37", "12:00", 84)], []),
            "meal a las 5 h": ([("07:00", "07:08", 84), ("07:08", "11:05", 84), ("11:05", "12:00", 66),
                                ("12:30", "14:00", 84)], []),
            "pasa de 10 h": ([("06:00", "06:04", 84), ("06:04", "07:14", 84), ("07:14", "16:00", 84)],
                             [simphony.SIN_MEAL]),
            "2do meal a las 10 h": ([("06:00", "09:00", 66), ("09:30", "14:40", 84), ("14:40", "16:30", 66),
                                     ("17:00", "18:00", 84)], []),
        }
        for caso, (tramos, esperado) in casos.items():
            with self.subTest(caso=caso):
                def armar(esc, ana, tramos=tramos):
                    for entra, sale, salida in tramos:
                        esc.tramo(ana, LUNES, entra, sale, salida=salida)
                self.assertEqual(simphony.auditar(una(armar), simphony.ESTRICTA), esperado)

    def test_las_horas_antes_del_meal_cuentan_los_tramos_encimados(self):
        def armar(esc, ana):
            esc.puesto(20, "Host")
            esc.tramo(ana, LUNES, "07:00", "12:00")
            esc.tramo(ana, LUNES, "11:00", "12:30", puesto=20, salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "13:00", "15:00")
        j = una(armar)
        self.assertEqual(j.encimados, 1)
        self.assertEqual(simphony.auditar(j), [simphony.MEAL_TARDE])

    def test_un_80_con_shift_type_0_tampoco_es_meal(self):
        def armar(esc, ana):
            esc.tramo(ana, LUNES, "07:00", "10:00", salida=simphony.SALIDA_A_BREAK_PAGADO)["shiftType"] = 0
            esc.tramo(ana, LUNES, "10:30", "13:30")
        self.assertEqual(una(armar).meals, [])

    def test_un_66_con_shift_type_0_tambien_es_salida_a_break(self):
        # Así llegaron 11 de las 1,322 salidas a comer del volcado.
        def armar(esc, ana):
            esc.tramo(ana, LUNES, "07:00", "10:00", salida=simphony.SALIDA_A_BREAK)["shiftType"] = 0
            esc.tramo(ana, LUNES, "10:15", "12:00")
        j = una(armar)
        self.assertEqual([(m.declarado, m.completo) for m in j.meals], [(True, False)])

    def test_un_meal_con_regreso_no_es_break_sin_regreso(self):
        j = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30"))
        self.assertEqual(j.sin_regreso, 0)

    def test_un_hueco_de_29_minutos_sin_66_no_es_meal(self):
        def armar(esc, ana):
            esc.tramo(ana, LUNES, "07:00", "10:00")
            esc.tramo(ana, LUNES, "10:29", "13:30")
        self.assertEqual(una(armar).meals, [])

    def test_un_meal_de_una_hora_no_es_turno_partido(self):
        j = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "11:00", meal=60, hasta="15:00"))
        self.assertEqual(j.huecos_largos, 0)

    def test_un_break_corto_antes_no_cuenta_como_el_primer_meal(self):
        def armar(esc, ana):
            esc.tramo(ana, LUNES, "07:00", "09:00", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "09:10", "12:30", salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "13:00", "15:00")
        j = una(armar)
        self.assertEqual([m.completo for m in j.meals], [False, True])
        self.assertEqual(simphony.auditar(j), [simphony.MEAL_TARDE])

    def test_las_horas_antes_del_meal_suman_aunque_cambie_de_puesto(self):
        def armar(esc, ana):
            esc.puesto(20, "Host")
            esc.tramo(ana, LUNES, "07:00", "09:00")
            esc.tramo(ana, LUNES, "09:00", "12:30", puesto=20, salida=simphony.SALIDA_A_BREAK)
            esc.tramo(ana, LUNES, "13:00", "15:00")
        j = una(armar)
        self.assertAlmostEqual(j.meals[0].horas_antes, 5.5)
        self.assertEqual(simphony.auditar(j), [simphony.MEAL_TARDE])

    def test_un_hueco_de_30_minutos_sin_codigo_66_tambien_es_meal(self):
        def armar(esc, ana):
            esc.tramo(ana, LUNES, "07:00", "11:00")
            esc.tramo(ana, LUNES, "11:30", "14:00")
        j = una(armar)
        self.assertEqual(simphony.auditar(j), [])
        self.assertFalse(j.meals[0].declarado)

    def test_un_hueco_de_20_minutos_sin_66_no_es_meal(self):
        def armar(esc, ana):
            esc.tramo(ana, LUNES, "07:00", "10:00")
            esc.tramo(ana, LUNES, "10:20", "13:30")
        j = una(armar)
        self.assertEqual(j.meals, [])
        self.assertEqual(simphony.auditar(j), [simphony.SIN_MEAL])

    def test_un_break_pagado_no_es_meal(self):
        for como in ("salida 80", "shiftType 1"):
            with self.subTest(como=como):
                def armar(esc, ana):
                    if como == "salida 80":
                        esc.tramo(ana, LUNES, "07:00", "10:00", salida=simphony.SALIDA_A_BREAK_PAGADO)
                    else:
                        esc.tramo(ana, LUNES, "07:00", "10:00")["shiftType"] = simphony.TURNO_A_BREAK_PAGADO
                    esc.tramo(ana, LUNES, "10:30", "13:30")
                j = una(armar)
                self.assertEqual(j.meals, [])
                self.assertEqual(simphony.auditar(j), [simphony.SIN_MEAL])

    def test_shift_type_2_sin_66_tambien_es_salida_a_break(self):
        def armar(esc, ana):
            esc.tramo(ana, LUNES, "07:00", "10:00")["shiftType"] = simphony.TURNO_A_BREAK
            esc.tramo(ana, LUNES, "10:15", "12:00")
        j = una(armar)
        self.assertEqual([(m.declarado, m.completo) for m in j.meals], [(True, False)])

    def test_cambiar_de_puesto_sin_salir_no_es_meal_y_las_horas_suman(self):
        def armar(esc, ana):
            esc.puesto(20, "Host")
            esc.tramo(ana, LUNES, "07:00", "10:00")
            esc.tramo(ana, LUNES, "10:00", "13:00", puesto=20)
        j = una(armar)
        self.assertEqual(j.meals, [])
        self.assertAlmostEqual(j.horas, 6.0)
        self.assertEqual(simphony.auditar(j), [simphony.SIN_MEAL])

    def test_salir_a_break_y_no_regresar_no_es_meal(self):
        j = una(lambda esc, ana: esc.tramo(ana, LUNES, "07:00", "13:00", salida=simphony.SALIDA_A_BREAK))
        self.assertEqual(j.sin_regreso, 1)
        self.assertEqual(simphony.auditar(j), [simphony.SIN_MEAL])

    def test_los_meals_se_miden_con_lo_que_duraron_las_checadas(self):
        # Oracle paga 5.0 h por un tramo que duró 5.5: el meal empezó tarde.
        j = una(lambda esc, ana: esc.turno(ana, LUNES, "07:00", "12:30", meal=30, hasta="15:00",
                                           horas=5.0))
        self.assertAlmostEqual(j.meals[0].horas_antes, 5.5)
        self.assertEqual(simphony.auditar(j), [simphony.MEAL_TARDE])

    def test_una_tarjeta_pagada_en_0_horas_igual_cuenta_para_el_meal(self):
        def armar(esc, ana):
            esc.tramo(ana, LUNES, "06:00", "09:00", horas=0.0)
            esc.tramo(ana, LUNES, "09:00", "12:00")
        j = una(armar)
        self.assertAlmostEqual(j.horas, 3.0)
        self.assertAlmostEqual(j.horas_trabajadas, 6.0)
        self.assertEqual(simphony.auditar(j), [simphony.SIN_MEAL])

    def test_turno_partido_y_tramos_encimados_se_cuentan(self):
        partido = una(lambda esc, ana: esc.turno(ana, LUNES, "06:00", "10:00", meal=300, hasta="19:00"))
        self.assertEqual((partido.huecos_largos, len(partido.meals)), (1, 1))

        def encimados(esc, ana):
            esc.puesto(20, "Host")
            esc.tramo(ana, LUNES, "07:00", "12:00")
            esc.tramo(ana, LUNES, "10:00", "14:00", puesto=20)
        self.assertEqual(una(encimados).encimados, 1)

    def test_un_meal_que_cruza_la_medianoche(self):
        j = una(lambda esc, ana: esc.turno(ana, LUNES, "20:00", "23:45", meal=30, hasta="02:00"))
        self.assertEqual(len(j.tramos), 2)
        self.assertLess(j.tramos[0].entra, j.tramos[1].entra)
        self.assertEqual(round(j.meals[0].minutos), 30)
        self.assertAlmostEqual(j.horas_trabajadas, 3.75 + 1.75)

    def test_la_prima_tiene_tope_por_dia(self):
        dos = [simphony.SIN_MEAL, simphony.SIN_SEGUNDO_MEAL]
        self.assertEqual(simphony.horas_de_prima(dos, simphony.ESTRICTA), 1.0)
        self.assertEqual(simphony.horas_de_prima(dos, simphony.CON_WAIVERS), 1.0)
        sin_tope = simphony.PoliticaMeals("sin tope", 5, 5, 10, 10, None)
        self.assertEqual(simphony.horas_de_prima(dos, sin_tope), 2.0)


class TestTramos(unittest.TestCase):
    def tramos(self, esc: Escenario):
        return simphony.tramos_de(simphony.jalar(esc.fuente(), esc.tiendas, INICIO, FIN))

    def test_las_horas_son_las_que_paga_oracle_con_todos_sus_niveles(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.tramo(ana, LUNES, "06:00", "20:00", ovt=4.0, ovt2=2.0)
        tramos, _ = self.tramos(esc)
        self.assertAlmostEqual(tramos[0].horas, 14.0)
        self.assertAlmostEqual(tramos[0].horas_ovt_oracle, 6.0)

    def test_sin_payroll_id_en_la_tarjeta_usa_el_del_empleado(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.tramo(ana, LUNES, "07:00", "11:00", payroll_id="")
        tramos, _ = self.tramos(esc)
        self.assertEqual(tramos[0].persona, ana["payrollId"])

    def test_un_payroll_id_en_blanco_en_la_tarjeta_usa_el_del_empleado(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.tramo(ana, LUNES, "07:00", "11:00", payroll_id="  ")
        tramos, _ = self.tramos(esc)
        self.assertEqual(tramos[0].persona, ana["payrollId"])

    def test_sin_payroll_id_en_ningun_lado_usa_tienda_y_numero(self):
        for vacio in ("", "0", "00", " 000 "):
            with self.subTest(payroll_id=vacio):
                esc = Escenario()
                ana = esc.empleado("Ejemplo", "Ana", payroll_id=vacio)
                esc.tramo(ana, LUNES, "07:00", "11:00", payroll_id=vacio)
                tramos, _ = self.tramos(esc)
                self.assertEqual(tramos[0].persona, f"{TIENDA}#{ana['num']}")

    def test_un_valor_raro_no_tumba_la_lectura(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        tarjeta = esc.tramo(ana, LUNES, "07:00", "11:00")
        tarjeta["shiftType"] = "raro"
        tarjeta["clkOutStatus"] = None
        tramos, problemas = self.tramos(esc)
        self.assertEqual((len(tramos), problemas), (1, []))

    def test_numeros_que_no_son_finitos_valen_0(self):
        for raro in (float("nan"), float("inf"), "NaN"):
            with self.subTest(valor=raro):
                self.assertEqual(simphony._numero(raro), 0.0)

    def test_una_tarjeta_sin_entrada_o_sin_dia_bloquea(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        del esc.tramo(ana, LUNES, "07:00", "11:00")["clkInUTC"]
        self.assertIn("tarjeta_sin_entrada", tipos(esc.correr(INICIO, FIN), "bloquea"))
        datos = simphony.Datos(tiendas=[TIENDA], desde=INICIO, hasta=FIN, tarjetas=[{
            "locRef": TIENDA, "businessDates": [{"busDt": "mal", "timeCardDetails": [{"tcId": 1}]}]}])
        self.assertEqual(simphony.tramos_de(datos)[1][0]["tipo"], "tarjeta_sin_dia")


class TestTips(unittest.TestCase):
    def test_tips_y_horas_de_alguien_sin_payroll_id_van_a_la_misma_persona(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana", payroll_id="")
        esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30")
        esc.tips(ana, LUNES, tarjeta=80.0, pagados=80.0)
        r = esc.correr(INICIO, FIN)
        self.assertEqual(persona(r, "Ejemplo, Ana")["tips"]["tarjeta"], 80.0)
        self.assertEqual(r["retenidos"], {})
        self.assertNotIn("tips_retenidos", tipos(r))

    def test_los_tips_van_a_la_persona_de_la_tarjeta(self):
        casos = {
            # Oracle no trae el payrollId del empleado, pero la tarjeta sí.
            "ficha sin payrollId": lambda esc, ana: None,
            # La ficha no viene en getEmployeeDimensions.
            "sin ficha": lambda esc, ana: esc.empleados[TIENDA].remove(ana),
            # La ficha trae otro payrollId que la tarjeta.
            "ficha con otro payrollId": lambda esc, ana: ana.update(payrollId="OTRO"),
        }
        for caso, despues in casos.items():
            with self.subTest(caso=caso):
                esc = Escenario()
                ana = esc.empleado("Ejemplo", "Ana", payroll_id="" if caso == "ficha sin payrollId" else None)
                esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30", payroll_id="EJ7")
                esc.tips(ana, LUNES, tarjeta=80.0)
                despues(esc, ana)
                r = esc.correr(INICIO, FIN)
                self.assertEqual(len(r["personas"]), 1)
                self.assertEqual(r["personas"][0]["tips"]["tarjeta"], 80.0)
                self.assertEqual(r["retenidos"], {})

    def test_los_dias_de_los_retenidos_son_por_cuenta(self):
        esc = Escenario()
        uno = esc.cuenta("Gerente", clase="Manager")
        otro = esc.cuenta("Gerente", clase="Manager", tienda="BYC998")
        otro["num"] = uno["num"]   # el mismo número de empleado en otra tienda
        for cuenta in (uno, otro):
            esc.tips(cuenta, LUNES, tarjeta=5.0)
        r = esc.correr(INICIO, FIN)
        self.assertEqual((r["retenidos"]["Manager"]["cuentas"], r["retenidos"]["Manager"]["dias"]), (2, 2))

    def test_el_cuadre_de_tips_bloquea_en_los_dos_sentidos_desde_un_centavo(self):
        for campo, nombre in (("chrgTipsTtl", "tarjeta"), ("tipsPdTtl", "pagados")):
            with self.subTest(campo=campo, sentido="se leyó de menos"):
                esc = Escenario()
                ana = esc.empleado("Ejemplo", "Ana")
                esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30")
                datos = simphony.jalar(esc.fuente(), esc.tiendas, INICIO, FIN)
                datos.totales.append({"locRef": TIENDA, "busDt": LUNES, "revenueCenters": [
                    {"rvcNum": 3, "empleados": [{"empNum": ana["num"], campo: 0.01}]}]})
                self.assertIn("cuadre_tips", tipos(motor.calcular(datos, INICIO, FIN), "bloquea"))
            with self.subTest(campo=campo, sentido="se leyó de más"):
                esc = Escenario()
                ana = esc.empleado("Ejemplo", "Ana")
                esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30")
                esc.tips(ana, LUNES, **{nombre: 80.0})
                original = simphony.tips_de

                def duplica(datos):
                    filas, problemas = original(datos)
                    return filas + filas, problemas
                with mock.patch.object(simphony, "tips_de", duplica):
                    self.assertIn("cuadre_tips", tipos(esc.correr(INICIO, FIN), "bloquea"))

    def test_tips_en_varios_revenue_centers_se_suman_y_cuentan_un_dia(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        caja = esc.cuenta("Online")
        esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30")
        for rvc in (1, 2):
            esc.tips(ana, LUNES, tarjeta=10.0, pagados=10.0, rvc=rvc)
            esc.tips(caja, "2026-11-04", tarjeta=5.0, rvc=rvc)
        r = esc.correr(INICIO, FIN)
        self.assertEqual(persona(r, "Ejemplo, Ana")["tips"]["tarjeta"], 20.0)
        self.assertEqual(r["retenidos"]["SuperUser"]["dias"], 1)
        self.assertIn("1 día(s) con tips", texto_de(r, "tips_retenidos"))
        self.assertFalse(r["bloquea"])

    def test_los_dias_con_tips_sin_tarjeta_cuentan_dias(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30")
        for rvc in (1, 2):
            esc.tips(ana, "2026-11-04", tarjeta=10.0, rvc=rvc)
        r = esc.correr(INICIO, FIN)
        self.assertEqual(persona(r, "Ejemplo, Ana")["dias_tips_sin_tarjeta"], 1)
        self.assertIn("1 día(s) con tips de personas que no checaron", texto_de(r, "tips_sin_tarjeta"))

    def test_los_renglones_sin_tips_no_cuentan(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.tips(ana, LUNES, netSlsTtl=300.0, service=0.0)
        filas, problemas = simphony.tips_de(simphony.jalar(esc.fuente(), esc.tiendas, INICIO, FIN))
        self.assertEqual((filas, problemas), ([], []))

    def test_los_retenidos_llevan_sus_tips_pagados(self):
        esc = Escenario()
        jefe = esc.cuenta("Gerente", clase="Manager")
        esc.tips(jefe, LUNES, tarjeta=5.0, pagados=40.0)
        r = esc.correr(INICIO, FIN)
        self.assertEqual((r["retenidos"]["Manager"]["tarjeta"], r["retenidos"]["Manager"]["pagados"]), (5.0, 40.0))
        self.assertEqual(r["cuadre"]["tips_pagados"], 40.0)

    def test_el_cuadre_de_tips_compara_contra_lo_que_mando_oracle(self):
        # Una respuesta con los empleados donde el lector no los busca: lo crudo
        # sí los ve, así que el cuadre bloquea en vez de perderlos callado.
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30")
        datos = simphony.jalar(esc.fuente(), esc.tiendas, INICIO, FIN)
        datos.totales.append({"locRef": TIENDA, "busDt": LUNES, "revenueCenters": [
            {"rvcNum": 3, "empleados": [{"empNum": ana["num"], "chrgTipsTtl": 25.0}]}]})
        r = motor.calcular(datos, INICIO, FIN)
        self.assertIn("cuadre_tips", tipos(r, "bloquea"))

    def test_una_respuesta_de_tips_sin_dia_bloquea(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30")
        datos = simphony.jalar(esc.fuente(), esc.tiendas, INICIO, FIN)
        datos.totales.append({"locRef": TIENDA, "busDt": "", "revenueCenters": []})
        self.assertIn("tips_sin_dia", tipos(motor.calcular(datos, INICIO, FIN), "bloquea"))

    def test_los_tips_de_dias_antes_del_periodo_no_cuentan(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, "2026-10-31", "07:00", "11:00", meal=30, hasta="14:30")
        esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30")
        esc.tips(ana, "2026-10-31", tarjeta=50.0)
        esc.tips(ana, LUNES, tarjeta=20.0)
        r = esc.correr(date(2026, 11, 2), FIN)
        self.assertEqual(persona(r, "Ejemplo, Ana")["tips"]["tarjeta"], 20.0)
        self.assertEqual(r["cuadre"]["tips_oracle"], 20.0)
        self.assertFalse(r["bloquea"])


class TestMotor(unittest.TestCase):
    def escenario(self) -> tuple[Escenario, dict, dict]:
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        beto = esc.empleado("Ejemplo", "Beto", puesto=30, nombre_puesto="Cook", clase="Clock-In Only")
        esc.turno(ana, LUNES, "07:00", "11:00", meal=31, hasta="14:30")
        esc.turno(beto, LUNES, "06:00", "11:00", meal=30, hasta="16:30")   # 10 h
        esc.tips(ana, LUNES, tarjeta=80.0, pagados=80.0)
        return esc, ana, beto

    def test_las_horas_repartidas_son_las_que_paga_oracle(self):
        esc, _, _ = self.escenario()
        r = esc.correr(INICIO, FIN)
        self.assertFalse(r["bloquea"])
        self.assertEqual(r["cuadre"]["horas_oracle"], r["cuadre"]["horas_colocadas"])
        beto = persona(r, "Ejemplo, Beto")
        self.assertEqual((beto["regular"], beto["overtime"], beto["double"]), (8.0, 2.0, 0.0))
        self.assertEqual(beto["puestos"], {"Cook": 10.0})

    def test_el_doble_llega_a_la_persona(self):
        esc = Escenario()
        beto = esc.empleado("Ejemplo", "Beto")
        esc.turno(beto, LUNES, "05:00", "10:00", meal=30, hasta="18:30")   # 13 h
        p = persona(esc.correr(INICIO, FIN), "Ejemplo, Beto")
        self.assertEqual((p["regular"], p["overtime"], p["double"]), (8.0, 4.0, 1.0))

    def test_la_prima_del_motor_respeta_el_tope_por_dia(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, LUNES, "06:00", "17:00")   # 11 h sin meals: dos violaciones
        r = esc.correr(INICIO, FIN)
        self.assertEqual(persona(r, "Ejemplo, Ana")["prima"], 1.0)
        sin_tope = simphony.PoliticaMeals("sin tope", 5, 5, 10, 10, None)
        with mock.patch.object(simphony, "POLITICA", sin_tope):
            self.assertEqual(persona(esc.correr(INICIO, FIN), "Ejemplo, Ana")["prima"], 2.0)

    def test_la_prima_y_lo_que_daria_la_otra_politica(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, LUNES, "07:00", "12:45")    # 5.75 h sin meal
        esc.turno(ana, "2026-11-03", "07:00", "14:00")   # 7 h sin meal
        r = esc.correr(INICIO, FIN)
        self.assertEqual(persona(r, "Ejemplo, Ana")["prima"], 2.0)
        self.assertEqual(r["cuadre"]["prima_otra_politica"], 1.0)
        detalle = persona(r, "Ejemplo, Ana")["violaciones"][0]
        self.assertEqual((detalle["dia"], detalle["codigos"]), (date(2026, 11, 2), [simphony.SIN_MEAL]))

    def test_el_overtime_contra_oracle_es_neto_por_semana(self):
        # Sábado 12 h sin overtime de Oracle; de domingo a jueves 6 h, y Oracle
        # pone 2 h de overtime el jueves. California da 4 h; la diferencia es 2.
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, "2026-10-31", "06:00", "10:00", meal=30, hasta="18:30")
        for i in range(1, 6):
            dia = (date(2026, 10, 31) + timedelta(days=i)).isoformat()
            esc.turno(ana, dia, "07:00", "10:00", meal=30, hasta="13:30", ovt=1.0 if i == 5 else 0.0)
        r = esc.correr(INICIO, FIN)
        self.assertEqual(r["cuadre"]["ot_de_mas_vs_oracle"], 2.0)
        self.assertIn("2.00 h más", texto_de(r, "overtime_vs_oracle"))

    def test_si_oracle_pone_mas_overtime_que_california_es_informativo(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, LUNES, "07:00", "10:00", meal=30, hasta="13:30", ovt=1.0)
        r = esc.correr(INICIO, FIN)
        self.assertNotIn("overtime_vs_oracle", tipos(r))
        self.assertIn("overtime_oracle_de_mas", tipos(r, "info"))

    def test_si_oracle_ya_puso_el_overtime_no_avisa(self):
        esc = Escenario()
        beto = esc.empleado("Ejemplo", "Beto")
        esc.turno(beto, LUNES, "06:00", "11:00", meal=30, hasta="16:30", ovt=1.0)
        r = esc.correr(INICIO, FIN)
        self.assertNotIn("overtime_vs_oracle", tipos(r))
        self.assertNotIn("overtime_oracle_de_mas", tipos(r))

    def test_una_tarjeta_abierta_bloquea(self):
        esc, ana, _ = self.escenario()
        esc.tramo(ana, "2026-11-03", "07:00", None)
        self.assertIn("tarjeta_abierta", tipos(esc.correr(INICIO, FIN), "bloquea"))

    def test_si_oracle_no_ha_cerrado_el_ultimo_dia_bloquea(self):
        esc, _, _ = self.escenario()
        esc.dia_abierto[TIENDA] = FIN
        r = esc.correr(INICIO, FIN)
        self.assertIn("periodo_sin_cerrar", tipos(r, "bloquea"))
        esc.dia_abierto[TIENDA] = FIN + timedelta(days=1)
        self.assertNotIn("periodo_sin_cerrar", tipos(esc.correr(INICIO, FIN)))

    def test_si_oracle_falla_un_dia_no_se_puede_concluir(self):
        esc, _, _ = self.escenario()
        esc.falla("tarjetas", dia="2026-11-05")
        r = esc.correr(INICIO, FIN)
        self.assertTrue(r["bloquea"])
        self.assertIn("faltan_datos", tipos(r, "bloquea"))

    def test_si_oracle_no_contesta_se_deja_de_pedir_tras_3_fallas(self):
        esc, _, _ = self.escenario()
        for consulta in ("empleados", "puestos", "dia_abierto", "tarjetas", "tips", "totales_puesto"):
            esc.falla(consulta)
        fuente = Contador(esc.fuente())
        datos = simphony.jalar(fuente, esc.tiendas, INICIO, FIN)
        self.assertEqual(fuente.llamadas, 3)
        self.assertEqual(len(datos.errores), 4)
        self.assertIn("ya no se pidió nada más", datos.errores[-1]["error"])
        self.assertTrue(motor.calcular(datos, INICIO, FIN)["bloquea"])

    def test_una_sola_consulta_que_siempre_falla_tambien_se_deja_de_pedir(self):
        esc, _, _ = self.escenario()
        esc.falla("totales_puesto")
        fuente = Contador(esc.fuente())
        datos = simphony.jalar(fuente, esc.tiendas, INICIO, FIN)
        fallidas = [e for e in datos.errores if e["consulta"].startswith("total por puesto")]
        self.assertEqual(len(fallidas), 3)
        self.assertTrue(any("get_job_code_daily_totals" in e["consulta"] for e in datos.errores))
        self.assertEqual(len(datos.tarjetas), 14)

    def test_si_oracle_rechaza_la_contrasena_se_corta_al_primer_intento(self):
        class Rechaza:
            llamadas = 0

            def __getattr__(self, nombre):
                def metodo(*args):
                    Rechaza.llamadas += 1
                    raise OracleBIAuthError("Oracle rechazó el inicio de sesión de la cuenta API")
                metodo.__name__ = nombre
                return metodo

        datos = simphony.jalar(Rechaza(), [TIENDA, "BYC998"], INICIO, FIN)
        self.assertEqual(Rechaza.llamadas, 1)
        self.assertIn("revisa la contraseña", datos.errores[-1]["error"])
        self.assertIn("429 o 5xx", datos.errores[-1]["error"])

    def test_si_oracle_no_da_el_dia_abierto_bloquea(self):
        esc, _, _ = self.escenario()
        fuente = esc.fuente()
        fuente.get_latest_business_date = lambda ref: {"locRef": ref, "latestBusDt": None}
        datos = simphony.jalar(fuente, esc.tiendas, INICIO, FIN)
        self.assertIn("periodo_sin_cerrar", tipos(motor.calcular(datos, INICIO, FIN), "bloquea"))

    def test_si_el_reparto_agrega_horas_o_pierde_poquitas_bloquea(self):
        original = ca_overtime.compute_overtime
        for cambio in (1.0, -0.02):
            with self.subTest(cambio=cambio):
                esc, _, _ = self.escenario()

                def mueve(horas_por_dia, inicio_semana, cambio=cambio):
                    resultado = original(horas_por_dia, inicio_semana)
                    for r in resultado.values():
                        for d in r["days"].values():
                            d["regular"] += cambio
                    return resultado
                with mock.patch.object(ca_overtime, "compute_overtime", mueve):
                    self.assertIn("cuadre_horas", tipos(esc.correr(INICIO, FIN), "bloquea"))

    def test_la_version_cambia_si_cambia_cualquier_archivo_que_calcula(self):
        self.assertEqual(motor._version_del_codigo(), motor.VERSION_DEL_RESULTADO)
        leer = open
        for modulo in (motor, simphony, ca_overtime, cliente_oracle):
            with self.subTest(archivo=Path(modulo.__file__).name):
                def abrir(ruta, *args, modulo=modulo, **kwargs):
                    if Path(ruta) == Path(modulo.__file__):
                        return io.BytesIO(Path(ruta).read_bytes() + b"\n# cambio\n")
                    return leer(ruta, *args, **kwargs)
                with mock.patch.object(motor, "open", abrir, create=True):
                    self.assertNotEqual(motor._version_del_codigo(), motor.VERSION_DEL_RESULTADO)

    def test_el_doble_cuenta_en_el_overtime_contra_oracle(self):
        esc = Escenario()
        beto = esc.empleado("Ejemplo", "Beto")
        esc.tramo(beto, LUNES, "05:00", "18:00", ovt=4.0)   # 13 h: California da 4 de OT y 1 doble
        self.assertEqual(esc.correr(INICIO, FIN)["cuadre"]["ot_de_mas_vs_oracle"], 1.0)

    def test_el_overtime_contra_oracle_no_se_compensa_entre_semanas(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, LUNES, "06:00", "10:00", meal=30, hasta="16:30")                    # 10 h; Oracle no pone OT
        esc.turno(ana, "2026-11-09", "07:00", "10:00", meal=30, hasta="13:30", ovt=1.0)   # 6 h; Oracle pone 2
        r = esc.correr(INICIO, FIN)
        self.assertEqual(r["cuadre"]["ot_de_mas_vs_oracle"], 2.0)
        self.assertIn("overtime_oracle_de_mas", tipos(r, "info"))

    def test_trece_horas_sin_meal_dan_una_hora_de_prima_con_las_dos_politicas(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, LUNES, "05:00", "18:00")
        r = esc.correr(INICIO, FIN)
        self.assertEqual((r["cuadre"]["prima"], r["cuadre"]["prima_otra_politica"]), (1.0, 1.0))

    def test_fallas_sueltas_de_una_consulta_no_la_cortan(self):
        esc, _, _ = self.escenario()
        for dia in ("2026-11-01", "2026-11-03", "2026-11-05"):
            esc.falla("totales_puesto", dia=dia)
        datos = simphony.jalar(esc.fuente(), esc.tiendas, INICIO, FIN)
        self.assertEqual(len(datos.errores), 3)
        self.assertEqual(len(datos.totales_puesto), 11)

    def test_los_turnos_partidos_se_cuentan_por_jornada_y_dicen_quien(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.tramo(ana, LUNES, "06:00", "08:00", salida=simphony.SALIDA_A_BREAK)
        esc.tramo(ana, LUNES, "10:00", "12:00", salida=simphony.SALIDA_A_BREAK)
        esc.tramo(ana, LUNES, "14:00", "16:00")
        texto = texto_de(esc.correr(INICIO, FIN), "turno_partido")
        self.assertIn("1 jornada(s)", texto)
        self.assertIn(f"Ejemplo, Ana ({TIENDA}, {LUNES})", texto)

    def test_un_payroll_id_que_juntan_dos_personas_bloquea(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana", payroll_id="TEMP")
        beto = esc.empleado("Ejemplo", "Beto", payroll_id="TEMP")
        esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30")
        esc.turno(beto, "2026-11-03", "07:00", "11:00", meal=30, hasta="14:30")
        r = esc.correr(INICIO, FIN)
        self.assertIn("cuentas_juntas", tipos(r, "bloquea"))
        self.assertIn("Ejemplo, Beto", texto_de(r, "cuentas_juntas"))

    def test_la_misma_persona_en_dos_tiendas_se_revisa(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana", payroll_id="EJ1")
        ana_otra = esc.empleado("Ejemplo", "Ana", payroll_id="EJ1", tienda="BYC998")
        esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30")
        esc.turno(ana_otra, "2026-11-03", "07:00", "11:00", meal=30, hasta="14:30")
        r = esc.correr(INICIO, FIN)
        self.assertIn("varias_tiendas", tipos(r, "revisar"))
        self.assertNotIn("cuentas_juntas", tipos(r))

    def test_tarjetas_y_total_por_puesto_en_dos_revenue_centers_cuadran(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.tramo(ana, LUNES, "07:00", "10:00", rvc=1)
        esc.tramo(ana, LUNES, "10:00", "12:00", rvc=2)
        self.assertNotIn("cuadre_oracle", tipos(esc.correr(INICIO, FIN)))

    def test_los_supuestos_del_calendario_se_avisan(self):
        esc, _, _ = self.escenario()
        self.assertIn("supuestos_calendario", tipos(esc.correr(INICIO, FIN), "info"))

    def test_las_tarjetas_cuadran_con_el_total_por_puesto_de_oracle(self):
        esc, _, _ = self.escenario()
        r = esc.correr(INICIO, FIN)
        self.assertNotIn("cuadre_oracle", tipos(r))
        self.assertEqual(r["cuadre"]["horas_oracle_puesto"], r["cuadre"]["horas_oracle"])

    def test_el_overtime_de_nivel_2_no_se_cuenta_dos_veces_en_el_total_por_puesto(self):
        # Como el 13 nov en BYC303: tarjetas con overtime de nivel 1 y 2. En el
        # total por puesto, ovtHrs ya trae los dos y ovt2Hrs es su desglose.
        esc = Escenario()
        beto = esc.empleado("Ejemplo", "Beto")
        esc.tramo(beto, LUNES, "05:00", "19:30", ovt=4.0, ovt2=2.5)
        r = esc.correr(INICIO, FIN)
        self.assertNotIn("cuadre_oracle", tipos(r))
        self.assertEqual(r["cuadre"]["horas_oracle_puesto"], 14.5)
        datos = simphony.Datos(tiendas=[TIENDA], desde=INICIO, hasta=FIN, totales_puesto=[{
            "locRef": TIENDA, "busDt": LUNES, "revenueCenters": [{"rvcNum": 1, "jobCodes": [
                {"jcNum": 10, "regHrs": 8.0, "ovt1Hrs": 4.0, "ovt2Hrs": 2.5}]}]}])
        self.assertEqual(simphony.horas_oracle_por_tienda_dia(datos)[(TIENDA, date(2026, 11, 2))], 14.5)

    def test_el_total_por_puesto_descuadra_en_los_dos_sentidos(self):
        for horas, bloquea in ((3.5, True), (-3.5, True), (0.06, True), (-0.06, True),
                               (0.04, False), (-0.04, False)):
            with self.subTest(horas=horas):
                esc, _, _ = self.escenario()
                esc.descuadre(LUNES, horas)
                r = esc.correr(INICIO, FIN)
                self.assertEqual("cuadre_oracle" in tipos(r, "bloquea"), bloquea)
                if bloquea:
                    self.assertIn(f"{TIENDA} {LUNES}", texto_de(r, "cuadre_oracle"))

    def test_un_dia_con_tarjetas_y_sin_total_por_puesto_tambien_bloquea(self):
        esc, _, _ = self.escenario()
        esc.falla("totales_puesto", dia=LUNES)
        self.assertIn("cuadre_oracle", tipos(esc.correr(INICIO, FIN), "bloquea"))

    def test_si_el_reparto_pierde_horas_bloquea(self):
        esc, _, _ = self.escenario()
        original = ca_overtime.compute_overtime

        def pierde_una_hora(horas_por_dia, inicio_semana):
            resultado = original(horas_por_dia, inicio_semana)
            for r in resultado.values():
                for d in r["days"].values():
                    d["regular"] = max(0.0, d["regular"] - 1.0)
            return resultado

        with mock.patch.object(ca_overtime, "compute_overtime", pierde_una_hora):
            self.assertIn("cuadre_horas", tipos(esc.correr(INICIO, FIN), "bloquea"))

    def test_avisa_las_salidas_a_break_cortas(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, LUNES, "07:00", "09:00", meal=12, hasta="11:00")
        self.assertIn("breaks_cortos", tipos(esc.correr(INICIO, FIN), "revisar"))

    def test_avisa_el_break_sin_regreso_con_el_umbral_de_la_politica(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.tramo(ana, LUNES, "07:00", "13:00", salida=simphony.SALIDA_A_BREAK)
        r = esc.correr(INICIO, FIN)
        self.assertIn("pasó de 5 h", texto_de(r, "break_sin_regreso"))
        with mock.patch.object(simphony, "POLITICA", simphony.CON_WAIVERS):
            self.assertIn("pasó de 6 h", texto_de(esc.correr(INICIO, FIN), "break_sin_regreso"))

    def test_avisa_las_tarjetas_que_no_duran_lo_que_paga_oracle(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, LUNES, "07:00", "10:00", meal=30, hasta="13:30", horas=2.0)
        r = esc.correr(INICIO, FIN)
        texto = texto_de(r, "duracion_distinta")
        self.assertIn("2 tarjeta(s)", texto)
        self.assertIn("Ejemplo, Ana", texto)

    def test_avisa_turnos_partidos_tramos_encimados_y_ajustes(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.puesto(20, "Host")
        esc.turno(ana, LUNES, "06:00", "10:00", meal=300, hasta="19:00")
        esc.tramo(ana, "2026-11-03", "07:00", "12:00", ajustes=2)
        esc.tramo(ana, "2026-11-03", "10:00", "14:00", puesto=20)
        r = esc.correr(INICIO, FIN)
        self.assertIn("turno_partido", tipos(r, "revisar"))
        self.assertIn("tramos_encimados", tipos(r, "revisar"))
        self.assertIn("2 ajuste(s)", texto_de(r, "ajustes"))

    def test_una_cuenta_con_dos_payroll_id_bloquea(self):
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        esc.turno(ana, LUNES, "07:00", "11:00", meal=30, hasta="14:30")
        esc.tramo(ana, "2026-11-03", "07:00", "11:00", payroll_id="OTRO")
        self.assertIn("identidad", tipos(esc.correr(INICIO, FIN), "bloquea"))

    def test_lo_de_fuera_del_periodo_no_cuenta(self):
        esc, ana, _ = self.escenario()
        esc.turno(ana, "2026-11-14", "07:00", "15:00")
        r = esc.correr(INICIO, FIN)
        self.assertEqual(persona(r, "Ejemplo, Ana")["dias"], 1)

    def test_si_el_periodo_no_empieza_con_la_semana_se_piden_los_dias_anteriores(self):
        # Ocho horas diarias de sábado a jueves son 48 en la semana laboral. Si
        # el periodo empieza en lunes, el sábado y el domingo no se pagan aquí,
        # pero cuentan para las 40: las 8 del jueves son overtime. Sin pedirlos,
        # de lunes a jueves serían 32 h y no habría overtime.
        esc = Escenario()
        ana = esc.empleado("Ejemplo", "Ana")
        for i in range(6):
            dia = (date(2026, 10, 31) + timedelta(days=i)).isoformat()
            esc.turno(ana, dia, "07:00", "11:00", meal=30, hasta="15:30")
        desde, _ = motor.dias_a_pedir(date(2026, 11, 2), FIN)
        self.assertEqual(desde, date(2026, 10, 31))
        r = esc.correr(date(2026, 11, 2), FIN)
        ana_r = persona(r, "Ejemplo, Ana")
        self.assertEqual((ana_r["dias"], ana_r["regular"], ana_r["overtime"]), (4, 24.0, 8.0))
        self.assertEqual(r["cuadre"]["horas_oracle"], 32.0)
        self.assertFalse(r["bloquea"])

    def test_el_resultado_dice_con_que_version_se_armo(self):
        esc, _, _ = self.escenario()
        self.assertEqual(esc.correr(INICIO, FIN)["version"], motor.VERSION_DEL_RESULTADO)


class Contador:
    """Envuelve una fuente y cuenta las llamadas que de verdad le llegan."""

    def __init__(self, fuente):
        self.fuente = fuente
        self.llamadas = 0

    def __getattr__(self, nombre):
        metodo = getattr(self.fuente, nombre)

        def contado(*args):
            self.llamadas += 1
            return metodo(*args)
        contado.__name__ = nombre
        return contado


class TestCalendario(unittest.TestCase):
    def test_periodos_de_sabado_a_viernes(self):
        self.assertEqual(motor.periodo_que_contiene(date(2026, 11, 5)), (INICIO, FIN))
        for inicio, fin in motor.periodos_recientes(6, date(2026, 11, 18)):
            self.assertEqual((inicio.weekday(), fin.weekday()), (5, 4))
            self.assertEqual((fin - inicio).days, 13)

    def test_el_ultimo_cerrado(self):
        self.assertEqual(motor.ultimo_periodo_cerrado(date(2026, 11, 18)), (INICIO, FIN))
        self.assertEqual(motor.ultimo_periodo_cerrado(date(2026, 11, 14)), (INICIO, FIN))
        self.assertEqual(motor.ultimo_periodo_cerrado(date(2026, 11, 13)),
                         (date(2026, 10, 17), date(2026, 10, 30)))

    def test_coincide_con_el_periodo_de_mealviolation(self):
        self.assertEqual(motor.periodo_que_contiene(date(2026, 7, 1)), (date(2026, 6, 27), date(2026, 7, 10)))

    def test_hoy_es_el_de_california(self):
        self.assertIsInstance(motor.hoy_pacifico(), date)


if __name__ == "__main__":
    unittest.main()
