"""Overtime y double time de California calculados desde las checadas.

Viene de payroll tacos (commit 6fab0a7). Cambios:
- La semana laboral de Broken Yolk está SIN CONFIRMAR (abajo).
- is_seventh_consecutive_day recibe el inicio de semana. Antes usaba siempre la
  constante, y si alguien pasaba otro start_weekday a compute_overtime, las
  semanas se partían de un modo y el séptimo día se juzgaba de otro.
- parse_business_date acepta también AAAA-MM-DD, que es como da las fechas
  Simphony.

Oracle Labor Management da su propio overtime (ovt1Hrs..ovt4Hrs), pero con sus
reglas, que no documentan ni el doble después de 12 h ni el séptimo día. Este
módulo lo calcula de cero, sobre las horas por persona y por jornada.

Labor Code 510(a) y 511, para empleados no exentos:

  Por jornada   más de 8 h        -> 1.5x
                más de 12 h       -> 2x
  Por semana    más de 40 h de tiempo recto -> 1.5x
  Séptimo día   todo el 7mo día consecutivo de la semana laboral:
                las primeras 8 h  -> 1.5x
                arriba de 8 h     -> 2x

No se piramida: una hora que ya se pagó a 1.5x por la regla diaria no se vuelve
a contar para el umbral de las 40 semanales.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

# Inicio de la semana laboral, en el formato de date.weekday():
# 0=lunes ... 6=domingo.
#
# OJO: la semana laboral es un periodo fijo y recurrente de 168 h que fija el
# patrón (29 CFR 778.105), y tiene que ser la misma que tiene Gusto.
# Sábado es un SUPUESTO: el periodo con el que se validó mealviolation fue del
# sábado 27 jun al viernes 10 jul de 2026, igual que el calendario de tacos.
# Hay que leerlo en Gusto > Pay > Pay settings de cada empresa de Broken Yolk.
WORKWEEK_START_WEEKDAY = 5
WORKWEEK_START_CONFIRMED = False

DAILY_OT_THRESHOLD = 8.0
DAILY_DT_THRESHOLD = 12.0
WEEKLY_OT_THRESHOLD = 40.0
SEVENTH_DAY_OT_HOURS = 8.0


def parse_business_date(value: str | date) -> date:
    """Acepta date, 'AAAA-MM-DD' (Simphony) o 'AAAAMMDD' (Toast)."""
    if isinstance(value, date):
        return value
    texto = str(value)
    if "-" in texto:
        return date.fromisoformat(texto)
    return datetime.strptime(texto, "%Y%m%d").date()


def workweek_start(day: date, start_weekday: int = WORKWEEK_START_WEEKDAY) -> date:
    """Primer día de la semana laboral que contiene a `day`."""
    delta = (day.weekday() - start_weekday) % 7
    return day - timedelta(days=delta)


def split_daily(hours: float) -> tuple:
    """Reparte las horas de UNA jornada en (recto, 1.5x, 2x) por la regla diaria."""
    straight = min(hours, DAILY_OT_THRESHOLD)
    overtime = max(0.0, min(hours, DAILY_DT_THRESHOLD) - DAILY_OT_THRESHOLD)
    double = max(0.0, hours - DAILY_DT_THRESHOLD)
    return straight, overtime, double


def split_seventh_day(hours: float) -> tuple:
    """El 7mo día consecutivo no tiene tiempo recto: 8 h a 1.5x y el resto a 2x."""
    overtime = min(hours, SEVENTH_DAY_OT_HOURS)
    double = max(0.0, hours - SEVENTH_DAY_OT_HOURS)
    return 0.0, overtime, double


def is_seventh_consecutive_day(day: date, worked_days: set,
                               start_weekday: int = WORKWEEK_START_WEEKDAY) -> bool:
    """True si `day` es el 7mo día trabajado de una racha de 7 en su semana laboral.

    La racha se cuenta DENTRO de la semana laboral: el derecho del 7mo día nace
    de trabajar los siete días de una misma semana laboral, no de siete días
    consecutivos cualesquiera a caballo de dos semanas.
    """
    week = workweek_start(day, start_weekday)
    week_days = {week + timedelta(days=i) for i in range(7)}
    return week_days.issubset(worked_days) and day == week + timedelta(days=6)


def compute_overtime(person_days: dict, start_weekday: int = WORKWEEK_START_WEEKDAY) -> dict:
    """Calcula el reparto de horas por persona y por jornada.

    person_days: {identidad: {business_date (date|str): horas_trabajadas}}

    Devuelve {identidad: {
        "days":  {business_date: {"regular","overtime","double","rule"}},
        "weeks": {inicio_semana: {"regular","overtime","double","worked_days"}},
        "totals": {"regular","overtime","double"},
    }}
    """
    result = {}

    for identity, days_raw in person_days.items():
        days = {parse_business_date(d): float(h or 0.0) for d, h in days_raw.items()}
        worked = {d for d, h in days.items() if h > 0}

        by_week = defaultdict(list)
        for day in sorted(days):
            by_week[workweek_start(day, start_weekday)].append(day)

        per_day = {}
        per_week = {}

        for week, week_days in sorted(by_week.items()):
            # Paso 1: regla diaria y séptimo día.
            daily = {}
            for day in week_days:
                hours = days[day]
                if hours <= 0:
                    daily[day] = (0.0, 0.0, 0.0, "sin horas")
                    continue
                if is_seventh_consecutive_day(day, worked, start_weekday):
                    straight, overtime, double = split_seventh_day(hours)
                    rule = "7mo dia consecutivo"
                else:
                    straight, overtime, double = split_daily(hours)
                    rule = ("diaria >12h" if double else
                            "diaria >8h" if overtime else "recto")
                daily[day] = (straight, overtime, double, rule)

            # Paso 2: las 40 semanales se miden SOLO sobre el tiempo recto, para
            # no piramidar sobre horas que la regla diaria ya pagó a 1.5x.
            straight_total = sum(v[0] for v in daily.values())
            weekly_excess = max(0.0, straight_total - WEEKLY_OT_THRESHOLD)

            # El excedente se convierte a 1.5x empezando por los días más
            # recientes, que es la convención que no altera el total de horas.
            remaining = weekly_excess
            for day in sorted(daily, reverse=True):
                if remaining <= 0:
                    break
                straight, overtime, double, rule = daily[day]
                moved = min(straight, remaining)
                if moved > 0:
                    daily[day] = (straight - moved, overtime + moved, double,
                                  rule + " + 40h semanales")
                    remaining -= moved

            for day, (straight, overtime, double, rule) in daily.items():
                per_day[day] = {
                    "regular": round(straight, 4),
                    "overtime": round(overtime, 4),
                    "double": round(double, 4),
                    "rule": rule,
                }

            per_week[week] = {
                "regular": round(sum(daily[d][0] for d in daily), 4),
                "overtime": round(sum(daily[d][1] for d in daily), 4),
                "double": round(sum(daily[d][2] for d in daily), 4),
                "worked_days": sum(1 for d in week_days if days[d] > 0),
            }

        result[identity] = {
            "days": per_day,
            "weeks": per_week,
            "totals": {
                "regular": round(sum(v["regular"] for v in per_day.values()), 2),
                "overtime": round(sum(v["overtime"] for v in per_day.values()), 2),
                "double": round(sum(v["double"] for v in per_day.values()), 2),
            },
        }

    return result
