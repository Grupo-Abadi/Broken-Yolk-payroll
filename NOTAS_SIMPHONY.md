# Notas de Simphony para la nómina

Lo que se sabe de Oracle MICROS Simphony y de su Business Intelligence API
para armar la nómina de Broken Yolk en Gusto. Cada dato trae su fuente. Hay
tres tipos de fuente:

- **Datos reales:** el volcado de `volcado_simphony.py`, que se guarda en
  `reference/` y no va al repo. Aquí solo van conteos y conclusiones, nunca
  datos de una persona.
- **Documentación de Oracle:** docs.oracle.com, revisada el 18 nov 2026 por un
  agente que buscaba y otro que trataba de refutarlo.
- **mealviolation:** el repo `Jmemijeabadi/mealviolation` (commit 575b084),
  que ya usa esta API con las 8 tiendas.

Cuando los datos reales y la documentación no coinciden, mandan los datos
reales.

## Lo que dicen los datos reales

Volcado de prueba: BYC301, día de negocio 12 nov 2026. El volcado completo
(31 oct–13 nov, 8 tiendas) confirmó todo esto y se resume en el README. Aquí
van solo conteos y porcentajes, nunca montos de una persona.

- **Cada entrada y salida es una tarjeta.** Un turno con meal llega como dos
  tarjetas.
- **`shiftType` va en el tramo de trabajo que termina en break**, no en el
  break.
  - En el ejemplo, el primer tramo tiene `shiftType = 2` y `clkOutStatus = 66`
    (On Break). Sus horas cuentan como trabajo: `regHrs` es igual a lo que
    duró.
  - El meal es el hueco hasta la siguiente entrada (31 a 33 min en los 4 casos
    de ese día). El siguiente tramo trae `shiftType` vacío y salida normal (84).
  - La documentación de Oracle se lee como "1 = break pagado, 2 = break no
    pagado", como si ese renglón fuera el break.
  - mealviolation lee `shftType`, un campo que Oracle no manda, y por eso
    nunca usa ese valor. Por suerte: detecta el meal por el hueco después del
    66, que es lo correcto.
- **Los tips no vienen en las tarjetas.** Ninguna de las 17 tarjetas trae
  `chrgTips`, `drctTips`, `indirTips`, `svcTips` ni `tipsPd`. Oracle omite los
  campos que valen 0 o null.
  - Los tips vienen en `getEmployeeDailyTotals`: por persona, por revenue
    center y por día, sin puesto.
- **Los tips con tarjeta se pagan en efectivo.** En los totales del día,
  tips con tarjeta (`chrgTipsTtl`) = tips pagados (`tipsPdTtl`) = service
  charges (`svcTtl`), el mismo monto los tres, y es el 18.2% de las ventas con
  tarjeta.
  - Por persona no siempre coinciden: en 2 de las 5 personas con tips, no.
  - 2 de esas 5 personas no tienen tarjeta de tiempo ese día en esa tienda.
- **No hay efectivo declarado** (`tipsDclrdTtl`), ni tips indirectos, ni Tip
  Track (`tipsOutTtl`).
- **Empleados de BYC301:**
  - `payrollId` viene lleno en 53 de 55;
  - `externalPayrollID` en 45 de 55;
  - `className` en 54 de 55.

## La BI API

- **Un solo endpoint de horas:** `POST /bi/v1/{org}/getTimeCardDetails`.
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
    días de 8 tiendas son 112 llamadas.
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
  - También `svcTtl`, ventas y conteos de cheques.
  - Fuente: [reconciliación de la BI API](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/biapi/reconcile_guest_check.html)
    y la página del endpoint.
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
  - El permiso de datos que importa es "Employee Time Card Details and Pay
    Rates".
  - Fuente: [permisos de la BI API](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/rause/c_BI_API_permissions.htm).
- **Sin documentar:** límites de llamadas, paginación y respuesta 429. El
  cliente reintenta 429 y 5xx por si acaso.
- **Retención:** las tarjetas se guardan 13 meses de forma predeterminada.

## Labor Management (el módulo de horas de Oracle)

- **Ahí se corrigen las checadas:** Reporting and Analytics > Labor Management
  > Payroll Preprocessing > Time Cards. El gerente pone la hora, el motivo y
  los tips.
  - Un periodo cerrado se puede volver a abrir.
  - Fuente: [editar tarjetas](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/lmusg/t_using_payroll_timecard_adjust.htm).
- **Su overtime no es el de California.** Hay umbrales diarios y semanales,
  días consecutivos, cuatro niveles y promedio ponderado. Nada documenta doble
  después de 12 h ni el séptimo día.
  - Por eso las horas se recalculan de las checadas, y `ovt1`–`ovt4` solo
    sirven para comparar.
  - Fuente: [reglas de overtime](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/lmusg/t_config_pay_laborovertimerule.htm).
- **`premHrs`/`premPay` son el "premium pay" de Labor Management** (monto,
  horas o split shift), no la prima de meal o rest de California.
  - Antes de mandar primas a Gusto hay que confirmar que Oracle no las esté
    pagando ya.
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
  - En Broken Yolk no vino ningún tip indirecto el 12 nov en BYC301.
  - Fuente: [tip pools](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/lmusg/c_config_pay_tippool.htm).

## Tips y service charges (definiciones de Oracle)

- **`drctTips`:** efectivo que el cliente le da directo al empleado; lo
  declara él.
- **`indirTips`:** efectivo que recibe de compañeros (por ejemplo, el busser
  del mesero), o lo que reparte un tip pool.
- **`chrgTips`:** tips con tarjeta, acreditados al dueño del cheque.
- **`tipsPd`:** tips con tarjeta que se le pagan en efectivo de la caja.
  - Ojo: un service charge ligado al tender de Tips Paid también cae ahí.
- **`svcTips` y los service charges:** según cómo esté configurado cada
  service charge, cuenta como tip con tarjeta (opción 3) o va al total de
  service charges del reporte de tips (opción 4).
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
- **La subject area "Time Cards"** (Report Builder, iQuery o exports) sí
  trae checadas, puesto, tarifa, overtime y tips por tipo.
  - Fuente: [subject areas de labor](https://docs.oracle.com/en/industries/food-beverage/back-office/20.1/rause/c_subj_area_labor_emp.htm).

## Gusto

Esto sale de fragmentos de búsqueda, porque support.gusto.com bloquea la
lectura, y de lo que ya se aprendió en tacos:

- **No hay integración directa de Gusto con Simphony.**
- **Smart Import:**
  - reemplaza lo que ya había en esa nómina;
  - un 0 sobrescribe y una celda vacía deja lo que había;
  - reconoce el código de empleado de Gusto si viene en el archivo.
- **7shifts** es la única herramienta que dice conectar Simphony y Gusto. Lo
  confirma su blog; su centro de ayuda bloquea la lectura.
  - Jala checadas y tips de Simphony.
  - A Gusto manda horas regulares, de overtime y de doble overtime.
  - No manda tarifas, y nada público dice que mande primas.
