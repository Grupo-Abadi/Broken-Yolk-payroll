# Broken Yolk payroll

Herramienta para pasar las horas y los tips de Oracle MICROS Simphony a un CSV
listo para importar en Gusto (Smart Import), para los empleados de Broken Yolk
en California (por confirmar para las 8 tiendas: pregunta 10 del README).

Es la hermana de `payroll tacos` (Toast → Gusto, Tacos El Franc), que ya está
estable y en producción en `C:\Users\Jordan Memije\Downloads\payroll tacos`.
De ahí se reusa todo lo que no dependa del POS: el cálculo de California, el
armado del CSV de Gusto, los cuadres y las pruebas.

## Cómo trabajar aquí

- Todo lo que Jordan ve va en español de México, con tono natural: las
  respuestas, las descripciones de los comandos y los workflows. Los nombres
  técnicos (endpoints, campos, columnas) se quedan como son.
- No adivinar nombres de reportes, columnas ni campos de Simphony: antes de
  escribir el lector, conseguir un export real o la documentación exacta.
  Lo que ya se sabe, con fuentes, está en `NOTAS_SIMPHONY.md`. Cuando los
  datos reales y la documentación no coinciden, mandan los datos.
  - Ejemplo: `shiftType = 2` va en el tramo de trabajo que termina en break
    (salida 66), no en el break.
  - Ejemplo: los tips no vienen en las tarjetas, sino en los totales diarios.
  - Ejemplo: en `getJobCodeDailyTotals`, `ovtHrs` es el overtime total y
    `ovt2Hrs` es parte de él; en las tarjetas, `ovt1Hrs` y `ovt2Hrs` se suman.
- Para mirar datos reales: `python -I volcado_simphony.py` los baja a
  `reference/`. Los scripts que los lean van en otra carpeta, corren con
  `python -I` y solo imprimen conteos y totales (una suma de uno o dos
  registros es el monto de una persona).
- Con datos reales, imprimir solo encabezados y totales, nunca renglones con
  nombres.

## Reglas que ya se aprendieron en tacos

**Gusto**
- No hay API de Gusto para la propia empresa: la vía es Smart Import con CSV.
- En Smart Import un 0 sobrescribe lo que ya hay y una celda vacía lo deja
  como está. Nunca escribir "0.0": si no hay nada, la celda va vacía.
- El export de empleados de Gusto trae un renglón por persona, con solo el
  puesto principal y la tarifa de hoy. También trae SSN, fecha de nacimiento y
  domicilio: nunca copiarlo; sacar una versión limpia con solo lo necesario.
- Cuando alguien tiene dos puestos, Gusto paga las horas de prima (meal/rest)
  al promedio semanal de sus tarifas, no a la del puesto principal. Si hace
  falta otra tarifa, la prima se paga en dólares.
- La conexión de Gusto por MCP es solo para leer: nunca `run_payroll` ni
  `update_payroll`. Hoy esa conexión solo ve Tacos Franc Bonita LLC.

**California**
- Overtime diario (más de 8 h), doble (más de 12 h), semanal (más de 40 h) y
  séptimo día. La semana laboral y el periodo salen de la configuración de
  Gusto de cada empresa; no suponer los de tacos.
- Las violaciones de meal y rest se revisan por persona y día, no por turno:
  un cambio de puesto o volver a checar parte el día y esconde violaciones.

**Tips**
- Lo que reporta el POS puede ser antes del pool. Confirmar qué es lo que de
  verdad se le paga a cada persona antes de mandarlo a Gusto.

**Cuadres**
- Un cuadre que compara el resultado contra sí mismo no prueba nada: medir lo
  que de verdad se escribe en el archivo contra la fuente.
- Identidad de persona no es identidad de pago: una persona puede tener varios
  puestos, varias sucursales o varias LLC.

## Seguridad

- Las credenciales (Simphony, contraseña de la app) viven solo en
  `.streamlit/secrets.toml` (está en .gitignore) o en los Secrets de Streamlit
  Cloud. Nunca en el repo, en logs ni en CSV, y nunca se le pasan a un subagente.
- Los datos reales (reportes de Simphony, exports de Gusto, archivos
  generados) van en `reference/` o `salida/`, que no van al repo. Nunca
  nombres, emails, teléfonos ni ids reales en el código o en las pruebas: las
  pruebas usan datos inventados. Revisarlo antes de cada commit.
- El Python que lea archivos descargados o de `reference/` corre con
  `python -I`.
- No saltarse bloqueos de sitios web (nada de cambiar user-agent, proxies ni
  mirrors).
- Nada publicado (artifacts, páginas) con sueldos o datos de empleados.
- El repo tiene que ser privado (Jmemijeabadi/Broken-Yolk-payroll; antes
  estuvo en la organización Grupo-Abadi y se movió el 19 nov 2026). No hagas
  `git push`: Jordan lo hace en GitHub Desktop.
- Los cuadres comparan contra una fuente aparte. El de horas usa el total por
  puesto de Oracle (`getJobCodeDailyTotals`), no las mismas tarjetas.
