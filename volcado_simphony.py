"""Volcado crudo de Simphony, para conocer los campos reales antes de escribir el lector.

    python -I volcado_simphony.py --desde 2026-10-31 --hasta 2026-11-13
    python -I volcado_simphony.py --desde 2026-11-12 --hasta 2026-11-12 --tiendas BYC301

Pide a la BI API, por tienda, los empleados, los puestos y la última fecha de
negocio; y por tienda y día, las tarjetas de tiempo (con sus ajustes) y los
totales diarios por empleado. Guarda cada respuesta tal cual en
reference/volcado_<desde>_<hasta>/, que no va al repo porque trae nombres,
tarifas y tips reales.

En pantalla solo sale el inventario de campos (en cuántos registros viene cada
uno y en cuántos trae algo) y sumas por tienda. Nunca un valor de un renglón:
ni nombres, ni ids, ni montos de una persona. Oracle omite los campos que valen
0 o null, así que un campo que "viene en" pocos registros no es un error.

Las credenciales salen de .streamlit/secrets.toml, sección [oracle_bi].
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

AQUI = Path(__file__).resolve().parent
# Con python -I no entran al path ni la carpeta del script ni la actual; esta
# carpeta es el repo, así que se agrega a mano (reference/ no queda en el path).
if str(AQUI) not in sys.path:
    sys.path.insert(0, str(AQUI))

from oracle_bi.client import OracleBIClient, OracleBIError  # noqa: E402
from oracle_bi.settings import config_from_toml_file  # noqa: E402

SECRETS = AQUI / ".streamlit" / "secrets.toml"
REFERENCIA = AQUI / "reference"
MAX_DIAS = 31

# Solo se imprimen llaves que parecen nombres de campo. Si Oracle llegara a
# usar un dato como llave (un número de empleado, un nombre), se cuenta pero
# no se enseña.
LLAVE_DE_CAMPO = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Campos que se suman: horas y dinero. La tarifa no se suma.
CAMPO_SUMABLE = re.compile(r"(Hrs|Pay|Tips|Ttl|Rcpts|Amt)$")
NO_SUMAR = {"payRt"}
# Una suma de uno o dos registros es el monto de una o dos personas: no se enseña.
MINIMO_PARA_SUMA = 3


def _tipo(valor: Any) -> str:
    if valor is None:
        return "null"
    if isinstance(valor, bool):
        return "bool"
    if isinstance(valor, int):
        return "int"
    if isinstance(valor, float):
        return "float"
    if isinstance(valor, str):
        return "str"
    if isinstance(valor, list):
        return "list"
    if isinstance(valor, dict):
        return "dict"
    return type(valor).__name__


def _tiene_valor(valor: Any) -> bool:
    if valor is None or valor == "" or valor == [] or valor == {}:
        return False
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return valor != 0
    return True


class Inventario:
    """Por cada ruta de registro y campo: en cuántos viene, en cuántos trae
    algo, de qué tipos, y la suma si es de horas o dinero."""

    def __init__(self) -> None:
        self.registros: dict[str, int] = defaultdict(int)
        self.campos: dict[tuple[str, str], dict[str, Any]] = {}
        self.llaves_ocultas: dict[str, int] = defaultdict(int)

    def agregar(self, objeto: Any, ruta: str = "$") -> None:
        if isinstance(objeto, list):
            for elemento in objeto:
                self.agregar(elemento, ruta + "[]")
            return
        if not isinstance(objeto, dict):
            return
        self.registros[ruta] += 1
        for clave, valor in objeto.items():
            clave = str(clave)
            if not LLAVE_DE_CAMPO.match(clave):
                self.llaves_ocultas[ruta] += 1
                continue
            campo = self.campos.setdefault(
                (ruta, clave), {"viene": 0, "con_valor": 0, "tipos": set(), "suma": None}
            )
            campo["viene"] += 1
            campo["tipos"].add(_tipo(valor))
            if _tiene_valor(valor):
                campo["con_valor"] += 1
            if (
                isinstance(valor, (int, float))
                and not isinstance(valor, bool)
                and CAMPO_SUMABLE.search(clave)
                and clave not in NO_SUMAR
            ):
                campo["suma"] = (campo["suma"] or 0.0) + float(valor)
            if isinstance(valor, (dict, list)):
                self.agregar(valor, f"{ruta}.{clave}")

    def formatear(self, titulo: str) -> str:
        lineas = [f"== {titulo}"]
        if not self.registros:
            lineas.append("   (sin respuestas)")
        for ruta in sorted(self.registros):
            lineas.append(f"   {ruta}   {self.registros[ruta]} registro(s)")
            for (r, clave), campo in sorted(self.campos.items()):
                if r != ruta:
                    continue
                if campo["suma"] is None:
                    suma = ""
                elif campo["con_valor"] < MINIMO_PARA_SUMA:
                    suma = "   suma sin enseñar (pocos registros)"
                else:
                    suma = f"   suma {campo['suma']:,.2f}"
                lineas.append(
                    f"      {clave:<30} viene en {campo['viene']:>6}   con valor en "
                    f"{campo['con_valor']:>6}   {'/'.join(sorted(campo['tipos']))}{suma}"
                )
            if self.llaves_ocultas.get(ruta):
                lineas.append(
                    f"      ({self.llaves_ocultas[ruta]} llave(s) que no parecen nombre de campo, sin enseñar)"
                )
        return "\n".join(lineas)


CONSULTAS_POR_TIENDA = (
    ("employees", "getEmployeeDimensions", "get_employees"),
    ("job_codes", "getJobCodeDimensions", "get_job_codes"),
    ("latest_bus_dt", "getLatestBusDt", "get_latest_business_date"),
)
CONSULTAS_POR_DIA = (
    ("timecards", "getTimeCardDetails", "get_timecards"),
    ("employee_daily_totals", "getEmployeeDailyTotals", "get_employee_daily_totals"),
)


def _contar_tarjetas(respuesta: dict[str, Any]) -> tuple[int, set[str]]:
    tarjetas = 0
    empleados: set[str] = set()
    dias = respuesta.get("businessDates") or []
    if not dias and isinstance(respuesta.get("timeCardDetails"), list):
        dias = [{"timeCardDetails": respuesta["timeCardDetails"]}]
    for dia in dias:
        if not isinstance(dia, dict):
            continue
        for tarjeta in dia.get("timeCardDetails") or []:
            if isinstance(tarjeta, dict):
                tarjetas += 1
                empleados.add(str(tarjeta.get("empNum")))
    return tarjetas, empleados


def correr(
    client: Any,
    desde: date,
    hasta: date,
    tiendas: list[str],
    carpeta: Path,
    salida: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Hace el volcado y regresa el resumen. client es un OracleBIClient o
    cualquier cosa con los mismos métodos (las pruebas usan uno falso)."""
    carpeta.mkdir(parents=True, exist_ok=True)
    errores: list[dict[str, str]] = []
    inventarios = {nombre: Inventario() for nombre in
                   ["locations"] + [c[0] for c in CONSULTAS_POR_TIENDA + CONSULTAS_POR_DIA]}
    por_tienda: dict[str, dict[str, Any]] = {}

    def guardar(nombre: str, datos: Any) -> None:
        (carpeta / nombre).write_text(
            json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def pedir(etiqueta: str, funcion: Callable[..., dict[str, Any]], *args: Any):
        try:
            return funcion(*args)
        except OracleBIError as exc:
            errores.append({"consulta": etiqueta, "error": str(exc)})
            salida(f"   x {etiqueta}: {exc}")
            return None

    salida(f"Volcado del {desde} al {hasta}; se guarda en {carpeta}")
    locations = pedir("getLocationDimensions", client.get_locations)
    if locations is not None:
        guardar("locations.json", locations)
        inventarios["locations"].agregar(locations)
        conocidas = {
            str(l.get("locRef")): l for l in locations.get("locations") or [] if isinstance(l, dict)
        }
        activas = [ref for ref, l in conocidas.items() if l.get("active", True)]
        elegidas = tiendas or sorted(activas)
        for ref in tiendas:
            if ref not in conocidas:
                salida(f"   ! {ref} no viene en getLocationDimensions; se pide de todos modos")

        for ref in elegidas:
            resumen = {"dias": 0, "dias_sin_tarjetas": 0, "tarjetas": 0, "empleados": set(), "errores": 0}
            antes = len(errores)
            for nombre, endpoint, metodo in CONSULTAS_POR_TIENDA:
                datos = pedir(f"{endpoint} {ref}", getattr(client, metodo), ref)
                if datos is not None:
                    guardar(f"{nombre}_{ref}.json", datos)
                    inventarios[nombre].agregar(datos)
            dia = desde
            while dia <= hasta:
                resumen["dias"] += 1
                for nombre, endpoint, metodo in CONSULTAS_POR_DIA:
                    datos = pedir(f"{endpoint} {ref} {dia}", getattr(client, metodo), ref, dia)
                    if datos is None:
                        continue
                    guardar(f"{nombre}_{ref}_{dia}.json", datos)
                    inventarios[nombre].agregar(datos)
                    if nombre == "timecards":
                        tarjetas, empleados = _contar_tarjetas(datos)
                        resumen["tarjetas"] += tarjetas
                        resumen["empleados"] |= empleados
                        if not tarjetas:
                            resumen["dias_sin_tarjetas"] += 1
                dia += timedelta(days=1)
            resumen["errores"] = len(errores) - antes
            por_tienda[ref] = resumen
            salida(
                f"   {ref}: {resumen['tarjetas']} tarjetas de {len(resumen['empleados'])} empleado(s) "
                f"en {resumen['dias']} días ({resumen['dias_sin_tarjetas']} sin tarjetas), "
                f"{resumen['errores']} error(es)"
            )

    guardar("errores.json", errores)
    partes = [inventarios[nombre].formatear(nombre) for nombre in inventarios]
    partes.append("== por tienda")
    for ref, r in por_tienda.items():
        partes.append(
            f"   {ref:<10} tarjetas {r['tarjetas']:>6}   empleados {len(r['empleados']):>4}   "
            f"días sin tarjetas {r['dias_sin_tarjetas']:>3} de {r['dias']}   errores {r['errores']}"
        )
    partes.append(f"== errores: {len(errores)} (detalle en errores.json)")
    texto = "\n".join(partes)
    (carpeta / "inventario.txt").write_text(texto + "\n", encoding="utf-8")
    salida(texto)
    return {"errores": errores, "por_tienda": por_tienda, "inventarios": inventarios}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Volcado crudo de Simphony (BI API) para conocer los campos reales."
    )
    parser.add_argument("--desde", required=True, type=date.fromisoformat, help="AAAA-MM-DD")
    parser.add_argument("--hasta", required=True, type=date.fromisoformat, help="AAAA-MM-DD")
    parser.add_argument("--tiendas", default="", help="locRef separados por coma; si no, todas las activas")
    parser.add_argument("--secrets", default=str(SECRETS), help="archivo TOML con la sección [oracle_bi]")
    args = parser.parse_args(argv)

    if args.hasta < args.desde:
        parser.error("--hasta no puede ser antes de --desde")
    if (args.hasta - args.desde).days + 1 > MAX_DIAS:
        parser.error(f"el rango no puede pasar de {MAX_DIAS} días")

    try:
        config = config_from_toml_file(args.secrets)
    except ValueError as exc:
        print(f"No se pudieron leer las credenciales: {exc}")
        return 2

    tiendas = [t.strip() for t in args.tiendas.split(",") if t.strip()]
    carpeta = REFERENCIA / f"volcado_{args.desde}_{args.hasta}"
    resultado = correr(OracleBIClient(config), args.desde, args.hasta, tiendas, carpeta)
    return 1 if resultado["errores"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
