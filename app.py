"""Nómina de Broken Yolk: de Oracle Simphony a Gusto.

    streamlit run app.py

Primera versión. Jala un periodo de Oracle y enseña, por persona:
- las horas regulares, de overtime y dobles con las reglas de California;
- las primas de meal;
- los tips, para revisar.
Todavía no arma el CSV de Gusto: falta el template de cada empresa.

Para probarla sin pegarle a Oracle, PAYROLL_VOLCADO puede apuntar a una carpeta
de volcado_simphony.py; la pantalla lo avisa arriba.
"""
from __future__ import annotations

import hmac
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

# streamlit run agrega esta carpeta al path; con python -I (las pruebas que
# leen datos reales) no, así que se agrega a mano.
_AQUI = str(Path(__file__).resolve().parent)
if _AQUI not in sys.path:
    sys.path.insert(0, _AQUI)

import motor  # noqa: E402
import simphony  # noqa: E402
from oracle_bi.client import OracleBIClient, OracleBIError  # noqa: E402
from oracle_bi.settings import config_from_secret_mapping  # noqa: E402

st.set_page_config(page_title="Nómina Broken Yolk", page_icon="🍳", layout="wide")

MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def exigir_acceso() -> None:
    """Candado de la app.

    Las apps de Streamlit Community Cloud son públicas por URL salvo que se
    restrinjan los viewers. Esta enseña horas, tarifas y tips de cientos de
    personas, así que además lleva su propia contraseña.
    """
    try:
        esperada = str(st.secrets["APP_PASSWORD"])
    except Exception:
        esperada = os.environ.get("APP_PASSWORD", "")

    # El valor de la plantilla (YOUR_...) o uno en blanco es como no tener.
    esperada = esperada.strip()
    if not esperada or esperada.upper().startswith(("YOUR_", "TU_")):
        st.error("**Esta app no tiene contraseña y enseña datos de nómina.**")
        st.caption(
            "Agrega `APP_PASSWORD` en los secrets antes de abrirla, hasta arriba, antes de "
            "`[oracle_bi]`: si queda abajo, cuenta como parte de esa sección y no se encuentra. "
            "En Streamlit Cloud: Settings > Secrets. Y en Settings > Sharing, deja que solo la "
            "vean las personas que la operan."
        )
        st.stop()

    if st.session_state.get("acceso_ok"):
        return

    st.title("Nómina Broken Yolk")
    with st.form("acceso"):
        clave = st.text_input("Contraseña", type="password")
        if st.form_submit_button("Entrar"):
            if hmac.compare_digest(clave.strip().encode(), esperada.encode()):
                st.session_state["acceso_ok"] = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta")
    st.stop()


exigir_acceso()

VOLCADO = os.environ.get("PAYROLL_VOLCADO", "")


def fuente():
    """Un cliente nuevo cada vez, a propósito. Uno guardado en caché sobrevive a
    un cambio de contraseña en los Secrets, y sus intentos con la vieja
    bloquean la cuenta 30 minutos; también sobrevive a una actualización del
    código, y entonces sus errores ya no se atrapan."""
    if VOLCADO:
        return simphony.VolcadoFuente(VOLCADO)
    return OracleBIClient(config_from_secret_mapping(st.secrets))


@st.cache_data(ttl=3600, show_spinner=False)
def tiendas_activas(origen: str) -> list[tuple[str, str]]:
    """(locRef, nombre) de las tiendas activas. origen solo separa el caché."""
    respuesta = fuente().get_locations()
    # Oracle omite los campos que valen false, así que una tienda sin "active"
    # se toma como inactiva. En el volcado, las 8 tiendas traen active = true.
    return sorted(
        (str(l.get("locRef")), str(l.get("name") or l.get("locRef")))
        for l in respuesta.get("locations") or []
        if isinstance(l, dict) and l.get("active") is True
    )


def etiqueta_periodo(inicio: date, fin: date) -> str:
    return (f"{inicio.day} {MESES[inicio.month - 1]} – {fin.day} {MESES[fin.month - 1]} "
            f"{fin.year}")


def huella_de_entradas(inicio: date, fin: date, tiendas: list[str]) -> tuple:
    return (inicio, fin, tuple(sorted(tiendas)), VOLCADO)


def motivo_para_volver_a_generar(resultado: dict, huella_guardada, huella_actual) -> str:
    if resultado.get("version") != motor.VERSION_DEL_RESULTADO:
        return ("La herramienta se actualizó desde que generaste este resultado, así que se "
                "armó con las reglas de antes. Dale **Generar** otra vez.")
    if huella_guardada != huella_actual:
        return "Cambiaste el periodo o las tiendas. Dale **Generar** otra vez."
    return ""


def tabla_personas(personas: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Empleado": p["nombre"],
        "Tienda": ", ".join(p["tiendas"]),
        "Puestos": ", ".join(p["puestos"]),
        "Días": p["dias"],
        "Regular": p["regular"],
        "Overtime": p["overtime"],
        "Doble": p["double"],
        "Prima meal (h)": p["prima"],
        "Tips tarjeta": p["tips"].get("tarjeta", 0.0),
        "Tips pagados": p["tips"].get("pagados", 0.0),
        "OT de Oracle": p["ovt_oracle"],
    } for p in personas])


def tabla_violaciones(personas: list[dict]) -> pd.DataFrame:
    filas = []
    for p in personas:
        for v in p["violaciones"]:
            meals = "; ".join(
                f"{m['sale']}–{m['regresa']} ({m['minutos']} min, a las {m['horas_antes']:g} h)"
                + ("" if m["completo"] else " corto")
                for m in v["meals"]
            ) or "ninguno"
            filas.append({"Empleado": p["nombre"], "Día": v["dia"].isoformat(), "Horas": v["horas"],
                          "Meals": meals, "Violación": ", ".join(v["codigos"]), "Prima (h)": v["prima"]})
    return pd.DataFrame(filas, columns=["Empleado", "Día", "Horas", "Meals", "Violación", "Prima (h)"])


HORAS = {"Regular", "Overtime", "Doble", "Prima meal (h)", "OT de Oracle", "Horas", "Prima (h)"}
DINERO = {"Tips tarjeta", "Tips pagados"}


def formato(marco: pd.DataFrame):
    return marco.style.format(
        {**{c: "{:,.2f}" for c in marco.columns if c in HORAS},
         **{c: "${:,.2f}" for c in marco.columns if c in DINERO}})


# ---------------------------------------------------------------------------
# Barra lateral: periodo, tiendas y Generar.

with st.sidebar:
    st.header("Periodo")
    if VOLCADO:
        st.warning(f"Usando un volcado local, no Oracle: `{VOLCADO}`")
    try:
        tiendas = tiendas_activas(VOLCADO or "oracle")
    except (ValueError, OracleBIError) as exc:
        st.error(f"No se pudo conectar con Oracle: {exc}")
        st.stop()

    periodos = motor.periodos_recientes(8)
    inicio, fin = st.selectbox("Periodo de pago", periodos, format_func=lambda p: etiqueta_periodo(*p))
    nombres = dict(tiendas)
    elegidas = st.multiselect("Tiendas", list(nombres), default=list(nombres),
                              format_func=lambda ref: f"{ref} · {nombres[ref]}")
    generar = st.button("Generar", type="primary", disabled=not elegidas, width="stretch")
    st.caption("Periodo de sábado a viernes, cada dos semanas: es un supuesto hasta "
               "confirmarlo en Gusto.")

st.title("Nómina Broken Yolk: de Oracle a Gusto")
st.caption("Primera versión: horas, overtime y primas de meal por persona, con las reglas de "
           "California. Los tips se enseñan para revisar. El CSV de Gusto llega cuando estén "
           "los templates de cada empresa.")

if generar:
    # Se borra antes: si algo truena a media corrida, no se queda en pantalla el
    # resultado anterior como si fuera de esta.
    st.session_state.pop("resultado", None)
    st.session_state.pop("huella", None)
    desde, hasta = motor.dias_a_pedir(inicio, fin)
    barra = st.progress(0.0, text="Jalando de Oracle…")

    def avance(hechos: int, total: int) -> None:
        barra.progress(hechos / total, text=f"Jalando de Oracle: {hechos} de {total}")

    datos = simphony.jalar(fuente(), elegidas, desde, hasta, progreso=avance)
    barra.empty()
    st.session_state["resultado"] = motor.calcular(datos, inicio, fin)
    st.session_state["huella"] = huella_de_entradas(inicio, fin, elegidas)

resultado = st.session_state.get("resultado")
motivo = (motivo_para_volver_a_generar(resultado, st.session_state.get("huella"),
                                       huella_de_entradas(inicio, fin, elegidas))
          if resultado else "")

if not resultado:
    st.info("Elige el periodo y las tiendas, y dale **Generar**.")
    st.stop()
if motivo:
    st.info(motivo)
    st.stop()

# ---------------------------------------------------------------------------
# Resultado.

avisos = resultado["avisos"]
bloquean = [a for a in avisos if a["nivel"] == "bloquea"]
revisar = [a for a in avisos if a["nivel"] == "revisar"]
informativos = [a for a in avisos if a["nivel"] == "info"]
c = resultado["cuadre"]

st.subheader(etiqueta_periodo(resultado["inicio"], resultado["fin"]))
if bloquean:
    st.error("**No se puede concluir con estos datos.** Arregla esto y vuelve a generar:\n\n"
             + "\n".join(f"- {a['texto']}" for a in bloquean))
else:
    st.success(f"Las horas cuadran con Oracle: {c['horas_oracle']:,.2f} h. "
               f"Hay {len(revisar)} cosa(s) para revisar abajo.")

columnas = st.columns(6)
columnas[0].metric("Personas", len(resultado["personas"]))
columnas[1].metric("Horas", f"{c['horas_oracle']:,.2f}")
columnas[2].metric("Overtime", f"{c['overtime']:,.2f}")
columnas[3].metric("Doble", f"{c['double']:,.2f}")
columnas[4].metric("Horas de prima", f"{c['prima']:,.0f}",
                   help=f"Con la política {c['otra_politica']} serían {c['prima_otra_politica']:,.0f}.")
columnas[5].metric("Tips con tarjeta", f"${c['tips_oracle']:,.2f}")

for a in revisar:
    st.warning(a["texto"])

def pintar(marco: pd.DataFrame) -> None:
    """Con datos que no cuadran va en st.table, que no tiene descarga: la barra
    de st.dataframe trae su propio "Download as CSV"."""
    (st.table if bloquean else st.dataframe)(formato(marco), hide_index=True, width="stretch")


st.markdown("#### Por persona")
personas = tabla_personas(resultado["personas"])
if bloquean:
    st.caption("⚠️ Con datos incompletos o que no cuadran: estas cifras son solo para revisar, "
               "no para pagar, y no se pueden descargar.")
pintar(personas)
if not bloquean:
    st.download_button(
        "⬇ Descargar la vista previa (CSV)",
        # Con BOM, para que Excel en Windows lea bien los acentos y las eñes.
        personas.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"vista_previa_broken_yolk_{resultado['inicio']}_{resultado['fin']}.csv",
        mime="text/csv",
        help="Es la tabla de arriba, para revisarla. No es el archivo para Gusto.",
    )

violaciones = tabla_violaciones(resultado["personas"])
with st.expander(f"Violaciones de meal: {len(violaciones)} día(s)"):
    st.caption("Un meal es la salida a break (código 66) y el regreso, o un hueco de 30 min o más. "
               "Las horas de cada meal son de trabajo antes de salir.")
    pintar(violaciones)

if resultado["retenidos"]:
    with st.expander("Tips de cuentas que no checan"):
        st.caption("No se le asignan a nadie hasta saber de quién son.")
        pintar(pd.DataFrame([
            {"Clase de cuenta": clase, "Cuentas": r["cuentas"], "Días con tips": r["dias"],
             "Tips tarjeta": r["tarjeta"], "Tips pagados": r["pagados"]}
            for clase, r in sorted(resultado["retenidos"].items())
        ]))

with st.expander("Cómo cuadran las cuentas"):
    st.markdown(
        f"- **Horas:** las tarjetas suman {c['horas_oracle']:,.2f} h; el total por puesto que "
        f"Oracle calcula aparte da {c['horas_oracle_puesto']:,.2f} h. Se repartieron "
        f"{c['horas_colocadas']:,.2f} h = {c['regular']:,.2f} regulares + {c['overtime']:,.2f} de "
        f"overtime + {c['double']:,.2f} dobles.\n"
        f"- **Overtime:** con las reglas de California, {c['overtime'] + c['double']:,.2f} h; "
        f"Oracle puso {c['ovt_oracle']:,.2f} h de overtime.\n"
        f"- **Tips con tarjeta:** Oracle mandó ${c['tips_oracle']:,.2f} = "
        f"${c['tips_personas']:,.2f} a personas + ${c['tips_retenidos']:,.2f} retenidos. "
        f"El tender Tips Paid registra ${c['tips_pagados']:,.2f}.\n"
        f"- **Primas de meal:** {c['prima']:,.0f} h; con la política {c['otra_politica']} serían "
        f"{c['prima_otra_politica']:,.0f} h."
    )

if informativos:
    with st.expander(f"Avisos informativos ({len(informativos)})"):
        for a in informativos:
            st.markdown(f"- {a['texto']}")
