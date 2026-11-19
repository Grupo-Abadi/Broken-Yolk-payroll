# Notas de Simphony para la nómina

Lo que se sabe de Oracle MICROS Simphony y de su Business Intelligence API
para armar la nómina de Broken Yolk en Gusto. Cada dato trae su fuente. Hay
tres tipos de fuente:

- **Datos reales:** los volcados de `volcado_simphony.py`, que se guardan en
  `reference/` y no van al repo. Aquí solo van conteos y porcentajes, nunca
  datos de una persona.
- **Documentación de Oracle:** docs.oracle.com, revisada el 18 nov 2026 por un
  agente que buscaba y otro que trataba de refutarlo.
- **mealviolation:** el repo `Jmemijeabadi/mealviolation` (commit 575b084),
  que ya usa esta API con las 8 tiendas.

Cuando los datos reales y la documentación no coinciden, mandan los datos
reales.

## Lo que dicen los datos reales

Hubo dos volcados:

- uno de prueba: BYC301, día de negocio 12 nov 2026;
- uno completo: del 31 oct al 13 nov, las 8 tiendas, 3,309 tarjetas.

El completo confirmó la forma de las tarjetas y que los tips vienen en los
totales diarios. Los números del periodo están en el README.

- **Cada entrada y salida es una tarjeta.** Un turno con meal llega como dos
  tarjetas.
- **`shiftType` va en el tramo de trabajo que termina en break**, no en el
  break.
  - En el periodo, 1,311 de las 1,322 salidas con código 66 (On Break) traen
    `shiftType = 2`. Todas las demás tarjetas traen `shiftType = 0`. No hubo
    ni `shiftType = 1` ni salida 80 (break pagado).
  - Las horas de ese tramo cuentan como trabajo: `regHrs` es igual a lo que
    duró.
  - El meal es el hueco hasta la siguiente entrada. En la prueba fueron de 31
    a 33 minutos.
  - La documentación de Oracle se lee como "1 = break pagado, 2 = break no
    pagado", como si ese renglón fuera el break.
  - mealviolation lee `shftType`, un campo que Oracle no manda, y por eso
    nunca usa ese valor. Por suerte: detecta el meal por el hueco después del
    66. Pero su tabla SHIFT_TYPE y sus filtros (`compliance/normalize.py:40`,
    `engine.py`) suponen el significado de la documentación. Si se reusan, no
    basta con renombrar el campo.
- **Los tips no vienen en las tarjetas.** Ninguna de las 3,309 trae `chrgTips`,
  `drctTips`, `indirTips`, `svcTips` ni `tipsPd`. Oracle omite los campos que
  valen 0 o null.
  - Los tips vienen en `getEmployeeDailyTotals`: por persona, por revenue
    center y por día, sin puesto.
  - Según Oracle, en el reporte Time Card Detail los tips por tarjeta solo
    salen si Simphony tiene el Employee Shift Tracking configurado (opciones
    #49 y #57). Puede ser la razón de que no vengan.
- **Los tips con tarjeta, los pagados y `svcTtl`:**
  - **Ese día (12 nov, BYC301)**, en el total de la tienda, `chrgTipsTtl` =
    `tipsPdTtl` = `svcTtl`, el mismo monto los tres. Fue el 18.2% de las
    ventas con tarjeta, y parece que los tips con tarjeta se pagan en efectivo.
  - **En el periodo**, `tipsPdTtl` suma como 9% más que `chrgTipsTtl`, y por
    persona coinciden solo en 288 de 619 renglones.
  - `svcTtl` incluye los tips con tarjeta: no se suma a ellos.
- **Lo que no hay:** efectivo declarado (`tipsDclrdTtl`) ni Tip Track
  (`tipsOutTtl`). Tips indirectos (`indirTipsPdTtl`) hay en solo 2 renglones
  del periodo.
- **Primas:** en el periodo, ninguna tarjeta trae `premHrs` ni `premPay`.
- **Empleados de BYC301:**
  - `payrollId` viene lleno en 53 de 55;
  - `externalPayrollID` en 45 de 55;
  - `className` en 54 de 55.
- **Cuadre independiente.** En el periodo, las horas de las tarjetas cuadraron
  exacto con el total por puesto (`getJobCodeDailyTotals`) en los 112
  tienda-días.
  - Al principio 2 no cuadraban porque se leía mal ese total: ahí `ovtHrs` es
    el overtime total y `ovt2Hrs` es parte de él (ver abajo).

## La BI API

- **Un solo endpoint de horas por persona:** `POST /bi/v1/{org}/getTimeCardDetails`.
  - Existe desde Reporting and Analytics 20.2 y pide que se use el *payroll
    preprocessing* de Labor Management. Como en Broken Yolk sí regresa datos,
    ese módulo está en uso.
  - Fuente: [getTimeCardDetails](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/biapi/op-bi-v1-orgidentifier-gettimecarddetails-post.html).
- **Lo que se le pide:**
  - `locRef` (obligatorio);
  - `busDt` o `postedDt`;
  - opcionales: `empNum` o `extPayrollID`, `includeAdjustments`,
    `changedSinceUTC`, `applicationName`.
  - No hay rango de fechas: una tienda y un día por llamada. Un periodo de 14
    días de 8 tiendas son 112 llamadas por cada endpoint.
- **Lo que trae cada tarjeta:**
  - identidad: `tcId`, `empNum`, `payrollID`, `extPayrollID`;
  - puesto y tienda: `jcNum`, `rvcNum`;
  - checadas en hora local y UTC, y los códigos de entrada y salida;
  - pago: `payRt`, `regHrs`/`regPay`, `ovt1Hrs`–`ovt4Hrs` con su pago y
    `premHrs`/`premPay`;
  - tips por tipo (que en Broken Yolk vienen vacíos);
  - los ajustes: valores anteriores, gerente y motivo.
  - Los campos que valen 0 o null no vienen.
- **`getEmployeeDailyTotals`** (`locRef` + `busDt`): por revenue center y
  empleado.
  - Tips: `chrgTipsTtl`, `tipsPdTtl`, `tipsDclrdTtl`, `indirTipsPdTtl`,
    `tipTtl`, `tipsOutTtl`.
  - También `svcTtl` (todos los service charges de la persona),
    `nonRevSvcTtl`, ventas y conteos de cheques.
  - Fuente: [getEmployeeDailyTotals](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/biapi/op-bi-v1-orgidentifier-getemployeedailytotals-post.html).
- **`getJobCodeDailyTotals`** (`locRef` + `busDt`): horas y pago por revenue
  center y puesto, sin persona.
  - **`ovtHrs` es el overtime total**, de todos los niveles. `ovt2Hrs` a
    `ovt4Hrs` son el desglose de los niveles altos y ya van dentro de
    `ovtHrs`.
  - Medido el 13 nov en BYC303: las tarjetas del puesto 12 traían 4.06 h de
    `ovt1Hrs` y 7.50 de `ovt2Hrs`, y el total por puesto `ovtHrs` = 11.56 y
    `ovt2Hrs` = 7.50.
  - Es el único total de horas que Oracle calcula aparte de las tarjetas, así
    que la app lo usa para cuadrarlas.
- **Otros endpoints:**
  - `getEmployeeDimensions`, `getJobCodeDimensions` y
    `getLocationDimensions`;
  - `getLatestBusDt`, que da la última fecha de negocio y la versión de
    Reporting and Analytics.
- **Inicio de sesión:** OIDC Authorization Code con PKCE, en tres pasos
  (authorize → signin → token).
  - El `id_token` dura 14 días y el `refresh_token` 28.
  - **La contraseña de la cuenta API vence cada 60 días.** Se cambia con
    "Can't sign in" en la página de entrada de Reporting and Analytics, y
    varios intentos fallidos la bloquean 30 minutos.
  - Fuente: [Authenticate](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/biapi/authenticate.html).
- **La cuenta API** se crea en Reporting and Analytics: Administration >
  System > API Accounts.
  - Hace falta el privilegio Manage API Accounts en un rol de enterprise.
  - Se puede limitar a ciertas tiendas.
  - **Permisos que usa esta herramienta:**
    - para las tarjetas, "Employee Time Card Details and Pay Rates" o "Labor
      General" (Oracle no dice cuál abre las tarifas);
    - "Employee Performance", para los totales diarios con los tips;
    - probablemente "Personal Identifiable Information", para los nombres.
  - Fuente: [permisos de la BI API](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/rause/c_BI_API_permissions.htm).
- **Sin documentar:** límites de llamadas, paginación y respuesta 429. El
  cliente reintenta 429 y 5xx por si acaso, y se rinde tras 3 fallas
  seguidas.
- **Retención:**
  - Oracle guarda las tarjetas 13 meses de forma predeterminada; se puede
    subir hasta 120.
  - Los totales diarios, 13 meses (hasta 48); el detalle de cheques, 3.
  - En California las checadas se guardan al menos 3 años. Hay que pedir que
    suban la retención de las tarjetas o archivar el volcado de cada periodo
    pagado, cifrado y fuera de la laptop.

## Labor Management (el módulo de horas de Oracle)

- **Ahí se corrigen las checadas:** Reporting and Analytics > Labor Management
  > Payroll Preprocessing > Time Cards. El gerente pone la hora, el motivo y
  los tips.
  - Con el periodo cerrado no se ajusta nada, salvo que alguien con permiso
    para reabrirlo lo reabra.
  - Fuentes: [editar tarjetas](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/lmusg/t_using_payroll_timecard_adjust.htm)
    y [cerrar y reabrir periodos](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/lmusg/t_gettingstarted_roadmap_payroll.htm).
- **Su overtime no es el de California.**
  - Tiene umbrales diarios y semanales, días consecutivos, cuatro niveles y
    promedio ponderado.
  - Nada documenta que siga las reglas de California: ni doble después de 12 h
    ni séptimo día.
  - Por eso las horas se recalculan de las checadas, y `ovt1`–`ovt4` solo
    sirven para comparar.
  - Fuentes: [reglas de overtime](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/lmusg/t_config_pay_laborovertimerule.htm)
    y [cálculo del overtime](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/lmusg/t_config_pay_overtimecalc.htm).
- **`premHrs`/`premPay` son el "premium pay" de Labor Management:** por monto,
  por horas o por split shift.
  - Nada documenta que sea la prima de meal o de rest de California.
  - Un gerente también puede capturarla a mano.
  - Labor Management tiene además "Other Pay", que no viene en las tarjetas.
  - Antes de mandar primas a Gusto hay que confirmar que nadie las esté
    pagando ya por ahí.
- **Redondeo:** Labor Management sube cada checada al centésimo de hora (36
  segundos).
  - Fuente: [tarjetas](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/lmusg/c_using_timecards.htm).
- **El día laboral se define por tienda:** Start Labor Hour y Labor Week
  Start Day.
  - Fuente: [reglas de la tienda](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/lmusg/t_config_locations_laborrules.htm).
- **Tip pools:**
  - pueden ser manuales o automáticos;
  - se reparten en efectivo o en el cheque;
  - con "Post Indirect Tip To Timecard" llegan a la tarjeta como `indirTips`.
  - En el periodo casi no hubo tips indirectos (2 renglones).
  - Fuente: [tip pools](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/lmusg/c_config_pay_tippool.htm).

## Tips y service charges (definiciones de Oracle)

- **`drctTips`:** efectivo que el cliente le da directo al empleado; lo
  declara él.
- **`indirTips`:** efectivo que recibe de compañeros (por ejemplo, el busser
  del mesero), o lo que reparte un tip pool.
- **`chrgTips`:** tips con tarjeta, acreditados al dueño del cheque.
- **`tipsPd`:** el tender Tips Paid, que registra el pago de tips con tarjeta
  y saca el dinero de la caja. Puede registrarse solo.
  - Ojo: un service charge ligado al tender de Tips Paid también cae ahí.
- **Service charges:** según cómo esté configurado cada uno,
  - cuenta como tip con tarjeta (opción 3);
  - va al total de service charges del reporte de tips (opción 4);
  - es un cargo que no cuenta como venta (opción 7, probablemente
    `nonRevSvcTtl`);
  - va a los tips en efectivo declarados (opción 13);
  - o no va a nadie, y se lo queda el negocio.
  - En California un cargo obligatorio no es tip: es sueldo.
  - Fuente: [service charges](https://docs.oracle.com/en/industries/food-beverage/simphony/simcg/t_service_charges_config.htm).

## Reportes que se pueden bajar a mano

Solo sirven para cuadrar, no como fuente:

- **Time Card Detail** es el más cercano.
  - Trae un renglón por tarjeta con entrada, salida, código de salida, horas
    y pago regular y overtime, ventas y una sola columna de Tips.
  - No trae tarifa, ni columna de break, ni tips por tipo.
  - Se baja en .xlsx o CSV.
  - Fuente: [Time Card Detail](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/rarrg/c_time_card_detail_add.htm).
- **La subject area "Time Cards"** (Report Builder, iQuery o exports) sí trae
  checadas, puesto, tarifa, overtime y tips por tipo.
  - **Ojo:** con Labor Management, Oracle recomienda "Enterprise Time Cards",
    que trae el SSN (`empSsn`) y no trae número de puesto. Desde la versión
    20.2 las subject areas de tarjetas traen SSN.
  - No seleccionar esa columna. Si viene, el archivo va en `reference/` y
    nunca al chat.
  - Fuente: [subject areas de labor](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/rause/c_subj_area_labor_emp.htm).

## Gusto

Esto sale de fragmentos de búsqueda, porque support.gusto.com bloquea la
lectura, y de lo que ya se aprendió en tacos:

- **No encontramos integración directa de Gusto con Simphony.** Gusto no deja
  leer su directorio, así que es ausencia de evidencia.
- **Smart Import:**
  - reemplaza lo que ya había en esa nómina;
  - cada celda con valor, aunque sea 0, reemplaza lo capturado, y una celda
    vacía lo deja como está;
  - reconoce el código de empleado de Gusto si viene en el archivo.
- **7shifts** es la única herramienta que dice conectar Simphony y Gusto. Lo
  confirma su blog; su centro de ayuda bloquea la lectura.
  - Jala checadas y tips de Simphony.
  - A Gusto manda horas trabajadas y de overtime.
  - Sin confirmar: que mande doble overtime y que no mande tarifas.
