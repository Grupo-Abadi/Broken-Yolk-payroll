"""Simphony falso: escenarios de tarjetas y tips para probar el motor de punta a punta.

    esc = Escenario()
    ana = esc.empleado("Ejemplo", "Ana")
    esc.turno(ana, "2026-11-02", "07:00", "11:00", meal=31, hasta="14:30")
    esc.tips(ana, "2026-11-02", tarjeta=50.0, pagados=50.0)
    resultado = esc.correr(date(2026, 10, 31), date(2026, 11, 13))

Las tarjetas salen con la forma que da Oracle de verdad:
- el tramo que termina en meal trae clkOutStatus 66 y shiftType 2; el que sale
  a break pagado, 80 y 1; los demás, shiftType 0;
- las horas van en regHrs, y en ovt1Hrs/ovt2Hrs si se piden;
- los campos en cero se omiten;
- los tips y el total por puesto vienen por revenue center;
- el total por puesto llama ovtHrs al primer nivel de overtime.
Todos los datos son inventados.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import json

from oracle_bi.client import OracleBIError
import motor
import simphony

TIENDA = "BYC999"
# Diferencia fija con UTC. Los meals y las horas se miden en UTC, así que basta
# con que sea la misma en todo el escenario.
DESFASE_UTC = timedelta(hours=8)


class Escenario:
    def __init__(self) -> None:
        self.empleados: dict[str, list[dict]] = defaultdict(list)
        self.puestos: dict[str, dict[int, str]] = defaultdict(dict)
        self.tarjetas: dict[tuple[str, date], list[dict]] = defaultdict(list)
        self.totales: dict[tuple[str, date], dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
        self.fallas: set[tuple[str, str, date | None]] = set()
        self.horas_de_mas: dict[tuple[str, date], float] = defaultdict(float)
        # El día abierto de Oracle por tienda. Sin dar, hoy: todo lo pasado ya cerró.
        self.dia_abierto: dict[str, date] = {}
        self.tiendas: list[str] = []
        # Tiendas que Oracle da como inactivas: salen en getLocationDimensions
        # y no se deben pedir.
        self.tiendas_inactivas: list[str] = []
        self._siguiente_num = 9001
        self._siguiente_tc = 1

    def _tienda(self, tienda: str) -> str:
        if tienda not in self.tiendas:
            self.tiendas.append(tienda)
        return tienda

    def empleado(self, apellido: str, nombre: str, *, tienda: str = TIENDA, puesto: int = 10,
                 nombre_puesto: str = "Server", tarifa: float = 18.0, clase: str = "Server",
                 payroll_id: str | None = None) -> dict:
        num = self._siguiente_num
        self._siguiente_num += 1
        registro = {"num": num, "payrollId": payroll_id if payroll_id is not None else f"EJ{num}",
                    "fName": nombre, "lName": apellido, "className": clase,
                    "_tienda": self._tienda(tienda), "_puesto": puesto, "_tarifa": tarifa}
        self.empleados[tienda].append(registro)
        self.puestos[tienda].setdefault(puesto, nombre_puesto)
        return registro

    def cuenta(self, nombre: str, *, clase: str = "SuperUser", tienda: str = TIENDA) -> dict:
        """Una cuenta que no checa, de las que en Oracle traen tips sin tarjetas."""
        return self.empleado(nombre, "", tienda=tienda, clase=clase)

    def puesto(self, num: int, nombre: str, *, tienda: str = TIENDA) -> None:
        self.puestos[self._tienda(tienda)][num] = nombre

    def tramo(self, emp: dict, dia: str, entra: str, sale: str | None, *, salida: int = 84,
              puesto: int | None = None, tienda: str | None = None, horas: float | None = None,
              ovt: float = 0.0, ovt2: float = 0.0, tarifa: float | None = None, ajustes: int = 0,
              payroll_id: str | None = None, rvc: int = 1, dia_entra: str | None = None) -> dict:
        """Una tarjeta del día de negocio `dia`. sale=None la deja abierta. Las
        horas, si no se dan, son lo que dura. dia_entra es el día de calendario
        de la entrada, si no es el de negocio."""
        tienda = self._tienda(tienda or emp["_tienda"])
        dia_negocio = date.fromisoformat(dia)
        inicio = _local(dia_entra or dia, entra)
        tarjeta = {
            "tcId": self._siguiente_tc, "empNum": emp["num"],
            "payrollID": emp["payrollId"] if payroll_id is None else payroll_id,
            "jcNum": puesto if puesto is not None else emp["_puesto"], "rvcNum": rvc,
            "clkInLcl": inicio.isoformat(), "clkInUTC": _utc(inicio), "clkInStatus": 84,
            "payRt": tarifa if tarifa is not None else emp["_tarifa"],
            "adjustments": [{"adjId": n, "rsn": "Ajuste de prueba"} for n in range(ajustes)],
        }
        self._siguiente_tc += 1
        if sale is not None:
            fin = _fin(inicio, sale)
            dura = (fin - inicio).total_seconds() / 3600
            pagadas = round(dura if horas is None else horas, 2)
            tarjeta.update({"clkOutLcl": fin.isoformat(), "clkOutUTC": _utc(fin),
                            "clkOutStatus": salida})
            regulares = round(pagadas - ovt - ovt2, 2)
            if regulares:
                tarjeta["regHrs"] = regulares
                tarjeta["regPay"] = round(regulares * tarjeta["payRt"], 2)
            for n, extra, factor in ((1, ovt, 1.5), (2, ovt2, 2.0)):
                if extra:
                    tarjeta[f"ovt{n}Hrs"] = extra
                    tarjeta[f"ovt{n}Pay"] = round(extra * tarjeta["payRt"] * factor, 2)
        # Oracle manda shiftType en todas las tarjetas: 2 en el tramo que sale
        # a break sin pago, 1 en el que sale a break pagado y 0 en las demás.
        tarjeta["shiftType"] = {simphony.SALIDA_A_BREAK: simphony.TURNO_A_BREAK,
                                simphony.SALIDA_A_BREAK_PAGADO: simphony.TURNO_A_BREAK_PAGADO}.get(salida, 0)
        self.tarjetas[(tienda, dia_negocio)].append(tarjeta)
        return tarjeta

    def descuadre(self, dia: str, horas: float, *, tienda: str = TIENDA) -> None:
        """Que el total por puesto de Oracle traiga horas de más (o de menos, con
        horas negativas) respecto a las tarjetas, como si sobrara o faltara una."""
        self.horas_de_mas[(self._tienda(tienda), date.fromisoformat(dia))] += horas

    def turno(self, emp: dict, dia: str, entra: str, sale: str, *, meal: float | None = None,
              hasta: str | None = None, **kwargs) -> None:
        """Un turno. Con meal (minutos) y hasta, sale a comer con 66 y regresa."""
        if meal is None:
            self.tramo(emp, dia, entra, sale, **kwargs)
            return
        self.tramo(emp, dia, entra, sale, salida=simphony.SALIDA_A_BREAK, **kwargs)
        regreso = _fin(_local(kwargs.get("dia_entra") or dia, entra), sale) + timedelta(minutes=meal)
        kwargs["dia_entra"] = regreso.date().isoformat()
        self.tramo(emp, dia, regreso.strftime("%H:%M:%S"), hasta, **kwargs)

    def tips(self, emp: dict, dia: str, *, tarjeta: float = 0.0, pagados: float = 0.0,
             service: float | None = None, tienda: str | None = None, rvc: int = 1,
             **otros: float) -> None:
        fila = {"empNum": emp["num"], "chkCnt": 1}
        campos = {"chrgTipsTtl": tarjeta, "tipsPdTtl": pagados,
                  "svcTtl": tarjeta if service is None else service, **otros}
        fila.update({k: v for k, v in campos.items() if v})
        self.totales[(self._tienda(tienda or emp["_tienda"]), date.fromisoformat(dia))][rvc].append(fila)

    def falla(self, consulta: str, tienda: str = TIENDA, dia: str | None = None) -> None:
        """Que Oracle falle en una consulta: 'tarjetas', 'tips', 'totales_puesto',
        'empleados', 'puestos' o 'dia_abierto'."""
        self.fallas.add((consulta, tienda, date.fromisoformat(dia) if dia else None))

    def fuente(self) -> "FuenteFalsa":
        return FuenteFalsa(self)

    def correr(self, inicio: date, fin: date) -> dict:
        desde, hasta = motor.dias_a_pedir(inicio, fin)
        datos = simphony.jalar(self.fuente(), self.tiendas, desde, hasta)
        return motor.calcular(datos, inicio, fin)

    def guardar_volcado(self, carpeta: Path, desde: date, hasta: date) -> Path:
        """Escribe el escenario como lo dejaría volcado_simphony.py."""
        carpeta.mkdir(parents=True, exist_ok=True)
        fuente = self.fuente()

        def escribir(nombre: str, datos: dict) -> None:
            (carpeta / nombre).write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")

        escribir("locations.json", fuente.get_locations())
        for ref in self.tiendas:
            escribir(f"employees_{ref}.json", fuente.get_employees(ref))
            escribir(f"job_codes_{ref}.json", fuente.get_job_codes(ref))
            escribir(f"latest_bus_dt_{ref}.json", fuente.get_latest_business_date(ref))
            dia = desde
            while dia <= hasta:
                escribir(f"timecards_{ref}_{dia}.json", fuente.get_timecards(ref, dia))
                escribir(f"employee_daily_totals_{ref}_{dia}.json",
                         fuente.get_employee_daily_totals(ref, dia))
                escribir(f"job_code_daily_totals_{ref}_{dia}.json",
                         fuente.get_job_code_daily_totals(ref, dia))
                dia += timedelta(days=1)
        return carpeta


def _local(dia: str, hora: str) -> datetime:
    """'07:00' o '07:00:30' del día dado."""
    return datetime.fromisoformat(f"{dia}T{hora}" + (":00" if len(hora) == 5 else ""))


def _fin(inicio: datetime, hora: str) -> datetime:
    """La salida a esa hora; si es antes que la entrada, es al día siguiente."""
    fin = _local(inicio.date().isoformat(), hora)
    return fin + timedelta(days=1) if fin <= inicio else fin


def _utc(local: datetime) -> str:
    return (local + DESFASE_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FuenteFalsa:
    """Contesta como OracleBIClient con los datos del escenario."""

    def __init__(self, escenario: Escenario) -> None:
        self.esc = escenario

    def _quizas_falla(self, consulta: str, tienda: str, dia: date | None = None) -> None:
        if (consulta, tienda, dia) in self.esc.fallas or (consulta, tienda, None) in self.esc.fallas:
            raise OracleBIError(f"Oracle falló (HTTP 503) en {consulta} de {tienda}")

    def get_locations(self) -> dict:
        return {"locations": [{"locRef": t, "name": f"Tienda {t}", "active": True}
                              for t in self.esc.tiendas]
                + [{"locRef": t, "name": f"Cerrada {t}", "active": False}
                   for t in self.esc.tiendas_inactivas]}

    def get_employees(self, ref: str) -> dict:
        self._quizas_falla("empleados", ref)
        return {"locRef": ref, "employees": [
            {k: v for k, v in e.items() if not k.startswith("_")} for e in self.esc.empleados[ref]]}

    def get_job_codes(self, ref: str) -> dict:
        self._quizas_falla("puestos", ref)
        return {"locRef": ref, "jobCodes": [{"num": n, "name": nombre}
                                            for n, nombre in sorted(self.esc.puestos[ref].items())]}

    def get_latest_business_date(self, ref: str) -> dict:
        self._quizas_falla("dia_abierto", ref)
        abierto = self.esc.dia_abierto.get(ref) or motor.hoy_pacifico()
        return {"locRef": ref, "latestBusDt": abierto.isoformat(), "curUTC": "2026-11-19T00:00:00"}

    def get_timecards(self, ref: str, dia: date) -> dict:
        self._quizas_falla("tarjetas", ref, dia)
        return {"locRef": ref, "businessDates": [
            {"busDt": dia.isoformat(), "timeCardDetails": list(self.esc.tarjetas.get((ref, dia), []))}],
            "_requestedBusDt": dia.isoformat(), "_includeAdjustmentsRequested": True}

    def get_employee_daily_totals(self, ref: str, dia: date) -> dict:
        self._quizas_falla("tips", ref, dia)
        por_rvc = self.esc.totales.get((ref, dia), {})
        return {"locRef": ref, "busDt": dia.isoformat(), "_requestedBusDt": dia.isoformat(),
                "revenueCenters": [{"rvcNum": rvc, "employees": list(filas)}
                                   for rvc, filas in sorted(por_rvc.items())]}

    def get_job_code_daily_totals(self, ref: str, dia: date) -> dict:
        """El total por puesto sale de las mismas tarjetas, más el descuadre
        que se haya pedido, como lo da Oracle: ovtHrs es el overtime total y
        ovt2Hrs el desglose del nivel 2, ya incluido en ovtHrs."""
        self._quizas_falla("totales_puesto", ref, dia)
        por_rvc: dict[int, dict[int, dict]] = defaultdict(
            lambda: defaultdict(lambda: {"regHrs": 0.0, "ovtHrs": 0.0, "ovt2Hrs": 0.0}))
        for t in self.esc.tarjetas.get((ref, dia), []):
            h = por_rvc[t["rvcNum"]][t["jcNum"]]
            h["regHrs"] += t.get("regHrs", 0.0)
            h["ovtHrs"] += t.get("ovt1Hrs", 0.0) + t.get("ovt2Hrs", 0.0)
            h["ovt2Hrs"] += t.get("ovt2Hrs", 0.0)
        de_mas = self.esc.horas_de_mas.get((ref, dia), 0.0)
        if de_mas:
            por_rvc[1][999]["regHrs"] += de_mas
        return {"locRef": ref, "busDt": dia.isoformat(), "_requestedBusDt": dia.isoformat(),
                "revenueCenters": [
                    {"rvcNum": rvc, "jobCodes": [{"jcNum": n, **{k: round(v, 2) for k, v in h.items() if v}}
                                                 for n, h in sorted(puestos.items())]}
                    for rvc, puestos in sorted(por_rvc.items())]}
