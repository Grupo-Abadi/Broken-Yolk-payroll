"""Lector de Simphony: de las respuestas de la BI API a tramos, jornadas, meals y tips.

Lo que dicen los datos reales (volcado del 31 oct al 13 nov de 2026, 8 tiendas,
3,309 tarjetas; ver NOTAS_SIMPHONY.md):
- Cada entrada y salida es una tarjeta, o sea un tramo. Un turno con meal llega
  en dos tramos.
- Salir a comer es checar salida con código 66, y casi siempre (1,311 de 1,322)
  ese tramo trae shiftType = 2; los demás traen 0. El meal es el hueco hasta la
  siguiente entrada de la misma persona. No hubo ni un break pagado (80).
- Las horas de cada tarjeta (regHrs más ovt1Hrs..ovt4Hrs) son las que paga
  Oracle, con el redondeo de Labor Management. Oracle omite los campos que
  valen 0, así que un campo que falta es 0.
- Los tips no vienen en las tarjetas: vienen en getEmployeeDailyTotals, por
  persona, revenue center y día, sin puesto.
- getLatestBusDt da el día de negocio abierto: un día ya cerró si es anterior.

Las horas que paga Oracle se usan para pagar y para el overtime. Los meals se
miden con lo que duraron las checadas, que es el tiempo trabajado de verdad.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from oracle_bi.client import OracleBIAuthError, OracleBIError, iter_timecards

# Códigos de salida de Simphony.
SALIDA_A_BREAK = 66          # On Break: se fue a su meal, sin pago
SALIDA_A_BREAK_PAGADO = 80   # On Paid Break: no apareció en el periodo
TURNO_A_BREAK = 2            # shiftType del tramo que termina en break sin pago
TURNO_A_BREAK_PAGADO = 1

MINIMO_MEAL_MIN = 30
# Sin tolerancia. Labor Management sube cada checada al centésimo de hora (36
# s), y 30 min son 50 centésimos exactos: un meal real de 30 min siempre mide
# 30:00 o más. Lo que mide menos, fue más corto. Además, en California los
# meals no se redondean (Donohue v. AMN Services, 2021).
TOLERANCIA_MEAL_SEG = 0
# Un hueco de más de esto dentro de una jornada parece turno partido (split
# shift), que en California puede llevar su propia prima.
HUECO_DE_TURNO_PARTIDO_MIN = 60


@dataclass(frozen=True)
class PoliticaMeals:
    """Cuándo se debe cada meal y cuántas horas de prima se pagan por día.

    tope_por_dia: el Labor Code 226.7 pide una hora por día de trabajo sin el
    meal, sin importar cuántos se perdieron (1.0). None es una hora por
    violación, sin tope, que es lo que decidió pagar el dueño de tacos.
    """
    nombre: str
    primer_si_pasa_de_h: float
    limite_primer_h: float
    segundo_si_pasa_de_h: float
    limite_segundo_h: float
    tope_por_dia: float | None


# Labor Code 512(a): el primer meal se debe en jornadas de más de 5 h y el
# segundo en las de más de 10 h, y cada uno empieza a más tardar al terminar la
# hora 5 o la 10. Con waivers firmados, el primero se puede omitir hasta 6 h y
# el segundo hasta 12 h (si no se omitió el primero). Tacos usa 6 y 12 porque
# allá hay waivers.
ESTRICTA = PoliticaMeals("estricta, sin waivers", 5.0, 5.0, 10.0, 10.0, 1.0)
CON_WAIVERS = PoliticaMeals("con waivers firmados", 6.0, 5.0, 12.0, 10.0, 1.0)

# La que se aplica. Es la estricta mientras no se sepa si hay waivers firmados;
# lo decide el dueño. En el periodo del 31 oct al 13 nov la diferencia fue de
# 476 a 105 horas de prima: 369 de las 430 jornadas sin meal duraron de 5 a 6 h.
POLITICA = ESTRICTA

SIN_MEAL = "Sin meal"
MEAL_TARDE = "Meal tarde"
SIN_SEGUNDO_MEAL = "Sin 2do meal"
SEGUNDO_MEAL_TARDE = "2do meal tarde"

_EPS = 1e-6
MAX_FALLAS_SEGUIDAS = 3


@dataclass
class Datos:
    """Todo lo que se pidió a Oracle para un rango, tal cual llegó."""
    tiendas: list[str]
    desde: date
    hasta: date
    empleados: dict[tuple[str, int], dict] = field(default_factory=dict)
    puestos: dict[tuple[str, int], str] = field(default_factory=dict)
    dia_abierto: dict[str, date] = field(default_factory=dict)   # getLatestBusDt por tienda
    tarjetas: list[dict] = field(default_factory=list)
    totales: list[dict] = field(default_factory=list)          # getEmployeeDailyTotals
    totales_puesto: list[dict] = field(default_factory=list)   # getJobCodeDailyTotals
    errores: list[dict] = field(default_factory=list)


def _dias(desde: date, hasta: date):
    dia = desde
    while dia <= hasta:
        yield dia
        dia += timedelta(days=1)


def jalar(fuente: Any, tiendas: list[str], desde: date, hasta: date, *,
          progreso: Callable[[int, int], None] | None = None) -> Datos:
    """Pide a Oracle (o a un volcado) todo lo de un rango.

    fuente es un OracleBIClient o cualquier cosa con sus métodos. Una consulta
    que falla no tumba la corrida: queda en errores, y el motor no deja
    concluir con datos incompletos.

    Se deja de pedir en cuanto Oracle rechaza el inicio de sesión (cada intento
    fallido acerca el bloqueo de la cuenta) o tras MAX_FALLAS_SEGUIDAS fallas
    seguidas (cada consulta reintenta por su cuenta, y con Oracle caído la
    corrida tardaría una hora en rendirse).
    """
    datos = Datos(tiendas=list(tiendas), desde=desde, hasta=hasta)
    dias = list(_dias(desde, hasta))
    total = len(tiendas) * (3 + len(dias))
    hechos = 0
    fallas_seguidas = 0
    seguidas_por_tipo: dict[str, int] = defaultdict(int)
    cortado = False
    tipos_cortados: set[str] = set()

    def avanzar() -> None:
        nonlocal hechos
        hechos += 1
        if progreso:
            progreso(hechos, total)

    def pedir(consulta: str, funcion: Callable[..., dict], *args: Any) -> dict | None:
        """Una consulta. El tipo es el método: si uno solo falla siempre (por
        ejemplo, un permiso que falta), también se deja de pedir ese."""
        nonlocal fallas_seguidas, cortado
        tipo = getattr(funcion, "__name__", consulta)
        if cortado or tipo in tipos_cortados:
            return None
        try:
            respuesta = funcion(*args)
        except OracleBIAuthError as exc:
            datos.errores.append({"consulta": consulta, "error": str(exc)})
            cortado = True
            datos.errores.append({
                "consulta": "lo que faltaba",
                "error": "Oracle no dejó iniciar sesión (el error de arriba trae el HTTP) y ya no se "
                         "pidió nada más, para no bloquear la cuenta. Si es 400, 401 o 403, revisa la "
                         "contraseña de la cuenta API; si es 429 o 5xx, Oracle está fallando: intenta "
                         "más tarde.",
            })
            return None
        except OracleBIError as exc:
            datos.errores.append({"consulta": consulta, "error": str(exc)})
            fallas_seguidas += 1
            seguidas_por_tipo[tipo] += 1
            if fallas_seguidas >= MAX_FALLAS_SEGUIDAS:
                cortado = True
                datos.errores.append({
                    "consulta": "lo que faltaba",
                    "error": f"Oracle falló {MAX_FALLAS_SEGUIDAS} veces seguidas y ya no se pidió nada más",
                })
            elif seguidas_por_tipo[tipo] >= MAX_FALLAS_SEGUIDAS:
                tipos_cortados.add(tipo)
                datos.errores.append({
                    "consulta": f"lo que faltaba de {tipo}",
                    "error": f"esa consulta falló {MAX_FALLAS_SEGUIDAS} veces seguidas y ya no se pidió más",
                })
            return None
        fallas_seguidas = 0
        seguidas_por_tipo[tipo] = 0
        return respuesta

    for ref in tiendas:
        respuesta = pedir(f"empleados de {ref}", fuente.get_employees, ref)
        for e in _objetos((respuesta or {}).get("employees")):
            if _entero(e.get("num")) is not None:
                datos.empleados[(ref, _entero(e.get("num")))] = e
        avanzar()
        respuesta = pedir(f"puestos de {ref}", fuente.get_job_codes, ref)
        for p in _objetos((respuesta or {}).get("jobCodes")):
            if _entero(p.get("num")) is not None:
                datos.puestos[(ref, _entero(p.get("num")))] = str(p.get("name") or p.get("num"))
        avanzar()
        respuesta = pedir(f"día abierto de {ref}", fuente.get_latest_business_date, ref)
        try:
            datos.dia_abierto[ref] = date.fromisoformat(str((respuesta or {}).get("latestBusDt")))
        except ValueError:
            pass
        avanzar()
        for dia in dias:
            respuesta = pedir(f"tarjetas de {ref} del {dia}", fuente.get_timecards, ref, dia)
            if respuesta is not None:
                datos.tarjetas.append(respuesta)
            respuesta = pedir(f"tips de {ref} del {dia}", fuente.get_employee_daily_totals, ref, dia)
            if respuesta is not None:
                datos.totales.append(respuesta)
            respuesta = pedir(f"total por puesto de {ref} del {dia}",
                              fuente.get_job_code_daily_totals, ref, dia)
            if respuesta is not None:
                datos.totales_puesto.append(respuesta)
            avanzar()
    return datos


class VolcadoFuente:
    """Lee un volcado de volcado_simphony.py como si fuera Oracle.

    Sirve para probar la pantalla y el motor con datos reales sin pegarle a
    Oracle. Lo que no esté en el volcado, o no se pueda leer, falla como
    fallaría una consulta.
    """

    def __init__(self, carpeta: str | Path) -> None:
        self.carpeta = Path(carpeta)

    def _leer(self, nombre: str) -> dict:
        ruta = self.carpeta / nombre
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise OracleBIError(f"no está en el volcado: {nombre}") from None
        except (OSError, ValueError) as exc:
            raise OracleBIError(f"no se pudo leer {nombre} del volcado: {type(exc).__name__}") from None
        if not isinstance(datos, dict):
            raise OracleBIError(f"{nombre} del volcado no es un objeto JSON")
        return datos

    def get_locations(self) -> dict:
        return self._leer("locations.json")

    def get_employees(self, ref: str) -> dict:
        return self._leer(f"employees_{ref}.json")

    def get_job_codes(self, ref: str) -> dict:
        return self._leer(f"job_codes_{ref}.json")

    def get_latest_business_date(self, ref: str) -> dict:
        return self._leer(f"latest_bus_dt_{ref}.json")

    def get_timecards(self, ref: str, dia: date) -> dict:
        return self._leer(f"timecards_{ref}_{dia}.json")

    def get_employee_daily_totals(self, ref: str, dia: date) -> dict:
        return self._leer(f"employee_daily_totals_{ref}_{dia}.json")

    def get_job_code_daily_totals(self, ref: str, dia: date) -> dict:
        return self._leer(f"job_code_daily_totals_{ref}_{dia}.json")


def _entero(valor: Any) -> int | None:
    try:
        return int(valor)
    except (TypeError, ValueError, OverflowError):
        return None


def _numero(valor: Any) -> float:
    """Oracle omite los ceros, así que lo que falta vale 0. Lo que no es un
    número finito también se toma como 0, para que no tumbe la corrida ni
    vuelva inútil un cuadre (con NaN ninguna comparación falla)."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return numero if math.isfinite(numero) else 0.0


def _hora_utc(texto: Any) -> datetime | None:
    if not texto:
        return None
    try:
        hora = datetime.fromisoformat(str(texto).replace("Z", "+00:00"))
    except ValueError:
        return None
    if hora.tzinfo is None:
        hora = hora.replace(tzinfo=timezone.utc)
    return hora


def _payroll_id(valor: Any) -> str:
    """El payrollID limpio. Vacío o puros ceros ("0", "00") es de relleno y
    cuenta como que no viene: si no, juntaría a todos los que lo traen."""
    texto = str(valor or "").strip()
    return "" if not texto.strip("0") else texto


def persona_de(tienda: str, emp_num: Any, empleados: dict, payroll_id_tarjeta: Any = None) -> str:
    """La identidad de una cuenta, la misma para tarjetas y tips: el payrollID
    de la tarjeta, si no el payrollId del empleado en Oracle, y si no la tienda
    y el número de empleado."""
    return (_payroll_id(payroll_id_tarjeta)
            or _payroll_id((empleados.get((tienda, _entero(emp_num))) or {}).get("payrollId"))
            or f"{tienda}#{emp_num}")


@dataclass(frozen=True)
class Tramo:
    persona: str
    emp_num: int | None
    tienda: str
    dia: date
    entra: datetime
    sale: datetime | None
    entra_local: str
    sale_local: str
    horas: float              # regHrs + ovt1..4Hrs: lo que paga Oracle
    horas_ovt_oracle: float   # solo ovt1..4Hrs, para comparar
    puesto_num: int | None
    tarifa: float | None
    salida: int | None        # clkOutStatus
    sale_a_break: bool
    sale_a_break_pagado: bool
    ajustes: int
    tc_id: Any

    @property
    def duracion_h(self) -> float | None:
        if self.sale is None:
            return None
        return (self.sale - self.entra).total_seconds() / 3600

    @property
    def horas_trabajadas(self) -> float:
        """Lo que duró, que es lo que cuenta para los meals. Sin salida, lo que
        paga Oracle."""
        duracion = self.duracion_h
        return self.horas if duracion is None else max(0.0, duracion)


def tramos_de(datos: Datos) -> tuple[list[Tramo], list[dict]]:
    """Las tarjetas como tramos, más los problemas que impiden usar alguna."""
    tramos: list[Tramo] = []
    problemas: list[dict] = []
    for tienda, bus_dt, t in iter_timecards(datos.tarjetas):
        try:
            dia = date.fromisoformat(bus_dt)
        except ValueError:
            problemas.append({"tipo": "tarjeta_sin_dia", "tienda": tienda, "tc_id": t.get("tcId")})
            continue
        entra = _hora_utc(t.get("clkInUTC"))
        if entra is None:
            problemas.append({"tipo": "tarjeta_sin_entrada", "tienda": tienda, "dia": dia,
                              "tc_id": t.get("tcId")})
            continue
        ovt = sum(_numero(t.get(f"ovt{n}Hrs")) for n in range(1, 5))
        salida = _entero(t.get("clkOutStatus"))
        tipo = _entero(t.get("shiftType"))
        ajustes = t.get("adjustments")
        tramos.append(Tramo(
            persona=persona_de(tienda, t.get("empNum"), datos.empleados, t.get("payrollID")),
            emp_num=_entero(t.get("empNum")),
            tienda=tienda,
            dia=dia,
            entra=entra,
            sale=_hora_utc(t.get("clkOutUTC")),
            entra_local=str(t.get("clkInLcl") or ""),
            sale_local=str(t.get("clkOutLcl") or ""),
            horas=_numero(t.get("regHrs")) + ovt,
            horas_ovt_oracle=ovt,
            puesto_num=_entero(t.get("jcNum")),
            tarifa=_numero(t.get("payRt")) or None,
            salida=salida,
            sale_a_break=salida == SALIDA_A_BREAK or tipo == TURNO_A_BREAK,
            sale_a_break_pagado=salida == SALIDA_A_BREAK_PAGADO or tipo == TURNO_A_BREAK_PAGADO,
            ajustes=len(ajustes) if isinstance(ajustes, list) else 0,
            tc_id=t.get("tcId"),
        ))
    tramos.sort(key=lambda x: (x.persona, x.entra))
    return tramos, problemas


@dataclass(frozen=True)
class Meal:
    sale: datetime
    regresa: datetime
    sale_local: str
    regresa_local: str
    minutos: float
    horas_antes: float    # horas trabajadas en la jornada antes de salir
    declarado: bool       # salió con código 66; si no, fue un hueco de 30 min o más

    @property
    def completo(self) -> bool:
        return self.minutos * 60 >= MINIMO_MEAL_MIN * 60 - TOLERANCIA_MEAL_SEG


@dataclass
class Jornada:
    persona: str
    dia: date
    tramos: list[Tramo]
    meals: list[Meal]
    sin_regreso: int = 0      # salió a break y ya no volvió ese día
    encimados: int = 0        # tramos que empiezan antes de que termine el anterior
    huecos_largos: int = 0    # huecos de más de HUECO_DE_TURNO_PARTIDO_MIN

    @property
    def horas(self) -> float:
        """Lo que paga Oracle por la jornada."""
        return sum(t.horas for t in self.tramos)

    @property
    def horas_trabajadas(self) -> float:
        """Lo que duraron las checadas, para los meals."""
        return sum(t.horas_trabajadas for t in self.tramos)


def jornadas_de(tramos: list[Tramo]) -> dict[tuple[str, date], Jornada]:
    """Junta los tramos por persona y día de negocio de Oracle, y encuentra los meals.

    Un meal es el hueco que sigue a una salida a break (66). Un hueco de 30 min
    o más sin ese código también cuenta: la persona estuvo fuera y sin pago.
    Un hueco que sigue a un break pagado (80) no es meal. Si los tramos se
    pegan (cambio de puesto), siguió trabajando; si se enciman, se cuenta.
    """
    grupos: dict[tuple[str, date], list[Tramo]] = {}
    for t in tramos:
        grupos.setdefault((t.persona, t.dia), []).append(t)

    jornadas: dict[tuple[str, date], Jornada] = {}
    for clave, lista in grupos.items():
        lista.sort(key=lambda x: x.entra)
        jornada = Jornada(persona=clave[0], dia=clave[1], tramos=lista, meals=[])
        trabajadas = 0.0
        for a, b in zip(lista, lista[1:]):
            trabajadas += a.horas_trabajadas
            if a.sale is None:
                continue
            minutos = (b.entra - a.sale).total_seconds() / 60
            if minutos < 0:
                jornada.encimados += 1
                continue
            if minutos == 0 or a.sale_a_break_pagado:
                continue
            if minutos > HUECO_DE_TURNO_PARTIDO_MIN:
                jornada.huecos_largos += 1
            if a.sale_a_break or minutos * 60 >= MINIMO_MEAL_MIN * 60 - TOLERANCIA_MEAL_SEG:
                jornada.meals.append(Meal(
                    sale=a.sale, regresa=b.entra,
                    sale_local=a.sale_local, regresa_local=b.entra_local,
                    minutos=minutos, horas_antes=trabajadas, declarado=a.sale_a_break,
                ))
        jornada.sin_regreso = 1 if lista[-1].sale_a_break else 0
        jornadas[clave] = jornada
    return jornadas


def auditar(jornada: Jornada, politica: PoliticaMeals | None = None) -> list[str]:
    """Las violaciones de meal de una jornada. Sin política, la de POLITICA."""
    politica = politica or POLITICA
    horas = jornada.horas_trabajadas
    completos = [m for m in jornada.meals if m.completo]
    violaciones = []
    if horas > politica.primer_si_pasa_de_h + _EPS:
        if not completos:
            violaciones.append(SIN_MEAL)
        elif completos[0].horas_antes > politica.limite_primer_h + _EPS:
            violaciones.append(MEAL_TARDE)
    if horas > politica.segundo_si_pasa_de_h + _EPS:
        if len(completos) < 2:
            violaciones.append(SIN_SEGUNDO_MEAL)
        elif completos[1].horas_antes > politica.limite_segundo_h + _EPS:
            violaciones.append(SEGUNDO_MEAL_TARDE)
    return violaciones


def horas_de_prima(violaciones: list[str], politica: PoliticaMeals | None = None) -> float:
    """Horas de prima por las violaciones de un mismo día."""
    politica = politica or POLITICA
    if not violaciones:
        return 0.0
    if politica.tope_por_dia is None:
        return float(len(violaciones))
    return min(float(len(violaciones)), politica.tope_por_dia)


def _dia_de(respuesta: dict) -> date | None:
    try:
        return date.fromisoformat(str(respuesta.get("busDt") or respuesta.get("_requestedBusDt")))
    except ValueError:
        return None


def _objetos(valor: Any) -> list[dict]:
    """Los elementos de una lista que sí son objetos; lo demás se ignora."""
    return [x for x in valor if isinstance(x, dict)] if isinstance(valor, list) else []


def horas_oracle_por_tienda_dia(datos: Datos) -> dict[tuple[str, date], float]:
    """Las horas que Oracle totaliza por puesto (getJobCodeDailyTotals), por
    tienda y día. Se calculan aparte de las tarjetas, así que sirven para
    cuadrarlas: si falta o sobra una tarjeta, no coinciden."""
    horas: dict[tuple[str, date], float] = {}
    for respuesta in datos.totales_puesto:
        tienda = str(respuesta.get("locRef") or "")
        dia = _dia_de(respuesta)
        if dia is None:
            continue
        total = 0.0
        for rvc in _objetos(respuesta.get("revenueCenters")):
            for puesto in _objetos(rvc.get("jobCodes")):
                # Aquí ovtHrs es el overtime TOTAL, y ovt2Hrs..ovt4Hrs son el
                # desglose de los niveles altos, ya incluidos en ovtHrs. Medido
                # el 13 nov en BYC303: tarjetas con 4.06 h de ovt1 y 7.50 de
                # ovt2, y aquí ovtHrs = 11.56 y ovt2Hrs = 7.50. Sumarlos contaba
                # dos veces el nivel 2. Si no viniera ovtHrs, se suman los niveles.
                if "ovtHrs" in puesto:
                    overtime = _numero(puesto.get("ovtHrs"))
                else:
                    overtime = sum(_numero(puesto.get(f"ovt{n}Hrs")) for n in range(1, 5))
                total += _numero(puesto.get("regHrs")) + overtime
        horas[(tienda, dia)] = horas.get((tienda, dia), 0.0) + total
    return horas


@dataclass(frozen=True)
class TipsDelDia:
    tienda: str
    dia: date
    emp_num: int | None
    persona: str          # misma identidad que en las tarjetas (persona_de)
    clase: str            # className de la cuenta en Simphony
    tarjeta: float        # chrgTipsTtl: tips con tarjeta, del dueño del cheque
    pagados: float        # tipsPdTtl: registro del tender Tips Paid
    service: float        # svcTtl: todos los service charges, incluidos los tips con tarjeta
    service_no_ingreso: float  # nonRevSvcTtl
    tip_ttl: float        # tipTtl: sin explicar todavía
    indirectos: float     # indirTipsPdTtl
    declarados: float     # tipsDclrdTtl


def tips_de(datos: Datos) -> tuple[list[TipsDelDia], list[dict]]:
    """Un renglón por cuenta, revenue center y día que trae algún tip o service
    charge, más las respuestas que no se pudieron usar."""
    filas = []
    problemas = []
    for respuesta in datos.totales:
        tienda = str(respuesta.get("locRef") or "")
        dia = _dia_de(respuesta)
        if dia is None:
            problemas.append({"tipo": "tips_sin_dia", "tienda": tienda})
            continue
        for rvc in _objetos(respuesta.get("revenueCenters")):
            for e in _objetos(rvc.get("employees")):
                emp_num = _entero(e.get("empNum"))
                fila = TipsDelDia(
                    tienda=tienda, dia=dia, emp_num=emp_num,
                    persona=persona_de(tienda, e.get("empNum"), datos.empleados),
                    clase=str((datos.empleados.get((tienda, emp_num)) or {}).get("className") or ""),
                    tarjeta=_numero(e.get("chrgTipsTtl")),
                    pagados=_numero(e.get("tipsPdTtl")),
                    service=_numero(e.get("svcTtl")),
                    service_no_ingreso=_numero(e.get("nonRevSvcTtl")),
                    tip_ttl=_numero(e.get("tipTtl")),
                    indirectos=_numero(e.get("indirTipsPdTtl")),
                    declarados=_numero(e.get("tipsDclrdTtl")),
                )
                if any((fila.tarjeta, fila.pagados, fila.service, fila.service_no_ingreso,
                        fila.tip_ttl, fila.indirectos, fila.declarados)):
                    filas.append(fila)
    return filas, problemas


def suma_cruda(respuestas: list[dict], campo: str, desde: date, hasta: date) -> float:
    """Suma un campo en todo lo que haya dentro de las respuestas del rango,
    sin importar dónde venga. No pasa por tips_de, así que sirve para cuadrar lo
    que tips_de leyó contra lo que Oracle mandó."""
    def recorrer(valor: Any) -> float:
        if isinstance(valor, dict):
            return sum((_numero(v) if k == campo and not isinstance(v, (dict, list)) else recorrer(v))
                       for k, v in valor.items())
        if isinstance(valor, list):
            return sum(recorrer(x) for x in valor)
        return 0.0

    total = 0.0
    for respuesta in respuestas:
        dia = _dia_de(respuesta)
        if dia is not None and desde <= dia <= hasta:
            total += recorrer(respuesta)
    return total
