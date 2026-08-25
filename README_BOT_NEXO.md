# Bot NEXO — Instrucciones de instalación

## 1. Dónde va cada archivo en el repo `mainSLA.sis`

```
mainSLA.sis/
├── .github/workflows/
│   ├── bot_nexo_subir.yml        ← nuevo
│   └── bot_nexo_resultado.yml    ← nuevo
├── bot_nexo/
│   ├── bot_nexo_subir.py         ← nuevo
│   └── bot_nexo_resultado.py     ← nuevo
└── nexo_uploads/                 ← se crea solo (la app sube los CSV acá)
```

Si el repo ya tiene otras carpetas de bots (por ejemplo para SAP), estos archivos
conviven sin problema — cada bot tiene su propio workflow y su propia carpeta.

## 2. Secrets a configurar en el repo

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Valor |
|---|---|
| `SUPABASE_URL` | `https://iebfyjbkmjuicrrbezbi.supabase.co` |
| `SUPABASE_SERVICE_KEY` | La **service_role key** de tu proyecto Supabase (Settings → API en el panel de Supabase). **No** uses la `anon` key acá — el bot necesita poder escribir sin pasar por el login de usuario. |

> ⚠️ La service_role key tiene acceso total a la base saltándose RLS. Guardala
> únicamente como secret de GitHub Actions, nunca la pongas en el código del
> front-end (`index.html`).

## 3. Qué NO hace falta cargar como secret

El usuario/contraseña de NEXO (= los mismos que Bot SAP) y el email de
resultados con su contraseña de aplicación **ya se leen en vivo desde
Supabase** (tabla `configuracion`), cargados desde la app en:
**Configuración → Sistemas AMX → Bot SAP** (usuario/contraseña) y
**Configuración → Sistemas AMX → Distribución** (email de resultados).
Si el día de mañana cambian, se actualizan ahí — no hay que tocar el repo.

## 4. Repositorio de GitHub configurado en la app

En **Configuración → Sistemas AMX → Bot SAP** asegurate de tener cargado:
- Usuario GitHub / organización: el dueño de `mainSLA.sis`
- Repositorio: `mainSLA.sis`
- Token: un Personal Access Token con permisos `repo` + `workflow` (el mismo
  que ya usás para disparar el Bot SAP debería servir, si tiene esos scopes).

## 5. Primera prueba recomendada

1. Cargá una caja de prueba chica en Distribución (podés usar la misma de
   ejemplo si no importa duplicar — o una nueva).
2. Tocá "Generar archivo NEXO" y mirá en GitHub → Actions que se haya
   disparado el workflow `Bot NEXO - Subir presuspensión`.
3. Si falla, revisá las capturas de pantalla que sube como *artifact* del
   run fallido (`capturas-error-<numero_caja>`) — el sitio de NEXO puede
   tener pequeñas diferencias de selectores/timing la primera vez.
4. Una vez que el mail llegue, esperá al próximo disparo del cron (cada 15
   min) o corré manualmente `Bot NEXO - Revisar resultados` desde la
   pestaña Actions para no esperar.

## 6. Ajustes esperables en la primera corrida

Los `XPath` y selectores de login SSO se armaron a partir de la descripción
y las URLs que pasaste, pero **no pude probarlos contra el sistema real**
(no tengo acceso a NEXO desde este entorno). Es normal que en el primer
intento haya que afinar algún selector — las capturas de pantalla que el
bot guarda en cada paso están pensadas justamente para poder diagnosticar
rápido qué pantalla no coincidió con lo esperado.
