# Broken Yolk payroll

Pasa las horas y los tips de Oracle MICROS Simphony a la nómina de Gusto. Es
para las 8 tiendas de Broken Yolk: BYC301 y de BYC303 a BYC309.

Es la hermana de `payroll tacos` (Toast → Gusto, Tacos El Franc), que ya está
estable. De ahí viene lo que no depende del POS.

## Estado al 18 nov 2026

- ✅ **Primera versión de la app** (`app.py`). Jala de Oracle un periodo y
  enseña, por persona:
  - horas regulares, de overtime y dobles, con las reglas de California;
  - primas de meal;
  - tips, para revisar.

  Trae cuadres y avisos.
- ✅ **Probada con el periodo del 31 oct al 13 nov:** 8 tiendas, 3,309
  tarjetas, 241 personas.
- ✅ **122 pruebas sin conexión:**
  `python -m unittest test_payroll test_simphony test_app`.
- ⏳ **Falta el CSV de Gusto:** necesito el template de Smart Import de cada
  empresa.
- ⏳ **Falta decidir los tips:** cómo se pagan y a quién le tocan (preguntas 1
  a 5, abajo).

## Lo que dicen los datos (31 oct–13 nov)

### Horas

- **Tarjetas:** ninguna abierta. 171 traen ajustes de gerente.
- **Personas:** 241 por payrollID. Ninguna trabajó en dos tiendas (también
  por payrollID). 17 tuvieron más de un puesto y 3 más de una tarifa.
- **Primas:** ninguna tarjeta trae `premHrs` ni `premPay`, así que Labor
  Management no calcula primas en las tarjetas.

### Overtime: Oracle pone menos del que pide California

- **Lo que puso Oracle:** su overtime viene de las reglas de Labor
  Management: `ovt1` en 57 tarjetas y `ovt2` en 4, en total 60.30 h.
- **Lo que da California** con las mismas horas: 81.77 h de overtime y 12.93
  h dobles.
- **La diferencia, neta por persona y semana:**
  - En 19 semanas de alguien, California da 36.06 h más de overtime o doble
    que Oracle.
  - En otras 3, Oracle puso 1.66 h de más.
  - De 68 jornadas de más de 8 horas, Oracle les puso overtime solo a 38.
- **Qué significa:** si hoy se paga lo que calcula Oracle, esas horas se
  estarían pagando como regulares. Puede ser que Labor Management corte los
  días distinto (su "Start Labor Hour"); hay que revisarlo.

### Meals

- **Cómo se ven:** salir a comer es checar salida con el código 66. Casi
  siempre (1,311 de 1,322 veces) ese tramo trae `shiftType = 2`. El meal es el
  hueco hasta la siguiente entrada.
- **Cuánto duraron las 1,275 salidas a break con regreso:**
  - 1,230 duraron de 30 a 44 min;
  - 15 duraron 45 min o más;
  - 25 duraron de 10 a 29 min;
  - 5 duraron menos de 10 min.

  Además, 47 veces salieron a break y no regresaron ese día.
- **Las salidas a break de menos de 30 minutos no cuentan como meal** (29 en
  el periodo).
  - No hay tolerancia: con el redondeo de Oracle, un meal de 30 min siempre
    mide 30:00 o más, y en California los meals no se redondean.
  - Si eran descansos de 10 minutos, en California se pagan, y Oracle no los
    pagó.
- **Los meals se miden con lo que duraron las checadas**, no con lo que paga
  Oracle. En 24 tarjetas no coinciden.
- **Los waivers cambian todo.**
  - Con la regla estricta (sin waivers) salen 476 horas de prima en las dos
    semanas, de 145 personas.
  - Con waivers firmados serían 105 horas, de 64 personas.
  - De las 430 jornadas de más de 5 horas sin meal completo, 369 duraron de 5
    a 6 horas.

  La app usa la estricta y enseña la otra al lado.
- **Turnos partidos:** 5 jornadas tienen un hueco de más de una hora. Cuenta
  como meal, pero en California un turno partido puede llevar una hora extra
  de pago.

### Rest breaks

No hay ningún break pagado en el periodo. Si los descansos de 10 minutos no se
checan, su prima no se puede calcular con los datos.

### Tips: vienen por otro lado y no cuadran solos

- **No vienen en las tarjetas.** Vienen en los totales diarios por persona
  (`getEmployeeDailyTotals`), sin puesto.
- **`chrgTipsTtl`** son los tips con tarjeta. Oracle se los acredita al dueño
  del cheque, antes de cualquier pool.
- **`tipsPdTtl`** es el registro del tender "Tips Paid".
  - Puede registrarse solo y traer también service charges.
  - En el periodo suma como 9% más que los de tarjeta.
  - Por persona coincide con los de tarjeta solo en 288 de 619 renglones.
  - A nombre de quién queda, está por confirmar.
- **`svcTtl` incluye los tips con tarjeta:** el 12 nov en BYC301 fue igual a
  `chrgTipsTtl`. No se debe sumar a los tips ni pagarse como service charge.
  Un cargo de verdad sería la diferencia, y hay que confirmarlo con la
  configuración de cada service charge.
- **Lo que no hay:** efectivo declarado ni Tip Track. Tips indirectos hay en
  solo 2 renglones.
- **Campos sin explicar:** `nonRevSvcTtl`, en 44 renglones, y `tipTtl`, en
  68, que Oracle lista entre los tips. No se suma ninguno hasta saber qué son.
- **Cuentas que nunca checan en el periodo y traen tips:**

  | Clase | Cuentas | Días con tips |
  | --- | --- | --- |
  | SuperUser | 8 | 81 |
  | Manager | 6 | 37 |
  | Server | 6 | 63 |

  Las SuperUser traen sobre todo tips con tarjeta; las Manager y Server,
  sobre todo tips pagados. Qué son esas cuentas está por confirmar. La app
  deja sus tips retenidos y enseña los montos.

Más detalle, con las fuentes de Oracle, en [NOTAS_SIMPHONY.md](NOTAS_SIMPHONY.md).

## Cómo funciona

1. **Simphony.** Pide a la BI API:
   - por tienda, `getLatestBusDt`: si el último día del periodo no ha cerrado,
     no deja concluir;
   - por tienda y día, `getTimeCardDetails`: los tramos;
   - por tienda y día, `getEmployeeDailyTotals`: los tips;
   - por tienda y día, `getJobCodeDailyTotals`: el total por puesto, que
     sirve para cuadrar.

   Si Oracle rechaza la contraseña, deja de pedir al primer intento, para no
   bloquear la cuenta. Si falla 3 veces seguidas, también se detiene.
2. **Jornadas.** Los tramos se juntan por persona y día de negocio de Oracle.
   Los meals son los huecos después de un 66, o huecos de 30 min o más.
3. **California.** Con las checadas, no con lo que calcula Oracle:
   - overtime diario (más de 8 h), doble (más de 12 h), semanal (más de 40 h)
     y séptimo día;
   - la prima de meal por persona y día.
4. **Cuadres.**
   - Las horas repartidas son las mismas que paga Oracle.
   - Las tarjetas cuadran, tienda por tienda y día por día, con el total por
     puesto que Oracle calcula aparte.
   - Los tips leídos (personas más retenidos) cuadran con lo que Oracle mandó
     crudo.

   Si algo no cuadra o falta un dato, la app no deja concluir ni descargar.
5. **Gusto** (falta). Un CSV de Smart Import por empresa, con las reglas de
   tacos: celda vacía, nunca 0.

## De dónde sale cada pieza

| Pieza | Viene de | Qué cambió |
| --- | --- | --- |
| Cliente de la BI API (`oracle_bi/`) | mealviolation, commit 575b084 | Reintenta ante errores de Oracle, se rinde tras 3 fallas seguidas, tacha las credenciales de los errores y trae los totales diarios. |
| Lector de Simphony (`simphony.py`) | nuevo | Lee los campos como vienen de verdad. |
| Overtime de California (`ca_overtime.py`) | tacos, commit 6fab0a7 | El séptimo día respeta el inicio de semana que se le pasa. |
| Motor y pantalla (`motor.py`, `app.py`) | nuevos, al estilo de tacos | Contraseña, cuadres, avisos y la versión del resultado, como en tacos. |

**Ojo con el código de mealviolation (`compliance/`):** le da a `shiftType` el
significado de la documentación de Oracle (2 = break sin pago). En los datos,
2 es un tramo de trabajo que se paga. Si algún día se reusa algo de ahí, no
basta con renombrar el campo.

## Tips en Gusto: lo que hay que decidir

En Gusto, **Paycheck tips** son tips que se pagan en el cheque. **Cash tips**
son tips que la persona ya se llevó: se reportan para impuestos, pero no se
pagan otra vez. Si los tips con tarjeta se pagan en efectivo cada noche y
además se mandan como Paycheck tips, se pagan dos veces.

| En Oracle | Qué es | A Gusto (propuesta, por confirmar) |
| --- | --- | --- |
| `chrgTipsTtl` | Tips con tarjeta, acreditados al dueño del cheque | Cash tips si se pagan en efectivo esa noche; Paycheck tips si van en el cheque. |
| `tipsPdTtl` | Registro del tender Tips Paid | Sirve para cuadrar, no para repartir. |
| `svcTtl` | Todos los service charges, incluidos los tips con tarjeta | Nada directo. Solo la diferencia contra los tips sería un cargo, y un cargo obligatorio es sueldo, como la gratuity de tacos. |
| `nonRevSvcTtl`, `tipTtl` | Sin explicar | Nada hasta saber qué son. |
| Tips de cuentas que nunca checan | Por confirmar | Retenidos. |

## Pasos

1. **Tú: la seguridad de mealviolation.**
   - Poner privado su repo y quitarle el acceso público a la app.
   - Cambiar la contraseña de la cuenta API. Vence cada 60 días: pon un
     recordatorio.
   - Mientras las dos herramientas compartan la cuenta, hay que cambiarla en
     las dos (aquí en `.streamlit/secrets.toml` y en los Secrets de
     mealviolation). Si una se queda con la vieja, sus intentos bloquean la
     cuenta 30 minutos para las dos.
   - Lo mejor es pedir una cuenta API solo para la nómina, limitada a las 8
     tiendas.
2. **Tú: confirmar que este repo es privado** (GitHub → Settings → Danger Zone).
3. **Tú: publicar la app en Streamlit Cloud.**
   1. Que Streamlit pueda leer repos privados de tu cuenta: si al crear la app
      no aparece el repo, en share.streamlit.io entra a tu perfil y da el
      permiso de repos privados.
   2. En share.streamlit.io: **Create app**. Repo
      `Jmemijeabadi/Broken-Yolk-payroll`, rama `main`, archivo `app.py`.
   3. En **Settings → Secrets**, pega lo de `.streamlit/secrets.toml.example`
      con los valores reales y una `APP_PASSWORD` larga.
   4. En **Settings → Sharing**, deja que solo la vean las personas que operan
      la nómina.
4. **Tú: lo de Gusto y las respuestas de abajo.**
5. **Yo: el CSV de Gusto** y lo que salga de tus respuestas.
6. **Juntos:** de 2 a 4 periodos ya pagados en sombra, comparados contra lo
   que se pagó en Gusto, como la prueba de Bonita en tacos.
7. **Primera corrida real.**

## Lo que necesito de ti

1. **Tips con tarjeta:** ¿se los dan en efectivo esa misma noche o van en el
   cheque de nómina? En el total del periodo casi cuadran con los tips pagados
   (+9%), pero por persona solo en 288 de 619 renglones.
2. **Efectivo:** ¿los clientes dejan propinas en efectivo? ¿Las declaran en la
   terminal al checar salida, en otro lado, o no se declaran? En Oracle no hay
   ninguna declarada.
3. **Pool o tip share:** ¿reparten con cocina, bussers o hosts? ¿Cómo y dónde
   lo anotan? Oracle no trae nada de eso.
4. **Service charges:** ¿hay cargo automático para grupos grandes, catering o
   pedidos en línea? ¿A quién le toca ese dinero?
5. **Cuentas SuperUser, Manager y Server que nunca checan:** ¿qué son, y de
   quién son sus tips con tarjeta y sus tips pagados?
6. **Gusto:**
   - ¿cuántas empresas (LLC) hay y qué tiendas van en cada una?
   - el calendario de pago y el día en que empieza la semana laboral;
   - el template de Smart Import de cada empresa;
   - el export de empleados.

   **Ojo:** el export de empleados de Gusto trae SSN, fecha de nacimiento y
   domicilio. Ponlo en `reference/` de esta carpeta, nunca lo pegues en el
   chat. Si puedes, saca uno con solo nombre, código, puesto y tarifa.

   El periodo que usó mealviolation fue de sábado a viernes, cada dos semanas,
   igual que tacos.
7. **Meals:**
   - ¿hay waivers firmados?
   - ¿hoy pagan la prima de meal, y en horas o en dólares?
   - ¿una hora de prima por día, que es lo que pide la ley y lo que hace hoy
     la app, o una por cada meal perdido, como se decidió en tacos?
   - ¿alguien captura primas en Oracle (premium pay u "Other Pay")?
8. **Descansos de 10 minutos:** ¿los checan como break? ¿Qué política tienen?
9. **Hoy:** ¿quién hace la nómina de Broken Yolk, de qué reporte saca las horas
   y los tips, y qué captura a mano en Gusto?
10. ¿Las 8 tiendas están en California?
11. ¿Para cuándo quieres la primera corrida?

## Archivos

| Archivo | Para qué |
| --- | --- |
| `app.py` | La pantalla: `streamlit run app.py`. |
| `motor.py` | Horas, primas, tips, cuadres y avisos por persona; el calendario. |
| `simphony.py` | Lector de Oracle: tramos, jornadas, meals y tips. |
| `ca_overtime.py` | Overtime de California. |
| `oracle_bi/` | Cliente de la BI API. |
| `volcado_simphony.py` | Baja los datos crudos de un rango a `reference/`. En pantalla solo enseña qué campos vienen y sumas de 3 personas o más. |
| `simphony_falso.py`, `fixtures/` | Datos inventados con la forma real, para las pruebas. |
| `test_payroll.py`, `test_simphony.py`, `test_app.py` | Pruebas sin conexión. |
| `NOTAS_SIMPHONY.md` | Lo que se sabe de Simphony, con fuentes. |
| `CLAUDE.md` | Reglas de trabajo y de seguridad que vienen de tacos. |
| `.streamlit/secrets.toml.example` | Plantilla de las credenciales y de la contraseña de la app. El archivo real no va al repo. |
| `reference/`, `salida/` | Datos reales. No van al repo. |

Para bajar un rango crudo (cada corrida va a su propia carpeta):

```
python -I volcado_simphony.py --desde 2026-10-31 --hasta 2026-11-13
```
