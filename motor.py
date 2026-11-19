"""El motor: de los datos de Oracle a horas, primas y tips por persona, con sus cuadres.

Primera versión. Todavía no arma el CSV de Gusto, porque falta el template de
cada empresa. Los tips se enseñan para revisar y no se reparten, porque falta
saber cómo se pagan (ver README).

Las horas de cada persona son las que paga Oracle; lo que cambia es el reparto
entre regular, overtime y doble, que se hace con las reglas de California
(ca_overtime.py). Los cuadres comparan contra fuentes aparte: las horas contra
el total por puesto de Oracle, y los tips contra lo que Oracle mandó crudo.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, datetime, timedelta

import ca_overtime
import simphony
from oracle_bi import client as cliente_oracle

TOLERANCIA_HORAS = 0.01
TOLERANCIA_DINERO = 0.005
# Tarjetas contra el total por puesto de Oracle, por tienda y día.
TOLERANCIA_CUADRE_ORACLE = 0.05
# Diferencia entre lo que dura una tarjeta y las horas que paga Oracle por ella
# a partir de la cual se avisa. El redondeo de Labor Management mueve cada
# checada hasta 36 s (0.01 h).
DIFERENCIA_DURACION_H = 0.05
EJEMPLOS_POR_AVISO = 5

# Calendario de pago. SUPUESTO hasta leerlo en Gusto: cada dos semanas, de
# sábado a viernes, como el periodo con el que se validó mealviolation (27 jun
# al 10 jul de 2026) y como el calendario de tacos.
PERIODO_DIAS = 14
FIN_DE_PERIODO_CONOCIDO = date(2026, 7, 10)
CALENDARIO_CONFIRMADO = False


def hoy_pacifico() -> date:
    """El día de hoy en California. El servidor de Streamlit Cloud corre en
    UTC: un viernes a las 5 pm en San Diego, allá ya es sábado."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        return date.today()


def periodo_que_contiene(dia: date) -> tuple[date, date]:
    """(inicio, fin) del periodo de pago que contiene a `dia`."""
    corrimiento = (FIN_DE_PERIODO_CONOCIDO - dia).days % PERIODO_DIAS
    fin = dia + timedelta(days=corrimiento)
    return fin - timedelta(days=PERIODO_DIAS - 1), fin


def ultimo_periodo_cerrado(hoy: date | None = None) -> tuple[date, date]:
    """El último periodo cuyo último día ya pasó. Que Oracle ya lo haya cerrado
    se revisa aparte, con su día abierto."""
    hoy = hoy or hoy_pacifico()
    inicio, fin = periodo_que_contiene(hoy)
    if fin >= hoy:
        inicio -= timedelta(days=PERIODO_DIAS)
        fin -= timedelta(days=PERIODO_DIAS)
    return inicio, fin


def periodos_recientes(cuantos: int = 8, hoy: date | None = None) -> list[tuple[date, date]]:
    """Los últimos periodos cerrados, el más reciente primero."""
    inicio, fin = ultimo_periodo_cerrado(hoy)
    salida = []
    for _ in range(cuantos):
        salida.append((inicio, fin))
        inicio -= timedelta(days=PERIODO_DIAS)
        fin -= timedelta(days=PERIODO_DIAS)
    return salida


def dias_a_pedir(inicio: date, fin: date) -> tuple[date, date]:
    """Desde dónde pedir a Oracle. Si el periodo no empieza con la semana
    laboral, hacen falta los días anteriores de esa semana para el overtime
    semanal y el séptimo día."""
    return ca_overtime.workweek_start(inicio, ca_overtime.WORKWEEK_START_WEEKDAY), fin


def _version_del_codigo() -> str:
    """Qué código armó un resultado. La app guarda el resultado en la sesión, y
    la sesión sobrevive a una actualización de la app; con esto se niega a
    enseñar uno armado con otras reglas. Entran los archivos que deciden qué
    se lee y cómo se calcula."""
    huella = hashlib.sha256()
    for modulo in (__file__, simphony.__file__, ca_overtime.__file__, cliente_oracle.__file__):
        with open(modulo, "rb") as h:
            huella.update(h.read())
    return huella.hexdigest()[:12]


VERSION_DEL_RESULTADO = _version_del_codigo()


def aviso(nivel: str, tipo: str, texto: str, **datos) -> dict:
    """nivel: 'bloquea' (no se puede concluir), 'revisar' o 'info'."""
    return {"nivel": nivel, "tipo": tipo, "texto": texto, **datos}


def nombre_de(datos: simphony.Datos, tienda: str, emp_num: int | None) -> str:
    empleado = datos.empleados.get((tienda, emp_num)) or {}
    apellido = str(empleado.get("lName") or "").strip()
    nombre = str(empleado.get("fName") or "").strip()
    if apellido or nombre:
        return f"{apellido}, {nombre}".strip(", ")
    return f"Empleado {emp_num} ({tienda})"


def _hora(local: str) -> str:
    """'2026-11-12T11:02:00' -> '11:02'."""
    return local[11:16] if len(local) >= 16 else local


def _lista(ejemplos: list[str]) -> str:
    extra = len(ejemplos) - EJEMPLOS_POR_AVISO
    return "; ".join(ejemplos[:EJEMPLOS_POR_AVISO]) + (f"; y {extra} más" if extra > 0 else "")


def calcular(datos: simphony.Datos, inicio: date, fin: date) -> dict:
    """Horas, primas y tips por persona del periodo [inicio, fin]."""
    avisos: list[dict] = []
    for e in datos.errores:
        avisos.append(aviso("bloquea", "faltan_datos", f"Oracle no dio {e['consulta']}: {e['error']}"))

    # Que Oracle ya haya cerrado el último día del periodo en cada tienda.
    fallidas = {e["consulta"] for e in datos.errores}
    for tienda in datos.tiendas:
        abierto = datos.dia_abierto.get(tienda)
        if abierto is None:
            if f"día abierto de {tienda}" not in fallidas:
                avisos.append(aviso("bloquea", "periodo_sin_cerrar",
                                    f"No se sabe si Oracle ya cerró el {fin} en {tienda}."))
        elif abierto <= fin:
            avisos.append(aviso(
                "bloquea", "periodo_sin_cerrar",
                f"Oracle todavía no cierra el {fin} en {tienda} (su día abierto es el {abierto}). "
                f"Genera cuando cierre."))

    tramos, problemas = simphony.tramos_de(datos)
    for p in problemas:
        avisos.append(aviso("bloquea", p["tipo"],
                            f"Una tarjeta de {p['tienda']} no se pudo leer ({p['tipo']}, tarjeta {p['tc_id']})."))
    en_periodo = [t for t in tramos if inicio <= t.dia <= fin]
    for t in en_periodo:
        if t.sale is None:
            avisos.append(aviso(
                "bloquea", "tarjeta_abierta",
                f"{nombre_de(datos, t.tienda, t.emp_num)}: la tarjeta del {t.dia} en {t.tienda} "
                f"no tiene salida. Hay que cerrarla en Oracle y volver a generar.",
                persona=t.persona))

    # Una cuenta (tienda, número de empleado) tiene que ser una sola persona, y
    # una persona no puede juntar cuentas de gente distinta.
    personas_por_cuenta: dict[tuple[str, int | None], set[str]] = defaultdict(set)
    cuentas_por_persona: dict[str, set[tuple[str, int | None]]] = defaultdict(set)
    for t in tramos:
        personas_por_cuenta[(t.tienda, t.emp_num)].add(t.persona)
        cuentas_por_persona[t.persona].add((t.tienda, t.emp_num))
    for (tienda, emp_num), quienes in sorted(personas_por_cuenta.items(), key=str):
        if len(quienes) > 1:
            avisos.append(aviso(
                "bloquea", "identidad",
                f"{nombre_de(datos, tienda, emp_num)} ({tienda}) sale con {len(quienes)} payrollID "
                f"distintos en sus tarjetas: sus horas quedan partidas y el overtime saldría mal. "
                f"Hay que corregirlo en Oracle."))
    for quien, cuentas in sorted(cuentas_por_persona.items()):
        if len(cuentas) < 2:
            continue
        nombres = sorted({nombre_de(datos, tienda, emp_num) for tienda, emp_num in cuentas})
        tiendas_de = sorted({tienda for tienda, _ in cuentas})
        if len(nombres) > 1:
            avisos.append(aviso(
                "bloquea", "cuentas_juntas",
                f"El payrollID {quien} lo traen {len(cuentas)} cuentas de personas distintas "
                f"({'; '.join(nombres)}): sus horas se sumarían como de una sola persona."))
        elif len(tiendas_de) > 1:
            avisos.append(aviso(
                "revisar", "varias_tiendas",
                f"{nombres[0]} trabajó en {len(tiendas_de)} tiendas ({', '.join(tiendas_de)}) y sus "
                f"horas se suman para el overtime. Eso solo vale si esas tiendas son del mismo "
                f"patrón (la misma LLC)."))
        else:
            avisos.append(aviso(
                "revisar", "varias_cuentas",
                f"{nombres[0]} tiene {len(cuentas)} números de empleado en {tiendas_de[0]} con el "
                f"mismo payrollID; sus horas se suman como de una sola persona."))

    jornadas = simphony.jornadas_de(tramos)

    # El overtime se reparte con todos los días pedidos, que pueden incluir los
    # de la semana laboral anterior al inicio del periodo.
    horas_por_dia: dict[str, dict[date, float]] = defaultdict(dict)
    for (persona, dia), jornada in jornadas.items():
        horas_por_dia[persona][dia] = round(jornada.horas, 4)
    reparto = ca_overtime.compute_overtime(horas_por_dia, ca_overtime.WORKWEEK_START_WEEKDAY)

    personas: dict[str, dict] = {}

    def persona_dict(clave: str, tienda: str, emp_num: int | None) -> dict:
        if clave not in personas:
            personas[clave] = {
                "persona": clave, "nombre": nombre_de(datos, tienda, emp_num),
                "tiendas": set(), "dias": 0, "regular": 0.0, "overtime": 0.0, "double": 0.0,
                "ovt_oracle": 0.0, "puestos": defaultdict(float), "tarifas": set(),
                "prima": 0.0, "violaciones": [], "sin_regreso": 0, "ajustes": 0,
                "tips": defaultdict(float), "dias_tips_sin_tarjeta": set(),
            }
        return personas[clave]

    # Lo que saldría con la otra política, para que el dueño vea qué cambia.
    otra = simphony.CON_WAIVERS if simphony.POLITICA is simphony.ESTRICTA else simphony.ESTRICTA
    prima_otra, personas_otra = 0.0, set()
    # Overtime de California menos el de Oracle, por persona y semana laboral.
    ot_de_mas_por_semana: dict[tuple[str, date], float] = defaultdict(float)
    breaks_cortos = 0
    encimados: list[str] = []
    turnos_partidos: list[str] = []
    duraciones: list[str] = []
    for (clave, dia), jornada in sorted(jornadas.items()):
        if not inicio <= dia <= fin:
            continue
        primero = jornada.tramos[0]
        p = persona_dict(clave, primero.tienda, primero.emp_num)
        p["dias"] += 1
        dia_ca = reparto[clave]["days"][dia]
        p["regular"] += dia_ca["regular"]
        p["overtime"] += dia_ca["overtime"]
        p["double"] += dia_ca["double"]
        semana = ca_overtime.workweek_start(dia, ca_overtime.WORKWEEK_START_WEEKDAY)
        ot_de_mas_por_semana[(clave, semana)] += (
            dia_ca["overtime"] + dia_ca["double"] - sum(t.horas_ovt_oracle for t in jornada.tramos))
        for t in jornada.tramos:
            p["tiendas"].add(t.tienda)
            p["puestos"][datos.puestos.get((t.tienda, t.puesto_num), f"Puesto {t.puesto_num}")] += t.horas
            if t.tarifa:
                p["tarifas"].add(t.tarifa)
            p["ovt_oracle"] += t.horas_ovt_oracle
            p["ajustes"] += t.ajustes
            if t.duracion_h is not None and abs(t.duracion_h - t.horas) > DIFERENCIA_DURACION_H:
                duraciones.append(f"{p['nombre']} ({t.tienda}, {t.dia}): duró {t.duracion_h:.2f} h y "
                                  f"Oracle paga {t.horas:.2f} h")
        p["sin_regreso"] += jornada.sin_regreso
        breaks_cortos += sum(1 for m in jornada.meals if m.declarado and not m.completo)
        donde = f"{p['nombre']} ({primero.tienda}, {dia})"
        encimados += [donde] * jornada.encimados
        if jornada.huecos_largos:
            turnos_partidos.append(donde)
        violaciones_otra = simphony.auditar(jornada, otra)
        if violaciones_otra:
            prima_otra += simphony.horas_de_prima(violaciones_otra, otra)
            personas_otra.add(clave)
        violaciones = simphony.auditar(jornada)
        if violaciones:
            prima = simphony.horas_de_prima(violaciones)
            p["prima"] += prima
            p["violaciones"].append({
                "dia": dia, "horas": round(jornada.horas_trabajadas, 2), "codigos": violaciones,
                "prima": prima,
                "meals": [{"sale": _hora(m.sale_local), "regresa": _hora(m.regresa_local),
                           "minutos": round(m.minutos), "horas_antes": round(m.horas_antes, 2),
                           "completo": m.completo} for m in jornada.meals],
            })

    # Tips: van a la persona si la cuenta es de alguien que checa en el rango
    # pedido. Las cuentas que nunca checan (de clase SuperUser, Manager o
    # Server; falta saber qué son) se quedan retenidas hasta saber de quién son.
    filas_tips, problemas_tips = simphony.tips_de(datos)
    for p in problemas_tips:
        avisos.append(aviso("bloquea", p["tipo"],
                            f"Una respuesta de tips de {p['tienda']} no trae día de negocio."))
    tips = [f for f in filas_tips if inicio <= f.dia <= fin]
    persona_por_cuenta = {(t.tienda, t.emp_num): t.persona for t in tramos}
    checan = {t.persona: t for t in tramos}
    con_tarjeta_ese_dia = {(t.persona, t.dia) for t in tramos}
    retenidos: dict[str, dict] = defaultdict(lambda: {"cuentas": set(), "dias": set(), "tarjeta": 0.0,
                                                      "pagados": 0.0})
    for f in tips:
        quien = persona_por_cuenta.get((f.tienda, f.emp_num), f.persona)
        if quien in checan:
            ejemplo = checan[quien]
            p = persona_dict(quien, ejemplo.tienda, ejemplo.emp_num)
            for campo in ("tarjeta", "pagados", "service", "service_no_ingreso", "tip_ttl",
                          "indirectos", "declarados"):
                p["tips"][campo] += getattr(f, campo)
            if (quien, f.dia) not in con_tarjeta_ese_dia:
                p["dias_tips_sin_tarjeta"].add(f.dia)
        else:
            r = retenidos[f.clase or "(sin clase)"]
            r["cuentas"].add((f.tienda, f.emp_num))
            r["dias"].add((f.tienda, f.emp_num, f.dia))
            r["tarjeta"] += f.tarjeta
            r["pagados"] += f.pagados

    # Cuadres. El primero mide que el reparto no pierda horas de las tarjetas.
    horas_oracle = sum(t.horas for t in en_periodo)
    horas_colocadas = sum(p["regular"] + p["overtime"] + p["double"] for p in personas.values())
    if abs(horas_oracle - horas_colocadas) > TOLERANCIA_HORAS:
        avisos.append(aviso(
            "bloquea", "cuadre_horas",
            f"Las horas no cuadran: Oracle paga {horas_oracle:,.2f} h y se repartieron "
            f"{horas_colocadas:,.2f} h."))
    # El segundo es contra una fuente aparte: el total por puesto que Oracle
    # calcula sin las tarjetas. Si falta o sobra una tarjeta, aquí se ve.
    por_tarjetas: dict[tuple[str, date], float] = defaultdict(float)
    for t in en_periodo:
        por_tarjetas[(t.tienda, t.dia)] += t.horas
    oracle_puesto = simphony.horas_oracle_por_tienda_dia(datos)
    descuadres = []
    horas_oracle_puesto = 0.0
    for (tienda, dia), horas in sorted(oracle_puesto.items()):
        if not inicio <= dia <= fin or tienda not in datos.tiendas:
            continue
        horas_oracle_puesto += horas
        if abs(por_tarjetas.get((tienda, dia), 0.0) - horas) > TOLERANCIA_CUADRE_ORACLE:
            descuadres.append(f"{tienda} {dia}: tarjetas {por_tarjetas.get((tienda, dia), 0.0):,.2f} h, "
                              f"total por puesto {horas:,.2f} h")
    descuadres += [f"{t} {d}: sin total por puesto" for t, d in sorted(k for k in por_tarjetas
                                                                         if k not in oracle_puesto)]
    if descuadres:
        avisos.append(aviso(
            "bloquea", "cuadre_oracle",
            "Las tarjetas no cuadran con el total por puesto que calcula Oracle: " + _lista(descuadres)
            + ". Puede faltar o sobrar una tarjeta, por ejemplo si un gerente la estaba corrigiendo: "
              "vuelve a generar y, si sigue, revísalo en Oracle."))
    # El tercero: los tips que se leyeron (a personas y retenidos) contra lo que
    # Oracle mandó crudo, sin pasar por la lectura.
    tips_personas = sum(p["tips"]["tarjeta"] for p in personas.values())
    tips_retenidos = sum(r["tarjeta"] for r in retenidos.values())
    tips_crudos = simphony.suma_cruda(datos.totales, "chrgTipsTtl", inicio, fin)
    pagados_leidos = sum(p["tips"]["pagados"] for p in personas.values()) + sum(
        r["pagados"] for r in retenidos.values())
    pagados_crudos = simphony.suma_cruda(datos.totales, "tipsPdTtl", inicio, fin)
    if (abs(tips_crudos - tips_personas - tips_retenidos) > TOLERANCIA_DINERO
            or abs(pagados_crudos - pagados_leidos) > TOLERANCIA_DINERO):
        avisos.append(aviso(
            "bloquea", "cuadre_tips",
            f"Los tips leídos no cuadran con lo que mandó Oracle: con tarjeta ${tips_crudos:,.2f} "
            f"contra ${tips_personas + tips_retenidos:,.2f} leídos; pagados ${pagados_crudos:,.2f} "
            f"contra ${pagados_leidos:,.2f}."))

    # Lo que hay que revisar.
    politica = simphony.POLITICA
    sin_regreso = sum(p["sin_regreso"] for p in personas.values())
    if sin_regreso:
        avisos.append(aviso(
            "revisar", "break_sin_regreso",
            f"{sin_regreso} vez/veces alguien salió a break y ya no regresó ese día. Ese break no "
            f"cuenta como meal: si ese día no hubo otro meal completo y la jornada pasó de "
            f"{politica.primer_si_pasa_de_h:g} h, sale como violación."))
    if breaks_cortos:
        avisos.append(aviso(
            "revisar", "breaks_cortos",
            f"{breaks_cortos} salida(s) a break sin pago duraron menos de {simphony.MINIMO_MEAL_MIN} min. "
            f"No cuentan como meal. Si eran descansos de 10 minutos, en California se pagan, y "
            f"Oracle no los pagó."))
    if turnos_partidos:
        avisos.append(aviso(
            "revisar", "turno_partido",
            f"{len(turnos_partidos)} jornada(s) con un hueco de más de "
            f"{simphony.HUECO_DE_TURNO_PARTIDO_MIN} min. Cuenta como meal, pero si fue un turno "
            f"partido, en California puede tocar una hora extra de pago: " + _lista(turnos_partidos) + "."))
    if encimados:
        avisos.append(aviso(
            "revisar", "tramos_encimados",
            f"{len(encimados)} vez/veces una tarjeta empieza antes de que termine la anterior de la "
            f"misma persona. Esas horas se estarían pagando dos veces: " + _lista(encimados) + "."))
    for clase, r in sorted(retenidos.items()):
        avisos.append(aviso(
            "revisar", "tips_retenidos",
            f"Cuentas '{clase}' que no checan en el periodo: {len(r['cuentas'])} cuenta(s), "
            f"{len(r['dias'])} día(s) con tips: ${r['tarjeta']:,.2f} con tarjeta y ${r['pagados']:,.2f} "
            f"pagados. No se le asignan a nadie hasta saber de quién son.",
            clase=clase))
    dias_sin_tarjeta = sum(len(p["dias_tips_sin_tarjeta"]) for p in personas.values())
    if dias_sin_tarjeta:
        avisos.append(aviso(
            "revisar", "tips_sin_tarjeta",
            f"{dias_sin_tarjeta} día(s) con tips de personas que no checaron ese día. Se les suman, "
            f"pero vale la pena ver por qué."))
    if duraciones:
        avisos.append(aviso(
            "revisar", "duracion_distinta",
            f"{len(duraciones)} tarjeta(s) donde lo que paga Oracle difiere en más de "
            f"{DIFERENCIA_DURACION_H} h de lo que duró la tarjeta. Se pagan las horas de Oracle; los "
            f"meals se miden con lo que duró: " + _lista(duraciones) + "."))
    de_mas = {k: v for k, v in ot_de_mas_por_semana.items() if v > TOLERANCIA_HORAS}
    de_menos = {k: v for k, v in ot_de_mas_por_semana.items() if v < -TOLERANCIA_HORAS}
    if de_mas:
        avisos.append(aviso(
            "revisar", "overtime_vs_oracle",
            f"En {len(de_mas)} semana(s) de alguien, las reglas de California dan "
            f"{sum(de_mas.values()):,.2f} h más de overtime o doble que las que puso Oracle esa "
            f"semana. Si hoy se paga lo de Oracle, esas horas se estarían pagando como regulares."))
    if de_menos:
        avisos.append(aviso(
            "info", "overtime_oracle_de_mas",
            f"En {len(de_menos)} semana(s) de alguien, Oracle puso {-sum(de_menos.values()):,.2f} h más "
            f"de overtime que las reglas de California."))
    ajustes = sum(p["ajustes"] for p in personas.values())
    if ajustes:
        avisos.append(aviso("info", "ajustes",
                            f"{ajustes} ajuste(s) de gerentes en las tarjetas del periodo."))

    # Las reglas que todavía son supuestos.
    if not ca_overtime.WORKWEEK_START_CONFIRMED or not CALENDARIO_CONFIRMADO:
        avisos.append(aviso(
            "info", "supuestos_calendario",
            "La semana laboral (sábado a viernes) y el calendario de pago (cada dos semanas) son "
            "supuestos: falta confirmarlos en Gusto."))
    tope = ("una hora de prima por violación" if politica.tope_por_dia is None
            else f"máximo {politica.tope_por_dia:g} hora de prima por día")
    avisos.append(aviso(
        "info", "politica_meals",
        f"Meals con la política {politica.nombre}: primer meal en jornadas de más de "
        f"{politica.primer_si_pasa_de_h:g} h y segundo en las de más de "
        f"{politica.segundo_si_pasa_de_h:g} h; {tope}. Con la política {otra.nombre} serían "
        f"{prima_otra:,.0f} h de prima de {len(personas_otra)} persona(s)."))
    avisos.append(aviso(
        "info", "rest_breaks",
        "No se calcula la prima de los descansos de 10 minutos: Oracle no trae breaks pagados, y "
        "si esos descansos no se checan, no hay con qué medirlos."))
    avisos.append(aviso(
        "info", "tips_fuera",
        "Los tips se enseñan para revisar; todavía no van a ningún archivo."))

    filas = []
    for p in personas.values():
        filas.append({
            **p,
            "tiendas": sorted(p["tiendas"]),
            "puestos": {k: round(v, 2) for k, v in sorted(p["puestos"].items(), key=lambda kv: -kv[1])},
            "tarifas": sorted(p["tarifas"]),
            "regular": round(p["regular"], 2),
            "overtime": round(p["overtime"], 2),
            "double": round(p["double"], 2),
            "ovt_oracle": round(p["ovt_oracle"], 2),
            "tips": {k: round(v, 2) for k, v in p["tips"].items()},
            "dias_tips_sin_tarjeta": len(p["dias_tips_sin_tarjeta"]),
        })
    filas.sort(key=lambda f: (f["tiendas"][:1], f["nombre"]))

    return {
        "inicio": inicio,
        "fin": fin,
        "version": VERSION_DEL_RESULTADO,
        "personas": filas,
        "retenidos": {k: {"cuentas": len(v["cuentas"]), "dias": len(v["dias"]),
                          "tarjeta": round(v["tarjeta"], 2), "pagados": round(v["pagados"], 2)}
                      for k, v in retenidos.items()},
        "cuadre": {
            "horas_oracle": round(horas_oracle, 2),
            "horas_colocadas": round(horas_colocadas, 2),
            "horas_oracle_puesto": round(horas_oracle_puesto, 2),
            "regular": round(sum(f["regular"] for f in filas), 2),
            "overtime": round(sum(f["overtime"] for f in filas), 2),
            "double": round(sum(f["double"] for f in filas), 2),
            "ovt_oracle": round(sum(f["ovt_oracle"] for f in filas), 2),
            "ot_de_mas_vs_oracle": round(sum(de_mas.values()), 2),
            "prima": round(sum(f["prima"] for f in filas), 2),
            "prima_otra_politica": round(prima_otra, 2),
            "otra_politica": otra.nombre,
            "tips_oracle": round(tips_crudos, 2),
            "tips_personas": round(tips_personas, 2),
            "tips_retenidos": round(tips_retenidos, 2),
            "tips_pagados": round(pagados_crudos, 2),
        },
        "avisos": avisos,
        "bloquea": any(a["nivel"] == "bloquea" for a in avisos),
    }
