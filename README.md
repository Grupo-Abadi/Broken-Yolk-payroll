# Broken Yolk payroll

Pasa las horas y los tips de Oracle MICROS Simphony a un CSV listo para
importar en Gusto (Smart Import). Es para las 8 tiendas de Broken Yolk:
BYC301 y de BYC303 a BYC309.

Es la hermana de `payroll tacos` (Toast → Gusto, Tacos El Franc), que ya está
estable. De ahí se reusa todo lo que no depende del POS.

## Estado al 18 nov 2026

- ✅ **Conexión con Oracle.** El cliente de la BI API, traído de mealviolation,
  ya entra con la cuenta API.
- ✅ **Volcado del 31 oct al 13 nov:** 8 tiendas, 14 días, 3,309 tarjetas y 0
  errores, en 3 minutos.
- ✅ **24 pruebas sin conexión:** `python -m unittest test_simphony`.
- ⏭️ **Siguiente:** el lector de Simphony y el motor de tacos, que juntos arman
  el CSV.
- ⏳ **Falta de tu lado:** lo de "Lo que necesito de ti", hasta abajo.

## Lo que dicen los datos (31 oct–13 nov)

### Horas: vienen limpias

- **Tarjetas:** ninguna abierta. 171 traen ajustes de gerente.
- **Personas:** 241. Ninguna trabajó en dos tiendas en el periodo. 17 tuvieron
  más de un puesto y solo 3 más de una tarifa.
- **Overtime:** el de Oracle (Labor Management) viene en dos niveles: `ovt1`
  en 57 tarjetas y `ovt2` en 4.
  - Se recalcula con las reglas de California, y lo de Oracle queda para
    comparar.
- **Primas:** Oracle no paga ninguna por su lado; ninguna tarjeta trae
  `premHrs`.

### Meals: se pueden revisar

- **Cómo se ve un meal:** al salir a comer se checa la salida con el código 66,
  y ese tramo trae `shiftType = 2`. El meal es el hueco hasta la siguiente
  entrada.
- **Cuánto duraron:** de 1,275 meals,
  - 1,230 duraron de 30 a 44 min;
  - 15 duraron 45 min o más;
  - 25 duraron de 10 a 29 min;
  - 5 duraron menos de 10 min.
- **Sin regreso:** 47 veces salieron a break y no regresaron en el mismo turno.
- **Falta:** contar los turnos de más de 5 horas sin meal. Eso lo hace el
  motor.

### Rest breaks de 10 minutos: no se checan

En todo el periodo no hay ni un break pagado. La prima de rest no se puede
calcular con los datos: es una decisión de política.

### Tips: vienen por otro lado y no cuadran solos

- **No vienen en las tarjetas.** Vienen en los totales diarios por persona
  (`getEmployeeDailyTotals`), sin puesto.
- **Tips con tarjeta (`chrgTipsTtl`):** los gana el dueño del cheque.
- **Tips pagados (`tipsPdTtl`):** son el efectivo que sale de la caja.
  - En el periodo suman como 9% más que los de tarjeta.
  - Solo coinciden con los de tarjeta en 288 de 619 renglones.
  - Parece que el pago queda a nombre de quien lo registra en la caja y que
    ahí caen también service charges. Hay que confirmarlo.
- **Lo que no hay:** efectivo declarado ni Tip Track. Tips indirectos hay en
  solo 2 renglones.
- **Cuentas que no checan ese día:** 169 renglones con tips son de 22 cuentas
  (por tienda) que no checaron ese día:
  - 8 son "SuperUser": accesos de sistema que nunca checan y que juntan tips
    con tarjeta.
  - 6 son "Manager" que nunca checan.
  - 8 son "Server"; 2 de ellos sí checan otros días.
- **Service charges sin explicar:** hay algunos que no son tips con tarjeta:
  `nonRevSvcTtl` en 44 renglones y `tipTtl` en 68. Falta saber qué son.

Más detalle, con las fuentes de Oracle, en [NOTAS_SIMPHONY.md](NOTAS_SIMPHONY.md).

## Cómo va a funcionar

1. **Simphony.** Por tienda y día, `getTimeCardDetails` da los tramos y
   `getEmployeeDailyTotals` da los tips.
2. **Jornadas.** Los tramos se juntan por persona y día legal. Los meals son
   los huecos después de un 66.
3. **California.** Con las checadas, no con lo que calcula Oracle:
   - overtime diario (más de 8 h), doble (más de 12 h), semanal (más de 40 h)
     y séptimo día;
   - la prima de meal por persona y día.
4. **Gusto.** Un CSV de Smart Import por empresa, con las reglas de tacos:
   - celda vacía, nunca 0;
   - cuadres de horas y tips;
   - lo que no se puede asignar se queda retenido para revisar.

## De dónde sale cada pieza

| Pieza | Viene de | Qué cambia |
| --- | --- | --- |
| Cliente de la BI API (`oracle_bi/`) | mealviolation, commit 575b084 | Ya está aquí. Reintenta ante errores de Oracle, trae `getEmployeeDailyTotals` y la contraseña no sale en logs. |
| Códigos de checada, día legal con horario de verano, revisión de checadas y de cobertura | mealviolation (`compliance/`) | Leer el `shiftType` como de verdad viene, no prorratear horas y no convertir un campo que falta en 0. |
| Overtime de California (`ca_overtime.py`) | tacos, commit 6fab0a7 | La semana laboral y el periodo de Broken Yolk. |
| Prima de meal por persona y día, CSV de Gusto, cuadres, retenidos, avisos y pantalla | tacos (`gusto_export.py`, `app.py`) | Separar la parte de Toast del motor, usar las columnas del template de Broken Yolk y permitir varias tiendas por empresa. |
| Tips | nuevo | Según cómo reparta Broken Yolk (abajo). |

## Tips en Gusto: lo que hay que decidir

En Gusto, **Paycheck tips** son tips que se pagan en el cheque. **Cash tips**
son tips que la persona ya se llevó: se reportan para impuestos, pero no se
pagan otra vez. Si los tips con tarjeta se pagan en efectivo cada noche y
además se mandan como Paycheck tips, se pagan dos veces.

| En Oracle | Qué es | A Gusto (propuesta, por confirmar) |
| --- | --- | --- |
| `chrgTipsTtl` | Tips con tarjeta, del dueño del cheque | Cash tips si se pagan en efectivo esa noche; Paycheck tips si van en el cheque. |
| `tipsPdTtl` | Efectivo que salió de la caja para pagar tips | Sirve para cuadrar, no para repartir: queda a nombre de quien lo registra. |
| `svcTtl` y `nonRevSvcTtl` | Service charges | Si es un cargo obligatorio (grupos grandes, catering), no es tip: es sueldo, como la gratuity de tacos. |
| Tips de cuentas SuperUser o Manager | Cheques cerrados con accesos de sistema | Retenidos hasta saber de quién son. |

## Pasos

1. **Tú:** la seguridad de mealviolation.
   - Poner privado el repo.
   - Quitarle el acceso público a la app.
   - Cambiar la contraseña de la cuenta API. La comparten las dos
     herramientas, así que hay que actualizarla en `.streamlit/secrets.toml`
     de aquí y en los Secrets de mealviolation.
2. **Tú:** lo de Gusto y las respuestas de abajo.
3. **Yo:** el lector de Simphony, el motor y las pruebas con datos inventados.
4. **Juntos:** de 2 a 4 periodos ya pagados en sombra, comparados contra lo
   que se pagó en Gusto, como la prueba de Bonita en tacos.
5. **Primera corrida real.**

## Lo que necesito de ti

1. **Tips con tarjeta:** ¿se los dan en efectivo esa misma noche o van en el
   cheque de nómina? Los datos dicen que en efectivo.
2. **Pool o tip share:** ¿reparten con cocina, bussers o hosts? ¿Cómo y dónde
   lo anotan? Oracle no trae nada de eso.
3. **Service charges:** ¿hay cargo automático para grupos grandes, catering o
   pedidos en línea? ¿A quién le toca ese dinero?
4. **Cuentas SuperUser y Manager que cobran tips sin checar:** ¿de quién son
   esos tips?
5. **Gusto:**
   - ¿cuántas empresas (LLC) hay y qué tiendas van en cada una?
   - el calendario de pago y el día en que empieza la semana laboral;
   - el template de Smart Import de cada empresa;
   - el export de empleados.

   El periodo que usó mealviolation fue de sábado a viernes, cada dos
   semanas, igual que tacos.
6. **Meals:** ¿hay waivers firmados? ¿Hoy pagan la prima de meal? ¿En horas o
   en dólares?
7. **Rest breaks:** no se checan. ¿Qué política tienen?
8. **Hoy:** ¿quién hace la nómina de Broken Yolk, de qué reporte saca las horas
   y los tips, y qué captura a mano en Gusto?
9. ¿Las 8 tiendas están en California?
10. ¿Para cuándo quieres la primera corrida?

## Archivos

| Archivo | Para qué |
| --- | --- |
| `oracle_bi/` | Cliente de la BI API: inicio de sesión, tarjetas, totales diarios, empleados y puestos. |
| `volcado_simphony.py` | Baja los datos crudos de un rango a `reference/` y en pantalla solo enseña qué campos vienen y totales. |
| `test_simphony.py`, `fixtures/` | Pruebas sin conexión, con datos inventados que tienen la forma real. |
| `NOTAS_SIMPHONY.md` | Lo que se sabe de Simphony, con fuentes. |
| `CLAUDE.md` | Reglas de trabajo y de seguridad que vienen de tacos. |
| `.streamlit/secrets.toml.example` | Plantilla de las credenciales. El archivo real no va al repo. |
| `reference/`, `salida/` | Datos reales y archivos generados. No van al repo. |

Para bajar un rango:

```
python -I volcado_simphony.py --desde 2026-10-31 --hasta 2026-11-13
```
